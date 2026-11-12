"""Small system helpers. Commands use argument arrays and a fixed PATH."""

from .i18n import tr
import json
import os
from pathlib import Path
import subprocess
import signal
import tempfile
import threading

_stalled = {}
_stalled_lock = threading.Lock()


def gpu_holder_hint(process):
    if process.get("name", "").lower() == "ptyxis":
        return tr('Save your terminal work, then close all Ptyxis terminal windows and try again.')
    if process.get("name") == "kwin_wayland":
        return tr('KWin still holds the GPU. Disabling monitors may not release it; automatic reclamation is not integrated yet.')
    return ""


def stalled_commands():
    with _stalled_lock:
        for key, process in list(_stalled.items()):
            if process.poll() is not None:
                del _stalled[key]
        return list(_stalled)


def command(args, timeout=15, check=True, guard_key=None):
    # subprocess.run kills and then waits indefinitely on timeout. A process in
    # uninterruptible GPU-driver sleep cannot exit yet, so that wait hangs too.
    key = tuple(guard_key or args)
    with _stalled_lock:
        old = _stalled.get(key)
        if old is not None:
            if old.poll() is None:
                raise subprocess.TimeoutExpired(args, timeout, stderr=tr('The previous driver call has not exited; duplicate requests are paused.'))
            del _stalled[key]
    process = subprocess.Popen(args, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True,
                               env={**os.environ, "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                                    "LC_ALL": "C", "PYTHONNOUSERSITE": "1"})
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=0.2)
        except subprocess.TimeoutExpired:
            with _stalled_lock:
                _stalled[key] = process
            for pipe in (process.stdout, process.stderr):
                if pipe:
                    pipe.close()
        raise error
    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:1500]
                           or f"Command failed: {args[0]}")
    return result


def read(path, default="unknown"):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def atomic_write(path, content, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".gpu-manager-")
    try:
        with os.fdopen(fd, "wb" if isinstance(content, bytes) else "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default
