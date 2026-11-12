from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from subprocess import CompletedProcess

from usb4_gpu_manager import internal_gpu
from usb4_gpu_manager.client import Client

PCI = '0000:65:00.0'


def result(code=0):
    return CompletedProcess([], code, '', '')


class ReleaseTests(unittest.TestCase):
    def status(self, processes):
        return {'graphics_driver': 'nvidia', 'audio_driver': 'snd_hda_intel',
                'holders': {'complete': True, 'processes': processes}}

    def test_only_known_service_and_control_holders_are_preparable(self):
        processes = [{'name': 'nvidia-powerd', 'pid': 1, 'nodes': ['/dev/nvidia0']},
                     {'name': 'wireplumber', 'pid': 2, 'nodes': ['/dev/snd/controlC2']}]
        self.assertEqual(internal_gpu.internal_switch_blocker(self.status(processes), 'internal-detach'), '')
        for process in ({'name': 'ChatGPT', 'pid': 3, 'nodes': ['/dev/nvidia0']},
                        {'name': 'wireplumber', 'pid': 2, 'nodes': ['/dev/snd/pcmC2D0p']}):
            self.assertTrue(internal_gpu.internal_switch_blocker(self.status([process]), 'internal-detach'))
        self.assertTrue(internal_gpu.internal_switch_blocker(
            {**self.status(processes), 'holders': {'complete': False, 'processes': processes}}, 'internal-detach'))

    def test_audio_service_restored_on_success_authorization_failure_and_stop_failure(self):
        for failure in (None, RuntimeError('authorization denied'), RuntimeError('stop failed')):
            with self.subTest(failure=failure):
                client = Client.__new__(Client)
                client.internal_gpu_status = Mock(return_value=self.status([
                    {'name': 'wireplumber', 'pid': 2, 'nodes': ['/dev/snd/controlC2']}]))
                client.call = Mock(return_value='done', side_effect=failure if str(failure) == 'authorization denied' else None)
                def command(args, **kwargs):
                    if 'stop' in args and str(failure) == 'stop failed':
                        raise failure
                    return result()
                with patch('usb4_gpu_manager.client.command', side_effect=command) as run:
                    if failure:
                        with self.assertRaises(RuntimeError):
                            client.detach_internal({'expected_pci': PCI})
                    else:
                        self.assertEqual(client.detach_internal({'expected_pci': PCI}), 'done')
                self.assertEqual(run.call_args.args[0], ['systemctl', '--user', 'start', 'wireplumber.service'])
                if str(failure) == 'stop failed':
                    client.call.assert_not_called()

    def test_other_application_blocks_before_any_service_change(self):
        client = Client.__new__(Client)
        client.internal_gpu_status = Mock(return_value=self.status([
            {'name': 'ptyxis', 'pid': 3, 'nodes': ['/dev/nvidia0']}]))
        with patch('usb4_gpu_manager.client.command') as run, self.assertRaises(RuntimeError):
            client.detach_internal({'expected_pci': PCI})
        run.assert_not_called()

    def test_power_service_restored_on_failure_and_on_later_attach(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'paused'
            with (patch.object(internal_gpu, 'POWERD_MARKER', marker),
                  patch.object(internal_gpu, 'device_pair', return_value=(Path('gpu'), Path('audio'), Path('group'))),
                  patch.object(internal_gpu, 'driver', return_value='nvidia'),
                  patch.object(internal_gpu, 'command', return_value=result()) as run,
                  patch.object(internal_gpu, 'switch_internal_gpu') as switch):
                switch.side_effect = RuntimeError('still occupied')
                with self.assertRaisesRegex(RuntimeError, 'still occupied'):
                    internal_gpu.managed_internal_switch(PCI, 'internal-detach')
                self.assertFalse(marker.exists())
                self.assertEqual(run.call_args.args[0], ['systemctl', 'start', 'nvidia-powerd.service'])
                switch.side_effect = None
                internal_gpu.managed_internal_switch(PCI, 'internal-detach')
                self.assertTrue(marker.exists())
                internal_gpu.managed_internal_switch(PCI, 'internal-attach')
                self.assertFalse(marker.exists())
                self.assertEqual(run.call_args.args[0], ['systemctl', 'start', 'nvidia-powerd.service'])

    def test_existing_inactive_power_service_is_not_started(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch.object(internal_gpu, 'POWERD_MARKER', Path(directory) / 'paused'),
                  patch.object(internal_gpu, 'device_pair', return_value=(Path('gpu'), Path('audio'), Path('group'))),
                  patch.object(internal_gpu, 'driver', return_value='nvidia'),
                  patch.object(internal_gpu, 'command', return_value=result(3)) as run,
                  patch.object(internal_gpu, 'switch_internal_gpu')):
                internal_gpu.managed_internal_switch(PCI, 'internal-detach')
                internal_gpu.managed_internal_switch(PCI, 'internal-attach')
                run.assert_called_once()
