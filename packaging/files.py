"""源码安装与 DEB/RPM 共用文件清单；只复制文件，不管理服务或设备策略。"""

import argparse
import os
from pathlib import Path
import shutil

APP = Path("usr/lib/usb4-gpu-manager")
SHARE = Path("usr/share/usb4-gpu-manager")


def installation_files(source, live=False):
    """保留 Python 包相对路径，自动包含 ui/core/desktop 及以后新增子包。"""
    source = Path(source)
    for path in sorted((source / "usb4_gpu_manager").rglob("*")):
        relative = path.relative_to(source)
        if path.is_file() and (path.suffix == ".py" or "locales" in relative.parts):
            yield relative, APP / relative, 0o644
    for name in ("gui_entry.py", "cli_entry.py", "daemon_entry.py"):
        yield Path("data") / name, APP / name, 0o644
    # 源码安装保留管理员配置的 /etc 位置，原生包使用发行版的 /usr 安装位置。
    fixed = [
        ("data/usb4-gpu-manager.service",
         "etc/systemd/system/usb4-gpu-manager.service" if live else "usr/lib/systemd/system/usb4-gpu-manager.service"),
        ("data/io.github.usb4gpumanager.Manager.conf",
         "etc/dbus-1/system.d/io.github.usb4gpumanager.Manager.conf" if live else "usr/share/dbus-1/system.d/io.github.usb4gpumanager.Manager.conf"),
        ("data/io.github.usb4gpumanager.Manager.service", "usr/share/dbus-1/system-services/io.github.usb4gpumanager.Manager.service"),
        ("data/io.github.usb4gpumanager.policy", "usr/share/polkit-1/actions/io.github.usb4gpumanager.policy"),
        ("data/io.github.usb4gpumanager.App.desktop", "usr/share/applications/io.github.usb4gpumanager.App.desktop"),
        ("skills/mutter-egpu-port/SKILL.md", str(SHARE / "skills/mutter-egpu-port/SKILL.md")),
        ("docs/mutter-porting.md", str(SHARE / "mutter-porting.md")),
        ("NOTICE.md", "usr/share/doc/usb4-gpu-manager/NOTICE.md"),
    ]
    for origin, destination in fixed:
        yield Path(origin), Path(destination), 0o644
    yield Path("tools/mutter_update_watch.py"), Path("usr/bin/usb4-gpu-mutter-watch"), 0o755
    for path in sorted((source / "data").glob("usb4-gpu-mutter-watch.*")):
        yield path.relative_to(source), Path("usr/lib/systemd/user") / path.name, 0o644
    experiment = source / "experimental/mutter-poc"
    for path in sorted(experiment.iterdir()):
        if path.is_file() and (path.suffix in {".py", ".json", ".patch"} or path.name in {"README.md", "COPYING.GPL-2"}):
            yield path.relative_to(source), SHARE / "mutter-poc" / path.name, 0o644
    for directory in ("overlay", "tests"):
        for path in sorted((experiment / directory).iterdir()):
            if path.is_file() and (directory == "overlay" or path.suffix in {".c", ".h"}):
                yield path.relative_to(source), SHARE / "mutter-poc" / directory / path.name, 0o644


def write_file(destination, content, mode):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    destination.write_text(content)
    destination.chmod(mode)
    if os.geteuid() == 0:
        os.chown(destination, 0, 0)


def copy_files(source, root, live=False):
    source, root = Path(source), Path(root)
    if not root.is_absolute():
        raise ValueError("Use an absolute staging root")
    root = root.resolve()
    # 先规范化路径再拒绝 live 根目录，避免 /../ 或符号链接绕过 staging 边界。
    if (root == Path("/") and not live) or (live and root != Path("/")):
        raise ValueError("Use an absolute staging root; --live-install is reserved for /")
    if live and os.geteuid() != 0:
        raise PermissionError("Live installation requires root")
    for origin, relative, mode in installation_files(source, live):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        shutil.copyfile(source / origin, destination)
        # 权限由清单指定，不继承开发机文件模式；root 安装时同时修正文件属主。
        destination.chmod(mode)
        if os.geteuid() == 0:
            os.chown(destination, 0, 0)
    for name, entry in (("usb4-gpu-manager", "gui_entry"), ("usb4-gpu-managerctl", "cli_entry")):
        # -I 隔离用户 Python 环境；入口脚本负责加载固定安装目录中的应用包。
        write_file(root / "usr/bin" / name,
                   f'#!/bin/sh\nexec /usr/bin/python3 -I /usr/lib/usb4-gpu-manager/{entry}.py "$@"\n', 0o755)
    copyright_text = (source / "LICENSE").read_text() + (
        "\nThe Mutter patch and overlays are GPL-2.0-or-later. See\n"
        "/usr/share/usb4-gpu-manager/mutter-poc/COPYING.GPL-2 for the license.\n")
    write_file(root / "usr/share/doc/usb4-gpu-manager/copyright", copyright_text, 0o644)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--live-install", action="store_true")
    args = parser.parse_args()
    copy_files(Path(__file__).resolve().parents[1], args.root, args.live_install)


if __name__ == "__main__":
    main()
