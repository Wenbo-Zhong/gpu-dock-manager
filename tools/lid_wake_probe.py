#!/usr/bin/python3
"""GPL-3.0-only. Capture one user-initiated lid suspend; restore debug settings."""
import json
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError as error:
        return 'unavailable: ' + str(error)


def snapshot():
    sources = {}
    for node in Path('/sys/class/wakeup').glob('wakeup*'):
        sources[node.name] = {key: read(node / key) for key in ('name', 'event_count', 'wakeup_count', 'active_count')}
    return {'success': read('/sys/power/suspend_stats/success'),
            'fail': read('/sys/power/suspend_stats/fail'),
            'irq': read('/sys/power/pm_wakeup_irq'), 'sources': sources,
            'gpio': read('/sys/kernel/debug/gpio'),
            'wakeup_sources': read('/sys/kernel/debug/wakeup_sources'),
            'acpi_wakeup': read('/proc/acpi/wakeup'),
            'interrupts': read('/proc/interrupts')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disable-usb4-bridge-wake', action='store_true',
                        help='Temporarily disable wakeup for the validated eGPU upstream USB4 bridge')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('请使用 sudo 执行；工具只临时开启睡眠调试，不会自动挂起或更改唤醒设备。')
    folder = Path(tempfile.mkdtemp(prefix='gpu-dock-lid-wake-', dir='/var/tmp'))
    output = folder / 'report.json'
    debug = Path('/sys/power/pm_debug_messages')
    previous = debug.read_text().strip()
    bridge = Path('/sys/bus/pci/devices/0000:00:01.1')
    wake = bridge / 'power/wakeup'
    old_wake = None
    if args.disable_usb4_bridge_wake:
        gpu = Path('/sys/bus/pci/devices/0000:03:00.0')
        if (bridge.resolve() not in gpu.resolve().parents
                or read(bridge / 'vendor') != '0x1022'
                or read(bridge / 'device') != '0x150a'
                or read(gpu / 'vendor') != '0x10de'
                or read(wake) not in {'enabled', 'disabled'}):
            raise SystemExit('USB4 桥或外置显卡身份与预期不符，未更改设置。')
        old_wake = read(wake)
    start = time.time()
    before = snapshot()
    result = {'schema': 1, 'before': before, 'started_epoch': start, 'debug_restored': False}
    result['usb4_bridge_wakeup_original'] = old_wake
    try:
        debug.write_text('1\n')
        if old_wake is not None:
            wake.write_text('disabled\n')
            if read(wake) != 'disabled':
                raise RuntimeError('未能关闭目标桥的唤醒权限。')
            print('仅本次测试：00:01.1 USB4 桥的唤醒权限已暂时关闭。', flush=True)
        print('已开启短时诊断。请保持线缆不变，合盖至少 30 秒后开盖。', flush=True)
        print('如未自动唤醒，可按电源键唤醒。Ctrl+C 可取消并恢复设置。', flush=True)
        print('报告：' + str(output), flush=True)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if (read('/sys/power/suspend_stats/success'), read('/sys/power/suspend_stats/fail')) != (before['success'], before['fail']):
                time.sleep(2)
                break
            time.sleep(1)
        else:
            result['note'] = '等待睡眠/恢复超时；未检测到计数变化。'
        after = result['after'] = snapshot()
        delta = []
        for key, values in after['sources'].items():
            old = before['sources'].get(key, {})
            changes = {}
            for field in ('event_count', 'wakeup_count', 'active_count'):
                try:
                    count = int(values[field]) - int(old.get(field, '0'))
                    if count: changes[field] = count
                except ValueError:
                    pass
            if changes: delta.append({'name': values['name'], **changes})
        result['source_deltas'] = delta
        logs = subprocess.run(['journalctl', '-k', '-b', '--since', '@' + str(int(start)), '--no-pager'],
                              capture_output=True, text=True, timeout=10)
        result['kernel_events'] = [line for line in logs.stdout.splitlines()
                                   if re.search(r'wake|gpio|pinctrl|suspend|resume|PM:|ACPI: EC|ucsi|GPE', line, re.I)]
        print('恢复后唤醒 IRQ：' + after['irq'])
        print('变化的唤醒源：' + json.dumps(delta, ensure_ascii=False))
        for line in result['kernel_events']:
            print(line)
    finally:
        if old_wake is not None:
            try:
                wake.write_text(old_wake + '\n')
                result['usb4_bridge_wakeup_restored'] = read(wake) == old_wake
            except OSError as error:
                result['usb4_bridge_wakeup_restored'] = False
                result['wake_restore_error'] = str(error)
        try:
            debug.write_text(previous + '\n')
            result['debug_restored'] = debug.read_text().strip() == previous
        except OSError as error:
            result['debug_restore_error'] = str(error)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        output.chmod(0o600)
        restored = result['debug_restored'] and result.get('usb4_bridge_wakeup_restored', True)
        print(('测试设置已恢复。报告：' if restored else '部分设置恢复失败，请查看报告：') + str(output), flush=True)


if __name__ == '__main__':
    main()
