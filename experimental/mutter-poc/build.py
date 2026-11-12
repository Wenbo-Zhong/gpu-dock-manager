#!/usr/bin/python3
"""Build/install into a private user prefix. Never installs system packages."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from make_patch import patched_files
from source_patch import verify
from preflight import check as check_gi, use_system_companions
from headless_smoke import run_smoke

HERE = Path(__file__).resolve().parent


def run(args, env, log):
    print("Running:", args[0], args[1:3], flush=True)
    with log.open("w") as file:
        result = subprocess.run(args, env=env, stdout=file, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(str(log) + "\n" + log.read_text()[-3500:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="clean Ubuntu 50.1 source with the distribution patches applied")
    parser.add_argument("--work", type=Path, default=Path.home() / ".cache/usb4-gpu-manager/mutter-poc")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--drm-only", action="store_true", help="Mark this separate build for opt-in DRM-only CPU-copy trials")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Build as your normal user, not root.")
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    source = args.source.resolve()
    patch = HERE / "gpu-release-poc.patch"
    verify(source, patch)
    destination = work / "source"
    if source == destination:
        raise RuntimeError("Source must remain pristine; use a separate work directory.")
    if not destination.exists():
        shutil.copytree(source, destination, symlinks=True)
        subprocess.run(["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i", str(patch)],
                       cwd=destination, check=True)
    for name, expected in patched_files(source).items():
        if (destination / name).read_text() != expected:
            raise RuntimeError("Patched work tree differs from the distributed source: " + name)
    env = dict(os.environ)
    if (work / "build-env.json").exists():
        env.update(json.loads((work / "build-env.json").read_text()))
    meson, ninja = shutil.which("meson", path=env.get("PATH")), shutil.which("ninja", path=env.get("PATH"))
    if not meson or not ninja:
        raise RuntimeError("Missing meson/ninja or build dependencies. See README.md; nothing was installed system-wide.")
    build, prefix = work / "build", work / "prefix"
    flags = ["--prefix=" + str(prefix), "--libdir=lib/x86_64-linux-gnu", "-Dtests=disabled",
             "-Dintrospection=false", "-Ddevkit=disabled", "-Ddocs=false",
             "-Degl_device=true", "-Dwayland_eglstream=true"]
    command = [meson, "setup", str(build), str(destination)] + flags
    if (build / "build.ninja").exists():
        command.insert(2, "--reconfigure")
    run(command, env, work / "configure.log")
    run([ninja, "-C", str(build), "-j", str(max(1, min(args.jobs, 16)))], env, work / "compile.log")
    run([meson, "install", "-C", str(build), "--no-rebuild"], env, work / "install.log")
    companions = use_system_companions(prefix)
    libdir = prefix / "lib/x86_64-linux-gnu"
    paths = [libdir, libdir / "mutter-18"]
    sysroot = work / "sysroot/usr/lib/x86_64-linux-gnu"
    if sysroot.exists():
        paths.append(sysroot)
    runtime = dict(env, LD_LIBRARY_PATH=":".join(str(p) for p in paths))
    linked = subprocess.run(["/usr/bin/ldd", "/usr/bin/gnome-shell"], env=runtime,
                             check=True, capture_output=True, text=True).stdout
    (work / "gnome-shell-linkage.txt").write_text(linked)
    if "not found" in linked or str(libdir / "libmutter-18.so.0") not in linked:
        raise RuntimeError("Private GNOME linkage failed; see gnome-shell-linkage.txt")
    run(["/usr/bin/gnome-shell", "--version"], runtime, work / "gnome-shell-version.log")
    package = subprocess.run(["/usr/bin/dpkg-query", "-W", "-f=${Version}", "libmutter-18-0"],
                              check=True, capture_output=True, text=True).stdout
    manifest = {"schema": 2, "installed_mutter_version": package, "prefix": str(prefix),
                "library_paths": [str(p) for p in paths], "work": str(work),
                "patch_sha256": hashlib.sha256((HERE / "gpu-release-poc.patch").read_bytes()).hexdigest(),
                "library_sha256": hashlib.sha256((libdir / "libmutter-18.so.0").read_bytes()).hexdigest(),
                "system_companions": companions,
                "fd_lifecycle_trace": True,
                "drm_only_cpu_copy": args.drm_only,
                "hardware_verified": False, "system_compositor_replaced": False}
    manifest["gi_library_identity"] = check_gi(manifest)
    manifest["isolated_startup"] = run_smoke(manifest)
    (work / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Private build ready:", work / "manifest.json")
    print("The current GNOME session and system packages have not been replaced.")


if __name__ == "__main__":
    main()
