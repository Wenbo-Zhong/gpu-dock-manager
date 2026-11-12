"""KScreen session adapter; never detach a GPU or change privileged state."""

from .i18n import tr
import copy
import hashlib
import json
import math
import re
from pathlib import Path

from .displays import DesktopDisplays, BUSY, target_outputs, internal_outputs
from .system import command, atomic_write, load_json

ROTATIONS = {1: 'none', 2: 'left', 4: 'inverted', 8: 'right', 16: 'flipped',
             32: 'flipped90', 64: 'flipped180', 128: 'flipped270'}
FIELDS = ('enabled', 'currentModeId', 'pos', 'scale', 'rotation', 'priority',
          'hdr', 'wcg', 'iccProfilePath', 'overscan', 'brightness', 'sdr-brightness',
          'replicationSource', 'followPreferredMode', 'maxBpc', 'edrPolicy')


def layout_key(state):
    return json.dumps({n: {k: o.get(k) for k in FIELDS} for n, o in state.items()}, sort_keys=True)


def identities(state):
    return {name: output['identity'] for name, output in state.items()}


def output_identity(name):
    matches = list(Path('/sys/class/drm').glob('card[0-9]*-' + name))
    if len(matches) != 1:
        raise RuntimeError(tr('Cannot uniquely map the KDE output to DRM: ') + name)
    edid = (matches[0] / 'edid').read_bytes()
    if len(edid) < 128 or len(edid) % 128:
        raise RuntimeError(tr('Cannot read the complete monitor EDID: ') + name)
    return hashlib.sha256(edid).hexdigest()


