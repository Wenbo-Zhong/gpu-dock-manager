"""Run hardware/driver sysfs access in bounded, separately killable processes."""

from .i18n import tr
import json
from pathlib import Path
import sys
import subprocess

from .system import command

WORKER = str(Path(__file__).with_name("worker.py"))


def bounded_snapshot(profile=None, metrics=False):
    try:
        result = command([sys.executable, "-I", WORKER, "snapshot",
                          json.dumps({"profile": profile or {}, "metrics": metrics})],
                         timeout=5, guard_key=("hardware-snapshot",))
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(tr('Driver status query timed out and was isolated; connection status still updates independently.')) from error
    return json.loads(result.stdout)


def bounded_inventory():
    return bounded_snapshot()["gpus"]


def bounded_internal_status(pci):
    result = command([sys.executable, "-I", WORKER, "internal-status", json.dumps({"pci": pci})],
                     timeout=5, guard_key=("internal-status",))
    return json.loads(result.stdout)


def device_action(action, gpu, timeout=25):
    payload = {k: gpu[k] for k in ("pci", "vendor", "device")}
    return command([sys.executable, "-I", WORKER, action, json.dumps(payload)],
                   timeout=timeout, guard_key=("gpu-device-action",))


def internal_device_action(action, pci, protect_connected=False):
    payload = {"pci": pci, "protect_connected": protect_connected}
    return command(["systemd-inhibit", "--what=sleep:shutdown", "--mode=block",
                    "--who=USB4 GPU Manager", "--why=Switching internal GPU drivers",
                    sys.executable, "-I", WORKER, action, json.dumps(payload)],
                   timeout=75, guard_key=("gpu-device-action",))
