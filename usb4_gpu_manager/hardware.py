"""Read-only hardware discovery; never assume stable card numbers or PCI BDFs."""
import csv
import io
from pathlib import Path
import re
import subprocess
import time

from .system import command, read, gpu_holder_hint
from .internal_gpu import internal_gpu_status


def connection_state(connectors):
    external = [c for c in connectors if c["kind"] == "external"]
    if any(c["status"] == "connected" for c in external):
        return "connected"
    if not external or any(c["status"] != "disconnected" for c in external):
        return "unknown"
    return "disconnected"


def pci_names():
    """Use the distro's PCI database, without requiring lspci or a network."""
    names, vendor = {}, None
    for path in ("/usr/share/misc/pci.ids", "/usr/share/hwdata/pci.ids"):
        try:
            for line in Path(path).read_text(errors="replace").splitlines():
                if re.match(r"^[0-9a-f]{4}  ", line):
                    vendor = line[:4]
                elif vendor and re.match(r"^\t[0-9a-f]{4}  ", line):
                    names[("0x" + vendor, "0x" + line[1:5])] = line[7:].strip()
            break
        except OSError:
            continue
    return names


NAMES = pci_names()


def external_power_online(root=Path("/sys/class/power_supply")):
    values = []
    for supply in root.iterdir() if root.is_dir() else ():
        kind = read(supply / "type")
        scope = read(supply / "scope", "")
        if (kind == "Mains" and scope != "Device") or (kind.startswith("USB") and scope == "System"):
            values.append(read(supply / "online"))
    if "1" in values:
        return True
    return False if values and all(value == "0" for value in values) else None


def gpu_inventory(root=Path("/sys")):
    devices = {}
    for device in (root / "bus/pci/devices").glob("*"):
        value = read(device / "class", "0")
        try:
            is_gpu = int(value, 16) >> 16 == 3
        except ValueError:
            is_gpu = False
        if is_gpu:
            devices[device.resolve().name] = device
    cards = {}
    drm = root / "class/drm"
    for card in drm.glob("card*"):
        if re.fullmatch(r"card\d+", card.name) and (card / "device").exists():
            device = card / "device"
            devices[device.resolve().name] = device
            cards[device.resolve().name] = card
    gpus = []
    for bdf, device in sorted(devices.items()):
        vendor, product = read(device / "vendor"), read(device / "device")
        card, connectors, nodes = cards.get(bdf), [], []
        if card:
            nodes.append("/dev/dri/" + card.name)
            for connector in sorted(drm.glob(card.name + "-*")):
                if not (connector / "status").exists():
                    continue
                name = connector.name[len(card.name) + 1:]
                kind = ("internal" if name.startswith(("eDP-", "LVDS-", "DSI-"))
                        else "virtual" if name.startswith(("Writeback-", "Virtual-"))
                        else "external")
                connectors.append({"name": name, "kind": kind,
                                   "status": read(connector / "status"),
                                   "enabled": read(connector / "enabled")})
        for render in drm.glob("renderD*"):
            if (render / "device").resolve() == device.resolve():
                nodes.append("/dev/dri/" + render.name)
        driver = (device / "driver").resolve().name if (device / "driver").exists() else "unbound"
        gpus.append({"pci": bdf, "vendor": vendor, "device": product,
                     "name": NAMES.get((vendor, product), f"GPU {vendor}:{product}"),
                     "driver": driver, "card": card.name if card else None,
                     "nodes": nodes, "connectors": connectors,
                     "boot_vga": read(device / "boot_vga", "0") == "1",
                     "external": read(device / "removable", "") == "removable",
                     "power_state": read(device / "power_state", ""),
                     "runtime_status": read(device / "power/runtime_status", ""),
                     "external_display_state": connection_state(connectors),
                     "path": str(device.resolve())})
    return gpus


def usb4_inventory(root=Path("/sys")):
    devices = []
    for router in sorted((root / "bus/thunderbolt/devices").glob("*")):
        if not (router / "unique_id").exists():
            continue
        rx, tx = read(router / "rx_speed"), read(router / "tx_speed")
        rx_lanes, tx_lanes = read(router / "rx_lanes"), read(router / "tx_lanes")
        def aggregate(speed, lanes):
            try:
                return f"{float(speed.split()[0]) * int(lanes):g} Gb/s"
            except (ValueError, IndexError):
                return speed
        devices.append({"id": router.name, "uuid": read(router / "unique_id"),
                        "name": read(router / "device_name", router.name),
                        "vendor": read(router / "vendor_name"),
                        "authorized": read(router / "authorized"),
                        "rx_speed": aggregate(rx, rx_lanes), "rx_lanes": rx_lanes,
                        "tx_speed": aggregate(tx, tx_lanes), "tx_lanes": tx_lanes,
                        "is_host": router.name.endswith("-0")})
    return devices


