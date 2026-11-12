"""Isolated hardware worker. No arbitrary shell or arbitrary sysfs path input."""
import json
from pathlib import Path
import re
import sys
import syslog

if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from usb4_gpu_manager.system import read


def selected_device(gpu, root=Path("/sys/bus/pci/devices")):
    if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", gpu.get("pci", "")):
        raise ValueError("Invalid PCI address")
    device = root / gpu["pci"]
    if read(device / "vendor") != gpu.get("vendor") or read(device / "device") != gpu.get("device"):
        raise RuntimeError("GPU identity changed")
    if gpu["vendor"] != "0x10de" or read(device / "removable") != "removable":
        raise RuntimeError("Target is not a removable NVIDIA GPU")
    if not read(device / "class", "").startswith("0x03"):
        raise RuntimeError("Target is not a display-class GPU")
    return device


def remove_gpu(gpu, root=Path("/sys/bus/pci/devices")):
    device = selected_device(gpu, root)
    siblings = []
    for path in root.glob(gpu["pci"].rsplit(".", 1)[0] + ".*"):
        if path.name != gpu["pci"] and read(path / "vendor") == gpu["vendor"] and read(path / "class", "").startswith("0x0403"):
            siblings.append(path)
    for path in [*siblings, device]:
        syslog.openlog("usb4-gpu-manager")
        syslog.syslog(syslog.LOG_INFO, "I/O: starting PCI remove for " + path.name)
        (path / "remove").write_text("1\n")
        syslog.syslog(syslog.LOG_INFO, "I/O: completed PCI remove for " + path.name)


def main():
    action, raw = sys.argv[1:]
    value = json.loads(raw)
    if action == "snapshot":
        from usb4_gpu_manager.hardware import snapshot
        print(json.dumps(snapshot(value["profile"], metrics=value["metrics"])))
    elif action == "remove":
        remove_gpu(value)
    elif action == "internal-status":
        from usb4_gpu_manager.internal_gpu import inspect_internal_gpu
        try:
            print(json.dumps(inspect_internal_gpu(value["pci"])))
        except (OSError, RuntimeError) as error:
            raise SystemExit(str(error)) from None
    elif action in {"internal-attach", "internal-detach"}:
        from usb4_gpu_manager.internal_gpu import managed_internal_switch
        managed_internal_switch(value["pci"], action, protect_connected=value.get("protect_connected") is True)
    elif action == "bind":
        device = selected_device(value)
        if (device / "driver").exists():
            if (device / "driver").resolve().name != "nvidia":
                raise RuntimeError("Target has another driver")
        else:
            Path("/sys/bus/pci/drivers/nvidia/bind").write_text(value["pci"])
    elif action == "power":
        path = selected_device(value).resolve()
        while str(path).startswith("/sys/devices/"):
            control = path / "power/control"
            if control.exists():
                control.write_text("on\n")
            path = path.parent
    else:
        raise ValueError("Unsupported worker action")


if __name__ == "__main__":
    main()
