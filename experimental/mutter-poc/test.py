#!/usr/bin/python3
"""Run the actual PoC C interface on a private D-Bus with inert GPU stubs."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent


def main():
    flags = shlex.split(subprocess.check_output(["/usr/bin/pkg-config", "--cflags", "--libs", "gio-2.0"], text=True))
    with tempfile.TemporaryDirectory(prefix="mutter-poc-test-") as directory:
        binary = str(Path(directory) / "harness")
        subprocess.run(["/usr/bin/gcc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
            "-Wno-unused-parameter", "-g", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
            str(HERE / "tests/poc-harness.c"), "-I" + str(HERE / "overlay"), *flags, "-o", binary], check=True)
        env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:abort_on_error=1", UBSAN_OPTIONS="halt_on_error=1")
        subprocess.run(["/usr/bin/dbus-run-session", "--", binary], env=env, check=True)


if __name__ == "__main__":
    main()
