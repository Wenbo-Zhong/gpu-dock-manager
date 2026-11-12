"""System-bus API. Reads are unprivileged; every mutation checks Polkit."""
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import gi
from gi.repository import Gio, GLib

from . import BUS_NAME, BUS_PATH, INTERFACE
from .controller import Controller
from .deployment import plan
from .hardware import recent_events
from .internal_gpu import ACTIONS as INTERNAL_ACTIONS
from .probe import bounded_inventory as gpu_inventory, bounded_internal_status

XML = f"""<node><interface name="{INTERFACE}">
<method name="GetStatus"><arg type="s" direction="out"/></method>
<method name="GetLogs"><arg type="s" direction="out"/></method>
<method name="GetInternalGpuStatus"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
<method name="PreviewDeployment"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
<method name="Perform"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
</interface></node>"""


class Service:
    def __init__(self):
        self.controller = Controller()
        self.controller.events.extend(recent_events())
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.loop = GLib.MainLoop()
        self.connection = None
        self.stopping = threading.Event()

    def authorized(self, sender, action):
        result = self.connection.call_sync(
            "org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority",
            "org.freedesktop.PolicyKit1.Authority", "CheckAuthorization",
            GLib.Variant("((sa{sv})sa{ss}us)",
                         (("system-bus-name", {"name": GLib.Variant("s", sender)}),
                          "io.github.usb4gpumanager." + action, {}, 1, "")),
            GLib.VariantType.new("((bba{ss}))"), Gio.DBusCallFlags.NONE, 120000, None)
        return result.unpack()[0][0]

    def reply(self, invocation, value=None, error=None):
        if error is not None:
            invocation.return_dbus_error(BUS_NAME + ".Error", str(error))
        else:
            invocation.return_value(GLib.Variant("(s)", (json.dumps(value, ensure_ascii=False),)))
        return False

    def handle(self, connection, sender, path, interface, method, parameters, invocation):
        if method == "GetStatus":
            self.reply(invocation, self.controller.get_status())
        elif method == "GetLogs":
            self.reply(invocation, self.controller.get_status().get("events", []))
        elif method == "GetInternalGpuStatus":
            pci = parameters.unpack()[0]
            if len(pci) != 12:
                self.reply(invocation, error="无效的内置显卡 PCI 地址。")
                return
            self.pool.submit(self._internal_status, pci, invocation)
        elif method == "PreviewDeployment":
            self.pool.submit(self._preview, parameters.unpack()[0], invocation)
        elif method == "Perform":
            action, raw = parameters.unpack()
            if len(raw) > 16384 or action not in {"attach", "detach", "queue-detach", "cancel-detach", "deploy", "rollback", *INTERNAL_ACTIONS}:
                self.reply(invocation, error="不受支持的请求。")
                return
            self.pool.submit(self._perform, sender, action, raw, invocation)
        else:
            self.reply(invocation, error="未知方法。")

    def _internal_status(self, pci, invocation):
        try:
            # Do not report a partially switched pair while a job owns the device lock.
            if not self.controller.device_lock.acquire(blocking=False):
                raise RuntimeError("设备正在更新，请稍后检查 4070 状态。")
            try:
                status = bounded_internal_status(pci)
            finally:
                self.controller.device_lock.release()
            GLib.idle_add(self.reply, invocation, status)
        except Exception as error:
            GLib.idle_add(self.reply, invocation, None, error)

    def _perform(self, sender, action, raw, invocation):
        try:
            options = json.loads(raw)
            if action in {"attach", "detach", "queue-detach", *INTERNAL_ACTIONS} and not self.controller.get_status().get("can_mutate_devices", False):
                raise RuntimeError("设备详细状态已过期或驱动清理未完成，请先恢复驱动。")
            authorization = ("manage-internal" if action in INTERNAL_ACTIONS else
                             "configure" if action in {"deploy", "rollback"} else "manage")
            if not self.authorized(sender, authorization):
                raise PermissionError("管理员授权未通过。")
            if action == "cancel-detach":
                if options:
                    raise ValueError("取消操作不接收参数。")
                value = self.controller.cancel()
            elif action in INTERNAL_ACTIONS:
                # Keep the session client's audio-release scope alive until the
                # driver operation actually finishes, including failure rollback.
                value = self.controller.perform(action, options)
            else:
                # Return immediately after authorization; progress is exposed in GetStatus.
                if self.controller.operation_lock.locked():
                    raise RuntimeError("已有操作正在进行。")
                self.pool.submit(self._job, action, options)
                value = "操作已提交。"
            GLib.idle_add(self.reply, invocation, value)
        except Exception as error:
            GLib.idle_add(self.reply, invocation, None, error)

    def _preview(self, raw, invocation):
        try:
            if len(raw) > 16384:
                raise ValueError("配置过大。")
            GLib.idle_add(self.reply, invocation, plan(json.loads(raw), gpu_inventory()))
        except Exception as error:
            GLib.idle_add(self.reply, invocation, None, error)

    def _job(self, action, options):
        inhibitor = None
        try:
            if action in {"deploy", "rollback"}:
                reply, descriptors = self.connection.call_with_unix_fd_list_sync(
                    "org.freedesktop.login1", "/org/freedesktop/login1",
                    "org.freedesktop.login1.Manager", "Inhibit",
                    GLib.Variant("(ssss)", ("sleep:shutdown", "USB4 GPU Manager",
                                            "Changing GPU configuration", "block")),
                    GLib.VariantType.new("(h)"), Gio.DBusCallFlags.NONE, 5000, None, None)
                inhibitor = descriptors.get(reply.unpack()[0])
            self.controller.perform(action, options)
        except Exception as error:
            if not self.controller.operation_lock.locked():
                with self.controller.lock:
                    self.controller.operation = {"state": "error", "action": action, "message": str(error)}
            logging.getLogger("usb4-gpu-manager").exception("Manager operation failed")
        finally:
            if inhibitor is not None:
                os.close(inhibitor)

    def acquired(self, connection, name):
        self.connection = connection
        node = Gio.DBusNodeInfo.new_for_xml(XML)
        connection.register_object(BUS_PATH, node.interfaces[0], self.handle, None, None)
        connection.signal_subscribe("org.freedesktop.login1", "org.freedesktop.login1.Manager",
                                    "PrepareForSleep", "/org/freedesktop/login1", None,
                                    Gio.DBusSignalFlags.NONE, self.sleep_signal)
        threading.Thread(target=self.monitor, daemon=True).start()
        self.controller.event("统一管理服务已启动。")

    def sleep_signal(self, connection, sender, path, interface, signal, parameters):
        self.controller.prepare_for_sleep(parameters.unpack()[0])

    def monitor(self):
        try:
            if __import__("pathlib").Path("/run/usb4-gpu-manager/ignore.json").exists():
                self.controller.clear_ignore()
        except Exception:
            logging.exception("Could not recover temporary display rule")
        while not self.stopping.is_set():
            try:
                self.controller.tick()
            except Exception as error:
                if self.controller.last_error != str(error):
                    self.controller.event("硬件详细状态更新失败：" + str(error))
                self.controller.last_error = str(error)
            self.stopping.wait(3)

    def stop(self):
        self.stopping.set()
        self.controller.cancel_event.set()
        self.loop.quit()
        return False

    def run(self):
        if os.geteuid() != 0:
            raise SystemExit("The manager daemon must run as root through systemd.")
        Gio.bus_own_name(Gio.BusType.SYSTEM, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                         self.acquired, None, lambda *_: self.loop.quit())
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 15, self.stop)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 2, self.stop)
        self.loop.run()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Service().run()
