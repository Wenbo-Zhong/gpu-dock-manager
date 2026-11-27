"""管理状态机：操作锁串行化请求，设备锁保护硬件快照与修改的一致性。"""

from ..i18n import tr
from collections import deque
import json
import logging
import subprocess
from pathlib import Path
import threading
import time

from .. import deployment
from ..hardware import detach_check, holders, nvidia_metrics, internal_metrics_ready, other_internal_display_ready, service_status, target_gpu
from ..internal_gpu import HOST_DRIVERS, VFIO
from ..probe import bounded_inventory as gpu_inventory, bounded_snapshot as snapshot
from ..presence import observe, remember_transport
from ..system import atomic_write, command, load_json, read, stalled_commands
from ..contracts import INTERNAL_ACTIONS, DEVICE_ACTIONS, valid_options
from . import policy
from .operations import DeviceOperations

LOG = logging.getLogger("usb4-gpu-manager")
AUTOMATION_CONFIG = Path("/etc/usb4-gpu-manager/automation.json")
IGNORE_RULE = Path("/run/udev/rules.d/99-usb4-gpu-manager-detach.rules")
IGNORE_STATE = Path("/run/usb4-gpu-manager/ignore.json")


class Controller(DeviceOperations):
    def __init__(self):
        # 三把锁职责不同：状态锁只保护短时读写，操作锁占用整个任务，
        # 设备锁覆盖最终检查及硬件修改；慢速探测期间不要一直持有状态锁。
        self.lock = threading.RLock()
        self.operation_lock = threading.Lock()
        self.operation_reservation = None
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
        # 已确认状态与候选状态分开，连续观测同一变化后才触发自动切换。
        self.policy_state = None
        self.policy_candidate = None
        self.last_error = ""

    def event(self, message):
        LOG.info(message)
        with self.lock:
            self.events.append({"time": time.time(), "message": message})

    def active_profile(self):
        return load_json(deployment.CONFIG, {}) or {}

    def automation_enabled(self):
        return load_json(AUTOMATION_CONFIG, {}) != {"enabled": False}

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
        # 采样有缓存周期，驱动卸载后必须立即过滤旧指标，避免展示残留负载。
        self.metrics = [metric for metric in self.metrics if metric["pci"] in eligible]
        data.update({"service_connected": True, "metrics": self.metrics, "services": self.units,
                     "suggested_profile": deployment.suggested_profile(data["gpus"]),
                     "deployment": deployment.status(profile),
                     "kernel": read("/proc/sys/kernel/osrelease"),
                     "session_management": "kernel-notifier",
                     "last_resume": self.last_resume, "last_error": self.last_error})
        with self.lock:
            data["operation"] = dict(self.operation)
            data["events"] = list(self.events)
            self.cached = data
        return data

    def get_status(self):
        # API 读取缓存而不等待设备锁，驱动调用阻塞时仍能查询进度和取消预约。
        with self.lock:
            data = dict(self.cached, operation=dict(self.operation), events=list(self.events), last_error=self.last_error)
        profile = self.active_profile()
        data.update(service_connected=True, profile=profile, automation_enabled=self.automation_enabled(),
                    deployment=deployment.status(profile))
        data = observe(data)
        if stalled_commands():
            data["can_mutate_devices"] = False
            data["blocked_io"] = True
            data["last_error"] = tr('A driver call is still blocked in the kernel. Duplicate calls are paused; save your work and restart to recover.')
        return data

    def prepare_for_sleep(self, sleeping):
        self.sleeping = sleeping
        if sleeping:
            target = self.get_status().get("target")
            self.sleep_had_gpu = target is not None
            displays = target.get("connectors", []) if target else []
            self.event(tr('Entering sleep; display connections: ') + json.dumps(displays, ensure_ascii=False))
        else:
            self.signature, self.stable_since = None, 0
            self.auto_pending = True
            self.last_resume = {"at": time.time(), "state": "waiting" if self.sleep_had_gpu else "absent"}
            self.event(tr('System resumed; waiting for the USB4/PCIe path to stabilize.') if self.sleep_had_gpu else tr('System resumed; no external GPU was connected before sleep.'))

    def tick(self):
        data = self.refresh()
        data = observe(data)
        routers = policy.router_ids(data)
        # 物理拔线必须先清除手动移除抑制，再进入断开分支；同 UUID 重插仍可连接。
        if self.detached_routers and not self.detached_routers.issubset(routers):
            self.detached_routers = None
            self.event(tr('USB4 device disconnected; automatic connection is allowed again.'))
        if policy.transport_disconnected(data):
            self.signature, self.stable_since = None, 0
            self.auto_pending, self.retry_after = True, 0
        # 暂停自动管理仍需处理上面的物理拔线复位，恢复开关后才能正常重连。
        if not self.automation_enabled():
            return
        if data.get("internal_gpu"):
            self._tick_internal_policy(data)
            return
        if policy.transport_disconnected(data):
            return
        profile, gpu = data["profile"], data.get("target")
        if not policy.automatic_attach_allowed(data, self.sleeping):
            return
        if self.detached_routers is not None:
            return
        if not gpu:
            self.signature = None
            self.auto_pending = True
            if self.last_resume and self.last_resume["state"] == "waiting" and time.time() - self.last_resume["at"] > 30:
                self.last_resume["state"] = "absent"
                self.event(tr('No external GPU detected after resume; waiting for a connection.'))
            return
        if not self.auto_pending or time.monotonic() < self.retry_after:
            return
        if profile.get("keep_path_awake", True):
            with self.device_lock:
                if self.automation_enabled():
                    self._keep_awake(gpu)
        signature = policy.bar_signature(gpu, read(Path(gpu["path"]) / "resource", ""))
        if signature is None:
            self.signature = None
            return
        if signature != self.signature:
            # 地址、路径或 BAR 变化都重新计时，不能沿用上一次枚举的稳定时间。
            self.signature, self.stable_since = signature, time.monotonic()
            return
        if time.monotonic() - self.stable_since < profile.get("stable_seconds", 12):
            return
        if not self.operation_lock.acquire(blocking=False):
            return
        try:
            # 等待稳定期间用户可能暂停自动管理，取得操作槽后再次读取开关。
            if not self.automation_enabled():
                return
            self._attach(profile, rescan=False)
            self.auto_pending = False
            self.last_error = ""
            if self.last_resume and self.last_resume["state"] == "waiting":
                self.last_resume["state"] = "ready"
            self.event(tr('External GPU bound to NVIDIA; display outputs and CUDA are available.'))
        except Exception as error:
            self.last_error = str(error)
            self.retry_after = time.monotonic() + 15
            self.event(tr('Automatic connection failed: ') + str(error))
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
        state = policy.internal_policy_state(data, self.sleeping)
        if state is None:
            self.policy_candidate = None
            return
        if self.policy_state is None:
            self.policy_state = state
        if state == self.policy_state:
            self.policy_candidate = None
            return
        if state != self.policy_candidate:
            self.policy_candidate = state
            return

        # 先消费已确认的变化；本轮跳过或失败不会每次 tick 都重复切换驱动。
        # 用户可手动操作，下一次电源/物理连接变化仍会重新触发策略。
        previous = self.policy_state
        self.policy_state = state
        self.policy_candidate = None
        if not external:
            self.detached_routers = None
        if (not data["can_mutate_devices"] or data["deployment"]["restart_required"]
                or stalled_commands() or not self.operation_lock.acquire(blocking=False)):
            self.event(tr('Power or USB4 state changed. Automatic switching was skipped this time; use manual controls if needed.'))
            return
        try:
            with self.device_lock:
                if not self.automation_enabled():
                    return
                internal = data["internal_gpu"]
                gpu = data.get("target")
                profile = self._external_profile(data)
                use_external = external and self.detached_routers is None and (gpu is not None or not previous[1])
                wanted = (VFIO, VFIO) if not power or use_external else HOST_DRIVERS
                current = internal.get("graphics_driver"), internal.get("audio_driver")
                results, errors = [], []
                with self.lock:
                    self.operation = {"state": "running", "action": "auto-gpu-policy", "message": tr('Processing a power or USB4 state change…')}
                if current != wanted:
                    try:
                        self._switch_internal("internal-attach" if wanted == HOST_DRIVERS else "internal-detach",
                                              internal["pci"], protect_connected=True)
                        results.append(tr('4070 loaded') if wanted == HOST_DRIVERS else tr('4070 switched to VFIO'))
                    except (OSError, RuntimeError) as error:
                        errors.append("4070: " + str(error))
                # 内置 GPU 被占用不代表外接 GPU 不能加载，两项结果分别记录。
                if (external and not previous[1] and self.detached_routers is None
                        and profile.get("enabled", True) and profile.get("target")
                        and not (gpu and gpu["driver"] == "nvidia" and gpu.get("card"))
                        and not self.sleeping and not stalled_commands()):
                    try:
                        self._attach_locked(profile, rescan=True)
                        results.append(tr('External GPU loaded'))
                    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                        errors.append(tr('External GPU: ') + str(error))
                message = "; ".join(results + errors) or tr('Power or USB4 state changed; no driver switch is needed.')
                with self.lock:
                    self.operation = {"state": "error" if errors else "done", "action": "auto-gpu-policy", "message": message}
                self.event(message)
        finally:
            self.operation_lock.release()

    @staticmethod
    def _external_profile(data):
        return policy.external_profile(data)

    def _profile_and_gpu(self):
        gpus = gpu_inventory()
        profile = self.active_profile() or deployment.suggested_profile(gpus)
        return profile, target_gpu(gpus, profile)

    @staticmethod
    def _expected(gpu, options):
        if not gpu or options.get("expected_pci") != gpu["pci"]:
            raise RuntimeError(tr('The target GPU changed. Refresh and try again.'))

    def _ignore_for_next_session(self, gpu):
        info = command(["udevadm", "info", "--query=property", "--path", "/sys/class/drm/" + gpu["card"]]).stdout
        if ":mutter-device-ignore:" in info:
            return  # 已有标签不属于本次预约，不能在后续清理时替其他程序撤销。
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
            # 即使 udev 更新失败也删除本程序的临时文件，异常由调用者记录。
            IGNORE_RULE.unlink(missing_ok=True)
            IGNORE_STATE.unlink(missing_ok=True)
            self.ignored_gpu = None
            command(["udevadm", "control", "--reload-rules"], timeout=3)

    def reserve_operation(self, action, options):
        """确认异步请求前原子占用操作槽，避免两个客户端同时收到成功回执。"""
        if action == "cancel-detach" or not valid_options(action, options):
            raise ValueError(tr('Unsupported operation or options.'))
        if not self.operation_lock.acquire(blocking=False):
            raise RuntimeError(tr('Another operation is in progress. Wait or cancel the queued request.'))
        token = object()
        # 每次预约使用独立身份令牌；旧任务的迟到回调不能释放新任务的操作槽。
        self.cancel_event.clear()
        with self.lock:
            self.operation_reservation = (token, action, dict(options))
            self.operation = {"state": "running", "action": action, "message": tr('Working…')}
        return token

    def fail_reserved(self, token, error):
        with self.lock:
            # 提交线程池失败和执行失败都可能进入此处，非当前预约不重复收尾。
            if not self.operation_reservation or self.operation_reservation[0] is not token:
                return
            action = self.operation_reservation[1]
            self.operation = {"state": "error", "action": action, "message": str(error)}
            self.operation_reservation = None
            self.operation_lock.release()
        self.event(tr('Operation failed: ') + str(error))

    def perform(self, action, options, reservation=None):
        token = reservation if reservation is not None else self.reserve_operation(action, options)
        with self.lock:
            if self.operation_reservation != (token, action, options):
                raise RuntimeError(tr('Unsupported operation or options.'))
        try:
            if action in DEVICE_ACTIONS and deployment.status(self.active_profile())["restart_required"]:
                raise RuntimeError(tr('Configuration deployed; restart before reconnecting.'))
            if self.sleeping and action in DEVICE_ACTIONS:
                raise RuntimeError(tr('The system is preparing to sleep. Resume before operating the GPU.'))
            profile, gpu = (None, None) if action in {"deploy", "rollback", "set-automation", *INTERNAL_ACTIONS} else self._profile_and_gpu()
            if action == "set-automation":
                enabled = options.get("enabled")
                if type(enabled) is not bool:
                    raise ValueError(tr('Unsupported operation or options.'))
                with self.device_lock:
                    atomic_write(AUTOMATION_CONFIG, json.dumps({"enabled": enabled}) + "\n")
                    # 开关改变后从新观测建立基线，不沿用暂停前的候选边沿与重试延迟。
                    self.policy_state = self.policy_candidate = None
                    self.signature, self.stable_since = None, 0
                    self.retry_after = 0
                    self.auto_pending = True
                message = tr('Automatic GPU management enabled') if enabled else tr('Automatic GPU management paused')
            elif action == "deploy":
                message = deployment.deploy(options.get("profile"), gpu_inventory())
            elif action == "rollback":
                message = deployment.rollback()
            elif action in INTERNAL_ACTIONS:
                with self.device_lock:
                    pci = options.get("expected_pci")
                    if not isinstance(pci, str):
                        raise RuntimeError(tr('Specify the internal 4070 PCI address.'))
                    self._switch_internal(action, pci)
                message = (tr('Internal 4070 graphics and HDMI audio loaded with host drivers.') if action == "internal-attach"
                           else tr('Internal 4070 graphics and HDMI audio switched to VFIO isolation.'))
            elif action == "attach":
                if not self.active_profile():
                    data = self.get_status()
                    if not data.get("internal_gpu") or data["transport"]["state"] not in {"present", "detached"}:
                        raise RuntimeError(tr('Connect an identifiable external GPU or deploy management configuration first.'))
                    profile = self._external_profile(data)
                if profile.get("deployed_boot") == read("/proc/sys/kernel/random/boot_id"):
                    raise RuntimeError(tr('Configuration deployed; restart before reconnecting.'))
                self.detached_routers = None
                self._attach(profile)
                message = tr('GPU connected; display outputs and CUDA are available.')
            else:
                self._expected(gpu, options)
                if action == "detach":
                    message = self._detach(gpu)
                else:
                    check = detach_check(gpu, {"processes": [], "complete": True}, allow_connected=True)
                    if not check["safe"]:
                        raise RuntimeError("\n".join(check["blockers"]))
                    if not other_internal_display_ready(gpu_inventory(), gpu):
                        raise RuntimeError(tr('Enable the laptop panel driven by another GPU before queuing removal after logout.'))
                    initial_displays = {c["name"] for c in gpu["connectors"] if c["kind"] == "external" and c["status"] == "connected"}
                    self._ignore_for_next_session(gpu)
                    self.operation.update(state="waiting", message=tr('Waiting for logout and GPU release. Queued removal does not mean you may unplug. The request expires in 180 seconds.'))
                    self.event(tr('Safe removal queued. The next GNOME session will temporarily skip this GPU while waiting for current holders to release it.'))
                    deadline = time.monotonic() + 180
                    while time.monotonic() < deadline:
                        # Event 等待让取消及时生效；睡眠时不移除，但预约期限仍继续计时。
                        if self.cancel_event.wait(1):
                            raise RuntimeError(tr('Queued removal cancelled.'))
                        if self.sleeping:
                            continue
                        _, current = self._profile_and_gpu()
                        self._expected(current, options)
                        check = detach_check(current, holders(current), allow_connected=True)
                        now_displays = {c["name"] for c in current["connectors"] if c["kind"] == "external" and c["status"] == "connected"}
                        if current["external_display_state"] == "unknown" or now_displays - initial_displays:
                            raise RuntimeError(tr('Monitor connections changed; queued removal cancelled.'))
                        if check["safe"]:
                            # 等待循环只负责观察，真正提交时还会在设备锁内复查。
                            message = self._detach(current, allow_displays=True, allowed_displays=initial_displays)
                            break
                    else:
                        raise RuntimeError(tr('Timed out waiting for GPU release. The request was cancelled and display access restored.'))
            self.event(message)
            with self.lock:
                self.operation = {"state": "done", "action": action, "message": message}
            return message
        except Exception as error:
            self.event(tr('Operation failed: ') + str(error))
            with self.lock:
                self.operation = {"state": "error", "action": action, "message": str(error)}
            raise
        finally:
            try:
                if self.ignored_gpu:
                    try:
                        self.clear_ignore()
                    except Exception as cleanup_error:
                        self.event(tr('Temporary display rule cleanup was incomplete: ') + str(cleanup_error))
            finally:
                # 临时显示规则清理失败也必须释放自己的预约，不能永久占用操作槽。
                with self.lock:
                    if self.operation_reservation and self.operation_reservation[0] is token:
                        self.operation_reservation = None
                        self.operation_lock.release()

    def cancel(self):
        if self.operation.get("action") != "queue-detach" or self.operation.get("state") not in {"waiting", "running"}:
            raise RuntimeError(tr('No queued removal to cancel.'))
        self.cancel_event.set()
        return tr('Cancelling the queued request.')
