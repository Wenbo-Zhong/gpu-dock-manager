"""Versioned, reversible Ubuntu/NVIDIA configuration deployment."""
import base64
import json
import os
from pathlib import Path
import re
import time

from .hardware import target_gpu
from .system import atomic_write, command, load_json, read

CONFIG = Path("/etc/usb4-gpu-manager/profile.json")
STATE = Path("/var/lib/usb4-gpu-manager")
MANAGED_MODPROBE = Path("/etc/modprobe.d/99-usb4-gpu-manager.conf")
MANAGED_UDEV = Path("/etc/udev/rules.d/99-usb4-gpu-manager.rules")
LEGACY = [
    "/etc/systemd/system/egpu-nvidia-load.service",
    "/etc/udev/rules.d/98-nvidia-egpu-only.rules",
    "/etc/modprobe.d/blacklist-nvidia-auto.conf",
    "/etc/modprobe.d/zz-nvidia-egpu-manual.conf",
    "/etc/modprobe.d/vfio-nvidia-ignore-internal.conf",
    "/etc/modprobe.d/nvidia-exclude-4070.conf",
    "/etc/modprobe.d/zzzz-egpu-kernel-pm.conf",
    "/etc/systemd/system/nvidia-suspend.service.d/egpu-unload.conf",
    "/etc/systemd/system/nvidia-suspend.service.d/egpu-display-status.conf",
    "/etc/systemd/system/nvidia-resume.service.d/egpu-resume.conf",
    "/etc/systemd/system/nvidia-resume.service.d/zz-egpu-display-status.conf",
]


def suggested_profile(gpus):
    candidates = [g for g in gpus if g["vendor"] == "0x10de"
                  and g["driver"] != "vfio-pci" and not g["boot_vga"] and g.get("external")]
    target = candidates[0] if len(candidates) == 1 else None
    return {"schema": 1, "enabled": True,
            "target": {"vendor": target["vendor"], "device": target["device"]} if target else {},
            "protected": [{"vendor": g["vendor"], "device": g["device"]}
                          for g in gpus if g["driver"] == "vfio-pci"],
            "stable_seconds": 12, "keep_path_awake": True}


def validate_profile(profile, gpus):
    if not isinstance(profile, dict) or set(profile) - {"schema", "enabled", "target", "protected", "stable_seconds", "keep_path_awake"}:
        raise ValueError("配置字段不受支持。")
    for key in ("target",):
        value = profile.get(key)
        if not isinstance(value, dict) or set(value) != {"vendor", "device"}:
            raise ValueError("必须选择唯一的目标显卡。")
    protected = profile.get("protected", [])
    if not isinstance(protected, list) or len(protected) > 8:
        raise ValueError("内置显卡列表无效。")
    for selector in [profile["target"], *protected]:
        if not isinstance(selector, dict) or set(selector) != {"vendor", "device"}:
            raise ValueError("PCI 设备选择器无效。")
        if not all(isinstance(x, str) and re.fullmatch(r"0x[0-9a-f]{4}", x) for x in selector.values()):
            raise ValueError("PCI ID 必须为 0x1234 格式。")
    if profile["target"]["vendor"] != "0x10de":
        raise ValueError("首版仅部署 NVIDIA 外接显卡；其他设备只读展示。")
    if profile["target"] in protected:
        raise ValueError("目标显卡不能同时被隔离。")
    if type(profile.get("enabled", True)) is not bool or type(profile.get("keep_path_awake", True)) is not bool:
        raise ValueError("开关字段必须为布尔值。")
    stable = profile.get("stable_seconds", 12)
    if type(stable) is not int or not 5 <= stable <= 30:
        raise ValueError("稳定等待时间必须为 5–30 秒。")
    gpu = target_gpu(gpus, profile)
    if not gpu:
        raise ValueError("请连接目标显卡；多个相同 PCI ID 的显卡暂不支持自动部署。")
    if not gpu.get("external"):
        raise ValueError("目标设备不是 PCI 子系统标记的可移除外接显卡。")
    if gpu["boot_vga"] or any(c["kind"] == "internal" and c["status"] == "connected" for c in gpu["connectors"]):
        raise ValueError("所选设备是启动显卡或内屏显卡，请重新选择外接显卡。")
    for selector in protected:
        for item in gpus:
            if all(item[k] == v for k, v in selector.items()):
                if item["boot_vga"] or any(c["status"] == "connected" for c in item["connectors"]):
                    raise ValueError("待隔离显卡正在驱动显示器；请先调整显示连接。")
    result = {"schema": 1, "enabled": profile.get("enabled", True),
              "target": profile["target"], "protected": protected,
              "stable_seconds": stable, "keep_path_awake": profile.get("keep_path_awake", True)}
    return result


