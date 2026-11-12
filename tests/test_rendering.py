from pathlib import Path
import tempfile
import unittest
from usb4_gpu_manager.rendering import configure_renderer


class RenderingTests(unittest.TestCase):
    def test_integrated_gpu_overrides_discrete_environment(self):
        for vendor, driver in [('0x1002', 'amdgpu'), ('0x8086', 'i915'), ('0x8086', 'xe')]:
            with self.subTest(driver=driver), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                device = root / 'bus/pci/devices/0000:66:00.0'
                device.mkdir(parents=True)
                (device / 'vendor').write_text(vendor)
                (device / 'driver').symlink_to(root / driver)
                connector = root / 'class/drm/card1-eDP-1'
                connector.mkdir(parents=True)
                (connector / 'device').symlink_to(device)
                (root / 'class/drm/card1').mkdir()
                (root / 'class/drm/card1/device').symlink_to(device)
                (connector / 'status').write_text('connected')
                mesa = root / 'mesa.json'
                mesa.write_text('{}')
                env = {'GSK_RENDERER': 'vulkan', 'DRI_PRIME': '1', '__NV_PRIME_RENDER_OFFLOAD': '1', 'GBM_BACKEND': 'nvidia-drm'}
                self.assertEqual(configure_renderer(env, root, mesa), 'gl')
                self.assertEqual(env['DRI_PRIME'], 'pci-0000_66_00_0')
                self.assertEqual(env['__EGL_VENDOR_LIBRARY_FILENAMES'], str(mesa))
                self.assertNotIn('__NV_PRIME_RENDER_OFFLOAD', env)
                self.assertNotIn('GBM_BACKEND', env)
                mesa.unlink()
                self.assertEqual(configure_renderer(env, root, mesa), 'cairo')

    def test_no_identifiable_integrated_gpu_uses_software(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {'GSK_RENDERER': 'vulkan'}
            self.assertEqual(configure_renderer(env, Path(folder), Path(folder) / 'mesa.json'), 'cairo')
