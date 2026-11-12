import copy
import subprocess
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from usb4_gpu_manager import presence, system, controller
from tests.test_manager import gpu, PROFILE


class PresenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "bus/pci/devices/0000:43:00.0").mkdir(parents=True)
        (self.root / "bus/thunderbolt/devices").mkdir(parents=True)
        self.sample = {"timestamp": 100, "target": gpu(), "gpus": [gpu()],
                       "usb4": [{"uuid": "adapter-uuid", "is_host": False, "rx_speed": "40 Gb/s"}],
                       "metrics": [{"temperature_c": "32"}],
                       "operation": {"state": "waiting", "action": "queue-detach", "message": "waiting"}}

    def router(self):
        router = self.root / "bus/thunderbolt/devices/0-2"
        router.mkdir()
        (router / "unique_id").write_text("adapter-uuid")

    def test_unplug_overrides_stale_pci_and_stale_connected_cache(self):
        original = copy.deepcopy(self.sample)
        result = presence.observe(self.sample, self.root, now=200)
        self.assertIsNone(result["target"])
        self.assertEqual(result["transport"]["state"], "disconnected")
        self.assertTrue(result["transport"]["driver_cleanup_pending"])
        self.assertEqual(result["metrics"], [])
        self.assertEqual(result["usb4"], [])
        self.assertFalse(result["can_mutate_devices"])
        self.assertEqual(result["operation"]["state"], "error")
        self.assertEqual(self.sample, original)

    def test_repeated_overlay_retains_disconnected_identity(self):
        first = presence.observe(self.sample, self.root, now=200)
        second = presence.observe(first, self.root, now=201)
        self.assertEqual(second["transport"]["state"], "disconnected")
        self.assertEqual(second["last_known_target"]["pci"], gpu()["pci"])

    def test_stale_query_does_not_claim_live_metrics_when_cable_present(self):
        self.router()
        result = presence.observe(self.sample, self.root, now=200)
        self.assertIsNotNone(result["target"])
        self.assertTrue(result["status_stale"])
        self.assertEqual(result["metrics"], [])
        self.assertFalse(result["can_mutate_devices"])

    def test_fresh_sample_and_present_transport_remain_usable(self):
        self.router()
        result = presence.observe(self.sample, self.root, now=101)
        self.assertFalse(result["status_stale"])
        self.assertTrue(result["can_mutate_devices"])
        self.assertEqual(result["metrics"], self.sample["metrics"])

    def test_blocked_io_cannot_be_cleared_by_client_overlay(self):
        self.router()
        self.sample["blocked_io"] = True
        self.assertFalse(presence.observe(self.sample, self.root, now=101)["can_mutate_devices"])

    def test_successful_query_does_not_forget_disappeared_router(self):
        previous = presence.remember_transport({}, self.sample)
        current = dict(self.sample, timestamp=199, usb4=[])
        current = presence.remember_transport(previous, current)
        result = presence.observe(current, self.root, now=200)
        self.assertFalse(result["status_stale"])
        self.assertEqual(result["transport"]["state"], "disconnected")
        self.assertIsNone(result["target"])

    def test_logical_removal_is_distinct_from_unplugging_cable(self):
        self.router()
        (self.root / "bus/pci/devices/0000:43:00.0").rmdir()
        self.sample["operation"] = {"state": "done", "action": "detach", "message": "可以拔线"}
        result = presence.observe(self.sample, self.root, now=101)
        self.assertEqual(result["transport"]["state"], "detached")
        self.assertFalse(result["transport"]["driver_cleanup_pending"])
        self.assertIsNone(result["target"])
        self.assertEqual(result["operation"], self.sample["operation"])

    def test_reappeared_pci_device_hides_previous_safe_removal_message(self):
        self.router()
        for action in ("detach", "queue-detach"):
            with self.subTest(action=action):
                self.sample["operation"] = {"state": "done", "action": action, "message": "可以拔线"}
                result = presence.observe(self.sample, self.root, now=101)
                self.assertEqual(result["transport"]["state"], "present")
                self.assertEqual(result["operation"], {"state": "idle", "action": "", "message": ""})

    def test_no_first_sample_is_not_considered_a_fresh_empty_inventory(self):
        result = presence.observe({}, self.root, now=200)
        self.assertFalse(result["sample_available"])
        self.assertTrue(result["status_stale"])
        self.assertFalse(result["can_mutate_devices"])


class BoundedIoTests(unittest.TestCase):
    def test_unkillable_child_timeout_returns_without_waiting_and_no_duplicate(self):
        process = Mock(pid=12345)
        process.communicate.side_effect = subprocess.TimeoutExpired(["test"], 1)
        process.poll.return_value = None
        try:
            with patch.object(system.subprocess, "Popen", return_value=process) as spawn, patch.object(system.os, "killpg"):
                for _ in range(2):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        system.command(["test-only-kernel-stall"], timeout=1)
                self.assertEqual(spawn.call_count, 1)
                self.assertEqual(process.communicate.call_count, 2)
        finally:
            with system._stalled_lock:
                system._stalled.clear()

    def test_status_does_not_wait_for_device_operation_lock(self):
        manager = controller.Controller()
        manager.cached = {"timestamp": time.time()}
        held, release = threading.Event(), threading.Event()
        def block_driver():
            with manager.device_lock:
                held.set()
                release.wait(2)
        thread = threading.Thread(target=block_driver)
        thread.start()
        try:
            self.assertTrue(held.wait(1))
            started = time.monotonic()
            self.assertIn("live_timestamp", manager.get_status())
            self.assertLess(time.monotonic() - started, .5)
        finally:
            release.set()
            thread.join(2)

    def test_remove_timeout_never_reports_safe_to_unplug(self):
        manager = controller.Controller()
        manager._profile_and_gpu = lambda: (PROFILE, gpu())
        with patch.object(controller, "holders", return_value={"complete": True, "processes": []}), patch.object(controller, "usb4_inventory", return_value=[]), patch.object(controller, "device_action", side_effect=subprocess.TimeoutExpired(["remove"], 25)):
            with self.assertRaisesRegex(RuntimeError, "尚未确认"):
                manager.perform("detach", {"expected_pci": gpu()["pci"]})
        self.assertEqual(manager.operation["state"], "error")
        self.assertIn("尚未确认", manager.operation["message"])
