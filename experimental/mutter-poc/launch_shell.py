#!/usr/bin/python3
"""GNOME unit launcher. On a stale/missing build, fall back to system libraries."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from session import consume_trial, save_state, state_path, verify_manifest
from preflight import check as check_gi


def prepare_environment(state):
    environment = dict(os.environ)
    environment.pop("MUTTER_GPU_RELEASE_POC", None)
    environment.pop("MUTTER_GPU_RELEASE_POC_TRACE_DEVICE", None)
    environment.pop("MUTTER_GPU_RELEASE_POC_TRACE_FDS", None)
    environment.pop("MUTTER_GPU_RELEASE_POC_DRM_ONLY_PCI", None)
    if state.get("trial_consumed"):
        raise RuntimeError("单次 PoC 试用已结束，恢复系统 Mutter。")
    manifest = verify_manifest(Path(state["manifest"]))
    if bool(manifest.get("drm_only_cpu_copy")) != bool(state.get("drm_only_cpu_copy")):
        raise RuntimeError("DRM-only 构建与单次试用选择不匹配。")
    check_gi(manifest)
    paths = manifest["library_paths"]
    environment["LD_LIBRARY_PATH"] = ":".join(paths)
    environment["MUTTER_GPU_RELEASE_POC"] = "1"
    address = state["pci"]
    if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", address):
        raise RuntimeError("目标 PCI 地址无效。")
    pci = Path("/sys/bus/pci/devices") / address
    cards = [p.name for p in (pci / "drm").glob("card[0-9]*") if "-" not in p.name]
    absent = state.get("persistent") and not pci.exists()
    if not absent:
        if manifest.get("fd_lifecycle_trace"):
            environment["MUTTER_GPU_RELEASE_POC_TRACE_FDS"] = "1"
        environment["MUTTER_GPU_RELEASE_POC_TRACE_DEVICE"] = "/dev/dri/" + cards[0] if len(cards) == 1 else state["trace_device"]
    if state.get("drm_only_cpu_copy"):
        if not absent and (len(cards) != 1 or
            (pci / "vendor").read_text().strip() != "0x10de" or
            (pci / "boot_vga").read_text().strip() != "0" or
            (pci / "removable").read_text().strip() != "removable"):
            raise RuntimeError("DRM-only 目标身份或可移除属性已变化。")
        environment["MUTTER_GPU_RELEASE_POC_DRM_ONLY_PCI"] = state["pci"]
    environment["XDG_DATA_DIRS"] = manifest["prefix"] + "/share:" + environment.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    return environment


def begin_persistent_trial(state):
    if state.get("startup", {}).get("status") in {"starting", "failed"}:
        raise RuntimeError("上次实验会话未正常启动或退出，恢复系统 Mutter。")
    invocation = os.environ.get("INVOCATION_ID")
    if not invocation:
        raise RuntimeError("无法识别 systemd 登录启动实例。")
    state["startup"] = {"invocation": invocation, "status": "starting"}
    save_state(state)


def record_startup(ready):
    try:
        state = json.loads(state_path().read_text())
    except FileNotFoundError:
        return
    attempt = state.get("startup", {})
    invocation = os.environ.get("INVOCATION_ID")
    if not state.get("persistent") or not invocation or attempt.get("invocation") != invocation:
        return
    if ready and attempt["status"] == "starting":
        attempt["status"] = "ready"
    elif not ready and (attempt["status"] == "starting" or os.environ.get("SERVICE_RESULT") != "success"):
        attempt["status"] = "failed"
        attempt["service_result"] = os.environ.get("SERVICE_RESULT", "unknown")
    else:
        return
    save_state(state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True)
    parser.add_argument("--verify-only", action="store_true")
    hooks = parser.add_mutually_exclusive_group()
    hooks.add_argument("--record-ready", action="store_true")
    hooks.add_argument("--record-stop", action="store_true")
    args = parser.parse_args()
    if args.record_ready or args.record_stop:
        record_startup(args.record_ready)
        return 0
    state = {}
    try:
        state = json.loads(state_path().read_text())
        environment = prepare_environment(state)
        if not args.verify_only:
            if state.get("persistent"):
                begin_persistent_trial(state)
            else:
                consume_trial()
    except Exception as error:
        print("GPU-RELEASE-POC launcher fallback to system Mutter: " + str(error), file=sys.stderr)
        if args.verify_only:
            return 1
        if not state.get("persistent"):
            try:
                consume_trial()
            except Exception as cleanup_error:
                print("GPU-RELEASE-POC disarm: " + str(cleanup_error), file=sys.stderr)
        environment = dict(os.environ)
        environment.pop("MUTTER_GPU_RELEASE_POC", None)
        environment.pop("MUTTER_GPU_RELEASE_POC_TRACE_DEVICE", None)
        environment.pop("MUTTER_GPU_RELEASE_POC_TRACE_FDS", None)
        environment.pop("MUTTER_GPU_RELEASE_POC_DRM_ONLY_PCI", None)
    if args.verify_only:
        print("PoC 环境校验通过；未启动、重启或替换 GNOME。")
        return 0
    os.execve("/usr/bin/gnome-shell", ["/usr/bin/gnome-shell", "--mode=" + args.mode], environment)


if __name__ == "__main__":
    raise SystemExit(main())
