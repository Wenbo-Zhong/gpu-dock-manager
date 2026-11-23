import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import mutter_update_watch as watch


class MutterUpdateWatchTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("dpkg"), "Debian version comparator is unavailable")
    def test_apt_candidates_use_selected_repository_and_native_version_order(self):
        metadata = """mutter | 50.1-1 | https://dist.example stable/main amd64 Packages
mutter | 50.1-10 | https://dist.example updates/main amd64 Packages
mutter | 51-1 | https://local.example local/main amd64 Packages
mutter | 52-1 | /var/lib/dpkg/status
"""
        config = {"backend": "apt", "package": "mutter", "base": "50.1-1",
                  "repos": ["https://dist.example"]}
        command = watch.command

        def query(*args, **kwargs):
            if args[0] == "apt-cache":
                return subprocess.CompletedProcess(args, 0, metadata)
            return command(*args, **kwargs)

        with patch.object(watch, "command", side_effect=query):
            self.assertEqual(watch.latest_version(config), "50.1-10")

    def test_notification_retries_failure_then_deduplicates(self):
        config = {"backend": "apt", "package": "mutter", "base": "50.1-1", "repos": []}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with (patch.object(watch, "latest_version", return_value="50.1-2"),
                  patch.object(watch.shutil, "which", return_value="/usr/bin/notify-send"),
                  patch.object(watch, "command", side_effect=[
                      subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1),
                      subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
                      subprocess.CompletedProcess([], 0),
                  ]) as run):
                watch.check(config, path)
                self.assertFalse(path.exists())
                watch.check(config, path)
                self.assertEqual(json.loads(path.read_text())["version"], "50.1-2")
                watch.check(config, path)
                self.assertEqual(sum(call.args[0] == "/usr/bin/notify-send"
                                     for call in run.call_args_list), 2)

    @unittest.skipUnless(shutil.which("dpkg"), "Debian version comparator is unavailable")
    def test_installed_upgrade_notifies_once_without_repository_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = root / "usb4-gpu-manager/mutter-poc"
            library = build / "lib/x86_64-linux-gnu/libmutter-18.so.0"
            library.parent.mkdir(parents=True)
            library.touch()
            (build / "manifest.json").write_text(json.dumps({
                "schema": 2, "prefix": str(build), "installed_mutter_version": "50.1-2",
            }))
            installed = "50.1-2"
            notifications = []
            command = watch.command

            def run(*args, **kwargs):
                if args[0] == "dpkg-query":
                    return subprocess.CompletedProcess(args, 0, f"installed {installed}")
                if args[0] == "/usr/bin/notify-send":
                    notifications.append(args)
                    return subprocess.CompletedProcess(args, 0)
                return command(*args, **kwargs)

            with (patch.dict(watch.os.environ, XDG_CACHE_HOME=directory, XDG_STATE_HOME=directory),
                  patch.object(watch.shutil, "which", return_value="/usr/bin/notify-send"),
                  patch.object(watch, "command", side_effect=run)):
                watch.check_installed()
                self.assertEqual(notifications, [])
                installed = "50.1-10"
                watch.check_installed()
                watch.check_installed()

            self.assertEqual(len(notifications), 1)
            self.assertIn("50.1-10", notifications[0][-1])
            self.assertIn("mutter-egpu-port/SKILL.md", notifications[0][-1])
