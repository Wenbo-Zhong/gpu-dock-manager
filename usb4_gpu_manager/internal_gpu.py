"""Guarded driver switching for the internal RTX 4070 and its HDMI audio."""

from .i18n import tr
from pathlib import Path
import re

from .system import command, read, gpu_holder_hint

ACTIONS = {"internal-attach", "internal-detach"}
GPU_ID = "0x2820"
AUDIO_ID = "0x22bd"
PCI_ROOT = Path("/sys/bus/pci")
HOST_DRIVERS = ("nvidia", "snd_hda_intel")
VFIO = "vfio-pci"
POWERD_MARKER = Path("/var/lib/usb4-gpu-manager/internal-powerd-paused")


def driver(device):
    link = device / "driver"
    return link.resolve().name if link.is_symlink() else None


def device_pair(pci, bus):
    if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.0", pci):
        raise RuntimeError(tr('Invalid internal GPU PCI address.'))
    gpu, audio = (bus / "devices" / (pci[:-1] + function) for function in ("0", "1"))
    for device, product, device_class in ((gpu, GPU_ID, "0x03"), (audio, AUDIO_ID, "0x0403")):
        if (read(device / "vendor") != "0x10de" or read(device / "device") != product
                or not read(device / "class", "").startswith(device_class)
                or read(device / "removable", "") == "removable"):
            raise RuntimeError(tr('The graphics or audio function does not match the supported internal RTX 4070.'))
    if read(gpu / "boot_vga") != "0":
        raise RuntimeError(tr('The boot GPU cannot be switched.'))
    group = (gpu / "iommu_group").resolve()
    if (not group.name.isdecimal() or (audio / "iommu_group").resolve() != group
            or {member.resolve() for member in (group / "devices").iterdir()} != {gpu.resolve(), audio.resolve()}):
        raise RuntimeError(tr('The IOMMU group must contain only the 4070 graphics and audio functions.'))
    return gpu, audio, group


def internal_gpu_status(gpus, bus=PCI_ROOT):
    candidates = [gpu for gpu in gpus if gpu["vendor"] == "0x10de" and gpu["device"] == GPU_ID
                  and not gpu["external"] and not gpu["boot_vga"]]
    if len(candidates) != 1:
        return None
    pci = candidates[0]["pci"]
    try:
        gpu, audio, group = device_pair(pci, bus)
    except (OSError, RuntimeError) as error:
        return {"pci": pci, "reason": str(error)}
    current = driver(gpu), driver(audio)
    reason = "" if all(value in {host, VFIO, None} for value, host in zip(current, HOST_DRIVERS)) else tr('An unsupported driver binding exists.')
    return {"pci": pci, "audio_pci": audio.name, "graphics_driver": current[0] or "unbound",
            "audio_driver": current[1] or "unbound", "iommu_group": group.name, "reason": reason}


def check_idle(gpu, audio, group, sysfs, protect_connected, graphics_changing=True):
    from .hardware import gpu_inventory, other_internal_display_ready

    gpus = gpu_inventory(sysfs)
    selected = next((item for item in gpus if item["pci"] == gpu.name), None)
    amd = [item for item in gpus if item["vendor"] == "0x1002" and item["driver"] == "amdgpu"]
    if not selected or not other_internal_display_ready(amd, selected):
        raise RuntimeError(tr('Enable the AMD-driven internal panel first.'))
    if graphics_changing and driver(gpu) == "nvidia":
        if protect_connected and selected["external_display_state"] == "connected":
            raise RuntimeError(tr('The 4070 still has a connected monitor; keeping its driver.'))
        if (selected["external_display_state"] not in {"connected", "disconnected"}
                or any(c["enabled"] != "disabled" or c["status"] not in {"connected", "disconnected"}
                       or (c["kind"] == "internal" and c["status"] == "connected")
                       for c in selected["connectors"] if c["kind"] != "virtual")):
            raise RuntimeError(tr("Disable the 4070's display outputs and confirm connector states first."))
    if graphics_changing and any((fb / "device").resolve() == gpu.resolve() for fb in (sysfs / "class/graphics").glob("fb[0-9]*")):
        raise RuntimeError(tr('The 4070 provides a system framebuffer; its driver cannot be switched now.'))

    occupied = internal_holders(gpu, audio, group, selected["nodes"], include_graphics=graphics_changing)
    if not occupied["complete"]:
        raise RuntimeError(tr('Holder inspection is incomplete; driver switching is blocked.'))
    if occupied["processes"]:
        details = ", ".join(f'{p["name"]} (PID {p["pid"]})' for p in occupied["processes"])
        hints = list(dict.fromkeys(filter(None, (gpu_holder_hint(p) for p in occupied["processes"]))))
        raise RuntimeError(tr('Release NVIDIA, VFIO or audio device holders first: ') + details
                           + ("\n" + "\n".join(hints) if hints else ""))


