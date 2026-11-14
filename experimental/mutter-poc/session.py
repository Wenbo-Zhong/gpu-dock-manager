#!/usr/bin/python3
"""Configure the experimental Mutter for GNOME logins without restarting the session."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
MARKER = "# USB4 GPU Manager opt-in Mutter PoC v1\n"
NAME = "95-usb4-gpu-manager-poc.conf"


def state_path():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "usb4-gpu-manager/mutter-poc-session.json"


def config_root():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd/user"


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(text)
            output.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def save_state(data):
    atomic_write(state_path(), json.dumps(data, indent=2) + "\n")


def verify_manifest(path):
    manifest = json.loads(path.read_text())
    if manifest.get("schema") != 2 or not manifest.get("gi_library_identity", {}).get("passed"):
        raise RuntimeError("旧 PoC 构建未通过 GI 库混载检查，可能导致图形登录失败；请先重新构建。")
    startup = manifest.get("isolated_startup", {})
    if not startup.get("passed") or startup.get("library_sha256") != manifest.get("library_sha256"):
        raise RuntimeError("PoC 尚未通过对应构建的隔离 GNOME 启动验证；保持实验禁用。")
    installed = subprocess.run(["/usr/bin/dpkg-query", "-W", "-f=${Version}", "libmutter-18-0"],
                                check=True, capture_output=True, text=True).stdout
    if installed != manifest["installed_mutter_version"]:
        raise RuntimeError("系统 Mutter 版本已变化，需要重新核对并构建 PoC。")
    library = Path(manifest["prefix"]) / "lib/x86_64-linux-gnu/libmutter-18.so.0"
    if hashlib.sha256(library.read_bytes()).hexdigest() != manifest["library_sha256"]:
        raise RuntimeError("PoC 库与构建清单不一致。")
    if hashlib.sha256((HERE / "gpu-release-poc.patch").read_bytes()).hexdigest() != manifest["patch_sha256"]:
        raise RuntimeError("源码补丁已变化，请先重新构建。")
    return manifest


def consume_trial():
    """Disarm the next-login override before trying the experimental compositor."""
    state = state_path()
    data = json.loads(state.read_text())
    unit = data["unit"]
    if not re.fullmatch(r"org\.gnome\.Shell@[A-Za-z0-9_-]+\.service", unit):
        raise RuntimeError("Invalid PoC trial unit")
    path = config_root() / (unit + ".d") / NAME
    if path.exists():
        if not path.read_text().startswith(MARKER):
            raise RuntimeError("拒绝修改其他程序的启动配置：" + str(path))
        path.unlink()
    data["trial_consumed"] = True
    save_state(data)
    subprocess.run(["/usr/bin/systemctl", "--user", "daemon-reload"],
                   check=True, capture_output=True, text=True, timeout=10)


def quote(value):
    if any(c in value for c in "\n\r\x00$"):
        raise ValueError("unsupported systemd path characters")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("enable", "disable", "status"))
    parser.add_argument("--manifest", type=Path, default=Path.home() / ".cache/usb4-gpu-manager/mutter-poc/manifest.json")
    parser.add_argument("--drm-only", action="store_true", help="Explicitly select the DRM-only CPU-copy experiment")
    parser.add_argument("--persistent", action="store_true", help="Try the verified build each login unless a previous startup failed")
    parser.add_argument("--internal-4070", action="store_true", help="Explicitly enroll the supported internal RTX 4070")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("请以普通用户运行，不使用 sudo。")
    state = state_path()
    if args.action == "disable":
        removed = []
        for path in config_root().glob("org.gnome.Shell@*.service.d/" + NAME):
            if not path.read_text().startswith(MARKER):
                raise RuntimeError("拒绝删除其他程序创建的文件：" + str(path))
            path.unlink()
            removed.append(str(path))
        state.unlink(missing_ok=True)
        result = subprocess.run(["/usr/bin/systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
        print("已删除 PoC 登录配置；当前会话不被终止，下次登录恢复系统 Mutter。")
        if result.returncode:
            print("用户管理器暂不可访问；文件已删除，重新登录或重启后生效。")
        print("删除文件：", removed)
        return
    if args.action == "status":
        print(state.read_text() if state.exists() else "未配置 PoC 登录覆盖。")
        return
    manifest = verify_manifest(args.manifest.resolve())
    if bool(manifest.get("drm_only_cpu_copy")) != args.drm_only:
        raise RuntimeError("DRM-only 构建必须显式添加 --drm-only；普通构建不能使用此开关。")
    if args.internal_4070 and (not args.drm_only or not manifest.get('internal_4070_drm_only')):
        raise RuntimeError('This build does not support the internal RTX 4070 experiment')
    from preflight import check as check_gi
    check_gi(manifest)
    # Resolve the target from the read-only system service. Do not import the app.
    sys.path.insert(0, str(HERE))
    from control import validate_target
    # Obtain the normal service directly; the PoC is not running yet.
    from gi.repository import Gio, GLib
    system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    reply = system.call_sync("io.github.usb4gpumanager.Manager", "/io/github/usb4gpumanager/Manager",
        "io.github.usb4gpumanager.Manager1", "GetStatus", None, GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 5000, None)
    status = json.loads(reply.unpack()[0])
    previous = json.loads(state.read_text()) if args.persistent and state.exists() else {}
    enrolled = (args.persistent and args.drm_only and previous.get("drm_only_cpu_copy")
                and previous.get("manifest") == str(args.manifest.resolve())
                and re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", previous.get("pci", "")))
    try:
        if args.internal_4070:
            from device_policy import validate_internal
            info = status.get('internal_gpu') or {}
            if (not status.get('service_connected') or not status.get('can_mutate_devices')
                    or info.get('reason') or status.get('operation', {}).get('state') in {'running', 'waiting', 'removing'}):
                raise RuntimeError('Current internal GPU status is not eligible')
            pci = info.get('pci')
            device = validate_internal(pci)
            if not any(g.get('vendor') == '0x1002' and g.get('driver') == 'amdgpu'
                       and any(c.get('kind') == 'internal' and c.get('status') == 'connected' and c.get('enabled') == 'enabled'
                               for c in g.get('connectors', [])) for g in status.get('gpus', [])):
                raise RuntimeError('Enable the AMD internal panel before enrolling')
            cards = [p.name for p in (device / 'drm').glob('card[0-9]*') if '-' not in p.name]
            trace_device = '/dev/dri/' + cards[0] if len(cards) == 1 else '/dev/dri/card999999'
        elif (enrolled and status.get("service_connected") and not status.get("status_stale")
                and status.get("transport", {}).get("pci_visible") is False
                and not (Path("/sys/bus/pci/devices") / previous["pci"]).exists()
                and any(gpu.get("vendor") == "0x1002" and gpu.get("driver") == "amdgpu"
                        and any(c.get("kind") == "internal" and c.get("status") == "connected"
                                and c.get("enabled") == "enabled" for c in gpu.get("connectors", []))
                        for gpu in status.get("gpus", []))):
            pci, trace_device = previous["pci"], previous["trace_device"]
        else:
            gpu = validate_target(status)
            pci, trace_device = gpu["pci"], "/dev/dri/" + gpu["card"]
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    units = subprocess.run(["/usr/bin/systemctl", "--user", "list-units", "--type=service", "--state=running",
                             "--output=json", "org.gnome.Shell@*.service"], check=True, capture_output=True, text=True)
    units = json.loads(units.stdout)
    if len(units) != 1 or not re.fullmatch(r"org\.gnome\.Shell@[A-Za-z0-9_-]+\.service", units[0]["unit"]):
        raise RuntimeError("无法唯一识别当前 GNOME Shell 用户服务，请从正在运行的 GNOME 桌面启用。")
    unit = units[0]["unit"]
    if args.persistent:
        unit_type = subprocess.run(["/usr/bin/systemctl", "--user", "show", "-p", "Type", "--value", unit],
                                   check=True, capture_output=True, text=True).stdout.strip()
        if unit_type != "notify":
            raise RuntimeError("持续试用需要使用 Type=notify 的 GNOME Shell 用户服务。")
    path = config_root() / (unit + ".d") / NAME
    launcher = HERE / "launch_shell.py"
    command = ["/usr/bin/python3", "-I", str(launcher), "--mode=%i"]
    body = MARKER + "[Service]\nExecStart=\nExecStart=" + " ".join(quote(p) for p in command[:-1]) + ' "--mode=%i"\n'
    hook = " ".join(quote(p) for p in command[:-1]) + ' "--mode=%i"'
    persistent_body = body + f"ExecStartPost=-{hook} --record-ready\nExecStopPost=-{hook} --record-stop\n"
    if path.exists() and path.read_text() not in {body, persistent_body}:
        raise RuntimeError("目标覆盖文件已存在且内容不同，请先检查：" + str(path))
    save_state({"manifest": str(args.manifest.resolve()), "unit": unit,
                "trace_device": trace_device, "pci": pci, "dropin": str(path),
                "drm_only_cpu_copy": args.drm_only, "persistent": args.persistent,
                "internal_4070": args.internal_4070,
                "external_pci": next((g['pci'] for g in status.get('gpus', [])
                                      if g.get('external') and g.get('vendor') == '0x10de'), None)})
    atomic_write(path, persistent_body if args.persistent else body)
    subprocess.run(["/usr/bin/systemctl", "--user", "daemon-reload"], check=True)
    if args.persistent:
        print("已配置每次登录尝试使用 PoC；当前 GNOME 未重启。")
        print("若实验版启动失败，后续登录将使用系统 Mutter，直到重新启用。")
    else:
        print("已准备下次登录单次试用 PoC；当前 GNOME 未重启。")
        print("试用启动前自动撤销覆盖配置；若失败，再次登录将恢复系统 Mutter。")
    print("请保存工作后正常注销、重新登录一次。之后仅运行 control.py inspect，先不要回收或拔卡。")
    print("恢复命令：/usr/bin/python3 -I " + str(HERE / "session.py") + " disable")


if __name__ == "__main__":
    main()
