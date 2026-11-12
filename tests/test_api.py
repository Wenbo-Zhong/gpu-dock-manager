import os
import json
import threading
import unittest
from unittest.mock import Mock, patch

from gi.repository import GLib
from usb4_gpu_manager.daemon import Service


class InhibitorTests(unittest.TestCase):
    def test_internal_reply_waits_for_driver_completion_or_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                service = Service.__new__(Service)
                service.controller = Mock()
                service.controller.get_status.return_value = {"can_mutate_devices": True}
                service.authorized = Mock(return_value=True)
                service.pool = Mock()
                events = []

                def perform(*args):
                    events.append("driver-start")
                    self.assertEqual(events, ["driver-start"])
                    events.append("driver-finished")
                    if failure:
                        raise RuntimeError("driver failed")
                    return "completed"

                def reply(*args):
                    events.append("reply")

                service.controller.perform.side_effect = perform
                with patch("usb4_gpu_manager.daemon.GLib.idle_add", side_effect=reply) as respond:
                    service._perform("sender", "internal-detach", json.dumps({"expected_pci": "0000:65:00.0"}), Mock())
                self.assertEqual(events, ["driver-start", "driver-finished", "reply"])
                service.pool.submit.assert_not_called()
                if failure:
                    self.assertIsInstance(respond.call_args.args[-1], RuntimeError)
                else:
                    self.assertEqual(respond.call_args.args[-1], "completed")

    def test_deployment_holds_and_closes_inhibitor_fd(self):
        reader, writer = os.pipe()
        duplicate = os.dup(reader)
        service = Service.__new__(Service)
        descriptors = Mock()
        descriptors.get.return_value = duplicate
        service.connection = Mock()
        service.connection.call_with_unix_fd_list_sync.return_value = (GLib.Variant("(h)", (0,)), descriptors)
        service.controller = Mock()
        service.controller.operation_lock = threading.Lock()
        service.controller.lock = threading.RLock()
        try:
            service._job("deploy", {"profile": {}})
            service.controller.perform.assert_called_once_with("deploy", {"profile": {}})
            arguments = service.connection.call_with_unix_fd_list_sync.call_args.args
            self.assertEqual(arguments[4].unpack()[0], "sleep:shutdown")
            with self.assertRaises(OSError):
                os.fstat(duplicate)
        finally:
            os.close(reader)
            os.close(writer)

    def test_no_configuration_change_if_inhibitor_cannot_be_acquired(self):
        service = Service.__new__(Service)
        service.connection = Mock()
        service.connection.call_with_unix_fd_list_sync.side_effect = RuntimeError("logind unavailable")
        service.controller = Mock()
        service.controller.operation_lock = threading.Lock()
        service.controller.lock = threading.RLock()
        with patch("logging.getLogger"):
            service._job("deploy", {})
        service.controller.perform.assert_not_called()
        self.assertEqual(service.controller.operation["state"], "error")
