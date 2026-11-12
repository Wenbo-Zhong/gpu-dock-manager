#!/usr/bin/python3
"""Compare repeated GBM context cleanup on one thread versus fresh threads.

All EGL work happens in a disposable child. No compositor resources are changed.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gbm_context_probe import probe, snapshot


def series(pci, count, fresh_thread):
    result = {'fresh_thread': fresh_thread, 'cycles': []}
    for index in range(count):
        holder = []

        def worker():
            try:
                holder.append(probe(pci, True))
            except Exception as error:
                holder.append({'error': str(error)})

        if fresh_thread:
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()  # Parent process enforces the whole-child timeout.
        else:
            worker()
        cycle = {'index': index + 1, 'observation': holder[0],
                 'after_worker_return': snapshot('after-worker-return')}
        result['cycles'].append(cycle)
        if holder[0].get('error'):
            result['error'] = holder[0]['error']
            break
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pci', required=True)
    parser.add_argument('--cycles', type=int, choices=range(1, 6), default=3)
    parser.add_argument('--fresh-thread', action='store_true')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    pci = Path('/sys/bus/pci/devices') / args.pci
    if not re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', args.pci) or not pci.is_dir() or (pci/'vendor').read_text().strip() != '0x10de':
        parser.error('Expected an existing NVIDIA PCI address')
    if os.geteuid() == 0:
        parser.error('Run as the desktop user, without sudo')
    if args.child:
        result = series(args.pci, args.cycles, args.fresh_thread)
        print(json.dumps(result))
        return int('error' in result)
    env = dict(os.environ)
    for key in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'DISPLAY', 'WAYLAND_DISPLAY'):
        env.pop(key, None)
    env['__EGL_VENDOR_LIBRARY_FILENAMES'] = '/usr/share/glvnd/egl_vendor.d/10_nvidia.json'
    command = ['/usr/bin/python3', '-I', str(Path(__file__).resolve()), '--pci', args.pci,
               '--cycles', str(args.cycles), '--child']
    if args.fresh_thread:
        command.append('--fresh-thread')
    report = {'time': datetime.now().isoformat(), 'pci': args.pci, 'fresh_thread': args.fresh_thread,
              'requested_cycles': args.cycles, 'scope': 'disposable child, no compositor mutation'}
    try:
        done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=25)
        report.update(exit_code=done.returncode, stderr=done.stderr[-4000:])
        report['result'] = json.loads(done.stdout) if done.stdout else {}
    except (subprocess.TimeoutExpired, ValueError) as error:
        report['error'] = str(error)
    directory = Path(os.environ.get('XDG_STATE_HOME') or Path.home()/'.local/state') / 'usb4-gpu-manager/poc-reports'
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / ('gbm-series-' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8] + '.json')
    with output.open('x') as file:
        os.chmod(output, 0o600)
        json.dump(report, file, indent=2, ensure_ascii=False)
    print('报告：', output)
    for cycle in report.get('result', {}).get('cycles', []):
        samples = cycle['observation'].get('samples', [])
        print('cycle', cycle['index'], 'before=', len(samples[0]['fds']) if samples else None,
              'after=', len(cycle['after_worker_return']['fds']),
              'error=', cycle['observation'].get('error'))
    error = report.get('error') or report.get('result', {}).get('error')
    if error:
        print('Error:', error)
    return int(bool(error or report.get('exit_code')))


if __name__ == '__main__':
    raise SystemExit(main())