def internal_metrics_ready(gpu):
    return bool(gpu and not gpu.get("external") and gpu.get("device") == "0x2820"
                and gpu.get("vendor") == "0x10de" and gpu.get("driver") == "nvidia"
                and gpu.get("runtime_status") == "active" and gpu.get("power_state") == "D0")


def nvidia_metrics(gpu, include_internal=False):
    """Query one BDF; automatically query the internal GPU only while awake."""
    if (not gpu or gpu["vendor"] != "0x10de" or gpu["driver"] != "nvidia"
            or not (gpu["external"] or (include_internal and internal_metrics_ready(gpu)))):
        return []
    try:
        result = command(["nvidia-smi", "--id=" + gpu["pci"],
                          "--query-gpu=pci.bus_id,name,uuid,driver_version,memory.used,memory.total,temperature.gpu,power.draw,utilization.gpu",
                          "--format=csv,noheader,nounits"], timeout=4, check=False)
        if result.returncode:
            return []
        fields = ["pci", "name", "uuid", "driver_version", "memory_used_mib",
                  "memory_total_mib", "temperature_c", "power_w", "utilization_pct"]
        metrics = []
        for row in csv.reader(io.StringIO(result.stdout)):
            if len(row) != len(fields):
                continue
            data = dict(zip(fields, (x.strip() for x in row)))
            parts = data["pci"].lower().split(":")
            data["pci"] = f"{int(parts[0], 16):04x}:{parts[1]}:{parts[2]}"
            if data["pci"] == gpu["pci"]:
                metrics.append(data)
        return metrics
    except (OSError, RuntimeError, ValueError, TimeoutError):
        return []
    except subprocess.TimeoutExpired:
        return []


def module_parameters(root=Path("/proc")):
    result = {}
    for line in read(root / "driver/nvidia/params", "").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            if key in {"UseKernelSuspendNotifiers", "PreserveVideoMemoryAllocations", "EnableS0ixPowerManagement"}:
                result[key] = value.strip()
    return result


def target_gpu(gpus, profile):
    selector = profile.get("target", {})
    matches = [g for g in gpus if g["vendor"] == selector.get("vendor")
               and g["device"] == selector.get("device")]
    if len(matches) != 1:
        return None
    return matches[0]


