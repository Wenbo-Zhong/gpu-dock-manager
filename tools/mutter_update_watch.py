#!/usr/bin/python3
"""Notify about repository versions newer than the official Mutter build base."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1])
                if Path(__file__).resolve().parent.name == 'tools' else '/usr/lib/usb4-gpu-manager')
from usb4_gpu_manager.i18n import tr


def command(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True,
                          timeout=120, env={**os.environ, "LC_ALL": "C"})


def user_path(variable, fallback, name):
    return Path(os.environ.get(variable) or Path.home() / fallback) / "usb4-gpu-manager" / name


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(value, file, indent=2)
            file.write("\n")
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def candidates(config):
    package, repositories = config["package"], config["repos"]
    if config["backend"] == "apt":
        output = command("apt-cache", "madison", package).stdout
        for line in output.splitlines():
            fields = [part.strip() for part in line.split("|", 2)]
            if len(fields) == 3 and fields[0] == package and fields[2].endswith(" Packages"):
                if not repositories or any(fields[2].startswith(repo + " ") for repo in repositories):
                    yield fields[1]
        return

    # DNF's native architecture plus noarch avoids foreign multilib builds.
    arch = command("rpm", "--eval", "%{_arch}").stdout.strip()
    dnf = shutil.which("dnf5") or "dnf"
    options = [f"--repo={repo}" for repo in repositories]
    query_format = "%{name} %{arch} %{epoch}:%{version}-%{release}"
    if Path(dnf).name == "dnf5":
        query_format += r"\n"
    output = command(dnf, "-q", "--refresh", *options, "repoquery", "--available",
                     "--queryformat", query_format, package).stdout
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == package and parts[1] in {arch, "noarch"}:
            yield parts[2].replace("(none):", "0:")


def rpm_label(value):
    epoch, colon, remainder = value.partition(":")
    version, release = (remainder if colon else epoch).rsplit("-", 1)
    return (epoch if colon else "0", version, release)


def newer(backend, left, right):
    if backend == "apt":
        result = command("dpkg", "--compare-versions", left, "gt", right, check=False)
        if result.returncode not in (0, 1):
            raise ValueError(result.stderr.strip() or "Invalid Debian version")
        return result.returncode == 0
    import rpm
    return rpm.labelCompare(rpm_label(left), rpm_label(right)) > 0


def latest_version(config):
    latest = None
    for version in candidates(config):
        if latest is None or newer(config["backend"], version, latest):
            latest = version
    if latest is None:
        raise RuntimeError("No version found in the configured repositories. Check package name and repository metadata.")
    return latest


def check(config, state_path):
    latest = latest_version(config)
    if not newer(config["backend"], latest, config["base"]):
        print(f"{config['package']}: no newer repository version ({latest}).")
        return
    print(f"{config['package']}: repository {latest}, patch base {config['base']}.")
    state = {"config": config, "version": latest}
    try:
        if json.loads(state_path.read_text()) == state:
            return
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    notify = shutil.which("notify-send")
    if notify and command(notify, "--app-name=USB4 GPU Manager", tr('A new Mutter version is available'),
                          tr('{package} {version} is available. Use the mutter-egpu-port skill to port, build and validate the patch.').format(package=config['package'], version=latest),
                          check=False).returncode == 0:
        save(state_path, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    setup = actions.add_parser("configure")
    setup.add_argument("backend", choices=("apt", "dnf"))
    setup.add_argument("package", help="Distribution binary package to monitor, normally mutter")
    setup.add_argument("base", help="Official repository version used as patch base")
    setup.add_argument("--repo", action="append", default=[],
                       help="APT repository URI or DNF repository ID; repeat to restrict the check")
    actions.add_parser("check")
    args = parser.parse_args()
    config_path = user_path("XDG_CONFIG_HOME", ".config", "mutter-watch.json")
    state_path = user_path("XDG_STATE_HOME", ".local/state", "mutter-watch-notified.json")
    try:
        if args.action == "configure":
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+.-]*", args.package):
                parser.error("Invalid package name")
            config = {"backend": args.backend, "package": args.package, "base": args.base, "repos": args.repo}
            newer(args.backend, args.base, args.base)
            save(config_path, config)
            print(f"Saved {config_path}. Enable usb4-gpu-mutter-watch.timer in your user session.")
        else:
            check(json.loads(config_path.read_text()), state_path)
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
