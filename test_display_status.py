import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("status", Path(__file__).with_name("egpu-display-status.py"))
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


class DisplayDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "class/drm").mkdir(parents=True)
        (self.root / "bus/pci/devices").mkdir(parents=True)

    def gpu(self, card="card7", pci="0000:43:00.0", target=True):
        device = self.root / "bus/pci/devices" / pci
        device.mkdir()
        (device / "vendor").write_text("0x10de" if target else "0x1002")
        (device / "device").write_text("0x2bb3" if target else "0x150e")
        if card:
            node = self.root / "class/drm" / card
            node.mkdir()
            (node / "device").symlink_to(device)

    def connector(self, card, name, state="disconnected", enabled="disabled"):
        path = self.root / "class/drm" / f"{card}-{name}"
        path.mkdir()
        (path / "status").write_text(state)
        (path / "enabled").write_text(enabled)

    def test_internal_panel_does_not_count_as_egpu_or_external(self):
        self.gpu()
        self.gpu("card2", "0000:66:00.0", False)
        self.connector("card7", "DP-9")
        self.connector("card2", "eDP-1", "connected", "enabled")
        result = status.scan(self.root)
        self.assertEqual(result["egpu_display_state"], "disconnected")
        self.assertEqual(result["external_display_count"], 0)

    def test_connected_but_disabled_monitor_still_counts(self):
        self.gpu()
        self.connector("card7", "DP-10", "connected", "disabled")
        result = status.scan(self.root)
        self.assertEqual(result["egpu_display_state"], "connected")
        self.assertEqual(result["external_display_count"], 1)

    def test_other_gpu_external_monitor_not_attributed_to_egpu(self):
        self.gpu()
        self.gpu("card2", "0000:66:00.0", False)
        self.connector("card7", "DP-9")
        self.connector("card2", "HDMI-A-1", "connected", "enabled")
        result = status.scan(self.root)
        self.assertEqual(result["egpu_display_state"], "disconnected")
        self.assertEqual(result["external_display_count"], 1)

    def test_unknown_is_not_treated_as_disconnected(self):
        self.gpu()
        self.connector("card7", "DP-9", "unknown")
        self.assertEqual(status.scan(self.root)["egpu_display_state"], "unknown")

    def test_present_gpu_without_drm_is_unknown(self):
        self.gpu(card=None)
        self.assertEqual(status.scan(self.root)["egpu_display_state"], "unknown")

    def test_renumbered_card_and_pci_work(self):
        self.gpu(card="card19", pci="0000:08:00.0")
        self.connector("card19", "DP-2", "connected", "enabled")
        self.assertEqual(status.scan(self.root)["egpu_display_state"], "connected")

    def test_absent_gpu(self):
        self.assertEqual(status.scan(self.root)["egpu_display_state"], "absent")


if __name__ == "__main__":
    unittest.main()
