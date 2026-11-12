#!/usr/bin/python3
"""Ubuntu 26.04 amd64: stage source/build dependencies without root or apt install."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import urllib.request

HERE = Path(__file__).resolve().parent
ARCHIVES = {
    "mutter_50.1.orig.tar.xz": "9344502ce473f788795f26b45f8b8ff53c0a36b867470d705a11a3ee0911021e",
    "mutter_50.1-0ubuntu2.5.debian.tar.xz": "50d2d9f011972576ddc4467528c49601918e5a477aa6b3e8268b9d2bf86e7d06",
}


def dependencies(text):
    value = re.search(r"Build-Depends:(.*?)(?=\n\S)", text, re.S).group(1).replace("\n", " ")
    value = re.sub(r"[^,]*<!nocheck>[^,]*(?:,|$)", "", value)
    return re.sub(r"\[[^]]*\]", "", value)


def run(args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, default=Path.home() / ".cache/usb4-gpu-manager/mutter-poc")
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    if os.geteuid() == 0 or platform.machine() != "x86_64":
        parser.error("Run as a normal user on Ubuntu 26.04 amd64, not root.")
    work = args.work.resolve()
    archive_dir = work / "downloads"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for name, expected in ARCHIVES.items():
        path = archive_dir / name
        if not path.exists():
            print("Downloading", name, flush=True)
            with urllib.request.urlopen("https://archive.ubuntu.com/ubuntu/pool/main/m/mutter/" + name, timeout=30) as source:
                with path.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Archive hash mismatch: " + str(path))
    pristine = work / "base-source"
    source = pristine / "mutter-50.1"
    if not (pristine / "ready.json").exists():
        if source.exists():
            raise RuntimeError("Incomplete base source exists; inspect/remove it before retrying: " + str(pristine))
        pristine.mkdir(exist_ok=True)
        with tarfile.open(archive_dir / "mutter_50.1.orig.tar.xz") as archive:
            archive.extractall(pristine, filter="data")
        with tarfile.open(archive_dir / "mutter_50.1-0ubuntu2.5.debian.tar.xz") as archive:
            archive.extractall(source, filter="data")
        patches = source / "debian/patches"
        for line in (patches / "series").read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                run(["/usr/bin/patch", "-p1", "--batch", "-i", str(patches / line.split()[0])], cwd=source)
        (pristine / "ready.json").write_text(json.dumps(ARCHIVES, indent=2))
    debs = work / "debs"
    (debs / "partial").mkdir(parents=True, exist_ok=True)
    build_deps = dependencies((source / "debian/control").read_text())
    print("Downloading build dependencies only; system packages remain unchanged.", flush=True)
    run(["/usr/bin/apt-get", "--download-only", "--no-install-recommends", "-y",
         "-o", "Debug::NoLocking=true", "-o", "Dir::Cache::archives=" + str(debs),
         "-o", "Dir::State::extended_states=" + str(work / "extended_states"), "satisfy", build_deps])
    root = work / "sysroot"
    # Do not extract packages over a sysroot containing host-file symlinks.
    if not root.exists():
        root.mkdir()
        for archive in sorted(debs.glob("*.deb")):
            run(["/usr/bin/dpkg-deb", "-x", str(archive), str(root)])
    elif not (root / "usr/bin/meson").exists():
        raise RuntimeError("Incomplete sysroot; use a fresh --work directory.")
    for relative in ("usr/include", "usr/bin", "usr/lib/x86_64-linux-gnu/pkgconfig",
                     "usr/lib/x86_64-linux-gnu/glib-2.0", "usr/share/pkgconfig", "usr/share/dbus-1",
                     "usr/share/glib-2.0", "usr/share/wayland-protocols", "usr/share/gsettings-desktop-schemas",
                     "usr/share/gnome-settings-daemon", "usr/share/gnome-shell"):
        host = Path("/") / relative
        target = root / relative
        if host.exists():
            if target.is_symlink():
                if target.resolve() == host.resolve():
                    continue
                raise RuntimeError("Unexpected directory symlink: " + str(target))
            target.mkdir(parents=True, exist_ok=True)
            run(["/usr/bin/cp", "-as", "--update=none", str(host) + "/.", str(target) + "/"])
    libdir = root / "usr/lib/x86_64-linux-gnu"
    for host in Path("/usr/lib/x86_64-linux-gnu").glob("*.so*"):
        target = libdir / host.name
        if not target.exists() and not target.is_symlink():
            target.symlink_to(host)
    pc = root / "usr/share/pkgconfig/wayland-protocols.pc"
    text = pc.read_text().replace("${pc_sysrootdir}", "")
    if pc.is_symlink():
        pc.unlink()
    pc.write_text(text)
    env = {
        "PATH": str(root / "usr/bin") + ":/usr/bin:/bin",
        "PYTHONPATH": str(root / "usr/lib/python3/dist-packages"),
        "PKG_CONFIG_SYSROOT_DIR": str(root),
        "PKG_CONFIG_LIBDIR": ":".join(str(root / p) for p in (
            "usr/lib/x86_64-linux-gnu/pkgconfig", "usr/lib/pkgconfig", "usr/share/pkgconfig")),
        "LD_LIBRARY_PATH": str(libdir), "CFLAGS": "-O1 -g -isystem " + str(root / "usr/include"),
        "LDFLAGS": "-L" + str(libdir) + " -Wl,-rpath," + str(libdir),
    }
    (work / "build-env.json").write_text(json.dumps(env, indent=2) + "\n")
    if args.build:
        run(["/usr/bin/python3", str(HERE / "build.py"), "--source", str(source), "--work", str(work)])
    else:
        print("Ready:", source, "Run build.py --source with this path.")


if __name__ == "__main__":
    main()
