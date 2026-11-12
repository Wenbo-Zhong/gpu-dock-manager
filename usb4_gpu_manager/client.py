import json

from gi.repository import Gio, GLib

from . import BUS_NAME, BUS_PATH, INTERFACE
from .deployment import suggested_profile
from .displays import DesktopDisplays, INTERNAL_DISPLAY_ACTIONS, internal_display_data
from .hardware import recent_events, service_status
from .probe import bounded_snapshot as snapshot
from .presence import observe, remember_transport
from .internal_gpu import internal_switch_blocker, internal_switch_needed
from .system import command


class Client:
    def __init__(self):
        self.connection = None
        self.local_events = None
        self.last_status = {}
        self.displays = DesktopDisplays()
        self.internal_displays = DesktopDisplays(internal=True)

    def call(self, method, values=(), signature=None, timeout=10000):
        if self.connection is None:
            self.connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        value = self.connection.call_sync(BUS_NAME, BUS_PATH, INTERFACE, method,
                                          GLib.Variant(signature, values) if signature else None,
                                          GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE,
                                          timeout, None)
        return json.loads(value.unpack()[0])

    def status(self):
        data = self.device_status()
        data["desktop_displays"] = self.displays.status(data)
        selected = internal_display_data(data)
        if selected["target"]:
            data["internal_desktop_displays"] = self.internal_displays.status(selected)
        return data

    def device_status(self):
        try:
            self.last_status = observe(remember_transport(self.last_status, self.call("GetStatus")))
            return self.last_status
        except GLib.Error as error:
            try:
                data = snapshot()
            except Exception as probe_error:
                data = dict(self.last_status)
                data["last_error"] = str(probe_error)
            data["services"] = service_status()
            if self.local_events is None:
                self.local_events = recent_events()
            data["events"] = self.local_events
            data["suggested_profile"] = suggested_profile(data.get("gpus", []))
            data["service_connected"] = False
            data["connection_error"] = error.message
            data["deployment"] = {"managed": False, "restart_required": False, "rollback_available": False}
            data["operation"] = {"state": "idle", "action": "", "message": ""}
            self.last_status = observe(data)
            return self.last_status

    def perform(self, action, options=None):
        if action in INTERNAL_DISPLAY_ACTIONS:
            selected = internal_display_data(self.device_status())
            pci = (options or {}).get("expected_pci")
            if not pci or (selected.get("target") or {}).get("pci") != pci:
                raise RuntimeError("目标内置显卡已变化或驱动尚未加载。")
            if action == "internal-disable-displays":
                return self.internal_displays.disable(selected, pci)
            return self.internal_displays.restore(selected, pci)
        if action == "disable-displays":
            return self.displays.disable(self.device_status(), (options or {}).get("expected_pci"))
        if action == "restore-displays":
            return self.displays.restore(self.device_status())
        if action == "internal-detach":
            return self.detach_internal(options or {})
        return self.call("Perform", (action, json.dumps(options or {})), "(ss)", timeout=130000)

    def detach_internal(self, options):
        status = self.internal_gpu_status(options.get("expected_pci", ""))
        blocker = internal_switch_blocker(status, "internal-detach")
        if blocker:
            if not internal_switch_needed(status, "internal-detach"):
                return blocker
            raise RuntimeError(blocker)
        pause_audio = any(p["name"] == "wireplumber" for p in status["holders"]["processes"])
        running = pause_audio and command(
            ["systemctl", "--user", "is-active", "--quiet", "wireplumber.service"], check=False).returncode == 0
        error = None
        try:
            if running:
                command(["systemctl", "--user", "stop", "wireplumber.service"], timeout=15)
            return self.call("Perform", ("internal-detach", json.dumps(options)), "(ss)", timeout=130000)
        except Exception as caught:
            error = caught
            raise
        finally:
            if running:
                try:
                    command(["systemctl", "--user", "start", "wireplumber.service"], timeout=15)
                except Exception as restore_error:
                    raise RuntimeError(f"{error or '显卡操作已完成'}；恢复 WirePlumber 失败：{restore_error}") from restore_error

    def preview(self, profile):
        return self.call("PreviewDeployment", (json.dumps(profile),), "(s)")

    def internal_gpu_status(self, pci):
        return self.call("GetInternalGpuStatus", (pci,), "(s)")
