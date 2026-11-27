"""检视问题的回归场景：所有硬件、服务和会话操作均使用桩，禁止触碰真实设备。"""

import json
from contextlib import contextmanager
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from usb4_gpu_manager import controller, deployment
from usb4_gpu_manager.core import operations
from usb4_gpu_manager.daemon import Service
from usb4_gpu_manager.gui import Window
from tests.test_manager import PROFILE, gpu

ROOT = Path(__file__).resolve().parents[1]


class DeploymentRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state, self.config = self.root / "state", self.root / "profile.json"
        self.enterContext(patch.object(deployment, "STATE", self.state))
        self.enterContext(patch.object(deployment, "CONFIG", self.config))
        self.enterContext(patch.object(deployment, "read", return_value="boot-B"))
        self.enterContext(patch.object(deployment, "stalled_commands", return_value=[]))
        self.enterContext(patch.object(deployment.os, "chown"))
        self.run = self.enterContext(patch.object(deployment, "command", return_value=subprocess.CompletedProcess([], 0, "", "")))

    def backup(self, previous_profile):
        directory = self.state / "backups/1"
        directory.mkdir(parents=True)
        if previous_profile:
            self.config.write_text(json.dumps(previous_profile))
        deployment.save_backup([str(self.config)], directory)
        self.config.write_text(json.dumps(dict(PROFILE, deployed_boot="boot-B")))
        (self.state / "latest-backup.json").write_text(json.dumps({"path": str(directory)}))
        return directory

    def test_rollback_old_profile_still_requires_restart_of_current_boot(self):
        self.backup(dict(PROFILE, deployed_boot="boot-A"))
        deployment.rollback()
        restored = json.loads(self.config.read_text())
        self.assertEqual(restored["deployed_boot"], "boot-A")
        self.assertTrue(deployment.status(restored)["restart_required"])
        with patch.object(deployment, "read", return_value="boot-C"):
            self.assertFalse(deployment.status(restored)["restart_required"])

    def test_failed_first_rollback_retains_recovery_without_profile(self):
        self.backup(None)
        self.run.side_effect = lambda args, **_: (_ for _ in ()).throw(RuntimeError("full /boot")) if args[0] == "update-initramfs" else subprocess.CompletedProcess(args, 0, "", "")
        with self.assertRaises(RuntimeError):
            deployment.rollback()
        self.assertFalse(self.config.exists())
        status = deployment.status()
        self.assertTrue(status["managed"])
        self.assertTrue(status["recovery_pending"])
        self.assertTrue(status["restart_required"])
        self.run.side_effect = None
        deployment.rollback()
        self.assertFalse(deployment.status()["recovery_pending"])
        self.assertTrue(deployment.status()["restart_required"])

    def test_failed_first_deploy_finds_backup_even_if_compensation_fails(self):
        generated = self.root / "new.conf"
        real_write = deployment.atomic_write

        def write(path, *args, **kwargs):
            if Path(path) == generated:
                raise OSError("disk full")
            return real_write(path, *args, **kwargs)

        def run(args, **kwargs):
            output = "NVreg_UseKernelSuspendNotifiers:int" if args[:2] == ["modinfo", "-p"] else "MIT"
            return subprocess.CompletedProcess(args, 0, output, "")

        real_exists = Path.exists
        with (patch.object(deployment, "LEGACY", []),
              patch.object(deployment, "plan", return_value={"profile": PROFILE, "files": {str(generated): "new"}}),
              patch.object(deployment, "atomic_write", side_effect=write),
              patch.object(deployment, "restore_backup", side_effect=OSError("disk full")),
              patch.object(Path, "exists", lambda path: True if str(path) == "/usr/sbin/update-initramfs" else real_exists(path))):
            self.run.side_effect = run
            with self.assertRaisesRegex(RuntimeError, "recovery pending"):
                deployment.deploy(PROFILE, [gpu()])
        self.assertFalse((self.state / "latest-backup.json").exists())
        self.assertTrue(deployment.status()["rollback_available"])
        self.run.side_effect = None
        deployment.rollback()
        self.assertFalse(deployment.status()["recovery_pending"])


