import unittest
from unittest.mock import patch

from usb4_gpu_manager.controller import Controller
from tests.test_manager import PROFILE, gpu

PCI = "0000:65:00.0"


def status(power, external=None, *, graphics="nvidia", audio="snd_hda_intel", transport=None):
    return {"internal_gpu": {"pci": PCI, "graphics_driver": graphics, "audio_driver": audio},
            "external_power_online": power, "target": external, "profile": {},
            "suggested_profile": PROFILE, "can_mutate_devices": True,
            "transport": {"state": transport or ("present" if external else "absent")},
            "target_router_ids": ["adapter"], "deployment": {"restart_required": False}}


class GpuPolicyTests(unittest.TestCase):
    def setUp(self):
        self.manager = Controller()
        self.switch = self.enterContext(patch.object(self.manager, "_switch_internal"))
        self.attach = self.enterContext(patch.object(self.manager, "_attach_locked"))

    def tick(self, value, count=2):
        for _ in range(count):
            self.manager._tick_internal_policy(value)

    def test_battery_attempt_once_then_ac_loads_4070(self):
        self.tick(status(True))
        self.switch.assert_not_called()
        self.switch.side_effect = RuntimeError("busy")
        self.tick(status(False), count=4)
        self.switch.assert_called_once_with("internal-detach", PCI, protect_connected=True)
        self.assertEqual(self.manager.operation["state"], "error")
        self.switch.reset_mock(side_effect=True)
        self.tick(status(True, graphics="vfio-pci", audio="vfio-pci"), count=4)
        self.switch.assert_called_once_with("internal-attach", PCI, protect_connected=True)
        self.attach.assert_not_called()

    def test_connect_loads_external_even_when_4070_is_busy(self):
        self.tick(status(True))
        self.switch.side_effect = RuntimeError("HDMI connected")
        self.tick(status(True, gpu(driver="unbound", card=None)), count=4)
        self.switch.assert_called_once_with("internal-detach", PCI, protect_connected=True)
        self.attach.assert_called_once_with(PROFILE, rescan=True)
        self.assertIn("外置显卡已加载", self.manager.operation["message"])

    def test_manual_pci_removal_does_not_look_like_physical_disconnect(self):
        self.tick(status(True, gpu(), graphics="vfio-pci", audio="vfio-pci"))
        self.manager.detached_routers = {"adapter"}
        self.tick(status(True, transport="detached", graphics="vfio-pci", audio="vfio-pci"))
        self.switch.assert_not_called()
        self.attach.assert_not_called()
        self.tick(status(True, transport="disconnected", graphics="vfio-pci", audio="vfio-pci"))
        self.switch.assert_called_once_with("internal-attach", PCI, protect_connected=True)
        self.assertIsNone(self.manager.detached_routers)

    def test_incomplete_driver_cleanup_does_not_consume_disconnect_edge(self):
        self.tick(status(True, gpu(), graphics="vfio-pci", audio="vfio-pci"))
        pending = status(True, transport="disconnected", graphics="vfio-pci", audio="vfio-pci")
        pending["transport"]["driver_cleanup_pending"] = True
        self.tick(pending, count=4)
        self.switch.assert_not_called()
        pending["transport"]["driver_cleanup_pending"] = False
        self.tick(pending)
        self.switch.assert_called_once_with("internal-attach", PCI, protect_connected=True)
