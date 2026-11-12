"""Exercise report logic and return-to-desktop on failure; never switch real VTs."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, mock_open, patch

spec = importlib.util.spec_from_file_location("vt_demo", Path(__file__).resolve().parents[1] / "tools/vt_gpu_release_demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def sample(processes=None, complete=True, present=True):
    return {"holders": {"processes": processes or [], "complete": complete}, "pci_present": present}


class VtProbeTests(unittest.TestCase):
    def test_spare_vt_excludes_gdm_first_vts_allocations_and_sessions(self):
        def ioctl(_fd, request, state, _mutate):
            self.assertEqual(request, 0x5603)
            state[0], state[2] = 2, (1 << 1) | (1 << 2) | (1 << 3)
        with patch("builtins.open", mock_open()), patch.object(demo.fcntl, "ioctl", side_effect=ioctl), patch.object(demo, "run", return_value='[{"tty":"tty4"}]'):
            self.assertEqual(demo.spare_vt(), 5)

    def test_stable_remaining_shell_blocks_release_conclusion(self):
        occupied = sample([{"name": "gnome-shell", "pid": 4}])
        self.assertIn("仍持有", demo.verdict([occupied, occupied]))

    def test_transient_shell_reference_can_disappear_after_pause(self):
        occupied = sample([{"name": "gnome-shell", "pid": 4}])
        result = demo.verdict([occupied, sample(), sample()])
        self.assertIn("未发现", result)
        self.assertIn("尚未验证", result)

    def test_incomplete_scan_or_missing_gpu_never_proves_release(self):
        self.assertIn("不完整", demo.verdict([sample(), sample(complete=False)]))
        self.assertIn("不完整", demo.verdict([sample()]))
        self.assertIn("设备消失", demo.verdict([sample(), sample(present=False)]))
        self.assertIn("其他进程", demo.verdict([sample(), sample([{"name": "python3"}])]))

    def test_fd_probe_reads_only_target_and_conservative_nvidia_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            process = proc / "123"
            (process / "fd").mkdir(parents=True)
            (process / "comm").write_text("gnome-shell\n")
            for fd, node in enumerate(["/dev/dri/renderD129", "/dev/nvidiactl", "/dev/dri/card1"]):
                (process / "fd" / str(fd)).symlink_to(node)
            result = demo.scan_holders({"nodes": ["/dev/dri/renderD129"]}, proc)
            self.assertTrue(result["complete"])
            self.assertEqual(result["processes"][0]["nodes"], ["/dev/dri/renderD129", "/dev/nvidiactl"])

    def test_initial_switch_failure_still_returns_to_original_vt(self):
        target = {"pci": "0000:03:00.0", "vendor": "0x10de", "device": "0x2bb3", "nodes": []}
        watchdog = Mock()
        with tempfile.TemporaryDirectory() as directory, patch.object(demo.os, "geteuid", return_value=0), patch.object(demo, "current_vt", return_value=2), patch.object(demo, "get_status", return_value={}), patch.object(demo, "preflight", return_value=(target, "2")), patch.object(demo, "sample", return_value=sample()), patch.object(demo, "spare_vt", return_value=3), patch.object(demo.tempfile, "mkdtemp", return_value=directory), patch.object(demo.subprocess, "Popen", return_value=watchdog), patch.object(demo.signal, "signal"), patch.object(demo, "switch", side_effect=[RuntimeError("switch failed"), None]) as switch, patch.object(demo.sys, "argv", ["demo"]), patch("builtins.print"):
            self.assertEqual(demo.main(), 1)
            self.assertEqual([c.args[0] for c in switch.call_args_list], [3, 2])
            report = json.loads((Path(directory) / "report.json").read_text())
            self.assertTrue(report["restored"])
            self.assertIn("switch failed", report["error"])
            watchdog.terminate.assert_called_once()

    def test_return_failure_keeps_independent_watchdog_alive(self):
        target = {"pci": "0000:03:00.0", "vendor": "0x10de", "device": "0x2bb3", "nodes": []}
        watchdog = Mock()
        with tempfile.TemporaryDirectory() as directory, patch.object(demo.os, "geteuid", return_value=0), patch.object(demo, "current_vt", return_value=2), patch.object(demo, "get_status", return_value={}), patch.object(demo, "preflight", return_value=(target, "2")), patch.object(demo, "sample", return_value=sample()), patch.object(demo, "spare_vt", return_value=3), patch.object(demo.tempfile, "mkdtemp", return_value=directory), patch.object(demo.subprocess, "Popen", return_value=watchdog), patch.object(demo.signal, "signal"), patch.object(demo, "switch", side_effect=RuntimeError("failed")), patch.object(demo.sys, "argv", ["demo"]), patch("builtins.print"):
            self.assertEqual(demo.main(), 1)
            watchdog.terminate.assert_not_called()
