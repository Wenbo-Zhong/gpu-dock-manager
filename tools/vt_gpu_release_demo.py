#!/usr/bin/python3
"""Diagnostic only: preserve the Wayland session and measure GPU FDs on a spare VT.

No PCI remove/unbind, module unload, process termination or GDM restart.
Run with the system Python in isolated mode; --check-only never switches VT.
"""
import argparse
import array
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=5).stdout.strip()


def current_vt():
    value = Path("/sys/class/tty/tty0/active").read_text().strip()
    if not re.fullmatch(r"tty[1-9][0-9]*", value):
        raise RuntimeError("无法识别当前虚拟终端。")
    return int(value[3:])


def get_status():
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    reply = bus.call_sync("io.github.usb4gpumanager.Manager", "/io/github/usb4gpumanager/Manager",
                          "io.github.usb4gpumanager.Manager1", "GetStatus", None,
                          GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 5000, None)
    return json.loads(reply.unpack()[0])


def preflight(status, vt):
    target = status.get("target")
    if not target or not target.get("external") or target.get("vendor") != "0x10de" or target.get("boot_vga"):
        raise RuntimeError("没有唯一、可移除的外接 NVIDIA 显卡。")
    if not status.get("can_mutate_devices") or time.time() - status.get("timestamp", 0) > 10:
        raise RuntimeError("后台状态不可用或已过期，取消实验。")
    if status.get("operation", {}).get("state") in {"running", "waiting", "removing"}:
        raise RuntimeError("后台正在操作显卡，请先完成或取消。")
    if not status.get("other_internal_display_ready"):
        raise RuntimeError("请先打开并唤醒另一张 GPU 驱动的笔记本内屏。")
    if target.get("external_display_state") != "disconnected":
        raise RuntimeError("本 demo 先限定无 eGPU 外屏的情况，请先拔掉显卡上的显示器线。")
    sessions = json.loads(run(["/usr/bin/loginctl", "list-sessions", "--json=short"]))
    for session in sessions:
        details = run(["/usr/bin/loginctl", "show-session", str(session["session"]),
                       "-p", "Active", "-p", "Type", "-p", "VTNr", "-p", "User", "-p", "Seat"])
        props = dict(line.split("=", 1) for line in details.splitlines() if "=" in line)
        if props.get("Active") == "yes" and props.get("Type") == "wayland" and props.get("VTNr") == str(vt) and props.get("Seat") == "seat0":
            if os.environ.get("SUDO_UID") and props.get("User") != os.environ["SUDO_UID"]:
                raise RuntimeError("当前桌面不属于 sudo 发起者，拒绝切换。")
            return target, str(session["session"])
    raise RuntimeError("请从当前活跃 Wayland 桌面的终端运行本 demo。")