def holders(gpu, proc=Path("/proc")):
    """Conservative: shared NVIDIA control-file users also block removal."""
    nodes = set(gpu["nodes"])
    if gpu["vendor"] == "0x10de":
        nodes |= {"/dev/nvidiactl", "/dev/nvidia-modeset", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools"}
    found, incomplete = [], False
    for process in proc.glob("[0-9]*"):
        opened = set()
        try:
            descriptors = list((process / "fd").iterdir())
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            incomplete = True
            continue
        for fd in descriptors:
            try:
                name = str(fd.readlink()).removesuffix(" (deleted)")
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                incomplete = True
                continue
            if name in nodes or (gpu["vendor"] == "0x10de" and re.fullmatch(r"/dev/nvidia\d+", name)):
                opened.add(name)
        if opened:
            found.append({"pid": int(process.name), "name": read(process / "comm", "process"),
                          "nodes": sorted(opened)})
    return {"processes": found, "complete": not incomplete}


def other_internal_display_ready(gpus, gpu):
    return bool(gpu and any(g["pci"] != gpu["pci"] and
                           any(c["kind"] == "internal" and c["status"] == "connected" and c["enabled"] == "enabled"
                               for c in g["connectors"]) for g in gpus))


def detach_check(gpu, occupied, allow_connected=False, other_display_ready=False):
    blockers = []
    if gpu is None:
        return {"safe": False, "blockers": ["目标显卡不存在，或有多个同型号设备，无法唯一识别。"]}
    if not gpu.get("external", False):
        blockers.append("PCI 子系统未将此设备标记为可移除设备，拒绝自动移除。")
    if gpu["boot_vga"] or any(c["kind"] == "internal" and c["status"] == "connected" for c in gpu["connectors"]):
        blockers.append("该显卡是启动显卡或连接内屏，不能安全移除。")
    external = [c for c in gpu["connectors"] if c["kind"] == "external"]
    disabled = bool(external and all(c.get("enabled") == "disabled" for c in external)
                    and all(c["status"] in {"connected", "disconnected"} for c in external)
                    and other_display_ready)
    if gpu["external_display_state"] == "connected" and not allow_connected and not disabled:
        blockers.append("该显卡仍连接外屏；请先停用外屏并启用另一张显卡的内屏，或拔掉显示器线。")
    elif gpu["external_display_state"] not in {"connected", "disconnected"}:
        blockers.append("无法确定该显卡的显示器状态，拒绝移除。")
    if allow_connected and any(c["status"] not in {"connected", "disconnected"} for c in gpu["connectors"] if c["kind"] == "external"):
        blockers.append("外接接口存在未知状态，拒绝移除。")
    if not occupied["complete"]:
        blockers.append("无法完整检查进程占用，请通过管理员服务重新检查。")
    for process in occupied["processes"]:
        hint = gpu_holder_hint(process)
        blockers.append(f'{process["name"]}（PID {process["pid"]}）正在占用显卡。' + (" " + hint if hint else ""))
    if gpu["driver"] not in {"nvidia", "unbound"}:
        blockers.append(f'目标显卡由 {gpu["driver"]} 使用，当前版本不执行移除。')
    return {"safe": not blockers, "blockers": blockers}


def snapshot(profile=None, metrics=True):
    profile = profile or {}
    gpus = gpu_inventory()
    gpu = target_gpu(gpus, profile)
    if not gpu and not profile.get("target"):
        candidates = [g for g in gpus if g["vendor"] == "0x10de" and g["driver"] != "vfio-pci" and not g["boot_vga"] and g["external"]]
        gpu = candidates[0] if len(candidates) == 1 else None
    occupied = holders(gpu) if gpu else {"processes": [], "complete": True}
    return {"timestamp": time.time(), "gpus": gpus, "target": gpu, "internal_gpu": internal_gpu_status(gpus),
            "external_power_online": external_power_online(),
            "other_internal_display_ready": other_internal_display_ready(gpus, gpu),
            "usb4": usb4_inventory(), "metrics": nvidia_metrics(gpu) if metrics and gpu else [],
            "parameters": module_parameters(), "holders": occupied,
            "detach": detach_check(gpu, occupied, other_display_ready=other_internal_display_ready(gpus, gpu)), "profile": profile,
            "service_connected": False}


def service_status():
    try:
        result = command(["systemctl", "show", "--no-pager", "usb4-gpu-manager.service",
                          "egpu-nvidia-load.service", "nvidia-suspend.service", "nvidia-resume.service",
                          "-p", "Id", "-p", "LoadState", "-p", "ActiveState", "-p", "UnitFileState", "-p", "Result"],
                         timeout=4, check=False)
        units = []
        for block in result.stdout.strip().split("\n\n"):
            fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if fields.get("Id"):
                units.append(fields)
        return units
    except (OSError, subprocess.TimeoutExpired):
        return []


def recent_events():
    """Read only this project's and its predecessor's journal identifiers."""
    try:
        result = command(["journalctl", "-b", "--no-pager", "-o", "json", "-n", "60",
                          "-t", "usb4-gpu-manager", "-t", "egpu-nvidia-resume",
                          "-t", "egpu-display-status", "-t", "egpu-nvidia-suspend"], timeout=4, check=False)
        import json
        events = []
        for line in result.stdout.splitlines():
            try:
                entry = json.loads(line)
                message = entry.get("MESSAGE", "")
                if not isinstance(message, str):
                    continue
                if entry.get("SYSLOG_IDENTIFIER") == "egpu-display-status":
                    display = json.loads(message).get("egpu_display_state", "unknown")
                    message = "外接显卡显示器状态：" + display
                events.append({"time": int(entry["__REALTIME_TIMESTAMP"]) / 1e6,
                               "message": message[:2000]})
            except (ValueError, KeyError, TypeError):
                continue
        return events
    except (OSError, subprocess.TimeoutExpired):
        return []
