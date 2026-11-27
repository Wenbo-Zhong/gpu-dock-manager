"""桌面无关的显示所有权校验与布局备份位置。"""

import os
from pathlib import Path
import threading

from ..contracts import BUSY_STATES as BUSY, INTERNAL_DISPLAY_ACTIONS
from ..i18n import tr


class DisplaySession:
    """只共享会话资源；不把某个桌面的协议当作另一个桌面的基类。"""
    backend = "gnome"

    def __init__(self, backup_path=None, *, internal=False):
        state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        # 桌面后端和内外 GPU 分别保存备份，避免两组显示操作互相覆盖恢复入口。
        suffix = "-kde" if self.backend == "kde" else ""
        suffix += "-internal" if internal else ""
        self.backup_path = (Path(backup_path) if backup_path else
                            state_home / "usb4-gpu-manager" / f"display-layout{suffix}.json")
        self.lock = threading.RLock()

    @staticmethod
    def ready(data):
        if not data.get("service_connected") or not data.get("can_mutate_devices"):
            raise RuntimeError(tr('Device status is unavailable or stale. Restore status collection first.'))
        if data.get("operation", {}).get("state") in BUSY:
            raise RuntimeError(tr('A GPU operation is in progress. Wait or cancel the queued request.'))


def internal_display_data(data):
    internal = data.get("internal_gpu") or {}
    # 仅替换显示适配器的目标视图，保留原始整机数据用于跨 GPU 所有权检查。
    gpu = next((item for item in data.get("gpus", []) if item["pci"] == internal.get("pci")
                and item["driver"] == "nvidia" and not internal.get("reason")), None)
    return dict(data, target=gpu)


def target_outputs(data):
    gpu = data.get("target")
    internal = data.get("internal_gpu") or {}
    supported_internal = (gpu and gpu["pci"] == internal.get("pci") and gpu["driver"] == "nvidia"
                          and not internal.get("reason"))
    if not gpu or gpu.get("boot_vga") or not (gpu.get("external") or supported_internal):
        raise RuntimeError(tr('No unique loaded target GPU is available.'))
    owners = {}
    # 桌面协议按连接器名称操作，因此必须确认该名称在整机中只归属于目标 PCI。
    for device in data.get("gpus", []):
        for connector in device["connectors"]:
            owners.setdefault(connector["name"], []).append(device["pci"])
    outputs = set()
    for connector in gpu["connectors"]:
        if connector["kind"] != "external":
            continue
        if owners.get(connector["name"]) != [gpu["pci"]]:
            raise RuntimeError(tr('Multiple GPUs have the same connector name; ambiguous monitors will not be changed.'))
        if connector["status"] not in {"connected", "disconnected"}:
            raise RuntimeError(tr('External monitor connection state is unknown. Refresh and try again.'))
        if connector["status"] == "connected":
            outputs.add(connector["name"])
    return outputs


def internal_outputs(data):
    gpu = data.get("target") or {}
    # 面板必须归属于另一 PCI GPU，且连接器名称在整机中唯一。
    counts = {}
    for device in data.get("gpus", []):
        for c in device["connectors"]:
            counts[c["name"]] = counts.get(c["name"], 0) + 1
    return {c["name"] for g in data.get("gpus", []) if g["pci"] != gpu.get("pci")
            for c in g["connectors"] if c["kind"] == "internal" and c["status"] == "connected"
            and counts[c["name"]] == 1}
