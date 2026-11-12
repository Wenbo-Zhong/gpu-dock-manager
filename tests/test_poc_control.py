import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

path = Path(__file__).resolve().parents[1] / "experimental/mutter-poc/control.py"
spec = importlib.util.spec_from_file_location("poc_control", path)
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
session_spec = importlib.util.spec_from_file_location("poc_session", path.with_name("session.py"))
session = importlib.util.module_from_spec(session_spec)
session_spec.loader.exec_module(session)
launcher_spec = importlib.util.spec_from_file_location("poc_launcher", path.with_name("launch_shell.py"))
launcher = importlib.util.module_from_spec(launcher_spec)
launcher_spec.loader.exec_module(launcher)


class PocControlTests(unittest.TestCase):
    def test_target_unknown_stale_and_busy_rejected(self):
        gpu = {"vendor": "0x10de", "external": True, "boot_vga": False, "driver": "nvidia",
               "external_display_state": "disconnected", "card": "card0"}
        baseline = {"service_connected": True, "can_mutate_devices": True, "timestamp": 100,
                    "target": gpu, "other_internal_display_ready": True}
        self.assertEqual(control.validate_target(baseline, now=101), gpu)
        for change in ({"timestamp": 0}, {"can_mutate_devices": False}, {"target": None},
                       {"operation": {"state": "waiting"}}, {"other_internal_display_ready": False},
                       {"target": dict(gpu, external_display_state="unknown")}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                control.validate_target(dict(baseline, **change), now=101)

    def test_fd_scan_and_process_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / "42"
            (process / "fd").mkdir(parents=True)
            fields = ["S"] + ["0"] * 18 + ["START-ID"]
            (process / "stat").write_text("42 (gnome-shell with spaces) " + " ".join(fields))
            (process / "fd/3").symlink_to("/dev/dri/renderD129")
            (process / "fd/4").symlink_to("/dev/nvidiactl")
            (process / "fd/5").symlink_to("/dev/dri/card1")
            result = control.process_snapshot(42, ["/dev/dri/renderD129"], root)
            self.assertEqual(result["start_time"], "START-ID")
            self.assertEqual(result["nodes"], ["/dev/dri/renderD129", "/dev/nvidiactl"])
            self.assertTrue(result["complete"])

    def test_connected_screen_is_inspectable_but_never_reclaimable(self):
        gpu = {"vendor": "0x10de", "external": True, "boot_vga": False,
               "driver": "nvidia", "external_display_state": "connected", "card": "card0"}
        state = {"service_connected": True, "can_mutate_devices": True, "timestamp": 100,
                 "target": gpu, "other_internal_display_ready": True}
        self.assertEqual(control.validate_target(state, now=101, allow_connected=True), gpu)
        with self.assertRaisesRegex(RuntimeError, "拔掉外屏视频线"):
            control.validate_target(state, now=101)
        state["target"] = dict(gpu, external_display_state="unknown")
        with self.assertRaises(RuntimeError):
            control.validate_target(state, now=101, allow_connected=True)

    def test_release_is_not_claimed_for_cache_absent_or_remaining_fds(self):
        before = {"start_time": "1", "nodes": ["/dev/nvidia0"]}
        sample = {"process": {"start_time": "1", "complete": True, "nodes": []},
                  "inspect": {"primary_driver": "amdgpu", "cache_present": False, "view_count": 1}}
        result = {"outcome": "cache-reclaimed-not-a-gpu-eject"}
        self.assertIn("已观测", control.release_verdict(before, [sample, sample], result))
        self.assertIn("未完成", control.release_verdict(before, [sample, sample], {"outcome": "cache-already-absent-no-mutation"}))
        occupied = dict(sample, process=dict(sample["process"], nodes=["/dev/nvidia0"]))
        self.assertIn("仍持有", control.release_verdict(before, [occupied, occupied], result))
        restarted = dict(sample, process=dict(sample["process"], start_time="2"))
        self.assertIn("实例变化", control.release_verdict(before, [sample, restarted], result))

    def test_empty_or_unobserved_baseline_does_not_claim_release(self):
        result = {"outcome": "cache-reclaimed-not-a-gpu-eject"}
        s = {"process": {"start_time": "1", "complete": True, "nodes": []},
             "inspect": {"primary_driver": "amdgpu", "cache_present": False, "view_count": 1}}
        self.assertIn("不完整", control.release_verdict({"start_time": "1"}, [], result))
        self.assertIn("没有证明", control.release_verdict({"start_time": "1"}, [s, s], result))

    def test_systemd_paths_do_not_allow_expansion(self):
        self.assertEqual(session.quote("/tmp/a b%test"), '"/tmp/a b%%test"')
        for value in ("/tmp/$HOME", "x\ny", "x\x00y"):
            with self.assertRaises(ValueError):
                session.quote(value)

    def test_disable_only_removes_owned_dropin_and_never_stops_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dropin = root / "org.gnome.Shell@ubuntu.service.d" / session.NAME
            dropin.parent.mkdir()
            dropin.write_text(session.MARKER + "[Service]\n")
            unrelated = dropin.parent / "other.conf"
            unrelated.write_text("retain")
            state = root / "state.json"
            state.write_text("{}")
            with patch.object(session.os, "geteuid", return_value=1000), patch.object(session, "config_root", return_value=root), patch.object(session, "state_path", return_value=state), patch.object(session.sys, "argv", ["session.py", "disable"]), patch.object(session.subprocess, "run", return_value=Mock(returncode=0)) as run, patch("builtins.print"):
                session.main()
                self.assertEqual(run.call_args.args[0], ["/usr/bin/systemctl", "--user", "daemon-reload"])
            self.assertFalse(dropin.exists())
            self.assertFalse(state.exists())
            self.assertEqual(unrelated.read_text(), "retain")

    def test_disable_preserves_foreign_dropin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "org.gnome.Shell@ubuntu.service.d" / session.NAME
            path.parent.mkdir()
            path.write_text("not owned by PoC")
            with patch.object(session.os, "geteuid", return_value=1000), patch.object(session, "config_root", return_value=root), patch.object(session.sys, "argv", ["session.py", "disable"]), self.assertRaises(RuntimeError):
                session.main()
            self.assertTrue(path.exists())

    def test_old_build_or_missing_startup_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            for value in ({"schema": 1}, {"schema": 2, "gi_library_identity": {"passed": True}},
                          {"schema": 2, "gi_library_identity": {"passed": True}, "library_sha256": "new",
                           "isolated_startup": {"passed": True, "library_sha256": "old"}}):
                manifest.write_text(json.dumps(value))
                with self.subTest(value=value), patch.object(session.subprocess, "run") as run, self.assertRaises(RuntimeError):
                    session.verify_manifest(manifest)
                run.assert_not_called()

    def test_drm_only_requires_matching_explicit_selection(self):
        for flag, manifest in ((True, {}), (False, {"drm_only_cpu_copy": True})):
            args = ["session.py", "enable"] + (["--drm-only"] if flag else [])
            with self.subTest(flag=flag), patch.object(session.os, "geteuid", return_value=1000), patch.object(session.sys, "argv", args), patch.object(session, "verify_manifest", return_value=manifest), patch.object(session.subprocess, "run") as run, self.assertRaises(RuntimeError):
                session.main()
            run.assert_not_called()

    def test_trial_disarms_override_before_gnome_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dropin = root / "org.gnome.Shell@ubuntu.service.d" / session.NAME
            dropin.parent.mkdir()
            dropin.write_text(session.MARKER + "[Service]\n")
            state = root / "state.json"
            state.write_text(json.dumps({"unit": "org.gnome.Shell@ubuntu.service"}))
            with patch.object(session, "config_root", return_value=root), patch.object(session, "state_path", return_value=state), patch.object(session.subprocess, "run") as run:
                session.consume_trial()
            self.assertFalse(dropin.exists())
            self.assertTrue(json.loads(state.read_text())["trial_consumed"])
            self.assertEqual(run.call_args.args[0], ["/usr/bin/systemctl", "--user", "daemon-reload"])

    def test_trial_disarm_preserves_foreign_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dropin = root / "org.gnome.Shell@ubuntu.service.d" / session.NAME
            dropin.parent.mkdir()
            dropin.write_text("foreign")
            state = root / "state.json"
            state.write_text(json.dumps({"unit": "org.gnome.Shell@ubuntu.service"}))
            with patch.object(session, "config_root", return_value=root), patch.object(session, "state_path", return_value=state), patch.object(session.subprocess, "run") as run, self.assertRaises(RuntimeError):
                session.consume_trial()
            self.assertEqual(dropin.read_text(), "foreign")
            self.assertNotIn("trial_consumed", json.loads(state.read_text()))
            run.assert_not_called()

    def test_launcher_disarm_failure_uses_system_environment(self):
        with patch.object(launcher, "state_path", return_value=Mock(read_text=Mock(return_value="{}"))), patch.object(launcher.sys, "argv", ["launch_shell.py", "--mode=ubuntu"]), patch.object(launcher, "prepare_environment", return_value={"LD_LIBRARY_PATH": "/private/poc"}), patch.object(launcher, "consume_trial", side_effect=RuntimeError("reload failed")), patch.object(launcher.os, "environ", {"PATH": "/usr/bin", "MUTTER_GPU_RELEASE_POC": "1"}), patch.object(launcher.os, "execve") as execute, patch("builtins.print"):
            launcher.main()
        self.assertEqual(execute.call_args.args[2], {"PATH": "/usr/bin"})
        self.assertEqual(execute.call_args.args[1], ["/usr/bin/gnome-shell", "--mode=ubuntu"])

    def test_verify_only_does_not_consume_trial_or_start_gnome(self):
        with patch.object(launcher, "state_path", return_value=Mock(read_text=Mock(return_value="{}"))), patch.object(launcher.sys, "argv", ["launch_shell.py", "--mode=ubuntu", "--verify-only"]), patch.object(launcher, "prepare_environment", return_value={}), patch.object(launcher, "consume_trial") as consume, patch.object(launcher.os, "execve") as execute, patch("builtins.print"):
            self.assertEqual(launcher.main(), 0)
        consume.assert_not_called()
        execute.assert_not_called()

    def test_persistent_startup_records_only_matching_invocation_and_retries_after_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = {"persistent": True}
            with (patch.object(launcher, "state_path", return_value=path),
                  patch.object(launcher, "save_state", side_effect=lambda data: session.atomic_write(path, json.dumps(data)))):
                with patch.dict(launcher.os.environ, {"INVOCATION_ID": "first", "SERVICE_RESULT": "success"}):
                    launcher.begin_persistent_trial(state)
                    launcher.record_startup(ready=True)
                    launcher.record_startup(ready=False)
                state = json.loads(path.read_text())
                self.assertEqual(state["startup"]["status"], "ready")
                with patch.dict(launcher.os.environ, {"INVOCATION_ID": "second", "SERVICE_RESULT": "timeout"}):
                    launcher.begin_persistent_trial(state)
                    launcher.record_startup(ready=False)
                with patch.dict(launcher.os.environ, {"INVOCATION_ID": "first"}):
                    launcher.record_startup(ready=True)
                failed = json.loads(path.read_text())
                self.assertEqual(failed["startup"]["status"], "failed")
                with self.assertRaisesRegex(RuntimeError, "恢复系统 Mutter"):
                    launcher.begin_persistent_trial(failed)

    def test_interrupted_persistent_startup_runs_system_without_consuming_override(self):
        state = {"persistent": True, "startup": {"invocation": "old", "status": "starting"}}
        with (patch.object(launcher, "state_path", return_value=Mock(read_text=Mock(return_value=json.dumps(state)))),
              patch.object(launcher.sys, "argv", ["launch_shell.py", "--mode=ubuntu"]),
              patch.object(launcher, "prepare_environment", return_value={"LD_LIBRARY_PATH": "/private/poc"}),
              patch.object(launcher, "consume_trial") as consume,
              patch.object(launcher.os, "execve") as execute, patch("builtins.print")):
            launcher.main()
        consume.assert_not_called()
        self.assertNotEqual(execute.call_args.args[2].get("LD_LIBRARY_PATH"), "/private/poc")
