import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from usb4_gpu_manager.kde_displays import KdeDisplays, layout_key
from usb4_gpu_manager.client import Client
from tests.test_manager import gpu


def output(number, name, panel=False):
    return {'id': number, 'name': name, 'connected': True, 'enabled': True, 'type': 7 if panel else 14,
            'currentModeId': '1', 'modes': [{'id': '1'}], 'pos': {'x': 0 if panel else 1920, 'y': 0},
            'scale': 1.5, 'rotation': 2 if panel else 1, 'priority': number, 'identity': name + '-edid',
            'replicationSource': 0, 'clones': [], 'hdr': False}


class KdeDisplayTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.desktop = KdeDisplays(Path(temp.name) / 'layout.json')
        self.state = {'eDP-1': output(1, 'eDP-1', True), 'DP-9': output(2, 'DP-9')}
        external = gpu(connectors=[{'name': 'DP-9', 'kind': 'external', 'status': 'connected', 'enabled': 'enabled'}])
        internal = gpu(pci='0000:66:00.0', external=False, vendor='0x1002', driver='amdgpu',
                       connectors=[{'name': 'eDP-1', 'kind': 'internal', 'status': 'connected', 'enabled': 'enabled'}])
        self.data = {'target': external, 'gpus': [internal, external], 'service_connected': True,
                     'can_mutate_devices': True, 'operation': {'state': 'idle'}}
        self.pci = external['pci']

    def disable(self):
        after = copy.deepcopy(self.state)
        after['DP-9']['enabled'] = False
        with patch.object(self.desktop, 'read', side_effect=[self.state, self.state, after]), patch('usb4_gpu_manager.kde_displays.command') as run:
            self.desktop.disable(self.data, self.pci)
        self.assertEqual(run.call_args.args[0], ['kscreen-doctor', 'output.2.disable'])
        return after

    def test_disable_and_restore_preserves_scale_rotation_and_positions(self):
        after = self.disable()
        with patch.object(self.desktop, 'read', side_effect=[after, self.state]), patch('usb4_gpu_manager.kde_displays.command') as run:
            self.desktop.restore(self.data, self.pci)
        args = run.call_args.args[0]
        for flag in ('output.1.rotation.left', 'output.1.scale.1.5', 'output.2.position.1920,0', 'output.2.enable'):
            self.assertIn(flag, args)
        self.assertFalse(self.desktop.backup_path.exists())

    def test_rejects_inactive_panel_busy_and_stale_before_mutation(self):
        for change in ('panel', 'busy', 'stale'):
            state, data = copy.deepcopy(self.state), copy.deepcopy(self.data)
            if change == 'panel': state['eDP-1']['enabled'] = False
            if change == 'busy': data['operation']['state'] = 'running'
            if change == 'stale': data['can_mutate_devices'] = False
            with self.subTest(change=change), patch.object(self.desktop, 'read', return_value=state), patch('usb4_gpu_manager.kde_displays.command') as run, self.assertRaises(RuntimeError):
                self.desktop.disable(data, self.pci)
            run.assert_not_called()

    def test_restore_rejects_replaced_display_or_changed_layout(self):
        after = self.disable()
        for field, value in (('identity', 'new-screen'), ('scale', 2)):
            changed = copy.deepcopy(after)
            changed['DP-9'][field] = value
            with patch.object(self.desktop, 'read', return_value=changed), patch('usb4_gpu_manager.kde_displays.command') as run, self.assertRaises(RuntimeError):
                self.desktop.restore(self.data, self.pci)
            run.assert_not_called()

    def test_failed_disable_rolls_back_without_detaching(self):
        with patch.object(self.desktop, 'read', return_value=self.state), patch('usb4_gpu_manager.kde_displays.command') as run, self.assertRaisesRegex(RuntimeError, '已恢复原布局'):
            self.desktop.disable(self.data, self.pci)
        self.assertEqual(run.call_count, 2)
        self.assertIn('output.2.enable', run.call_args.args[0])

    def test_layout_change_between_check_and_apply_aborts(self):
        changed = copy.deepcopy(self.state)
        changed['eDP-1']['scale'] = 2
        with patch.object(self.desktop, 'read', side_effect=[self.state, changed]), patch('usb4_gpu_manager.kde_displays.command') as run, self.assertRaisesRegex(RuntimeError, '已变化'):
            self.desktop.disable(self.data, self.pci)
        run.assert_not_called()

    def test_read_validates_unique_names_and_refuses_mirroring(self):
        for mutation in ('mirror', 'duplicate', 'mode'):
            values = [output(1, 'eDP-1', True)]
            if mutation == 'mirror': values[0]['replicationSource'] = 2
            if mutation == 'duplicate': values.append(output(2, 'eDP-1'))
            if mutation == 'mode': values[0]['currentModeId'] = '999'
            with patch('usb4_gpu_manager.kde_displays.command') as run, patch('usb4_gpu_manager.kde_displays.output_identity', return_value='edid'), self.assertRaises(RuntimeError):
                run.return_value.stdout = json.dumps({'outputs': values})
                self.desktop.read()
            self.assertEqual(run.call_count, 1)

    def test_backend_selection_and_separate_backups(self):
        with patch.dict('os.environ', {'XDG_CURRENT_DESKTOP': 'KDE'}):
            client = Client()
        self.assertIsInstance(client.displays, KdeDisplays)
        self.assertIsInstance(client.internal_displays, KdeDisplays)
        self.assertNotEqual(client.displays.backup_path, client.internal_displays.backup_path)
        with patch.dict('os.environ', {'XDG_CURRENT_DESKTOP': 'ubuntu:GNOME'}):
            self.assertNotIsInstance(Client().displays, KdeDisplays)
