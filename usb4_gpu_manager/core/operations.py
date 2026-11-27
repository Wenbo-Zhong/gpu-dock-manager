"""硬件操作方法；使用 Controller 的设备锁、睡眠状态及事件，绝不复制状态机。"""

from pathlib import Path
import subprocess
import time

from ..i18n import tr
from ..hardware import target_gpu, detach_check, holders, other_internal_display_ready, usb4_inventory
from ..probe import bounded_inventory as gpu_inventory, device_action, internal_device_action
from ..system import command, read, sleep_inhibitor
from .policy import bar_signature


class DeviceOperations:
    # 作为 Controller 的方法分组使用，不单独实例化；锁和状态均来自同一控制器。
    def _switch_internal(self, action, pci, protect_connected=False):
        self._ensure_awake()
        try:
            internal_device_action(action, pci, protect_connected=protect_connected)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(tr('4070 driver switch timed out. Check the device state; restart if needed to restore the boot configuration.')) from error

    def _attach(self, profile, rescan=True):
        with self.device_lock:
            return self._attach_locked(profile, rescan)

    def _attach_locked(self, profile, rescan=True):
        self._ensure_awake()
        # BAR 等待与驱动绑定都在同一设备锁和睡眠锁内。
        with sleep_inhibitor():
            # 获取 inhibitor 也是一次外部调用，期间可能收到睡眠信号，需再次检查。
            self._ensure_awake()
            return self._attach_awake(profile, rescan)

    def _ensure_awake(self):
        if self.sleeping:
            raise RuntimeError(tr('The system is preparing to sleep. Resume before operating the GPU.'))

    def _attach_awake(self, profile, rescan=True):
        if not profile.get("target"):
            raise RuntimeError(tr('Select the target GPU on the Deployment page first.'))
        if rescan:
            Path("/sys/bus/pci/rescan").write_text("1\n")
            # PCI 条目出现不等于资源已分配；BAR 连续稳定后才能初始化 NVIDIA。
            deadline, previous, since = time.monotonic() + 100, None, 0
            while time.monotonic() < deadline:
                self._ensure_awake()
                gpu = target_gpu(gpu_inventory(), profile)
                signature = None
                if gpu:
                    if profile.get("keep_path_awake", True):
                        self._keep_awake(gpu)
                    signature = bar_signature(gpu, read(Path(gpu["path"]) / "resource", ""))
                if signature != previous:
                    previous, since = signature, time.monotonic()
                if signature and time.monotonic() - since >= profile.get("stable_seconds", 12):
                    break
                time.sleep(1)
            else:
                raise RuntimeError(tr('No stable external GPU found. Check power and the USB4 cable.'))
        gpu = target_gpu(gpu_inventory(), profile)
        # 稳定等待结束后重新选卡，避免使用等待过程中已失效的设备快照。
        if not gpu:
            raise RuntimeError(tr('The target GPU has not appeared.'))
        if gpu["driver"] not in {"nvidia", "unbound"}:
            raise RuntimeError(tr('Another driver holds the target GPU; takeover refused.'))
        if profile.get("keep_path_awake", True):
            self._keep_awake(gpu)
        for module in ("nvidia", "nvidia_modeset", "nvidia_drm", "nvidia_uvm"):
            self._ensure_awake()
            # --ignore-install 绕过部署的延迟加载规则，仅在管理器明确连接时执行。
            command(["modprobe", "--ignore-install", module], timeout=45)
        device = Path("/sys/bus/pci/devices") / gpu["pci"]
        if not (device / "driver").exists():
            self._ensure_awake()
            device_action("bind", gpu, timeout=15)
        for _ in range(20):
            self._ensure_awake()
            # 模块命令成功不能替代设备绑定确认，还需看到 NVIDIA 控制设备节点。
            if (device / "driver").resolve().name == "nvidia" and Path("/dev/nvidiactl").exists():
                return
            time.sleep(1)
        raise RuntimeError(tr('NVIDIA modules loaded, but GPU binding did not complete.'))

    @staticmethod
    def _keep_awake(gpu):
        device_action("power", gpu, timeout=3)

    def _detach(self, gpu, allow_displays=False, allowed_displays=None):
        with self.device_lock:
            return self._detach_locked(gpu, allow_displays, allowed_displays)

    def _detach_locked(self, gpu, allow_displays=False, allowed_displays=None):
        _, current = self._profile_and_gpu()
        if not current or current["pci"] != gpu["pci"]:
            raise RuntimeError(tr('The GPU changed; removal cancelled.'))
        # 队列预检不能代替此处检查；新增显示器必须在最终设备锁内拒绝。
        if allowed_displays is not None:
            connected_names = {c["name"] for c in current["connectors"]
                               if c["kind"] == "external" and c["status"] == "connected"}
            if current["external_display_state"] == "unknown" or connected_names - allowed_displays:
                raise RuntimeError(tr('Monitor connections changed; queued removal cancelled.'))
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
            # 超时只表示结果未确认，不能给出允许拔线的成功提示或立即重复移除。
            raise RuntimeError(tr('Driver removal exceeded 25 seconds. Device release is unconfirmed; monitoring continues. Do not unplug or retry removal yet.')) from error
        # 不卸载共享模块：softdep 可能连带释放内置 GPU 的 VFIO 隔离。
        self.event(tr('Target GPU and audio function removed from PCI. You may unplug the GPU cable.'))
        return tr('Safely removed. You may unplug the GPU cable. Reconnect USB4 or click Reconnect to use it again.')