class KdeDisplays(DesktopDisplays):
    def __init__(self, backup_path=None, *, internal=False):
        super().__init__(backup_path, internal=internal)
        if backup_path is None:
            self.backup_path = self.backup_path.with_name('display-layout-kde-internal.json' if internal else 'display-layout-kde.json')

    def read(self):
        raw = json.loads(command(['kscreen-doctor', '-j'], timeout=5).stdout)
        outputs = raw.get('outputs')
        if not isinstance(outputs, list):
            raise RuntimeError(tr('KScreen returned no valid monitor list.'))
        state = {}
        ids = set()
        for value in outputs:
            if not value.get('connected'):
                continue
            o = copy.deepcopy(value)
            name, number = o.get('name', ''), o.get('id')
            if (not re.fullmatch(r'[A-Za-z0-9_-]+', name) or name in state
                    or type(number) is not int or number < 1 or number in ids):
                raise RuntimeError(tr('KDE output names or IDs are not unique.'))
            if o.get('replicationSource', 0) or o.get('clones'):
                raise RuntimeError(tr('KDE mirrored layouts are not supported yet. Switch to an extended layout in System Settings.'))
            if type(o.get('enabled')) is not bool:
                raise RuntimeError(tr('The KDE output enable state is unknown.'))
            if o['enabled']:
                if o.get('followPreferredMode'):
                    raise RuntimeError(tr('KDE layouts that automatically follow the preferred mode are not supported yet. Select a fixed resolution first.'))
                mode = str(o.get('currentModeId', ''))
                if not re.fullmatch(r'[0-9]+', mode) or not any(str(m['id']) == mode for m in o.get('modes', [])):
                    raise RuntimeError(tr('Cannot read the current display mode.'))
                if o.get('rotation') not in ROTATIONS or not 0 < float(o.get('scale', 0)) <= 8 or not math.isfinite(float(o['scale'])):
                    raise RuntimeError(tr('Invalid display rotation or scale.'))
                if any(type(o.get('pos', {}).get(axis)) is not int for axis in ('x', 'y')):
                    raise RuntimeError(tr('Invalid monitor position.'))
                if type(o.get('priority')) is not int or o['priority'] < 1:
                    raise RuntimeError(tr('Cannot read monitor priority.'))
            o['identity'] = output_identity(name)
            ids.add(number)
            state[name] = o
        return state

    @staticmethod
    def ready(data):
        if not data.get('service_connected') or not data.get('can_mutate_devices'):
            raise RuntimeError(tr('Device status is unavailable or stale. Restore status collection first.'))
        if data.get('operation', {}).get('state') in BUSY:
            raise RuntimeError(tr('A GPU operation is in progress. Wait or cancel the queued request.'))

    def disable_outputs(self, state, data):
        self.ready(data)
        outputs = target_outputs(data)
        if not outputs.issubset(state):
            raise RuntimeError(tr('Kernel and KDE monitor reports differ. Refresh and try again.'))
        if any(state[n].get('type') == 7 for n in outputs):
            raise RuntimeError(tr('The target is identified as an internal panel; disabling refused.'))
        active = {n for n, o in state.items() if o['enabled']}
        if not outputs.intersection(active):
            raise RuntimeError(tr('This GPU has no active external monitor outputs.'))
        if not internal_outputs(data).intersection(active - outputs):
            raise RuntimeError(tr("Enable another GPU's internal panel in KDE Display Settings first."))
        return sorted(outputs.intersection(active))

    def _apply_layout(self, current, wanted):
        args = ['kscreen-doctor']
        for name, output in wanted.items():
            prefix = f'output.{current[name]["id"]}.'
            if not output['enabled']:
                args.append(prefix + 'disable')
                continue
            args.extend([prefix + 'enable', prefix + 'mode.' + str(output['currentModeId']),
                         prefix + 'position.' + str(output['pos']['x']) + ',' + str(output['pos']['y']),
                         prefix + 'scale.' + str(output['scale']),
                         prefix + 'rotation.' + ROTATIONS[output['rotation']],
                         prefix + 'priority.' + str(output['priority'])])
        command(args, timeout=8)

    def _restorable(self, state, data, backup):
        try:
            self.ready(data)
            gpu = data.get('target')
            target_outputs(data)
            return bool(gpu and backup and backup.get('backend') == 'kde'
                        and backup['target'] == {k: gpu[k] for k in ('pci', 'vendor', 'device')}
                        and backup['identities'] == identities(state)
                        and layout_key(backup['after']) == layout_key(state)
                        and internal_outputs(data).intersection(n for n, o in state.items() if o['enabled']))
        except (RuntimeError, KeyError, TypeError):
            return False

    def status(self, data):
        with self.lock:
            try:
                state = self.read()
                outputs = target_outputs(data)
                summary = {'supported': True, 'backend': 'kde', 'can_disable': False,
                           'can_restore': self._restorable(state, data, load_json(self.backup_path)),
                           'monitors': [{'connector': n, 'name': n, 'active': state[n]['enabled']}
                                        for n in sorted(outputs) if n in state], 'reason': ''}
                try:
                    self.disable_outputs(state, data)
                    summary['can_disable'] = True
                except RuntimeError as error:
                    summary['reason'] = str(error)
                return summary
            except Exception as error:
                return {'supported': False, 'backend': 'kde', 'can_disable': False, 'can_restore': False,
                        'monitors': [], 'reason': tr('Cannot control KDE monitors: ') + str(error)}

    def disable(self, data, expected_pci):
        with self.lock:
            if (data.get('target') or {}).get('pci') != expected_pci:
                raise RuntimeError(tr('The target GPU changed; monitor disabling cancelled.'))
            before = self.read()
            outputs = self.disable_outputs(before, data)
            gpu = data['target']
            backup = {'backend': 'kde', 'target': {k: gpu[k] for k in ('pci', 'vendor', 'device')},
                      'identities': identities(before), 'before': before, 'after': None}
            atomic_write(self.backup_path, json.dumps(backup), 0o600)
            # Re-read immediately before applying to avoid overwriting intervening changes.
            current = self.read()
            if identities(current) != identities(before) or layout_key(current) != layout_key(before):
                raise RuntimeError(tr('Monitor connections or layout changed. Refresh and try again.'))
            try:
                command(['kscreen-doctor', *[f'output.{current[n]["id"]}.disable' for n in outputs]], timeout=8)
                after = self.read()
                if identities(after) != identities(before):
                    raise RuntimeError(tr('Monitor connections changed while disabling outputs.'))
                if any(after[n]['enabled'] for n in outputs):
                    raise RuntimeError(tr('KDE has not confirmed that the target outputs are disabled.'))
                for n in set(before) - set(outputs):
                    for field in FIELDS:
                        if field not in {'pos', 'priority'} and before[n].get(field) != after[n].get(field):
                            raise RuntimeError(tr("KDE unexpectedly changed another monitor's settings."))
                if not internal_outputs(data).intersection(n for n, o in after.items() if o['enabled']):
                    raise RuntimeError(tr('KDE has not confirmed that the internal panel remains enabled.'))
                backup['after'] = after
                atomic_write(self.backup_path, json.dumps(backup), 0o600)
            except Exception as error:
                try:
                    current = self.read()
                    if identities(current) != identities(before):
                        raise RuntimeError(tr('Display topology changed. The backup is retained; restore the layout in KDE Display Settings.'))
                    self._apply_layout(current, before)
                    if layout_key(self.read()) != layout_key(before):
                        raise RuntimeError(tr('Layout restoration was not confirmed.'))
                except Exception as rollback:
                    raise RuntimeError(tr('{p0}; layout recovery failed: {p1}').format(p0=error, p1=rollback)) from error
                raise RuntimeError(tr('{p0}; the original layout was restored.').format(p0=error)) from error
            return tr('KDE external outputs disabled. The GPU remains connected; check holders again before safe removal.')

    def restore(self, data, expected_pci=None):
        with self.lock:
            if expected_pci is not None and (data.get('target') or {}).get('pci') != expected_pci:
                raise RuntimeError(tr('The target GPU changed; monitor restoration cancelled.'))
            current, backup = self.read(), load_json(self.backup_path)
            if not self._restorable(current, data, backup):
                raise RuntimeError(tr('Monitor identity or layout changed. Restore outputs in KDE Display Settings.'))
            # Require stored modes still to exist before enabling any output.
            for name, o in backup['before'].items():
                if o['enabled']:
                    old_mode = next((m for m in o['modes'] if str(m['id']) == str(o['currentModeId'])), None)
                    new_mode = next((m for m in current[name]['modes'] if str(m['id']) == str(o['currentModeId'])), None)
                    if old_mode is None or old_mode != new_mode:
                        raise RuntimeError(tr('The original display mode is unavailable or has changed; restoration refused.'))
            self._apply_layout(current, backup['before'])
            if layout_key(self.read()) != layout_key(backup['before']):
                raise RuntimeError(tr('KDE has not confirmed layout restoration; the backup is retained.'))
            self.backup_path.unlink(missing_ok=True)
            return tr('KDE monitor layout restored.')