def render(profile, pci_devices=Path("/sys/bus/pci/devices")):
    ids = set()
    for selector in profile["protected"]:
        ids.add(selector["vendor"][2:] + ":" + selector["device"][2:])
        # Isolate the audio function sharing the selected internal GPU's slot.
        for device in pci_devices.glob("*"):
            if read(device / "vendor") == selector["vendor"] and read(device / "device") == selector["device"]:
                for sibling in pci_devices.glob(device.name.rsplit(".", 1)[0] + ".*"):
                    cls = read(sibling / "class", "")
                    if cls.startswith("0x0403") and read(sibling / "vendor") == selector["vendor"]:
                        ids.add(read(sibling / "vendor")[2:] + ":" + read(sibling / "device")[2:])
    lines = ["# Managed by USB4 GPU Manager; use its deployment page to change this.",
             "options nvidia NVreg_UseKernelSuspendNotifiers=1 NVreg_PreserveVideoMemoryAllocations=1 NVreg_TemporaryFilePath=/var",
             "options nvidia_drm modeset=1", "blacklist nouveau"]
    if ids:
        lines += ["options vfio-pci ids=" + ",".join(sorted(ids))]
    for module in ("nvidia", "nvidia_modeset", "nvidia_drm", "nvidia_uvm"):
        lines += [f"blacklist {module}", f"install {module} /bin/false"]
        if ids:
            lines.append(f"softdep {module} pre: vfio-pci")
    selector = profile["target"]
    rule = ("# Device power policy only; the manager daemon handles loading and resume.\n"
            f'ACTION=="add|bind", SUBSYSTEM=="pci", ATTR{{vendor}}=="{selector["vendor"]}", '
            f'ATTR{{device}}=="{selector["device"]}", TEST=="power/control", ATTR{{power/control}}="on"\n')
    return {str(MANAGED_MODPROBE): "\n".join(lines) + "\n", str(MANAGED_UDEV): rule}


def plan(profile, gpus):
    normalized = validate_profile(profile, gpus)
    return {"profile": normalized, "files": render(normalized),
            "migrate": [p for p in LEGACY if Path(p).exists()],
            "requires_reboot": True,
            "summary": "部署一个管理服务；保留 NVIDIA 官方休眠服务；保存现有配置并更新 initramfs，重启后生效。"}


def save_backup(paths, directory):
    manifest = {}
    for name in paths:
        path = Path(name)
        if path.is_symlink():
            raise RuntimeError(f"配置路径是符号链接，需人工检查：{name}")
        if path.exists():
            stat = path.stat()
            manifest[name] = {"data": base64.b64encode(path.read_bytes()).decode(),
                              "mode": stat.st_mode & 0o777, "uid": stat.st_uid, "gid": stat.st_gid}
        else:
            manifest[name] = None
    atomic_write(directory / "manifest.json", json.dumps(manifest, indent=2), 0o600)


def restore_backup(directory):
    manifest = load_json(Path(directory) / "manifest.json")
    if not isinstance(manifest, dict):
        raise RuntimeError("没有完整的部署备份。")
    for name, saved in manifest.items():
        path = Path(name)
        if saved is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, base64.b64decode(saved["data"]), saved["mode"])
            os.chown(path, saved["uid"], saved["gid"])


def deploy(profile, gpus):
    details = plan(profile, gpus)
    params = command(["modinfo", "-p", "nvidia"]).stdout
    if "NVreg_UseKernelSuspendNotifiers:" not in params or "MIT" not in command(["modinfo", "-F", "license", "nvidia"]).stdout:
        raise RuntimeError("需要支持内核休眠通知器的 NVIDIA 开源驱动；请先安装发行版驱动。")
    if not Path("/usr/sbin/update-initramfs").exists():
        raise RuntimeError("首版部署支持使用 initramfs-tools 的 Ubuntu/Debian 系统。")
    legacy = command(["systemctl", "is-active", "egpu-nvidia-load.service"], check=False).stdout.strip()
    if legacy in {"active", "activating"}:
        raise RuntimeError("旧显卡加载服务正在工作，请稍后再迁移。")
    directory = STATE / "backups" / f"{time.time_ns()}"
    directory.mkdir(parents=True, mode=0o700)
    paths = [*LEGACY, *details["files"], str(CONFIG)]
    save_backup(paths, directory)
    previous_latest = load_json(STATE / "latest-backup.json")
    if previous_latest:
        atomic_write(directory / "previous-backup.json", json.dumps(previous_latest), 0o600)
    try:
        for path in LEGACY:
            Path(path).unlink(missing_ok=True)
        for path, text in details["files"].items():
            atomic_write(path, text)
        deployed = dict(details["profile"], deployed_boot=read("/proc/sys/kernel/random/boot_id"))
        atomic_write(CONFIG, json.dumps(deployed, indent=2), 0o600)
        command(["systemctl", "daemon-reload"])
        command(["udevadm", "control", "--reload-rules"])
        command(["update-initramfs", "-u", "-k", "all"], timeout=600)
        atomic_write(STATE / "latest-backup.json", json.dumps({"path": str(directory)}), 0o600)
    except Exception:
        restore_backup(directory)
        command(["systemctl", "daemon-reload"], check=False)
        command(["udevadm", "control", "--reload-rules"], check=False)
        recovery = command(["update-initramfs", "-u", "-k", "all"], timeout=600, check=False)
        if recovery.returncode:
            raise RuntimeError("部署失败，配置已恢复，但 initramfs 重建失败。请检查 /boot 空间并执行 sudo update-initramfs -u -k all。")
        if previous_latest:
            atomic_write(STATE / "latest-backup.json", json.dumps(previous_latest), 0o600)
        raise
    return "部署完成，现有配置已备份。请保存工作并重启，以启用统一管理服务。"


def rollback():
    latest = load_json(STATE / "latest-backup.json", {})
    directory = Path(latest.get("path", "/nonexistent"))
    if directory.parent != STATE / "backups" or not directory.is_dir():
        raise RuntimeError("没有可以回滚的管理器部署。")
    restore_backup(directory)
    command(["systemctl", "daemon-reload"])
    command(["udevadm", "control", "--reload-rules"])
    command(["update-initramfs", "-u", "-k", "all"], timeout=600)
    previous = load_json(directory / "previous-backup.json")
    if previous:
        atomic_write(STATE / "latest-backup.json", json.dumps(previous), 0o600)
    else:
        (STATE / "latest-backup.json").unlink(missing_ok=True)
    return "已恢复部署前的配置。请重启。"
