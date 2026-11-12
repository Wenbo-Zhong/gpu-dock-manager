#!/usr/bin/python3
"""Start a private software-rendered GNOME in a device-free bubblewrap sandbox."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from preflight import LIBRARIES, SYSTEM_COMPANIONS, check as check_gi


def child():
    render_only = os.environ.get("POC_TEST_RENDER_ONLY") == "1"
    nodes = list(Path("/dev/dri").glob("*"))
    if ((nodes and not render_only) or
        (render_only and (not nodes or any(not p.name.startswith("renderD") for p in nodes))) or
        Path("/run/dbus/system_bus_socket").exists()):
        raise RuntimeError("Smoke test must not access real DRM devices or the system bus")
    # GNOME also constructs system-bus proxies during JS startup. Resolve those
    # against this empty private bus, never the host system bus.
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = os.environ["DBUS_SESSION_BUS_ADDRESS"]
    log = Path(os.environ["HOME"]) / "shell.log"
    with log.open("w") as output:
        shell = subprocess.Popen(["/usr/bin/gnome-shell", "--headless", "--wayland", "--no-x11",
                                  "--virtual-monitor=800x600", "--mode=ubuntu"],
                                 stdout=output, stderr=subprocess.STDOUT)
    try:
        started = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if shell.poll() is not None:
                raise RuntimeError("Private GNOME exited: " + str(shell.returncode))
            contents = log.read_text(errors="replace")
            if "GNOME Shell started at " in contents:
                started = started or time.monotonic()
                if time.monotonic() - started >= 3:
                    break
            time.sleep(0.1)
        if started is None or time.monotonic() - started < 3:
            raise RuntimeError("Private GNOME never reached a stable Shell startup")
        subprocess.run(["/usr/bin/gdbus", "call", "--session", "--dest", "org.gnome.Mutter.DisplayConfig",
            "--object-path", "/org/gnome/Mutter/DisplayConfig", "--method",
            "org.gnome.Mutter.DisplayConfig.GetCurrentState"], check=True, capture_output=True, timeout=3)
        # A successful JS startup must still use one copy of every library.
        maps = Path(f"/proc/{shell.pid}/maps").read_text().splitlines()
        prefix = Path(os.environ["POC_TEST_PREFIX"])
        identities = {}
        for namespace, (name, _) in LIBRARIES.items():
            paths = sorted({line.split()[-1] for line in maps if "/" + name in line})
            expected = (prefix / "lib/x86_64-linux-gnu" / name
                        if namespace == "Meta" else SYSTEM_COMPANIONS / name).resolve()
            identities[namespace] = bool(paths) and all(Path(p).resolve() == expected for p in paths)
        if not all(identities.values()):
            raise RuntimeError("GNOME runtime library identity mismatch: " + str(identities))
        target_fds = []
        if render_only:
            contents = log.read_text(errors="replace")
            if "event=drm-only-device" not in contents or "event=drm-only-cpu-copy" not in contents:
                raise RuntimeError("DRM-only branch was not exercised")
            target_node = os.environ["POC_TEST_TARGET_RENDER"]
            for fd in Path(f"/proc/{shell.pid}/fd").iterdir():
                try:
                    node = str(fd.readlink())
                    if node.startswith("/dev/nvidia") or node == target_node:
                        target_fds.append(node)
                except FileNotFoundError:
                    pass
            if any(node.startswith("/dev/nvidia") for node in target_fds):
                raise RuntimeError("DRM-only renderer opened NVIDIA userspace device nodes")
        print(json.dumps({"passed": True, "mode": "headless-render-nodes" if render_only else "headless-software", "drm_devices_visible": render_only,
                          "drm_primary_nodes_visible": False, "target_fds": target_fds,
                          "system_bus_visible": False, "separate_session_bus": True,
                          "system_bus_redirected_to_private_bus": True,
                          "shell_js_started": True, "stable_after_start_seconds": 3,
                          "display_config_responded": True, "library_identity": identities,
                          "hardware_verified": False}), flush=True)
    finally:
        if shell.poll() is None:
            shell.terminate()
            try:
                shell.wait(timeout=3)
            except subprocess.TimeoutExpired:
                shell.kill()
                shell.wait(timeout=3)


def run_smoke(manifest, render_only_pcis=None):
    check_gi(manifest)
    work = Path(manifest["work"])
    with tempfile.TemporaryDirectory(prefix="headless-smoke-", dir=work) as directory:
        home = Path(directory)
        runtime = home / "run"
        runtime.mkdir(mode=0o700)
        # A private bus without service activation. Disabling bus-level AppArmor
        # queries here avoids writes to securityfs in the read-only sandbox; no
        # host daemon, profile, or security setting is changed.
        bus_config = home / "session-bus.conf"
        bus_config.write_text('''<busconfig>
  <type>session</type>
  <listen>unix:tmpdir=/tmp</listen>
  <auth>EXTERNAL</auth>
  <apparmor mode="disabled"/>
  <policy context="default">
    <allow own="*"/>
    <allow send_destination="*"/>
    <allow receive_sender="*"/>
  </policy>
</busconfig>
''')
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8",
               "XDG_RUNTIME_DIR": str(runtime), "XDG_CONFIG_HOME": str(home / "config"),
               "XDG_CACHE_HOME": str(home / "cache"), "XDG_DATA_HOME": str(home / "data"),
               "XDG_STATE_HOME": str(home / "state"), "GSETTINGS_BACKEND": "memory",
               "XDG_DATA_DIRS": manifest["prefix"] + "/share:/usr/local/share:/usr/share",
               "LD_LIBRARY_PATH": ":".join(manifest["library_paths"]),
               "LIBGL_ALWAYS_SOFTWARE": "1", "GALLIUM_DRIVER": "llvmpipe",
               "__EGL_VENDOR_LIBRARY_FILENAMES": "/usr/share/glvnd/egl_vendor.d/50_mesa.json",
               "MUTTER_GPU_RELEASE_POC": "1", "POC_TEST_PREFIX": manifest["prefix"]}
        if manifest.get("fd_lifecycle_trace"):
            env["MUTTER_GPU_RELEASE_POC_TRACE_FDS"] = "1"
        extra_binds = []
        if render_only_pcis:
            if not manifest.get("drm_only_cpu_copy"):
                raise RuntimeError("Render-only test requires the DRM-only build")
            amd_pci, nvidia_pci = render_only_pcis
            for pci, vendor in ((amd_pci, "0x1002"), (nvidia_pci, "0x10de")):
                path = Path("/sys/bus/pci/devices") / pci
                if path.name != pci or (path / "vendor").read_text().strip() != vendor:
                    raise RuntimeError("Unexpected render-only GPU identity")
                nodes = list((path / "drm").glob("renderD[0-9]*"))
                if len(nodes) != 1:
                    raise RuntimeError("Expected one render node per GPU")
                node = "/dev/dri/" + nodes[0].name
                extra_binds += ["--dev-bind", node, node]
                if vendor == "0x10de":
                    env["POC_TEST_TARGET_RENDER"] = node
                    env["MUTTER_GPU_RELEASE_POC_TRACE_DEVICE"] = node
            for key in ("LIBGL_ALWAYS_SOFTWARE", "GALLIUM_DRIVER", "__EGL_VENDOR_LIBRARY_FILENAMES"):
                env.pop(key, None)
            env["POC_TEST_RENDER_ONLY"] = "1"
            env["MUTTER_GPU_RELEASE_POC_DRM_ONLY_PCI"] = nvidia_pci
        command = ["/usr/bin/bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                   "--unshare-all", "--die-with-parent", "--new-session", "--tmpfs", "/run",
                   "--tmpfs", "/tmp", "--bind", directory, directory, *extra_binds, "--",
                   "/usr/bin/dbus-run-session", "--config-file=" + str(bus_config), "--",
                   "/usr/bin/python3", "-I", str(HERE / "headless_smoke.py"), "--child"]
        completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=45)
        log = home / "shell.log"
        log_path = work / ("render-only-smoke.log" if render_only_pcis else "headless-smoke.log")
        log_path.write_text((log.read_text(errors="replace") if log.exists() else "")
                                                + "\n" + completed.stderr)
        if completed.returncode:
            raise RuntimeError("Isolated GNOME startup failed; see " + str(log_path))
        result = json.loads(completed.stdout)
        result["library_sha256"] = manifest["library_sha256"]
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path.home() / ".cache/usb4-gpu-manager/mutter-poc/manifest.json")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--render-only-pcis", nargs=2, metavar=("AMD_PCI", "NVIDIA_PCI"))
    args = parser.parse_args()
    if args.child:
        child()
    else:
        print(json.dumps(run_smoke(json.loads(args.manifest.read_text()), args.render_only_pcis), indent=2))


if __name__ == "__main__":
    main()
