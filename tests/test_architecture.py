"""前后端生命周期、共享契约和递归安装清单的回归覆盖。"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from usb4_gpu_manager.contracts import SERVICE_ACTIONS, valid_options
from usb4_gpu_manager.core.policy import bar_signature, internal_policy_state
from usb4_gpu_manager.ui.application import Application
from usb4_gpu_manager.ui.requests import RequestPool
from usb4_gpu_manager.ui.state import removal_confirmed, session_cleanup_pending
from usb4_gpu_manager.ui.window import Window

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installation_manifest", ROOT / "packaging/files.py")
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)


class LifecycleTests(unittest.TestCase):
    def application(self, state="waiting", requests=None):
        window = SimpleNamespace(data={"operation": {"state": state, "action": "queue-detach"}},
                                 pending_requests=requests or {}, pending_logout=True,
                                 closing=False, set_visible=Mock(), refresh_source=0)
        return SimpleNamespace(window=window, quit=Mock())

    def test_exit_does_not_depend_on_backend_operation(self):
        for state in ("running", "waiting", "removing"):
            with self.subTest(state=state):
                app = self.application(state, {object(): "queue-detach"})
                Application.request_quit(app)
                app.quit.assert_called_once()
                app.window.set_visible.assert_called_once_with(False)
                self.assertEqual(app.window.data["operation"]["state"], state)
                self.assertFalse(app.window.pending_logout)

    def test_exit_waits_only_for_session_resource_cleanup(self):
        for action in ("internal-detach", "disable-displays", "internal-restore-displays"):
            with self.subTest(action=action):
                app = self.application(requests={object(): action})
                Application.request_quit(app)
                app.quit.assert_not_called()
                self.assertTrue(app.window.closing)
                app.window.pending_requests.clear()
                Application.request_quit(app)
                app.quit.assert_called_once()

    def test_local_completion_finishes_exit_even_when_backend_is_still_busy(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                app = self.application()
                window = app.window
                window.data = {}
                window.operation_pending = False
                window.display_busy = False
                window.display_action = window.display_message = ""
                window.toast, window.refresh = Mock(), Mock()
                window.apply_status, window.show_operation = Mock(), Mock()
                window.client, window.pool = Mock(), Mock()
                window.get_application = lambda: app
                app.request_quit = lambda: Application.request_quit(app)
                if failed:
                    window.client.perform.side_effect = RuntimeError("audio recovery failed")
                work = []
                window.pool.submit.side_effect = work.append
                Window.submit(window, "internal-detach", {"expected_pci": "0000:65:00.0"})
                Application.request_quit(app)
                app.quit.assert_not_called()
                window.data = {"operation": {"state": "running"}}
                # 同一退出路径涵盖收尾成功和失败；不再等待后台状态变为空闲。
                with patch("usb4_gpu_manager.ui.window.GLib.idle_add",
                           side_effect=lambda callback, *args: callback(*args)):
                    work[0]()
                app.quit.assert_called_once()
                window.refresh.assert_not_called()

    def test_cleanup_classification_ignores_remote_driver_load_and_authorization(self):
        self.assertFalse(session_cleanup_pending({1: "attach", 2: "internal-attach", 3: "deploy"}))
        self.assertTrue(session_cleanup_pending({1: "internal-detach", 2: "queue-detach"}))

    def test_session_threads_do_not_keep_process_alive_for_remote_rpc(self):
        pool = RequestPool(max_workers=1)
        entered, release = threading.Event(), threading.Event()
        try:
            def rpc():
                entered.set()
                release.wait(2)
            future = pool.submit(rpc)
            self.assertTrue(entered.wait(1))
            pending = pool.submit(lambda: self.fail("Unsent request must be cancelled on exit"))
            pool.shutdown()
            self.assertTrue(all(thread.daemon for thread in pool.threads))
            self.assertTrue(pending.cancelled())
            release.set()
            future.result(timeout=2)
        finally:
            release.set()
            pool.shutdown()
            for thread in pool.threads:
                thread.join(timeout=2)


class ContractAndPolicyTests(unittest.TestCase):
    def test_request_options_are_shared_and_reject_unknown_fields(self):
        self.assertIn("cancel-detach", SERVICE_ACTIONS)
        self.assertTrue(valid_options("cancel-detach", {}))
        self.assertFalse(valid_options("cancel-detach", {"force": True}))
        self.assertFalse(valid_options("attach", []))
        self.assertFalse(valid_options("unsupported", {}))
        self.assertTrue(valid_options("internal-detach", {"expected_pci": "0000:65:00.0"}))

    def test_invalid_bar_cannot_become_stable(self):
        gpu = {"pci": "0000:43:00.0", "path": "/sys/mock"}
        for resource in ("", "0x0 0x0", "invalid 0x1000", "0x1000"):
            self.assertIsNone(bar_signature(gpu, resource))
        self.assertEqual(bar_signature(gpu, "0x1000 0x2000"),
                         (gpu["pci"], gpu["path"], ("0x1000", "0x2000")))

    def test_unreliable_disconnect_does_not_generate_power_policy_edge(self):
        data = {"transport": {"state": "disconnected", "driver_cleanup_pending": True},
                "external_power_online": True}
        self.assertIsNone(internal_policy_state(data, False))
        data["transport"]["driver_cleanup_pending"] = False
        self.assertEqual(internal_policy_state(data, False), (True, False))
        self.assertIsNone(internal_policy_state(data, True))

    def test_removal_requires_service_confirmation_and_completed_driver_cleanup(self):
        data = {"service_connected": True, "operation": {"state": "done", "action": "detach"},
                "transport": {"state": "detached", "pci_visible": False}}
        self.assertTrue(removal_confirmed(data))
        data["transport"]["driver_cleanup_pending"] = True
        self.assertFalse(removal_confirmed(data))


class InstallationTests(unittest.TestCase):
    def test_manifest_covers_all_subpackages_and_existing_runtime_assets(self):
        files = list(manifest.installation_files(ROOT))
        destinations = [target for _, target, _ in files]
        self.assertEqual(len(destinations), len(set(destinations)))
        self.assertTrue(all((ROOT / origin).is_file() for origin, _, _ in files))
        for path in (ROOT / "usb4_gpu_manager").rglob("*.py"):
            self.assertIn(manifest.APP / path.relative_to(ROOT), destinations)
        for name in ("ui/gtk.py", "core/operations.py", "desktop/common.py", "worker.py"):
            self.assertIn(manifest.APP / "usb4_gpu_manager" / name, destinations)

    def test_staging_preserves_relative_paths_modes_and_isolated_launchers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest.copy_files(ROOT, root)
            for origin, destination, mode in manifest.installation_files(ROOT):
                target = root / destination
                self.assertEqual(target.read_bytes(), (ROOT / origin).read_bytes())
                self.assertEqual(target.stat().st_mode & 0o777, mode)
            launcher = root / "usr/bin/usb4-gpu-manager"
            self.assertIn("python3 -I /usr/lib/usb4-gpu-manager/gui_entry.py", launcher.read_text())
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)

    def test_staging_rejects_paths_that_resolve_to_live_root(self):
        for root in (Path("/"), Path("/../"), Path("relative")):
            with self.subTest(root=root), self.assertRaises(ValueError):
                manifest.copy_files(ROOT, root)