def internal_holders(gpu, audio, group, drm_nodes, include_graphics=True):
    from .hardware import holders

    nodes = [f"/dev/vfio/{group.name}"]
    for device in (gpu, audio):
        nodes.extend(f"/dev/vfio/devices/{node.name}" for node in (device / "vfio-dev").glob("vfio*"))
    for card in (audio / "sound").glob("card[0-9]*"):
        number = card.name[4:]
        nodes.append("/dev/snd/controlC" + number)
        nodes.extend(str(node) for node in Path("/dev/snd").glob(f"*C{number}D*"))
    audio_nodes = set(nodes)
    if include_graphics:
        nodes.extend(drm_nodes)
    nvidia = include_graphics and driver(gpu) == "nvidia"
    if nvidia:
        info = read(Path("/proc/driver/nvidia/gpus") / gpu.name / "information", "")
        minor = re.search(r"^Device Minor:\s*(\d+)\s*$", info, re.MULTILINE)
        if minor:
            nodes.append("/dev/nvidia" + minor[1])
    occupied = holders({"nodes": nodes, "vendor": "0x10de" if nvidia else "vfio"})
    direct = set(nodes)
    for process in occupied["processes"]:
        process["direct_nodes"] = [node for node in process.get("nodes", []) if node in direct]
        process["audio_nodes"] = [node for node in process.get("nodes", []) if node in audio_nodes]
    return occupied


def inspect_internal_gpu(pci, sysfs=Path("/sys")):
    from .hardware import gpu_inventory

    bus = sysfs / "bus/pci"
    gpu, audio, group = device_pair(pci, bus)
    gpus = gpu_inventory(sysfs)
    status = internal_gpu_status(gpus, bus)
    if not status or status["pci"] != pci or status.get("reason"):
        raise RuntimeError(tr('Cannot confirm the internal 4070 driver state.'))
    selected = next(item for item in gpus if item["pci"] == pci)
    status["holders"] = internal_holders(gpu, audio, group, selected["nodes"])
    status["audio_holders"] = {"complete": status["holders"]["complete"],
                               "processes": [p for p in status["holders"]["processes"] if p["audio_nodes"]]}
    if (driver(gpu) or "unbound", driver(audio) or "unbound") != (status["graphics_driver"], status["audio_driver"]):
        raise RuntimeError(tr('Driver state changed during inspection. Try again.'))
    return status


def internal_switch_needed(status, action):
    current = status["graphics_driver"], status["audio_driver"]
    return current != (HOST_DRIVERS if action == "internal-attach" else (VFIO, VFIO))


def internal_switch_blocker(status, action):
    if not internal_switch_needed(status, action):
        return tr('Already loaded; no further load is needed.') if action == "internal-attach" else tr('Already unloaded; no further unload is needed.')
    occupied = internal_switch_holders(status, action)
    if not occupied["complete"]:
        return tr('Holder inspection is incomplete; driver switching is blocked.')
    if any(not releasable_internal_holder(p, action) for p in occupied["processes"]):
        hints = list(dict.fromkeys(filter(None, (gpu_holder_hint(p) for p in occupied["processes"]))))
        return "\n".join([tr('Release the holders listed below first.'), *hints])
    return ""


def releasable_internal_holder(process, action):
    """Preview only: actual switching still requires a fresh empty holder scan."""
    if action != "internal-detach":
        return False
    if process["name"] == "nvidia-powerd":
        return True
    nodes = process.get("nodes", [])
    return (process["name"] == "wireplumber" and bool(nodes)
            and all(re.fullmatch(r"/dev/snd/controlC\d+", node) for node in nodes))


