"""Fast transport observation, independent of DRM/NVML and device-operation locks."""

from .i18n import tr
import copy
from pathlib import Path
import time

from .system import read


def live_routers(root=Path("/sys")):
    result = []
    for router in (root / "bus/thunderbolt/devices").glob("*"):
        if not (router / "unique_id").exists():
            continue
        result.append({"id": router.name, "uuid": read(router / "unique_id"),
                       "is_host": router.name.endswith("-0")})
    return result


def remember_transport(previous, data):
    """Keep router correlation when sysfs still lists a vanished PCI device."""
    result = dict(data)
    old_gpu = previous.get("target") or previous.get("last_known_target")
    gpu = result.get("target")
    old_ids = previous.get("target_router_ids", [])
    current_ids = [r["uuid"] for r in result.get("usb4", []) if not r["is_host"]]
    same = bool(gpu and old_gpu and all(gpu.get(k) == old_gpu.get(k) for k in ("pci", "vendor", "device")))
    if "target_router_ids" not in result:
        preserve = same and (previous.get("target") is not None or previous.get("transport", {}).get("driver_cleanup_pending"))
        result["target_router_ids"] = old_ids if old_ids and (preserve or gpu is None) else current_ids if gpu else []
    if gpu is None and old_gpu:
        result["last_known_target"] = old_gpu
    return result


def observe(data, root=Path("/sys"), now=None):
    result = copy.deepcopy(data)
    now = time.time() if now is None else now
    available = bool(result.get("timestamp"))
    age = max(0, now - result["timestamp"]) if available else 0
    result.update(status_age_seconds=round(age, 1), status_stale=not available or age > 10,
                  sample_available=available,
                  live_timestamp=now)
    gpu = result.get("target") or result.get("last_known_target")
    routers = live_routers(root)
    live_ids = {r["uuid"] for r in routers if not r["is_host"]}
    known_ids = result.get("target_router_ids")
    if known_ids is None:
        known_ids = [r["uuid"] for r in result.get("usb4", []) if not r["is_host"]] if gpu else []
    result["target_router_ids"] = known_ids
    pci_visible = bool(gpu and (root / "bus/pci/devices" / gpu["pci"]).exists())
    router_gone = bool(gpu and known_ids and not set(known_ids).intersection(live_ids))
    device_gone = bool(gpu and (router_gone or not pci_visible))
    # Match only observed router UUIDs, never present a cached 40 Gb/s link
    # after its USB4 router has disappeared.
    result["usb4"] = [r for r in result.get("usb4", []) if r["uuid"] in {v["uuid"] for v in routers}]
    state = "disconnected" if router_gone else "detached" if gpu and not pci_visible else "present" if gpu else "absent"
    result["transport"] = {"state": state,
                           "pci_visible": pci_visible,
                           "driver_cleanup_pending": bool(router_gone and pci_visible)}
    operation = result.get("operation", {})
    if pci_visible and operation.get("state") == "done" and operation.get("action") in {"detach", "queue-detach"}:
        result["operation"] = {"state": "idle", "action": "", "message": ""}
    if not available or age > 10:
        result["metrics"] = []
        for router in result["usb4"]:
            router["rx_speed"] = router["tx_speed"] = "unknown"
        result["detach"] = {"safe": False, "blockers": [tr('Detailed device status is stale; device operations are paused.')]}
    if device_gone:
        result["last_known_target"] = gpu
        result["target"] = None
        result["gpus"] = [g for g in result.get("gpus", []) if g["pci"] != gpu["pci"]]
        result["metrics"] = []
        result["detach"] = {"safe": False, "blockers": [tr('The target device disconnected or was removed from PCI.')]}
        if router_gone and pci_visible:
            result["last_error"] = tr('USB4 is disconnected, but the kernel still retains the PCI device. Driver cleanup may be incomplete.')
        operation = result.get("operation", {})
        if (operation.get("state") in {"running", "waiting", "removing"}
                and operation.get("action") in {"attach", "detach", "queue-detach"}):
            result["operation"] = dict(operation, backend_state=operation["state"],
                state="error", message=tr('The device disconnected or disappeared from PCI. Safe removal is not confirmed; do not act on cached status.'))
    result["can_mutate_devices"] = not result["status_stale"] and not result["transport"]["driver_cleanup_pending"] and not result.get("blocked_io", False)
    return result
