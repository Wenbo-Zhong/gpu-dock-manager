#!/usr/bin/python3
"""Observe NVIDIA EGL device/display FD lifetimes in a disposable child process.

Creates no contexts, surfaces, windows or modesets. Never touches compositor FDs.
"""
import argparse
import ctypes as C
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def snapshot(stage):
    fds = []
    for fd in Path('/proc/self/fd').iterdir():
        try:
            node = str(fd.readlink())
            if node.startswith(('/dev/nvidia', '/dev/dri/')):
                fds.append({'fd': int(fd.name), 'node': node})
        except FileNotFoundError:
            pass
    return {'stage': stage, 'fds': sorted(fds, key=lambda f: f['fd'])}


def probe(pci):
    result = {'samples': [snapshot('before-egl-load')], 'contexts_created': False,
              'surfaces_created': False, 'modesets_requested': False}
    egl = C.CDLL('libEGL.so.1')
    egl.eglGetProcAddress.argtypes = [C.c_char_p]
    egl.eglGetProcAddress.restype = C.c_void_p
    egl.eglGetError.restype = C.c_uint
    for name in ('eglInitialize', 'eglTerminate'):
        getattr(egl, name).restype = C.c_uint
    egl.eglInitialize.argtypes = [C.c_void_p, C.POINTER(C.c_int), C.POINTER(C.c_int)]
    egl.eglTerminate.argtypes = [C.c_void_p]
    egl.eglReleaseThread.restype = C.c_uint

    def extension(name, restype, *args):
        address = egl.eglGetProcAddress(name.encode())
        if not address:
            raise RuntimeError('Missing EGL extension: ' + name)
        return C.CFUNCTYPE(restype, *args)(address)

    query = extension('eglQueryDevicesEXT', C.c_uint, C.c_int, C.POINTER(C.c_void_p), C.POINTER(C.c_int))
    query_string = extension('eglQueryDeviceStringEXT', C.c_char_p, C.c_void_p, C.c_int)
    get_display = extension('eglGetPlatformDisplayEXT', C.c_void_p, C.c_uint, C.c_void_p, C.POINTER(C.c_int))
    result['samples'].append(snapshot('after-egl-load'))
    devices, count = (C.c_void_p * 32)(), C.c_int()
    if not query(32, devices, C.byref(count)):
        raise RuntimeError('eglQueryDevicesEXT failed: ' + hex(egl.eglGetError()))
    result['samples'].append(snapshot('after-query-devices'))
    matched = []
    for device in list(devices)[:count.value]:
        # EGL_DRM_DEVICE_FILE_EXT; no need to request unsupported attributes.
        raw = query_string(device, 0x3233)
        if not raw:
            continue
        node = os.fsdecode(raw)
        if (Path('/sys/class/drm') / Path(node).name / 'device').resolve().name == pci:
            matched.append((device, node))
    if len(matched) != 1:
        raise RuntimeError('Expected one EGL device matching PCI; found ' + str(len(matched)))
    device, node = matched[0]
    result['device_node'] = node
    display = get_display(0x313F, device, None)  # EGL_PLATFORM_DEVICE_EXT
    result['samples'].append(snapshot('after-get-platform-display'))
    if not display:
        raise RuntimeError('eglGetPlatformDisplayEXT failed: ' + hex(egl.eglGetError()))
    initialized = bool(egl.eglInitialize(display, None, None))
    result['initialize_succeeded'] = initialized
    if not initialized:
        result['initialize_error'] = hex(egl.eglGetError())
    result['samples'].append(snapshot('after-initialize'))
    if initialized:
        result['terminate_succeeded'] = bool(egl.eglTerminate(display))
        if not result['terminate_succeeded']:
            result['terminate_error'] = hex(egl.eglGetError())
    result['samples'].append(snapshot('after-terminate'))
    result['release_thread_succeeded'] = bool(egl.eglReleaseThread())
    result['samples'].append(snapshot('after-release-thread'))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pci', required=True)
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    device = Path('/sys/bus/pci/devices') / args.pci
    if device.name != args.pci or not device.is_dir() or (device / 'vendor').read_text().strip() != '0x10de':
        parser.error('Expected an existing NVIDIA PCI address')
    if args.child:
        try:
            print(json.dumps(probe(args.pci)))
        except Exception as error:
            print(json.dumps({'error': str(error)}))
            return 1
        return 0
    if os.geteuid() == 0:
        parser.error('Run as the desktop user, without sudo')
    env = dict(os.environ)
    for key in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'DISPLAY', 'WAYLAND_DISPLAY'):
        env.pop(key, None)
    env['__EGL_VENDOR_LIBRARY_FILENAMES'] = '/usr/share/glvnd/egl_vendor.d/10_nvidia.json'
    report = {'time': datetime.now().isoformat(), 'pci': args.pci,
              'scope': 'independent-child; no GNOME mutation; not a GPU eject test'}
    try:
        completed = subprocess.run(['/usr/bin/python3', '-I', str(Path(__file__).resolve()),
                                    '--pci', args.pci, '--child'], env=env, capture_output=True, text=True, timeout=15)
        report.update(exit_code=completed.returncode, stderr=completed.stderr[-4000:])
        report['observation'] = json.loads(completed.stdout) if completed.stdout else {}
    except subprocess.TimeoutExpired:
        report['error'] = 'Child exceeded 15 seconds; experiment incomplete'
    directory = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'usb4-gpu-manager/poc-reports'
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / ('egl-probe-' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8] + '.json')
    with output.open('x') as file:
        os.chmod(output, 0o600)
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print('报告：', output)
    return int(bool(report.get('error') or report.get('exit_code') or report.get('observation', {}).get('error')))


if __name__ == '__main__':
    raise SystemExit(main())
