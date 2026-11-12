#!/usr/bin/python3
"""Report physical display connections by owning PCI GPU; never change devices."""
import argparse
import json
from pathlib import Path
import re
import syslog

TARGET = ("0x10de", "0x2bb3")


def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return "unknown"


def display_state(connectors):
    external = [c for c in connectors if c["kind"] == "external"]
    if any(c["status"] == "connected" for c in external):
        return "connected"
    if not external or any(c["status"] != "disconnected" for c in external):
        return "unknown"
    return "disconnected"


def scan(root=Path("/sys")):
    gpus = []
    drm = root / "class/drm"
    for card in sorted(drm.glob("card*")):
        if not re.fullmatch(r"card\d+", card.name) or not (card / "device").exists():
            continue
        device = card / "device"
        vendor, product = read(device / "vendor"), read(device / "device")
        connectors = []
        for connector in sorted(drm.glob(f"{card.name}-*")):
            if not (connector / "status").exists():
                continue
            name = connector.name[len(card.name) + 1:]
            if name.startswith(("eDP-", "LVDS-", "DSI-")):
                kind = "internal"
            elif name.startswith(("Writeback-", "Virtual-")):
                kind = "virtual"
            else:
                kind = "external"
            connectors.append({"name": name, "kind": kind,
                               "status": read(connector / "status"),
                               "enabled": read(connector / "enabled")})
        gpus.append({"card": card.name, "pci": device.resolve().name,
                     "vendor": vendor, "device": product,
                     "driver": (device / "driver").resolve().name
                     if (device / "driver").exists() else "unbound",
                     "is_rtx_pro_5000": (vendor, product) == TARGET,
                     "external_display_state": display_state(connectors),
                     "connectors": connectors})

    # A present GPU without a registered DRM card has UNKNOWN display state,
    # not "disconnected" (e.g. after module unload or before driver startup).
    seen = {g["pci"] for g in gpus}
    for device in sorted((root / "bus/pci/devices").glob("*")):
        if (read(device / "vendor"), read(device / "device")) != TARGET:
            continue
        if device.resolve().name in seen:
            continue
        gpus.append({"card": None, "pci": device.resolve().name,
                     "vendor": TARGET[0], "device": TARGET[1],
                     "driver": (device / "driver").resolve().name
                     if (device / "driver").exists() else "unbound",
                     "is_rtx_pro_5000": True,
                     "external_display_state": "unknown", "connectors": []})

    states = [g["external_display_state"] for g in gpus if g["is_rtx_pro_5000"]]
    egpu_state = ("absent" if not states else "connected" if "connected" in states
                  else "unknown" if "unknown" in states else "disconnected")
    count = sum(c["kind"] == "external" and c["status"] == "connected"
                for g in gpus for c in g["connectors"])
    return {"egpu_display_state": egpu_state, "external_display_count": count,
            "gpus": gpus}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--journal", action="store_true")
    args = parser.parse_args()
    result = scan()
    if args.journal:
        syslog.openlog("egpu-display-status")
        syslog.syslog(syslog.LOG_INFO, json.dumps(result, separators=(",", ":")))
    if args.json:
        print(json.dumps(result, indent=2))
    elif not args.journal:
        print(f'RTX PRO 5000 external displays: {result["egpu_display_state"]}')
        print(f'All GPUs: {result["external_display_count"]} external display(s) connected')
        for gpu in result["gpus"]:
            print(f'{gpu["pci"]} ({gpu["driver"]}, {gpu["card"] or "no DRM card"})')
            for c in gpu["connectors"]:
                print(f'  {c["name"]}: {c["status"]}, {c["enabled"]}, {c["kind"]}')


if __name__ == "__main__":
    main()
