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
