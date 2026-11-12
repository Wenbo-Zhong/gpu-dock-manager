import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('kwin_probe', Path(__file__).resolve().parents[1] / 'experimental/kwin-poc/control.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def data():
    gpu = {'pci': '0000:03:00.0', 'card': 'card0', 'vendor': '0x10de', 'driver': 'nvidia',
           'boot_vga': False, 'external': True,
           'connectors': [{'kind': 'external', 'status': 'disconnected', 'enabled': 'disabled'}]}
    amd = {'pci': '0000:66:00.0', 'vendor': '0x1002', 'driver': 'amdgpu',
           'connectors': [{'kind': 'internal', 'status': 'connected', 'enabled': 'enabled'}]}
    return {'target': gpu, 'gpus': [gpu, amd], 'holders': {'complete': True,
            'processes': [{'name': 'kwin_wayland', 'pid': 123, 'nodes': ['/dev/dri/card0']}]}}


class KwinProbeTests(unittest.TestCase):
    def test_validation_requires_idle_external_and_amd_panel(self):
        self.assertEqual(probe.validate(data())['pci'], '0000:03:00.0')
        for change in ('connected', 'active', 'unknown', 'no-panel', 'primary', 'incomplete', 'workload', 'no-kwin', 'virtual'):
            d = copy.deepcopy(data())
            if change == 'connected': d['target']['connectors'][0]['status'] = 'connected'
            if change == 'active': d['target']['connectors'][0]['enabled'] = 'enabled'
            if change == 'unknown': d['target']['connectors'][0]['status'] = 'unknown'
            if change == 'no-panel': d['gpus'][1]['connectors'][0]['enabled'] = 'disabled'
            if change == 'primary': d['target']['boot_vga'] = True
            if change == 'incomplete': d['holders']['complete'] = False
            if change == 'workload': d['holders']['processes'].append({'name': 'python3'})
            if change == 'no-kwin': d['holders']['processes'] = []
            if change == 'virtual': d['target']['connectors'][0]['kind'] = 'virtual'
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                probe.validate(d)

    def test_refresh_rejects_other_tasks_without_sending_event(self):
        d = data()
        d['holders']['processes'].append({'name': 'python3'})
        with patch.object(probe.sys, 'argv', ['control.py', 'refresh']), patch.object(probe.os, 'geteuid', return_value=0), patch.object(probe, 'snapshot', return_value=d), patch.object(probe, 'command') as command, self.assertRaises(RuntimeError):
            probe.main()
        command.assert_not_called()

    def test_read_only_summary_never_claims_unplug_permission(self):
        d = data()
        d['detach'] = {'safe': True}
        self.assertFalse(probe.summary(d)['safe_to_unplug'])
