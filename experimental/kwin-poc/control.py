#!/usr/bin/python3
"""GPL-3.0-only. Read-only inspection, or one explicitly requested DRM change event."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from usb4_gpu_manager.hardware import snapshot
from usb4_gpu_manager.system import command


def validate(data):
    gpu = data.get('target')
    if (not gpu or gpu.get('vendor') != '0x10de' or not gpu.get('external')
            or gpu.get('driver') != 'nvidia' or gpu.get('boot_vga')
            or not re.fullmatch(r'card[0-9]+', gpu.get('card', ''))):
        raise RuntimeError('要求唯一、已加载、非启动卡的 NVIDIA 外置显卡。')
    connectors = [c for c in gpu.get('connectors', []) if c.get('kind') != 'virtual']
    if not connectors or any(c.get('status') != 'disconnected' or c.get('enabled') != 'disabled'
                             for c in connectors):
        raise RuntimeError('要求目标所有物理接口明确未接屏且已停用。')
    if not any(g.get('vendor') == '0x1002' and g.get('driver') == 'amdgpu'
               and g.get('pci') != gpu['pci'] and any(c.get('kind') == 'internal'
               and c.get('status') == 'connected' and c.get('enabled') == 'enabled'
               for c in g.get('connectors', [])) for g in data.get('gpus', [])):
        raise RuntimeError('要求 AMD 内屏已启用。')
    occupied = data.get('holders', {})
    if not occupied.get('complete'):
        raise RuntimeError('占用检查不完整，拒绝发送事件。')
    if not any(p['name'] == 'kwin_wayland' for p in occupied.get('processes', [])):
        raise RuntimeError('未检测到 KWin 持有目标显卡，无需执行此实验。')
    allowed = {'systemd', 'systemd-logind', 'kwin_wayland', 'Xwayland'}
    other = [p for p in occupied.get('processes', []) if p['name'] not in allowed]
    if other:
        raise RuntimeError('仍有其他应用占用：' + '、'.join(p['name'] for p in other))
    return gpu


def summary(data):
    gpu = data.get('target') or {}
    return {'pci': gpu.get('pci'), 'card': gpu.get('card'), 'connectors': gpu.get('connectors'),
            'holders': data.get('holders'), 'detach': data.get('detach'),
            'safe_to_unplug': False}


def main():
    parser = argparse.ArgumentParser(description='KWin 外置 GPU 引用实验；不移除 PCI、不切换驱动、不重启桌面。')
    parser.add_argument('action', choices=('inspect', 'refresh'), nargs='?', default='inspect')
    args = parser.parse_args()
    if args.action == 'inspect':
        from usb4_gpu_manager.client import Client
        data = Client().device_status()
        value = summary(data)
        value['fresh'] = bool(data.get('service_connected') and data.get('can_mutate_devices')
                              and not data.get('status_stale'))
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if os.geteuid() != 0:
        raise RuntimeError('refresh 需要 sudo；inspect 不需要提权。')
    before = snapshot(metrics=False)
    gpu = validate(before)
    path = Path('/sys/class/drm') / gpu['card']
    if (path / 'device').resolve().name != gpu['pci']:
        raise RuntimeError('DRM 与 PCI 映射已变化。')
    # Use a second complete snapshot to reject changed hardware or new workloads.
    current = snapshot(metrics=False)
    selected = validate(current)
    if (selected['pci'], selected['card']) != (gpu['pci'], gpu['card']):
        raise RuntimeError('目标已变化，取消事件。')
    if (path / 'device').resolve().name != gpu['pci']:
        raise RuntimeError('DRM 与 PCI 映射已变化。')
    command(['udevadm', 'trigger', '--action=change', str(path)], timeout=5)
    command(['udevadm', 'settle', '--timeout=5'], timeout=6)
    samples = []
    for _ in range(3):
        time.sleep(1)
        sample = snapshot(metrics=False)
        candidate = sample.get('target') or {}
        if (candidate.get('pci'), candidate.get('card')) != (gpu['pci'], gpu['card']):
            raise RuntimeError('采样时设备身份已变化，请重新检查。')
        samples.append(summary(sample))
    print(json.dumps({'action': 'one-drm-change-event', 'before': summary(before), 'after': samples,
                      'safe_to_unplug': False}, ensure_ascii=False, indent=2))
    print('只发送了一次 DRM change 事件。显卡尚未移除，请回管理器重新检查并安全移除。')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit(str(error)) from None
