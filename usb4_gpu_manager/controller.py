"""Manager state machine. All mutations are serialized and performed by root."""

from .i18n import tr
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
                self.event(tr('USB4 device disconnected; automatic connection is allowed again.'))
            else:
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
            self.event(tr('Power or USB4 state changed. Automatic switching was skipped this time; use manual controls if needed.'))
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
                    self.operation = {"state": "running", "action": "auto-gpu-policy", "message": tr('Processing a power or USB4 state change…')}
                if current != wanted:
                    try:
                        self._switch_internal("internal-attach" if wanted == HOST_DRIVERS else "internal-detach",
                                              internal["pci"], protect_connected=True)
                        results.append(tr('4070 loaded') if wanted == HOST_DRIVERS else tr('4070 switched to VFIO'))
                    except (OSError, RuntimeError) as error:
                        errors.append("4070: " + str(error))
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

    def _switch_internal(self, action, pci, protect_connected=False):
        if self.sleeping:
            raise RuntimeError(tr('The system is preparing to sleep. Resume before operating the GPU.'))
        try:
            internal_device_action(action, pci, protect_connected=protect_connected)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(tr('4070 driver switch timed out. Check the device state; restart if needed to restore the boot configuration.')) from error

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
            raise RuntimeError(tr('The target GPU changed. Refresh and try again.'))

    def _attach(self, profile, rescan=True):
        with self.device_lock:
            return self._attach_locked(profile, rescan)

    def _attach_locked(self, profile, rescan=True):
        if not profile.get("target"):
            raise RuntimeError(tr('Select the target GPU on the Deployment page first.'))
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
                raise RuntimeError(tr('No stable external GPU found. Check power and the USB4 cable.'))
        gpu = target_gpu(gpu_inventory(), profile)
        if not gpu:
            raise RuntimeError(tr('The target GPU has not appeared.'))
        if gpu["driver"] not in {"nvidia", "unbound"}:
            raise RuntimeError(tr('Another driver holds the target GPU; takeover refused.'))
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
        raise RuntimeError(tr('NVIDIA modules loaded, but GPU binding did not complete.'))

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
            raise RuntimeError(tr('The GPU changed; removal cancelled.'))
        connected = current["external_display_state"] == "connected"
        panel_ready = other_internal_display_ready(gpu_inventory(), current) if connected or allow_displays else False
        check = detach_check(current, holders(current), allow_connected=allow_displays, other_display_ready=panel_ready)
        if allow_displays and not panel_ready:
            raise RuntimeError(tr("The other GPU's internal panel is not enabled. Removal cancelled; enable the laptop panel first."))
        if not check["safe"]:
            raise RuntimeError("\n".join(check["blockers"]))
        self.detached_routers = {r["uuid"] for r in usb4_inventory() if not r["is_host"]}
        with self.lock:
            self.operation.update(state="removing", message=tr('Requesting device removal. Keep USB4 connected; do not unplug yet.'))
        self.event(tr('Holder checks passed. Removing GPU and audio devices; keep USB4 connected until completion.'))
        try:
            device_action("remove", gpu, timeout=25)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(tr('Driver removal exceeded 25 seconds. Device release is unconfirmed; monitoring continues. Do not unplug or retry removal yet.')) from error
        # PCI removal is sufficient. modprobe -r follows configured softdeps
        # and can also unload vfio-pci, releasing an isolated internal GPU.
        # Keep shared modules resident; never clean them up as part of detach.
        self.event(tr('Target GPU and audio function removed from PCI. You may unplug the GPU cable.'))
        return tr('Safely removed. You may unplug the GPU cable. Reconnect USB4 or click Reconnect to use it again.')

    def perform(self, action, options):
        allowed = {"attach": set(), "detach": {"expected_pci"},
                   "queue-detach": {"expected_pci"}, "deploy": {"profile"}, "rollback": set(),
                   **dict.fromkeys(INTERNAL_ACTIONS, {"expected_pci"})}
        if action not in allowed or not isinstance(options, dict) or set(options) - allowed[action]:
            raise ValueError(tr('Unsupported operation or options.'))
        if not self.operation_lock.acquire(blocking=False):
            raise RuntimeError(tr('Another operation is in progress. Wait or cancel the queued request.'))
        self.cancel_event.clear()
        with self.lock:
            self.operation = {"state": "running", "action": action, "message": tr('Working…')}
        try:
            if self.sleeping and action in {"attach", "detach", "queue-detach", *INTERNAL_ACTIONS}:
                raise RuntimeError(tr('The system is preparing to sleep. Resume before operating the GPU.'))
            profile, gpu = (None, None) if action in {"deploy", "rollback", *INTERNAL_ACTIONS} else self._profile_and_gpu()
            if action == "deploy":
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
                            message = self._detach(current, allow_displays=True)
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
                self.operation_lock.release()

    def cancel(self):
        if self.operation.get("action") != "queue-detach" or self.operation.get("state") not in {"waiting", "running"}:
            raise RuntimeError(tr('No queued removal to cancel.'))
        self.cancel_event.set()
        return tr('Cancelling the queued request.')
