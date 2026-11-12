import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from usb4_gpu_manager import controller, deployment, hardware
from usb4_gpu_manager.worker import remove_gpu


def gpu(**changes):
    value = {"pci": "0000:43:00.0", "vendor": "0x10de", "device": "0x2bb3",
             "driver": "nvidia", "boot_vga": False, "external": True,
             "external_display_state": "disconnected", "connectors": [],
             "nodes": ["/dev/dri/card7"], "name": "External GPU", "card": "card7"}
    value.update(changes)
    return value


PROFILE = {"schema": 1, "enabled": True, "target": {"vendor": "0x10de", "device": "0x2bb3"},
           "protected": [], "stable_seconds": 12, "keep_path_awake": True}


class SafetyTests(unittest.TestCase):
    def test_exiting_process_does_not_abort_gpu_holder_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            exiting = proc / "100/fd"
            live = proc / "101/fd"
            for path in (exiting, live):
                path.mkdir(parents=True)
            (live.parent / "comm").write_text("xwayland\n")
            (live / "3").symlink_to("/dev/dri/card7")
            disappearing_fd = live / "4"
            disappearing_fd.symlink_to("/dev/nvidiactl")
            iterdir, readlink = Path.iterdir, Path.readlink

            def list_fds(path):
                if path == exiting:
                    raise ProcessLookupError(3, "No such process", path)
                return iterdir(path)

            def resolve_fd(path):
                if path == disappearing_fd:
                    raise ProcessLookupError(3, "No such process", path)
                return readlink(path)

            with patch.object(Path, "iterdir", list_fds), patch.object(Path, "readlink", resolve_fd):
                result = hardware.holders(gpu(), proc)

        self.assertEqual(result, {"complete": True, "processes": [
            {"pid": 101, "name": "xwayland", "nodes": ["/dev/dri/card7"]},
        ]})

    def test_headless_but_compositor_busy_is_blocked(self):
        result = hardware.detach_check(gpu(), {"complete": True, "processes": [{"pid": 100, "name": "gnome-shell"}]})
        self.assertFalse(result["safe"])
        self.assertIn("gnome-shell", " ".join(result["blockers"]))

    def test_connected_disabled_monitor_is_protected(self):
        connectors = [{"kind": "external", "status": "connected", "enabled": "disabled"}]
        state = hardware.connection_state(connectors)
        self.assertEqual(state, "connected")
        self.assertFalse(hardware.detach_check(gpu(connectors=connectors, external_display_state=state), {"complete": True, "processes": []})["safe"])

    def test_unknown_display_or_incomplete_process_scan_is_blocked(self):
        for device, occupied in [(gpu(external_display_state="unknown"), {"complete": True, "processes": []}),
                                 (gpu(), {"complete": False, "processes": []}),
                                 (gpu(boot_vga=True), {"complete": True, "processes": []}),
                                 (gpu(external=False), {"complete": True, "processes": []})]:
            with self.subTest(device=device, occupied=occupied):
                self.assertFalse(hardware.detach_check(device, occupied)["safe"])

    def test_only_idle_removable_gpu_is_safe(self):
        self.assertTrue(hardware.detach_check(gpu(), {"complete": True, "processes": []})["safe"])

    def test_internal_panel_not_an_external_display(self):
        self.assertEqual(hardware.connection_state([{"kind": "internal", "status": "connected"}, {"kind": "external", "status": "disconnected"}]), "disconnected")

    def test_device_change_is_rejected(self):
        with self.assertRaises(RuntimeError):
            controller.Controller._expected(gpu(), {"expected_pci": "0000:44:00.0"})

    def test_non_unique_target_is_rejected(self):
        self.assertIsNone(hardware.target_gpu([gpu(), gpu(pci="0000:44:00.0")], PROFILE))

    def test_detach_only_removes_gpu_and_its_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name, cls in [("0000:43:00.0", "0x030000"), ("0000:43:00.1", "0x040300"), ("0000:43:00.2", "0x020000"), ("0000:42:00.0", "0x060400")]:
                path = base / name
                path.mkdir()
                for field, value in {"vendor": "0x10de", "device": "0x2bb3", "removable": "removable", "class": cls, "remove": "0"}.items():
                    (path / field).write_text(value)
            manager = controller.Controller()
            manager._profile_and_gpu = lambda: (PROFILE, gpu())
            real_path = Path
            def mapped(value):
                return base if str(value) == "/sys/bus/pci/devices" else real_path(value)
            def action(name, selected, **_):
                self.assertEqual(name, "remove")
                remove_gpu(selected, base)
            with patch("usb4_gpu_manager.worker.syslog.syslog"), patch("usb4_gpu_manager.worker.syslog.openlog"), patch.object(controller, "Path", side_effect=mapped), patch.object(controller, "device_action", side_effect=action), patch.object(controller, "holders", return_value={"complete": True, "processes": []}), patch.object(controller, "usb4_inventory", return_value=[]), patch.object(controller, "gpu_inventory", return_value=[]), patch.object(controller, "command", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                manager._detach(gpu())
                run.assert_called_once()
            self.assertEqual((base / "0000:43:00.0/remove").read_text(), "1\n")
            self.assertEqual((base / "0000:43:00.1/remove").read_text(), "1\n")
            self.assertEqual((base / "0000:43:00.2/remove").read_text(), "0")
            self.assertEqual((base / "0000:42:00.0/remove").read_text(), "0")

    def test_queued_detach_cancel_restores_display_rule(self):
        manager = controller.Controller()
        manager._profile_and_gpu = lambda: (PROFILE, gpu())
        def ignore(_):
            manager.ignored_gpu = {"pci": "test"}
        with patch.object(controller, "other_internal_display_ready", return_value=True), patch.object(controller, "gpu_inventory", return_value=[]), patch.object(manager, "_ignore_for_next_session", side_effect=ignore), patch.object(manager.cancel_event, "wait", return_value=True), patch.object(manager, "clear_ignore") as cleanup:
            with self.assertRaisesRegex(RuntimeError, "预约已取消"):
                manager.perform("queue-detach", {"expected_pci": gpu()["pci"]})
            cleanup.assert_called_once()

    def test_queue_rejects_when_no_other_internal_display_is_active(self):
        manager = controller.Controller()
        manager._profile_and_gpu = lambda: (PROFILE, gpu(external_display_state="connected"))
        with patch.object(controller, "other_internal_display_ready", return_value=False), patch.object(controller, "gpu_inventory", return_value=[]), patch.object(manager, "_ignore_for_next_session") as ignore:
            with self.assertRaisesRegex(RuntimeError, "内屏"):
                manager.perform("queue-detach", {"expected_pci": gpu()["pci"]})
            ignore.assert_not_called()

    def test_explicit_logout_flow_allows_known_connected_display(self):
        device = gpu(external_display_state="connected", connectors=[{"name": "DP-1", "kind": "external", "status": "connected", "enabled": "enabled"}])
        self.assertFalse(hardware.detach_check(device, {"complete": True, "processes": []})["safe"])
        self.assertTrue(hardware.detach_check(device, {"complete": True, "processes": []}, allow_connected=True)["safe"])
        self.assertFalse(hardware.detach_check(device, {"complete": True, "processes": [{"pid": 100, "name": "gnome-shell"}]}, allow_connected=True)["safe"])

    def test_explicit_logout_flow_still_rejects_unknown_connector(self):
        device = gpu(external_display_state="connected", connectors=[{"kind": "external", "status": "connected"}, {"kind": "external", "status": "unknown"}])
        self.assertFalse(hardware.detach_check(device, {"complete": True, "processes": []}, allow_connected=True)["safe"])

    def test_alternate_internal_panel_must_be_enabled_and_on_another_gpu(self):
        target = gpu()
        internal = gpu(pci="0000:66:00.0", external=False, connectors=[{"kind": "internal", "status": "connected", "enabled": "enabled"}])
        self.assertTrue(hardware.other_internal_display_ready([target, internal], target))
        internal["connectors"][0]["enabled"] = "disabled"
        self.assertFalse(hardware.other_internal_display_ready([target, internal], target))

    def test_profile_injection_and_extra_fields_rejected(self):
        for change in [{"target": {"vendor": "0x10de;reboot", "device": "0x2bb3"}},
                       {"command": "reboot"}, {"stable_seconds": True}, {"stable_seconds": 1000}]:
            profile = dict(copy.deepcopy(PROFILE), **change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                deployment.validate_profile(profile, [gpu()])

    def test_internal_gpu_cannot_be_selected_as_external(self):
        with self.assertRaises(ValueError):
            deployment.validate_profile(PROFILE, [gpu(external=False)])

    def test_target_cannot_be_isolated(self):
        profile = dict(PROFILE, protected=[PROFILE["target"]])
        with self.assertRaises(ValueError):
            deployment.validate_profile(profile, [gpu()])

    def test_active_display_gpu_cannot_be_isolated(self):
        profile = dict(PROFILE, protected=[{"vendor": "0x10de", "device": "0x2820"}])
        internal = gpu(device="0x2820", external=False, connectors=[{"status": "connected"}])
        with self.assertRaises(ValueError):
            deployment.validate_profile(profile, [gpu(), internal])


class DeploymentTests(unittest.TestCase):
    def test_backup_restores_non_utf8_bytes_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "existing.conf"
            old.write_bytes(b"# legacy comment \xff\noptions driver=1\n")
            deployment.save_backup([str(old)], root)
            old.write_text("changed")
            with patch.object(deployment.os, "chown"):
                deployment.restore_backup(root)
            self.assertEqual(old.read_bytes(), b"# legacy comment \xff\noptions driver=1\n")

    def test_renderer_preserves_outputs_and_kernel_notifier(self):
        with tempfile.TemporaryDirectory() as directory:
            files = deployment.render(PROFILE, Path(directory))
        text = files[str(deployment.MANAGED_MODPROBE)]
        self.assertIn("NVreg_UseKernelSuspendNotifiers=1", text)
        self.assertIn("options nvidia_drm modeset=1", text)
        self.assertNotIn("GPU-", text)
        self.assertNotIn("/home/", text)

    def test_backup_roundtrip_restores_old_and_removes_new(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "old", root / "new"
            old.write_text("original\n")
            old.chmod(0o640)
            deployment.save_backup([str(old), str(new)], root)
            old.write_text("changed")
            new.write_text("new")
            with patch.object(deployment.os, "chown"):
                deployment.restore_backup(root)
            self.assertEqual(old.read_text(), "original\n")
            self.assertEqual(old.stat().st_mode & 0o777, 0o640)
            self.assertFalse(new.exists())

    def test_symlink_config_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "link").symlink_to(root / "missing")
            with self.assertRaises(RuntimeError):
                deployment.save_backup([str(root / "link")], root)

    def test_failed_initramfs_restores_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, config, generated = root / "legacy", root / "profile.json", root / "generated.conf"
            legacy.write_text("working configuration")
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                if args[:2] == ["modinfo", "-p"]:
                    output = "NVreg_UseKernelSuspendNotifiers:int"
                elif args[:2] == ["modinfo", "-F"]:
                    output = "Dual MIT/GPL"
                elif args[0] == "update-initramfs" and sum(c[0] == "update-initramfs" for c in calls) == 1:
                    raise RuntimeError("simulated initramfs failure")
                else:
                    output = "inactive"
                return subprocess.CompletedProcess(args, 0, output, "")
            details = {"profile": PROFILE, "files": {str(generated): "new"}, "migrate": [str(legacy)]}
            with patch.object(deployment, "STATE", root / "state"), patch.object(deployment, "CONFIG", config), patch.object(deployment, "LEGACY", [str(legacy)]), patch.object(deployment, "plan", return_value=details), patch.object(deployment, "command", side_effect=run), patch.object(deployment.os, "chown"):
                with self.assertRaisesRegex(RuntimeError, "simulated"):
                    deployment.deploy(PROFILE, [gpu()])
            self.assertEqual(legacy.read_text(), "working configuration")
            self.assertFalse(config.exists())
            self.assertFalse(generated.exists())
            self.assertEqual(sum(c[0] == "update-initramfs" for c in calls), 2)


class LifecycleTests(unittest.TestCase):
    def test_internal_metrics_query_only_loaded_awake_gpu(self):
        card = {**gpu(external=False), "pci": "0000:65:00.0", "device": "0x2820",
                "runtime_status": "active", "power_state": "D0"}
        result = subprocess.CompletedProcess([], 0,
            "00000000:65:00.0, internal, GPU-test, 595.99.02, 512, 8192, 42, 15, 10\n", "")
        with patch.object(hardware, "command", return_value=result) as run:
            metrics = hardware.nvidia_metrics(card, include_internal=True)
            self.assertEqual(metrics[0]["memory_total_mib"], "8192")
            self.assertEqual(metrics[0]["temperature_c"], "42")
            self.assertEqual(metrics[0]["power_w"], "15")
            self.assertIn("--id=0000:65:00.0", run.call_args.args[0])
            run.reset_mock()
            for update in ({"runtime_status": "suspended", "power_state": "D3cold"},
                           {"driver": "vfio-pci"}, {"runtime_status": "resuming"},
                           {"runtime_status": ""}, {"power_state": "unknown"}):
                self.assertEqual(hardware.nvidia_metrics({**card, **update}, include_internal=True), [])
            run.assert_not_called()

    def test_status_does_not_probe_nvidia_after_removal(self):
        with patch.object(hardware, "gpu_inventory", return_value=[gpu(external=False)]), patch.object(hardware, "usb4_inventory", return_value=[]), patch.object(hardware, "module_parameters", return_value={}), patch.object(hardware, "nvidia_metrics") as metrics:
            hardware.snapshot()
            metrics.assert_not_called()

    def test_nvidia_metrics_only_queries_the_selected_external_gpu(self):
        result = subprocess.CompletedProcess([], 0,
            "00000000:43:00.0, eGPU, GPU-test, 595.99.02, 1, 2048, 32, 12, 0\n"
            "00000000:65:00.0, internal, GPU-other, 595.99.02, 2, 2048, 33, 11, 0\n", "")
        with patch.object(hardware, "command", return_value=result) as run:
            self.assertEqual(hardware.nvidia_metrics(gpu(external=False)), [])
            self.assertEqual(hardware.nvidia_metrics(None), [])
            run.assert_not_called()
            metrics = hardware.nvidia_metrics(gpu())
            self.assertEqual([item["pci"] for item in metrics], ["0000:43:00.0"])
            self.assertIn("--id=0000:43:00.0", run.call_args.args[0])

    def test_laptop_wake_without_egpu_does_not_report_pending_recovery(self):
        manager = controller.Controller()
        manager.cached = {"target": None}
        manager.prepare_for_sleep(True)
        manager.prepare_for_sleep(False)
        self.assertEqual(manager.last_resume["state"], "absent")

    def test_automatic_load_waits_for_stable_bar_and_avoids_rescan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "resource").write_text("0x00001000 0x00002000 0x00000200\n")
            device = gpu(path=str(path))
            manager = controller.Controller()
            manager.refresh = lambda: {"target": device, "profile": PROFILE,
                                       "deployment": {"restart_required": False}, "usb4": []}
            with patch.object(controller.time, "monotonic", return_value=0) as clock, patch.object(manager, "_keep_awake"), patch.object(manager, "_attach") as attach:
                manager.tick()
                clock.return_value = 8
                manager.tick()
                attach.assert_not_called()
                clock.return_value = 13
                manager.tick()
                attach.assert_called_once_with(PROFILE, rescan=False)
            self.assertFalse(manager.auto_pending)

    def test_new_deployment_does_not_rebind_before_reboot(self):
        manager = controller.Controller()
        manager.refresh = lambda: {"target": gpu(), "profile": PROFILE,
                                   "deployment": {"restart_required": True}, "usb4": []}
        with patch.object(manager, "_attach") as attach:
            manager.tick()
            attach.assert_not_called()


if __name__ == "__main__":
    unittest.main()
