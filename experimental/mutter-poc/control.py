#!/usr/bin/python3
"""Observe or request the opt-in Mutter PoC; never detach hardware."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

BUS = "org.gnome.Mutter.GpuReleasePoC"
OBJECT = "/org/gnome/Mutter/GpuReleasePoC"
INTERFACE = "org.gnome.Mutter.GpuReleasePoC1"


def relevant_node(name, nodes):
    return name in nodes or name.startswith("/dev/nvidia")


def process_snapshot(pid, nodes, proc=Path("/proc")):
    directory = proc / str(pid)
    start = (directory / "stat").read_text().rsplit(")", 1)[1].split()[19]
    opened, complete = set(), True
    for fd in (directory / "fd").iterdir():
        try:
            name = str(fd.readlink()).removesuffix(" (deleted)")
            if relevant_node(name, nodes):
                opened.add(name)
        except FileNotFoundError:
            continue
        except OSError:
            complete = False
    return {"pid": pid, "start_time": start, "nodes": sorted(opened), "complete": complete}


def validate_target(data, now=None, allow_connected=False):
    now = time.time() if now is None else now
    if not data.get("service_connected") or not data.get("can_mutate_devices") or now - data.get("timestamp", 0) > 10:
        raise RuntimeError("管理服务的设备状态不可用或已过期。")
    if data.get("operation", {}).get("state") in {"running", "waiting", "removing"}:
        raise RuntimeError("已有显卡管理操作，请先完成或取消。")
    gpu = data.get("target")
    if not gpu or gpu.get("vendor") != "0x10de" or not gpu.get("external") or gpu.get("boot_vga"):
        raise RuntimeError("没有唯一可移除 NVIDIA 目标。")
    allowed = {"disconnected", "connected"} if allow_connected else {"disconnected"}
    if gpu.get("driver") != "nvidia":
        raise RuntimeError("目标显卡尚未绑定 NVIDIA 驱动。")
    display_state = gpu.get("external_display_state")
    if display_state == "connected" and not allow_connected:
        raise RuntimeError("目标显卡仍检测到外屏。请从显卡端拔掉外屏视频线，保持 USB4 线连接，再重试。")
    if display_state not in allowed:
        raise RuntimeError("无法确定目标显卡的外屏连接状态，请等待管理器刷新后重试。")
    if not data.get("other_internal_display_ready"):
        raise RuntimeError("请打开并唤醒 AMD 内屏。")
    if not re.fullmatch(r"card\d+", gpu.get("card") or ""):
        raise RuntimeError("目标卡没有可识别 DRM 主节点。")
    return gpu


def release_verdict(before, samples, result):
    if result.get("outcome") != "cache-reclaimed-not-a-gpu-eject":
        return "未完成新的缓存回收；检查 outcome / reason，不能认定本 PoC 触发了释放。"
    if any(s["process"]["start_time"] != before["start_time"] for s in samples):
        return "GNOME 进程实例变化，实验不能证明原桌面会话被保留。"
    if len(samples) < 2 or any(not s["process"]["complete"] for s in samples):
        return "进程句柄扫描不完整，无法判定。"
    if any(s["process"]["nodes"] for s in samples[-2:]):
        return "回收后 GNOME 仍持有目标/共享 NVIDIA 句柄；需要结合对象销毁日志继续定位。"
    if any(s["inspect"].get("cache_present") for s in samples[-2:]):
        return "目标渲染缓存再次出现，尚未证明持续释放。"
    if not samples or any(s["inspect"].get("primary_driver") != "amdgpu" or
                          s["inspect"].get("view_count", 0) < 1 for s in samples):
        return "未确认 AMD 渲染视图持续存在，不能认定保留桌面成功。"
    if before.get("primary") and any(s["inspect"].get("primary") != before["primary"] for s in samples):
        return "主渲染设备发生变化，不能认定原 AMD 桌面保持不变。"
    if not before.get("nodes"):
        return "回收前已未见相关句柄，本次只验证了缓存清理，没有证明解决原有 GPU 占用。"
    return "已观测到同一 GNOME 进程的目标/共享 NVIDIA 句柄消失，AMD 视图持续存在；请人工确认原窗口仍正常绘制。这不是安全拔卡确认。"


class Client:
    def __init__(self):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.owner = self.dbus("GetNameOwner", "(s)", (BUS,))[0]
        self.pid = self.dbus("GetConnectionUnixProcessID", "(s)", (self.owner,))[0]
        owner_uid = self.dbus("GetConnectionUnixUser", "(s)", (self.owner,))[0]
        if owner_uid != os.getuid():
            raise RuntimeError("PoC 服务不属于当前用户。")

    def dbus(self, method, signature, values):
        return self.session.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", method,
            self.GLib.Variant(signature, values), None, self.Gio.DBusCallFlags.NO_AUTO_START, 3000, None).unpack()

    def status(self):
        reply = self.system.call_sync("io.github.usb4gpumanager.Manager", "/io/github/usb4gpumanager/Manager",
            "io.github.usb4gpumanager.Manager1", "GetStatus", None, self.GLib.VariantType.new("(s)"),
            self.Gio.DBusCallFlags.NO_AUTO_START, 5000, None)
        return json.loads(reply.unpack()[0])

    def call(self, method, device):
        # Pin the unique owner; a replacement compositor cannot inherit this request.
        return self.session.call_sync(self.owner, OBJECT, INTERFACE, method,
            self.GLib.Variant("(s)", (device,)), self.GLib.VariantType.new("(a{sv})"),
            self.Gio.DBusCallFlags.NO_AUTO_START, 8000, None).unpack()[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "reclaim"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("请在已加载 PoC 的 GNOME 会话中以普通用户运行，不使用 sudo。")
    report = {"schema": 1, "action": args.action, "time": datetime.now().isoformat(), "samples": []}
    output = args.output
    if output is None:
        state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        output = state / "usb4-gpu-manager/poc-reports" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the report before requesting a mutation, refusing existing files.
    with output.open("x") as file:
        os.chmod(output, 0o600)
        client = None
        try:
            client = Client()
            gpu = validate_target(client.status(), allow_connected=args.action == "inspect")
            device = "/dev/dri/" + gpu["card"]
            if (Path("/sys/class/drm") / gpu["card"] / "device").resolve().name != gpu["pci"]:
                raise RuntimeError("PCI 与 DRM 对应关系已变化，请刷新后重试。")
            report.update(pid=client.pid, target={k: gpu[k] for k in ("pci", "card", "vendor", "device")})
            initial = client.call("Inspect", device)
            report["initial"] = initial
            before = process_snapshot(client.pid, gpu["nodes"])
            before["primary"] = initial.get("primary")
            report["before"] = before
            if args.action == "inspect":
                report["conclusion"] = "只读检查完成，没有触发回收。"
            else:
                shell_pid = client.dbus("GetConnectionUnixProcessID", "(s)", ("org.gnome.Shell",))[0]
                if shell_pid != client.pid:
                    raise RuntimeError("PoC 不属于当前 GNOME Shell，拒绝用独立 compositor 代替实机验证。")
                if not before["complete"] or not initial.get("eligible"):
                    raise RuntimeError(initial.get("reason") or "进程扫描不完整，取消。")
                if not initial.get("cache_present"):
                    raise RuntimeError("目标缓存已经不存在；应先分析用户态驱动引用，重复回收没有意义。")
                # Recheck kernel-facing policy immediately before the request.
                current = validate_target(client.status())
                if (current["pci"], current["card"]) != (gpu["pci"], gpu["card"]):
                    raise RuntimeError("设备实例已变化，取消。")
                started = time.time()
                report["result"] = client.call("Reclaim", device)
                for delay in (0, 1, 2):
                    if delay:
                        time.sleep(delay)
                    report["samples"].append({"process": process_snapshot(client.pid, gpu["nodes"]),
                                               "inspect": client.call("Inspect", device)})
                report["conclusion"] = release_verdict(before, report["samples"], report["result"])
            # Inspect also needs startup events: an already-empty cache cannot
            # explain retained FDs without the earlier initialization history.
            try:
                logs = subprocess.run(["/usr/bin/journalctl", "--user", "-b", "_PID=" + str(client.pid),
                    "-n", "4000", "--no-pager", "-o", "json"], capture_output=True, text=True, timeout=5)
                lines = logs.stdout.splitlines()
                report["events"] = [json.loads(line).get("MESSAGE", "") for line in lines
                    if "GPU-RELEASE-POC" in line]
                report["event_capture"] = {"exit_code": logs.returncode, "line_limit": 4000,
                    "possibly_truncated": len(lines) >= 4000}
            except (OSError, subprocess.TimeoutExpired, ValueError) as error:
                report["event_capture"] = {"error": str(error), "complete": False}
        except Exception as error:
            report["error"] = str(error)
            report.setdefault("conclusion", "实验未完成，不可据此拔线或卸载。")
        json.dump(report, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print(report["conclusion"])
    if report.get("error"):
        print(report["error"])
    print("报告：" + str(output.resolve()))
    return 1 if report.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