def scan_holders(target, proc=Path("/proc")):
    nodes = set(target["nodes"]) | {"/dev/nvidiactl", "/dev/nvidia-modeset", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools"}
    found, incomplete = [], []
    for process in proc.glob("[0-9]*"):
        try:
            opened = set()
            for descriptor in (process / "fd").iterdir():
                try:
                    name = str(descriptor.readlink()).removesuffix(" (deleted)")
                except FileNotFoundError:
                    continue
                if name in nodes or re.fullmatch(r"/dev/nvidia\d+", name):
                    opened.add(name)
            if opened:
                found.append({"pid": int(process.name), "name": (process / "comm").read_text().strip(),
                              "nodes": sorted(opened)})
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            incomplete.append(int(process.name))
    return {"processes": found, "complete": not incomplete, "unreadable_pids": incomplete}


def sample(target):
    return {"elapsed_clock": time.monotonic(), "active_vt": current_vt(),
            "pci_present": (Path("/sys/bus/pci/devices") / target["pci"]).exists(),
            "holders": scan_holders(target)}


def spare_vt():
    # VT_OPENQRY may return tty1 even though GDM will start a greeter there
    # when activated. Exclude tty1/2, logind sessions and allocated kernel VTs.
    state = array.array("H", [0, 0, 0])
    with open("/dev/tty0", "rb", buffering=0) as terminal:
        fcntl.ioctl(terminal.fileno(), 0x5603, state, True)  # VT_GETSTATE: active, signal, allocation mask.
    sessions = json.loads(run(["/usr/bin/loginctl", "list-sessions", "--json=short"]))
    occupied = {s.get("tty") for s in sessions}
    for number in range(3, 16):
        if number != state[0] and not state[2] & (1 << number) and f"tty{number}" not in occupied:
            return number
    raise RuntimeError("tty3–tty15 没有空闲虚拟终端，取消实验。")


def switch(vt):
    run(["/usr/bin/chvt", str(vt)])
    if current_vt() != vt:
        raise RuntimeError("虚拟终端切换未完成。")


def verdict(samples):
    if len(samples) < 2 or any(not s["holders"]["complete"] for s in samples):
        return "占用采集不完整，无法下结论。"
    if any(not s["pci_present"] for s in samples):
        return "实验期间 PCI 设备消失，无法将句柄消失认定为 GNOME 主动释放。"
    shells = [p for s in samples[-2:] for p in s["holders"]["processes"] if p["name"] == "gnome-shell"]
    if shells:
        return "切换 TTY 后 GNOME 仍持有 GPU 句柄；此方法不能直接实现安全卸载。"
    if any(s["holders"]["processes"] for s in samples[-2:]):
        return "GNOME 未见占用，但仍有其他进程持有 GPU；不能卸载。"
    return "后两次采样未发现 GPU 设备句柄。只证明此时未见占用，尚未验证卸载和返回桌面是否安全。"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true", help="只读检查，不切换 TTY")
    parser.add_argument("--seconds", type=int, default=10, help="停留时间，5–30 秒；默认 10 秒")
    parser.add_argument("--watchdog", nargs=2, type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.watchdog:
        vt, delay = args.watchdog
        if os.geteuid() != 0 or not 1 <= vt <= 63 or not 10 <= delay <= 60:
            parser.error("invalid watchdog")
        time.sleep(delay)
        switch(vt)
        return 0
    if not 5 <= args.seconds <= 30:
        parser.error("--seconds must be 5–30")
    if not args.check_only and os.geteuid() != 0:
        parser.error("切换实验请使用 sudo /usr/bin/python3 -I tools/vt_gpu_release_demo.py")
    original = current_vt()
    target, session = preflight(get_status(), original)
    before = sample(target)
    if args.check_only:
        before["holders"]["unreadable_process_count"] = len(before["holders"].pop("unreadable_pids"))
        print(json.dumps({"session": session, "original_vt": original, "target": target["pci"], "before": before}, ensure_ascii=False, indent=2))
        print("只读检查完成，没有切换 TTY。非 root 运行时，进程占用采集可能不完整。")
        return 0
    destination = spare_vt()
    if destination == original:
        raise RuntimeError("未找到不同于桌面的空闲 TTY。")
    directory = Path(tempfile.mkdtemp(prefix="egpu-vt-demo-", dir="/var/tmp"))
    report_path = directory / "report.json"
    report = {"target": {k: target[k] for k in ("pci", "vendor", "device", "nodes")},
              "session": session, "original_vt": original, "test_vt": destination,
              "before": before, "during": [], "restored": False}
    print(f"桌面 tty{original} → 空闲 tty{destination}，约 {args.seconds} 秒后自动返回。", flush=True)
    print(f"不会卸载显卡或结束桌面。报告：{report_path}", flush=True)
    print(f"需要手动返回时：sudo chvt {original}", flush=True)
    # Independent process survives a parent crash/SIGKILL; no shell interpolation.
    watchdog = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()),
                                 "--watchdog", str(original), str(args.seconds + 15)],
                                start_new_session=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    def interrupted(signum, _frame):
        raise InterruptedError(f"实验收到信号 {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        switch(destination)
        with open(f"/dev/tty{destination}", "w") as terminal:
            terminal.write("\nGPU release probe: desktop session preserved; automatic return shortly.\n")
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            time.sleep(min(2, max(0, deadline - time.monotonic())))
            if current_vt() != destination:
                raise RuntimeError("前台 TTY 发生变化（可能由用户或系统触发），提前结束实验。")
            report["during"].append(sample(target))
    except Exception as error:
        report["error"] = str(error)
    finally:
        try:
            switch(original)
            report["restored"] = True
            watchdog.terminate()
            watchdog.wait(timeout=3)
            report["after"] = sample(target)
        except Exception as error:
            report["restore_error"] = str(error)
        report["conclusion"] = verdict(report["during"])
        if report.get("error"):
            report["conclusion"] = "实验提前结束；以下仅描述已取得的采样：" + report["conclusion"]
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        report_path.chmod(0o644)
        directory.chmod(0o755)
    print(report["conclusion"])
    print("自动返回桌面：" + ("成功" if report["restored"] else f"未确认；独立回切进程仍保留，请尝试 sudo chvt {original}"))
    print(f"完整报告：{report_path}")
    return 1 if report.get("error") or not report["restored"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("实验未完成：" + str(error), file=sys.stderr)
        raise SystemExit(1)
