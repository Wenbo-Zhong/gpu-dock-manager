import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1] / 'experimental/mutter-poc'
sys.path.insert(0, str(HERE))
import device_policy
import launch_shell


class MutterInternalPolicyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.pci = '0000:65:00.0'
        self.gpu = self.root / self.pci
        self.audio = self.root / '0000:65:00.1'
        group = self.root / 'groups/22'
        (group / 'devices').mkdir(parents=True)
        for path, product, cls in ((self.gpu, '0x2820', '0x030000'), (self.audio, '0x22bd', '0x040300')):
            path.mkdir()
            for key, value in {'vendor': '0x10de', 'device': product, 'class': cls, 'boot_vga': '0'}.items():
                (path / key).write_text(value)
            (path / 'iommu_group').symlink_to(group)
            (group / 'devices' / path.name).symlink_to(path)
        (self.gpu / 'drm/card7').mkdir(parents=True)

    def test_internal_identity_and_iommu_guards(self):
        self.assertEqual(device_policy.validate_internal(self.pci, self.root), self.gpu)
        for path, key, value in ((self.gpu, 'boot_vga', '1'), (self.audio, 'device', '0x0000'),
                                 (self.gpu, 'vendor', '0x1002'), (self.gpu, 'removable', 'removable')):
            with self.subTest(key=key):
                file = path / key
                previous = file.read_text() if file.exists() else None
                file.write_text(value)
                with self.assertRaises(RuntimeError):
                    device_policy.validate_internal(self.pci, self.root)
                if previous is None: file.unlink()
                else: file.write_text(previous)
        (self.root / 'groups/22/devices/extra').mkdir()
        with self.assertRaises(RuntimeError):
            device_policy.validate_internal(self.pci, self.root)

    def test_launcher_internal_opt_in_and_external_boot_guard(self):
        manifest = {'drm_only_cpu_copy': True, 'internal_4070_drm_only': True,
                    'library_paths': ['/private/lib'], 'prefix': '/private', 'clean_child_environment': True}
        state = {'manifest': '/test/manifest.json', 'pci': self.pci, 'trace_device': '/dev/dri/card7',
                 'drm_only_cpu_copy': True, 'internal_4070': True, 'external_pci': '0000:03:00.0'}
        real_path = Path
        def path(value):
            return real_path(str(value).replace('/sys/bus/pci/devices', str(self.root)))
        external = self.root / '0000:03:00.0'
        external.mkdir()
        for key, value in {'vendor': '0x10de', 'boot_vga': '1', 'removable': 'removable'}.items():
            (external / key).write_text(value)
        with (patch.object(launch_shell, 'verify_manifest', return_value=manifest),
              patch.object(launch_shell, 'check_gi'), patch.object(launch_shell, 'Path', side_effect=path),
              patch.object(device_policy, 'validate_internal', return_value=self.gpu) as validate,
              patch.dict(os.environ, {'XDG_DATA_DIRS': '/usr/share', 'MUTTER_GPU_RELEASE_POC_DRM_ONLY_INTERNAL_PCI': 'stale'})):
            env = launch_shell.prepare_environment(state)
            self.assertEqual(env['MUTTER_GPU_RELEASE_POC_DRM_ONLY_INTERNAL_PCI'], self.pci)
            self.assertNotIn('MUTTER_GPU_RELEASE_POC_DRM_ONLY_PCI', env)
            self.assertEqual(env['MUTTER_GPU_RELEASE_POC_CLEAN_CHILD_ENV'], '1')
            self.assertEqual(env['MUTTER_GPU_RELEASE_POC_ORIGINAL_DATA_DIRS'], '/usr/share')
            validate.assert_called_once_with(self.pci)
            manifest['internal_4070_drm_only'] = False
            with self.assertRaises(RuntimeError):
                launch_shell.prepare_environment(state)