def managed_internal_switch(pci, action, protect_connected=False):
    """Root worker: coordinate the fixed power service, then use strict guards."""
    marker = POWERD_MARKER
    gpu, audio, _ = device_pair(pci, PCI_ROOT)  # Validate before service changes.
    if action == "internal-detach" and (driver(gpu), driver(audio)) == (VFIO, VFIO):
        return
    stopped = False
    if action == "internal-detach":
        if command(["systemctl", "is-active", "--quiet", "nvidia-powerd.service"], check=False).returncode == 0:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("paused\n")
            stopped = True
            try:
                command(["systemctl", "stop", "nvidia-powerd.service"], timeout=15)
            except Exception:
                command(["systemctl", "start", "nvidia-powerd.service"], timeout=15)
                marker.unlink(missing_ok=True)
                raise
    try:
        switch_internal_gpu(pci, action, protect_connected=protect_connected)
    except Exception as error:
        if stopped:
            try:
                command(["systemctl", "start", "nvidia-powerd.service"], timeout=15)
                marker.unlink(missing_ok=True)
            except Exception as restore_error:
                raise RuntimeError(tr('{p0}; failed to restore nvidia-powerd: {p1}').format(p0=error, p1=restore_error)) from error
        raise
    if action == "internal-attach" and marker.exists():
        command(["systemctl", "start", "nvidia-powerd.service"], timeout=15)
        marker.unlink()


def internal_switch_holders(status, action):
    desired_graphics = HOST_DRIVERS[0] if action == "internal-attach" else VFIO
    if status["graphics_driver"] == desired_graphics:
        # Older services lack the scoped scan; keep their full conservative check.
        return status.get("audio_holders", status["holders"])
    return status["holders"]


def bind_driver(device, desired, bus):
    current = driver(device)
    if current == desired:
        return
    if current:
        (device / "driver" / "unbind").write_text(device.name + "\n")
    if desired:
        (bus / "drivers" / desired / "bind").write_text(device.name + "\n")
    if driver(device) != desired:
        raise RuntimeError(tr('{p0} did not switch to {p1}.').format(p0=device.name, p1=desired or 'unbound'))


def switch_internal_gpu(pci, action, sysfs=Path("/sys"), protect_connected=False):
    if action not in ACTIONS:
        raise ValueError(tr('Unsupported internal GPU action'))
    bus = sysfs / "bus/pci"
    gpu, audio, group = device_pair(pci, bus)
    devices = (gpu, audio)
    starting = tuple(driver(device) for device in devices)
    desired = HOST_DRIVERS if action == "internal-attach" else (VFIO, VFIO)
    for device, current, host in zip(devices, starting, HOST_DRIVERS):
        if current not in {host, VFIO, None}:
            raise RuntimeError(tr('{p0} is bound to an unsupported driver.').format(p0=device.name))
        if read(device / "driver_override") not in {"", "(null)"}:
            raise RuntimeError(tr('Another tool has set driver_override for {p0}.').format(p0=device.name))
    if starting == desired:
        return
    graphics_changing = starting[0] != desired[0]
    check_idle(gpu, audio, group, sysfs, protect_connected, graphics_changing=graphics_changing)
    modules = ("nvidia", "nvidia_modeset", "nvidia_drm", "nvidia_uvm", "snd_hda_intel") if action == "internal-attach" else (VFIO,)
    if action == "internal-attach" and not graphics_changing:
        modules = ("snd_hda_intel",)
    loaded_module = False
    for module in modules:
        if not (sysfs / "module" / module.replace("-", "_")).exists():
            command(["modprobe", "--ignore-install", module], timeout=10)
            loaded_module = True
    if loaded_module:
        check_idle(gpu, audio, group, sysfs, protect_connected, graphics_changing=graphics_changing)
    if (tuple(driver(device) for device in devices) != starting
            or any(read(device / "driver_override") not in {"", "(null)"} for device in devices)):
        raise RuntimeError(tr('Driver state changed. Refresh and try again.'))

    changed = [(device, previous, target) for device, previous, target in zip(devices, starting, desired)
               if previous != target]
    touched = []
    try:
        # Prepare both overrides before unbinding either function.
        for device, previous, target in changed:
            touched.append((device, previous))
            (device / "driver_override").write_text(target + "\n")
        for device, _, target in reversed(changed):
            bind_driver(device, target, bus)
        for device, _ in touched:
            (device / "driver_override").write_text("\n")
    except (OSError, RuntimeError) as error:
        rollback_errors = []
        for device, previous in reversed(touched):
            try:
                (device / "driver_override").write_text((previous or "none") + "\n")
                bind_driver(device, previous, bus)
            except (OSError, RuntimeError) as rollback_error:
                rollback_errors.append(f"{device.name}: {rollback_error}")
            finally:
                try:
                    (device / "driver_override").write_text("\n")
                except OSError as cleanup_error:
                    rollback_errors.append(f"{device.name}: {cleanup_error}")
        detail = (tr('; partial rollback failed: ') + "; ".join(rollback_errors)) if rollback_errors else tr('; the original driver bindings were restored.')
        raise RuntimeError(tr('4070 driver switch failed: {p0}{p1}').format(p0=error, p1=detail)) from error