class ControllerRegressionTests(unittest.TestCase):
    def setUp(self):
        self.manager = controller.Controller()
        self.enterContext(patch.object(deployment, "status", return_value={"restart_required": False}))
        self.enterContext(patch.object(self.manager, "automation_enabled", return_value=True))

    def test_physical_disconnect_resets_auto_bind_for_same_router(self):
        manager = self.manager
        manager.auto_pending, manager.signature = False, ("old",)
        manager.detached_routers = {"adapter"}
        manager.refresh = Mock(return_value={"usb4": [], "transport": {"state": "disconnected"}})
        with patch.object(controller, "observe", side_effect=lambda data: data):
            manager.tick()
        self.assertTrue(manager.auto_pending)
        self.assertIsNone(manager.signature)
        self.assertIsNone(manager.detached_routers)
        # 同一个路由 UUID 重插且 BAR 已稳定，允许再次绑定。
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "resource").write_text("0x1000 0x2000 0x200")
            manager.refresh.return_value = {"usb4": [{"uuid": "adapter", "is_host": False}],
                "target": gpu(path=str(path)), "profile": PROFILE,
                "transport": {"state": "present"}, "deployment": {"restart_required": False}}
            with (patch.object(controller, "observe", side_effect=lambda data: data),
                  patch.object(controller.time, "monotonic", return_value=0) as clock,
                  patch.object(manager, "_keep_awake"), patch.object(manager, "_attach") as attach):
                manager.tick()
                clock.return_value = 13
                manager.tick()
                attach.assert_called_once_with(PROFILE, rescan=False)

    def test_sleep_signal_during_stable_wait_stops_before_loading_modules(self):
        manager = self.manager
        selected = gpu(driver="unbound", path="/unused")

        @contextmanager
        def inhibitor():
            yield

        def wait(_):
            manager.sleeping = True

        with (patch.object(operations, "sleep_inhibitor", side_effect=inhibitor) as inhibit,
              patch.object(operations.Path, "write_text"),
              patch.object(operations, "gpu_inventory", return_value=[selected]),
              patch.object(operations, "read", return_value="0x1000 0x2000"),
              patch.object(manager, "_keep_awake"),
              patch.object(controller.time, "sleep", side_effect=wait),
              patch.object(operations, "command") as run):
            with self.assertRaises(RuntimeError):
                manager._attach_locked(PROFILE)
            inhibit.assert_called_once()
            run.assert_not_called()

    def test_queued_detach_rechecks_new_monitor_under_device_lock(self):
        selected = gpu(external_display_state="connected", connectors=[
            {"name": name, "kind": "external", "status": "connected"} for name in ("DP-1", "DP-2")])
        with (patch.object(self.manager, "_profile_and_gpu", return_value=(PROFILE, selected)),
              patch.object(operations, "device_action") as remove):
            with self.assertRaises(RuntimeError):
                self.manager._detach(selected, allow_displays=True, allowed_displays={"DP-1"})
            remove.assert_not_called()

    def test_ack_reservation_rejects_second_request_and_preserves_cancel(self):
        token = self.manager.reserve_operation("queue-detach", {"expected_pci": gpu()["pci"]})
        with self.assertRaises(RuntimeError):
            self.manager.reserve_operation("attach", {})
        self.manager.cancel()
        self.assertTrue(self.manager.cancel_event.is_set())
        self.manager.fail_reserved(object(), RuntimeError("unrelated"))
        self.assertTrue(self.manager.operation_lock.locked())
        self.manager.fail_reserved(token, RuntimeError("submission failed"))
        self.assertFalse(self.manager.operation_lock.locked())
        self.assertEqual(self.manager.operation["state"], "error")

    def test_inhibitor_failure_finishes_reserved_job_with_visible_event(self):
        token = self.manager.reserve_operation("deploy", {"profile": {}})
        service = Service.__new__(Service)
        service.controller, service.connection = self.manager, Mock()
        service.connection.call_with_unix_fd_list_sync.side_effect = RuntimeError("logind unavailable")
        with patch("usb4_gpu_manager.daemon.logging.getLogger"):
            service._job("deploy", {"profile": {}}, token)
        self.assertFalse(self.manager.operation_lock.locked())
        self.assertEqual(self.manager.operation["state"], "error")
        self.assertIn("logind unavailable", self.manager.events[-1]["message"])


class GuiRegressionTests(unittest.TestCase):
    def window(self):
        # 普通 Python 桩复用方法，不创建真实 GTK 窗口或打开授权弹窗。
        window = SimpleNamespace(operation_pending=False, pending_requests={}, display_busy=False,
            display_action="", display_message="", data={}, pending_logout=False,
            toast=Mock(), refresh=Mock(), apply_status=Mock(), show_operation=Mock(),
            client=Mock(), pool=Mock())
        return window

    def test_reentry_rejected_and_cancel_completion_cannot_clear_other_request(self):
        window = self.window()
        callbacks = []
        window.pool.submit.side_effect = callbacks.append
        with patch("usb4_gpu_manager.gui.GLib.idle_add", side_effect=lambda callback, *args: callback(*args)):
            Window.submit(window, "queue-detach")
            Window.submit(window, "attach")
            self.assertEqual(len(callbacks), 1)
            Window.submit(window, "cancel-detach")
            callbacks[1]()
            self.assertTrue(window.operation_pending)
            callbacks[0]()
            self.assertFalse(window.operation_pending)

    def test_close_without_tray_uses_guarded_quit(self):
        app = SimpleNamespace(tray=None, request_quit=Mock())
        window = SimpleNamespace(get_application=lambda: app)
        self.assertTrue(Window.close_to_tray(window))
        app.request_quit.assert_called_once()


class DistributedSourceTests(unittest.TestCase):
    def test_install_guard_covers_copy_and_service_start(self):
        text = (ROOT / "install.sh").read_text()
        guard = text.index("RefuseManualStart=yes")
        copy = text.index('"$source_dir/packaging/files.py"')
        release = text.index('rm -f "$upgrade_guard"')
        start = text.index("systemctl enable --now")
        self.assertLess(guard, copy)
        self.assertLess(copy, release)
        self.assertLess(release, start)

    def test_uninstall_checks_recovery_and_boot_marker_before_deleting_files(self):
        text = (ROOT / "uninstall.sh").read_text()
        delete = text.index("systemctl disable --now")
        for marker in ("deployment-pending.json", "restart-required.json", "saved['boot'] == boot"):
            self.assertLess(text.index(marker), delete)

    def test_both_cursor_policy_sources_enable_internal_only_overlay(self):
        for name in ("drm_only_patch.py", "gpu-release-poc.patch"):
            text = (ROOT / "experimental/mutter-poc" / name).read_text()
            predicate = text.split("gboolean per_view_overlay =", 1)[1].split("gboolean any_software_view", 1)[0]
            self.assertIn("MUTTER_GPU_RELEASE_POC_DRM_ONLY_INTERNAL_PCI", predicate)
            self.assertIn("||", predicate)
