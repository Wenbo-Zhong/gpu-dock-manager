"""Manager state machine. All mutations are serialized and performed by root."""
from collections import deque
import json
import logging
import subprocess
from pathlib import Path
import threading
import time

from . import deployment
from .hardware import detach_check, holders, nvidia_metrics, internal_metrics_ready, other_internal_display_ready, service_status, target_gpu, usb4_inventory
from .internal_gpu import ACTIONS as INTERNAL_ACTIONS, HOST_DRIVERS, VFIO
from .probe import bounded_inventory as gpu_inventory, bounded_snapshot as snapshot, device_action, internal_device_action
from .presence import observe, remember_transport
from .system import atomic_write, command, load_json, read, stalled_commands

LOG = logging.getLogger("usb4-gpu-manager")
IGNORE_RULE = Path("/run/udev/rules.d/99-usb4-gpu-manager-detach.rules")
IGNORE_STATE = Path("/run/usb4-gpu-manager/ignore.json")


class Controller:
    def __init__(self):
        self.lock = threading.RLock()
        self.operation_lock = threading.Lock()
        self.device_lock = threading.RLock()
        self.events = deque(maxlen=100)
        self.cached = {}
        self.operation = {"state": "idle", "action": "", "message": ""}
        self.cancel_event = threading.Event()
        self.sleeping = False
        self.last_resume = None
        self.sleep_had_gpu = False
        self.auto_pending = True
        self.signature = None
        self.stable_since = 0
        self.retry_after = 0
        self.metrics_at, self.metrics = 0, []
        self.units = []
        self.detached_routers = None
        self.ignored_gpu = None
        self.policy_state = None
        self.policy_candidate = None
        self.last_error = ""

    def event(self, message):
        LOG.info(message)
        with self.lock:
            self.events.append({"time": time.time(), "message": message})

    def active_profile(self):
        return load_json(deployment.CONFIG, {}) or {}

    def refresh(self):
        with self.device_lock:
            return self._refresh_locked()

    def _refresh_locked(self):
        profile = self.active_profile()
        data = snapshot(profile, metrics=False)
        with self.lock:
            data = remember_transport(self.cached, data)
        gpu = data.get("target")
        internal = next((g for g in data["gpus"] if internal_metrics_ready(g)), None)
        if time.monotonic() - self.metrics_at > 10:
            self.metrics = nvidia_metrics(gpu)
            if internal:
                self.metrics += nvidia_metrics(internal, include_internal=True)
            self.metrics_at = time.monotonic()
            self.units = service_status()
        eligible = {g["pci"] for g in (gpu, internal) if g and g["driver"] == "nvidia"}
        self.metrics = [metric for metric in self.metrics if metric["pci"] in eligible]
        data.update({"service_connected": True, "metrics": self.metrics, "services": self.units,
                     "suggested_profile": deployment.suggested_profile(data["gpus"]),
                     "deployment": {"managed": bool(profile),
                                    "restart_required": bool(profile.get("deployed_boot") == read("/proc/sys/kernel/random/boot_id")),
                                    "rollback_available": (deployment.STATE / "latest-backup.json").exists()},
                     "kernel": read("/proc/sys/kernel/osrelease"),
                     "session_management": "kernel-notifier",
                     "last_resume": self.last_resume, "last_error": self.last_error})
        with self.lock:
            data["operation"] = dict(self.operation)
            data["events"] = list(self.events)
            self.cached = data
        return data

    def get_status(self):
        with self.lock:
            data = dict(self.cached, operation=dict(self.operation), events=list(self.events), last_error=self.last_error)
        profile = self.active_profile()
        data.update(service_connected=True, profile=profile,
                    deployment={"managed": bool(profile),
                                "restart_required": bool(profile.get("deployed_boot") == read("/proc/sys/kernel/random/boot_id")),
                                "rollback_available": load_json(deployment.STATE / "latest-backup.json") is not None})
        data = observe(data)
        if stalled_commands():
            data["can_mutate_devices"] = False
            data["blocked_io"] = True
            data["last_error"] = "有驱动调用仍阻塞在内核中，已暂停重复调用；请保存工作后重启恢复驱动。"
        return data

    def prepare_for_sleep(self, sleeping):
        self.sleeping = sleeping
        if sleeping:
            target = self.get_status().get("target")
            self.sleep_had_gpu = target is not None
            displays = target.get("connectors", []) if target else []
            self.event("进入睡眠；显示连接：" + json.dumps(displays, ensure_ascii=False))
        else:
            self.signature, self.stable_since = None, 0
            self.auto_pending = True
            self.last_resume = {"at": time.time(), "state": "waiting" if self.sleep_had_gpu else "absent"}
            self.event("系统已唤醒，等待 USB4/PCIe 路径稳定。" if self.sleep_had_gpu else "系统已唤醒，睡眠前没有连接外接显卡。")

    def tick(self):
        data = self.refresh()
        if data.get("internal_gpu"):
            self._tick_internal_policy(observe(data))
            return
        if observe(data).get("transport", {}).get("state") == "disconnected":
            return
        profile, gpu = data["profile"], data.get("target")
        if self.sleeping or not profile.get("enabled") or data["deployment"]["restart_required"]:
            return
        routers = {r["uuid"] for r in data["usb4"] if not r["is_host"]}
        if self.detached_routers is not None:
            if self.detached_routers and not self.detached_routers.issubset(routers):
                self.detached_routers = None
                self.event("检测到 USB4 外设已拔出，重新允许自动连接。")
            else:
                return
        if not gpu:
            self.signature = None
            self.auto_pending = True
            if self.last_resume and self.last_resume["state"] == "waiting" and time.time() - self.last_resume["at"] > 30:
                self.last_resume["state"] = "absent"
                self.event("本次唤醒未检测到外接显卡，继续等待后续连接。")
            return
        if not self.auto_pending or time.monotonic() < self.retry_after:
            return
        if profile.get("keep_path_awake", True):
            with self.device_lock:
                self._keep_awake(gpu)
        resource = read(Path(gpu["path"]) / "resource", "").splitlines()
        if not resource:
            return
        fields = resource[0].split()
        try:
            assigned = len(fields) >= 2 and int(fields[0], 16) != 0
        except ValueError:
            assigned = False
        if not assigned:
            self.signature = None
            return
        signature = (gpu["pci"], gpu["path"], tuple(fields[:2]))
        if signature != self.signature:
            self.signature, self.stable_since = signature, time.monotonic()
            return
        if time.monotonic() - self.stable_since < profile.get("stable_seconds", 12):
            return
        if not self.operation_lock.acquire(blocking=False):
            return
        try:
            self._attach(profile, rescan=False)
            self.auto_pending = False
            self.last_error = ""
            if self.last_resume and self.last_resume["state"] == "waiting":
                self.last_resume["state"] = "ready"
            self.event("外接显卡已绑定 NVIDIA，显示输出和 CUDA 可用。")
        except Exception as error:
            self.last_error = str(error)
            self.retry_after = time.monotonic() + 15
            self.event("自动连接失败：" + str(error))
        finally:
            self.operation_lock.release()

    def _tick_internal_policy(self, data):
        transport = data["transport"]
        external = transport["state"] in {"present", "detached"}
        power = data.get("external_power_online")
        if self.last_resume and self.last_resume["state"] == "waiting" and not self.sleeping:
            if (data.get("target") or {}).get("driver") == "nvidia":
                self.last_resume["state"] = "ready"
            elif not external and time.time() - self.last_resume["at"] > 30:
                self.last_resume["state"] = "absent"
        if (self.sleeping or power is None or data.get("status_stale") or transport.get("driver_cleanup_pending")
                or (transport["state"] == "detached" and not data.get("target_router_ids"))):
            self.policy_candidate = None
            return
        state = power, external
        if self.policy_state is None:
            self.policy_state = state
        if state == self.policy_state:
            self.policy_candidate = None
            return
        if state != self.policy_candidate:
            self.policy_candidate = state
            return

        previous = self.policy_state
        self.policy_state = state
        self.policy_candidate = None
        if not external:
            self.detached_routers = None
        if (not data["can_mutate_devices"] or data["deployment"]["restart_required"]
                or stalled_commands() or not self.operation_lock.acquire(blocking=False)):
            self.event("电源或 USB4 状态已变化，本次跳过自动切换，请按需手动操作。")
            return
        try:
            with self.device_lock:
                internal = data["internal_gpu"]
                gpu = data.get("target")
                profile = self._external_profile(data)
                use_external = external and self.detached_routers is None and (gpu is not None or not previous[1])
                wanted = (VFIO, VFIO) if not power or use_external else HOST_DRIVERS
                current = internal.get("graphics_driver"), internal.get("audio_driver")
                results, errors = [], []
                with self.lock:
                    self.operation = {"state": "running", "action": "auto-gpu-policy", "message": "正在处理电源或 USB4 状态变化…"}
                if current != wanted:
                    try:
                        self._switch_internal("internal-attach" if wanted == HOST_DRIVERS else "internal-detach",
                                              internal["pci"], protect_connected=True)
                        results.append("4070 已加载" if wanted == HOST_DRIVERS else "4070 已切换到 VFIO")
                    except (OSError, RuntimeError) as error:
                        errors.append("4070：" + str(error))
                if (external and not previous[1] and self.detached_routers is None
                        and profile.get("enabled", True) and profile.get("target")
                        and not (gpu and gpu["driver"] == "nvidia" and gpu.get("card"))
                        and not self.sleeping and not stalled_commands()):
                    try:
                        self._attach_locked(profile, rescan=True)
                        results.append("外置显卡已加载")
                    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                        errors.append("外置显卡：" + str(error))
                message = "；".join(results + errors) or "电源或 USB4 状态已变化，驱动状态无需调整。"
                with self.lock:
                    self.operation = {"state": "error" if errors else "done", "action": "auto-gpu-policy", "message": message}
                self.event(message)
        finally:
            self.operation_lock.release()

    def _switch_internal(self, action, pci, protect_connected=False):
        if self.sleeping:
            raise RuntimeError("系统正在进入睡眠，请唤醒后再操作显卡。")
        try:
            internal_device_action(action, pci, protect_connected=protect_connected)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("4070 驱动切换超时，当前绑定状态尚未确认。请查看设备页，必要时重启恢复开机配置。") from error

    @staticmethod
    def _external_profile(data):
        profile = data.get("profile") or data.get("suggested_profile") or {}
        known = data.get("last_known_target")
        if (not profile.get("target") and known and known.get("external")
                and known.get("vendor") == "0x10de" and not known.get("boot_vga")):
            profile = dict(profile, target={key: known[key] for key in ("vendor", "device")})
        return profile

    def _profile_and_gpu(self):
        gpus = gpu_inventory()
        profile = self.active_profile() or deployment.suggested_profile(gpus)
        return profile, target_gpu(gpus, profile)

    @staticmethod
    def _expected(gpu, options):
        if not gpu or options.get("expected_pci") != gpu["pci"]:
            raise RuntimeError("目标显卡已变化，请刷新界面后重试。")

    def _attach(self, profile, rescan=True):
        with self.device_lock:
            return self._attach_locked(profile, rescan)

    def _attach_locked(self, profile, rescan=True):
        if not profile.get("target"):
            raise RuntimeError("请先在部署页面选择目标显卡。")
        if rescan:
            Path("/sys/bus/pci/rescan").write_text("1\n")
            # Do not initialize NVIDIA while USB4 BARs are still being assigned.
            deadline, previous, since = time.monotonic() + 100, None, 0
            while time.monotonic() < deadline:
                gpu = target_gpu(gpu_inventory(), profile)
                signature = None
                if gpu:
                    if profile.get("keep_path_awake", True):
                        self._keep_awake(gpu)
                    resources = read(Path(gpu["path"]) / "resource", "").splitlines()
                    values = resources[0].split() if resources else []
                    if values and int(values[0], 16):
                        signature = (gpu["pci"], gpu["path"], tuple(values[:2]))
                if signature != previous:
                    previous, since = signature, time.monotonic()
                if signature and time.monotonic() - since >= profile.get("stable_seconds", 12):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("未找到稳定的外接显卡，请检查电源和 USB4 线缆。")
        gpu = target_gpu(gpu_inventory(), profile)
        if not gpu:
            raise RuntimeError("目标显卡未出现。")
        if gpu["driver"] not in {"nvidia", "unbound"}:
            raise RuntimeError("目标显卡由其他驱动占用，拒绝接管。")
        if profile.get("keep_path_awake", True):
            self._keep_awake(gpu)
        for module in ("nvidia", "nvidia_modeset", "nvidia_drm", "nvidia_uvm"):
            command(["modprobe", "--ignore-install", module], timeout=45)
        device = Path("/sys/bus/pci/devices") / gpu["pci"]
        if not (device / "driver").exists():
            device_action("bind", gpu, timeout=15)
        for _ in range(20):
            if (device / "driver").resolve().name == "nvidia" and Path("/dev/nvidiactl").exists():
                return
            time.sleep(1)
        raise RuntimeError("NVIDIA 模块已加载，但显卡没有完成绑定。")

    @staticmethod
    def _keep_awake(gpu):
        device_action("power", gpu, timeout=3)

    def _ignore_for_next_session(self, gpu):
        info = command(["udevadm", "info", "--query=property", "--path", "/sys/class/drm/" + gpu["card"]]).stdout
        if ":mutter-device-ignore:" in info:
            return  # Preserve an ignore rule owned by somebody else.
        self.ignored_gpu = {"pci": gpu["pci"], "card": gpu["card"]}
        atomic_write(IGNORE_STATE, json.dumps(self.ignored_gpu), 0o600)
        atomic_write(IGNORE_RULE, self._ignore_rule(gpu, "+"))
        command(["udevadm", "control", "--reload-rules"])
        command(["udevadm", "trigger", "--action=change", "/sys/class/drm/" + gpu["card"]])
        command(["udevadm", "settle", "--timeout=5"], check=False)

    @staticmethod
    def _ignore_rule(gpu, sign):
        return (f'SUBSYSTEM=="drm", KERNEL=="card[0-9]*", KERNELS=="{gpu["pci"]}", '
                f'TAG{sign}="mutter-device-ignore"\n')

    def clear_ignore(self):
        gpu = self.ignored_gpu or load_json(IGNORE_STATE)
        try:
            if gpu and self.get_status().get("transport", {}).get("state") != "disconnected":
                card = Path("/sys/class/drm") / gpu["card"]
                if (card / "device").exists() and (card / "device").resolve().name == gpu["pci"]:
                    atomic_write(IGNORE_RULE, self._ignore_rule(gpu, "-"))
                    command(["udevadm", "control", "--reload-rules"], timeout=3)
                    command(["udevadm", "trigger", "--action=change", str(card)], timeout=3)
                    command(["udevadm", "settle", "--timeout=3"], check=False, timeout=4)
        finally:
            IGNORE_RULE.unlink(missing_ok=True)
            IGNORE_STATE.unlink(missing_ok=True)
            self.ignored_gpu = None
            command(["udevadm", "control", "--reload-rules"], timeout=3)

    def _detach(self, gpu, allow_displays=False):
        with self.device_lock:
            return self._detach_locked(gpu, allow_displays)

    def _detach_locked(self, gpu, allow_displays=False):
        # Recheck immediately before any device mutation. No force-unload or kills.
        _, current = self._profile_and_gpu()
        if not current or current["pci"] != gpu["pci"]:
            raise RuntimeError("显卡已变化，取消移除。")
        connected = current["external_display_state"] == "connected"
        panel_ready = other_internal_display_ready(gpu_inventory(), current) if connected or allow_displays else False
        check = detach_check(current, holders(current), allow_connected=allow_displays, other_display_ready=panel_ready)
        if allow_displays and not panel_ready:
            raise RuntimeError("另一张显卡的内屏尚未启用，取消移除。请先打开笔记本内屏。")
        if not check["safe"]:
            raise RuntimeError("\n".join(check["blockers"]))
        self.detached_routers = {r["uuid"] for r in usb4_inventory() if not r["is_host"]}
        with self.lock:
            self.operation.update(state="removing", message="正在请求驱动移除设备，请保持 USB4 连接；尚不能拔线。")
        self.event("已通过占用检查，开始移除 GPU/音频设备；完成前请保持 USB4 连接。")
        try:
            device_action("remove", gpu, timeout=25)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("驱动移除超过 25 秒，尚未确认设备释放。状态监测继续，暂不可拔线或重试移除。") from error
        if not any(item["driver"] == "nvidia" for item in gpu_inventory()):
            # With the desktop's handles gone and no other NVIDIA GPU bound,
            # ordinary module removal prevents premature probing on replug.
            try:
                result = command(["modprobe", "-r", "nvidia_uvm", "nvidia_drm", "nvidia_modeset", "nvidia"],
                                 timeout=30, check=False)
                unloaded = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                unloaded = False
            if not unloaded:
                self.event("PCI 设备已移除；部分空闲模块仍有内核引用，未强制卸载。")
        self.event("目标显卡及其音频功能已从 PCI 总线移除，可以拔掉显卡线。")
        return "已安全移除，可以拔掉显卡线。再次使用时接回 USB4，或点“重新连接”。"

    def perform(self, action, options):
        allowed = {"attach": set(), "detach": {"expected_pci"},
                   "queue-detach": {"expected_pci"}, "deploy": {"profile"}, "rollback": set(),
                   **dict.fromkeys(INTERNAL_ACTIONS, {"expected_pci"})}
        if action not in allowed or not isinstance(options, dict) or set(options) - allowed[action]:
            raise ValueError("操作或参数不受支持。")
        if not self.operation_lock.acquire(blocking=False):
            raise RuntimeError("已有操作正在进行，请稍候或取消预约。")
        self.cancel_event.clear()
        with self.lock:
            self.operation = {"state": "running", "action": action, "message": "正在处理…"}
        try:
            if self.sleeping and action in {"attach", "detach", "queue-detach", *INTERNAL_ACTIONS}:
                raise RuntimeError("系统正在进入睡眠，请唤醒后再操作显卡。")
            profile, gpu = (None, None) if action in {"deploy", "rollback", *INTERNAL_ACTIONS} else self._profile_and_gpu()
            if action == "deploy":
                message = deployment.deploy(options.get("profile"), gpu_inventory())
            elif action == "rollback":
                message = deployment.rollback()
            elif action in INTERNAL_ACTIONS:
                with self.device_lock:
                    pci = options.get("expected_pci")
                    if not isinstance(pci, str):
                        raise RuntimeError("请指定内置 4070 的 PCI 地址。")
                    self._switch_internal(action, pci)
                message = ("内置 4070 和 HDMI 音频已加载宿主驱动。" if action == "internal-attach"
                           else "内置 4070 和 HDMI 音频已切换到 VFIO 隔离。")
            elif action == "attach":
                if not self.active_profile():
                    data = self.get_status()
                    if not data.get("internal_gpu") or data["transport"]["state"] not in {"present", "detached"}:
                        raise RuntimeError("请先接入可识别的外置显卡，或部署管理配置。")
                    profile = self._external_profile(data)
                if profile.get("deployed_boot") == read("/proc/sys/kernel/random/boot_id"):
                    raise RuntimeError("配置已部署，请先重启再重新连接。")
                self.detached_routers = None
                self._attach(profile)
                message = "显卡已连接，显示输出和 CUDA 可用。"
            else:
                self._expected(gpu, options)
                if action == "detach":
                    message = self._detach(gpu)
                else:
                    check = detach_check(gpu, {"processes": [], "complete": True}, allow_connected=True)
                    if not check["safe"]:
                        raise RuntimeError("\n".join(check["blockers"]))
                    if not other_internal_display_ready(gpu_inventory(), gpu):
                        raise RuntimeError("请先启用由另一张显卡驱动的笔记本内屏，再预约注销后移除。")
                    initial_displays = {c["name"] for c in gpu["connectors"] if c["kind"] == "external" and c["status"] == "connected"}
                    self._ignore_for_next_session(gpu)
                    self.operation.update(state="waiting", message="等待注销并释放 GPU；预约成功不等于可以拔线。180 秒后自动取消。")
                    self.event("已预约安全移除；下一次 GNOME 登录会话临时跳过该卡，等待当前占用释放。")
                    deadline = time.monotonic() + 180
                    while time.monotonic() < deadline:
                        if self.cancel_event.wait(1):
                            raise RuntimeError("预约已取消。")
                        if self.sleeping:
                            continue
                        _, current = self._profile_and_gpu()
                        self._expected(current, options)
                        check = detach_check(current, holders(current), allow_connected=True)
                        now_displays = {c["name"] for c in current["connectors"] if c["kind"] == "external" and c["status"] == "connected"}
                        if current["external_display_state"] == "unknown" or now_displays - initial_displays:
                            raise RuntimeError("显示器连接状态变化，已取消预约。")
                        if check["safe"]:
                            message = self._detach(current, allow_displays=True)
                            break
                    else:
                        raise RuntimeError("等待释放 GPU 超时；已取消预约并恢复显示能力。")
            self.event(message)
            with self.lock:
                self.operation = {"state": "done", "action": action, "message": message}
            return message
        except Exception as error:
            self.event("操作失败：" + str(error))
            with self.lock:
                self.operation = {"state": "error", "action": action, "message": str(error)}
            raise
        finally:
            try:
                if self.ignored_gpu:
                    try:
                        self.clear_ignore()
                    except Exception as cleanup_error:
                        self.event("临时显示规则清理未完全完成：" + str(cleanup_error))
            finally:
                self.operation_lock.release()

    def cancel(self):
        if self.operation.get("action") != "queue-detach" or self.operation.get("state") not in {"waiting", "running"}:
            raise RuntimeError("没有待取消的移除预约。")
        self.cancel_event.set()
        return "正在取消预约。"
