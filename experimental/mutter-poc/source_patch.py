#!/usr/bin/python3
"""Verify the exact Mutter base and prepare a distro source build."""
import argparse
from datetime import datetime
from email.utils import format_datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

PATCH = Path(__file__).resolve().with_name("gpu-release-poc.patch")


def verify(source, patch):
    fingerprints = json.loads(patch.with_suffix(".sources.json").read_text())
    for name, digest in fingerprints.items():
        path = source / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if actual != digest or (digest is None and (path.exists() or path.is_symlink())):
            raise RuntimeError(f"Source differs from the audited patch base: {name}")
    subprocess.run(["patch", "--dry-run", "--batch", "--forward", "--fuzz=0",
                    "-p1", "-i", str(patch)], cwd=source, check=True)


def prepare_deb(source, patch, revision):
    debian = source / "debian"
    if (debian / "source/format").read_text().strip() != "3.0 (quilt)":
        raise RuntimeError("Use a fresh dpkg-source -x tree in 3.0 (quilt) format.")
    changelog = debian / "changelog"
    first_line = changelog.read_text().splitlines()[0]
    match = re.fullmatch(r"mutter \(([^)]+)\) ([^;]+); urgency=\S+", first_line)
    if not match:
        raise RuntimeError("Could not identify the distribution's Mutter source version.")
    base, distribution = match.groups()
    if "+usb4gpu" in base:
        raise RuntimeError("The source is already prepared for a local build.")
    version = f"{base}+usb4gpu{revision}"
    name = "usb4-gpu-manager-egpu.patch"
    target = debian / "patches" / name
    series = debian / "patches/series"
    original = series.read_text() if series.exists() else ""
    if target.exists() or any(line.split()[:1] == [name] for line in original.splitlines()):
        raise RuntimeError("The patch is already listed. Use a fresh extracted source tree.")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(patch, target)
    series.write_text(original.rstrip("\n") + ("\n" if original else "") + name + "\n")
    entry = (
        f"mutter ({version}) {distribution}; urgency=medium\n\n"
        "  * Add the USB4 GPU Manager eGPU release patch.\n\n"
        f" -- Local USB4 GPU Manager builder <local@localhost>  {format_datetime(datetime.now().astimezone())}\n\n"
    )
    changelog.write_text(entry + changelog.read_text())
    print(f"Official base: {base}\nLocal version: {version}\nRun dpkg-buildpackage -b -us -uc in {source}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "prepare-deb"))
    parser.add_argument("source", type=Path)
    parser.add_argument("--patch", type=Path, default=PATCH)
    parser.add_argument("--revision", type=int, default=1)
    args = parser.parse_args()
    if args.revision < 1:
        parser.error("Revision must be positive.")
    source, patch = args.source.resolve(), args.patch.resolve()
    try:
        verify(source, patch)
        if args.action == "prepare-deb":
            prepare_deb(source, patch, args.revision)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
