#!/usr/bin/python3
"""Reproduce Mutter's GBM/GLES detection in a disposable process, without windows.

No modesets, surfaces, rendering, compositor injection or compositor FD changes.
The optional context is current only on the probe thread, with no EGL surfaces.
"""
import argparse
import ctypes as C
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from egl_reference_probe import snapshot


def probe(pci, with_context, observe_current=None, render_node=False):
    result = {'samples': [], 'with_context': with_context, 'surfaces_created': False,
              'modesets_requested': False, 'cleanup': {}}
    sample = lambda stage: result['samples'].append(snapshot(stage))
    sample('before-library-load')
    fd, device, display, context = None, None, None, None
    initialized = current = False
    egl = gbm = None
    try:
        egl = C.CDLL('libEGL.so.1')
        gbm = C.CDLL('libgbm.so.1')
        gles = C.CDLL('libGLESv2.so.2')
        signatures = {
            'eglGetProcAddress': (C.c_void_p, [C.c_char_p]),
            'eglGetError': (C.c_uint, []),
            'eglBindAPI': (C.c_uint, [C.c_uint]),
            'eglInitialize': (C.c_uint, [C.c_void_p, C.c_void_p, C.c_void_p]),
            'eglCreateContext': (C.c_void_p, [C.c_void_p, C.c_void_p, C.c_void_p, C.POINTER(C.c_int)]),
            'eglMakeCurrent': (C.c_uint, [C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p]),
            'eglDestroyContext': (C.c_uint, [C.c_void_p, C.c_void_p]),
            'eglTerminate': (C.c_uint, [C.c_void_p]),
            'eglReleaseThread': (C.c_uint, []),
        }
        for name, (restype, args) in signatures.items():
            fn = getattr(egl, name)
            fn.restype, fn.argtypes = restype, args
        gbm.gbm_create_device.restype, gbm.gbm_create_device.argtypes = C.c_void_p, [C.c_int]
        gbm.gbm_device_destroy.restype, gbm.gbm_device_destroy.argtypes = None, [C.c_void_p]
        gles.glGetString.restype, gles.glGetString.argtypes = C.c_char_p, [C.c_uint]
        sample('after-library-load')

        def require(value, operation):
            if not value:
                raise RuntimeError(operation + ' failed, EGL error=' + hex(egl.eglGetError()))
            return value

        pattern = 'renderD[0-9]*' if render_node else 'card[0-9]*'
        cards = [p for p in (Path('/sys/bus/pci/devices') / pci / 'drm').glob(pattern) if '-' not in p.name]
        if len(cards) != 1:
            raise RuntimeError('Expected exactly one DRM card for target PCI')
        node = '/dev/dri/' + cards[0].name
        result['card_node'] = node
        result['drm_node_kind'] = 'render' if render_node else 'primary'
        fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
        sample('after-card-open')
        device = gbm.gbm_create_device(fd)
        if not device:
            raise RuntimeError('gbm_create_device failed')
        sample('after-gbm-create')
        require(egl.eglBindAPI(0x30A0), 'eglBindAPI(GLES)')
        address = require(egl.eglGetProcAddress(b'eglGetPlatformDisplayEXT'), 'get platform entry point')
        get_display = C.CFUNCTYPE(C.c_void_p, C.c_uint, C.c_void_p, C.POINTER(C.c_int))(address)
        display = require(get_display(0x31D7, device, None), 'get GBM platform display')
        sample('after-get-platform-display')
        require(egl.eglInitialize(display, None, None), 'eglInitialize')
        initialized = True
        sample('after-initialize')
        if with_context:
            # Same GLES2, no-config attributes as detect_hardware_rendering().
            attributes = (C.c_int * 3)(0x3098, 2, 0x3038)
            context = require(egl.eglCreateContext(display, None, None, attributes), 'eglCreateContext')
            sample('after-create-context')
            require(egl.eglMakeCurrent(display, None, None, context), 'eglMakeCurrent')
            current = True
            sample('after-make-current')
            renderer = gles.glGetString(0x1F01)
            if not renderer:
                raise RuntimeError('glGetString(GL_RENDERER) returned NULL')
            result['renderer'] = renderer.decode(errors='replace')
            sample('after-renderer-query')
            if observe_current is not None:
                result['current_context_observation'] = observe_current(egl, display, context, gles)
                sample('after-current-context-observation')
    except Exception as error:
        result['error'] = str(error)
    finally:
        # Retain the context if unbind fails; never destroy a current context.
        if current:
            ok = bool(egl.eglMakeCurrent(display, None, None, None))
            result['cleanup']['unbind'] = ok
            current = not ok
            sample('after-unbind')
        if context and not current:
            result['cleanup']['destroy_context'] = bool(egl.eglDestroyContext(display, context))
            sample('after-destroy-context')
        if initialized and not current:
            result['cleanup']['terminate'] = bool(egl.eglTerminate(display))
            sample('after-terminate')
        if device and not current:
            gbm.gbm_device_destroy(device)
            result['cleanup']['destroy_gbm'] = True
            sample('after-gbm-destroy')
        if fd is not None and not current:
            os.close(fd)
            sample('after-card-close')
        if egl:
            result['cleanup']['release_thread'] = bool(egl.eglReleaseThread())
            sample('after-release-thread')
        if any(not ok for ok in result['cleanup'].values()):
            result.setdefault('error', 'A cleanup call failed; do not infer successful release')
        time.sleep(1)
        sample('one-second-after-cleanup')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pci', required=True)
    parser.add_argument('--with-context', action='store_true')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    pci = Path('/sys/bus/pci/devices') / args.pci
    if not re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', args.pci) or not pci.is_dir() or (pci/'vendor').read_text().strip() != '0x10de':
        parser.error('Expected an existing NVIDIA PCI address')
    if os.geteuid() == 0:
        parser.error('Run without sudo as the desktop user')
    if args.child:
        result = probe(args.pci, args.with_context)
        print(json.dumps(result))
        return int('error' in result)
    report = {'time': datetime.now().isoformat(), 'pci': args.pci,
              'scope': 'independent GBM child; no compositor mutation', 'with_context': args.with_context}
    env = dict(os.environ)
    for name in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'DISPLAY', 'WAYLAND_DISPLAY'):
        env.pop(name, None)
    env['__EGL_VENDOR_LIBRARY_FILENAMES'] = '/usr/share/glvnd/egl_vendor.d/10_nvidia.json'
    command = ['/usr/bin/python3', '-I', str(Path(__file__).resolve()), '--pci', args.pci, '--child']
    if args.with_context:
        command.append('--with-context')
    try:
        completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        report.update(exit_code=completed.returncode, stderr=completed.stderr[-4000:])
        report['observation'] = json.loads(completed.stdout) if completed.stdout else {}
    except (subprocess.TimeoutExpired, ValueError) as error:
        report['error'] = str(error)
    directory = Path(os.environ.get('XDG_STATE_HOME') or Path.home()/'.local/state') / 'usb4-gpu-manager/poc-reports'
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / ('gbm-context-' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8] + '.json')
    with output.open('x') as file:
        os.chmod(output, 0o600)
        json.dump(report, file, indent=2, ensure_ascii=False)
    print('报告：', output)
    for sample in report.get('observation', {}).get('samples', []):
        print(sample['stage'], len(sample['fds']))
    if report.get('observation', {}).get('renderer'):
        print('Renderer:', report['observation']['renderer'])
    print('Cleanup:', report.get('observation', {}).get('cleanup'))
    error = report.get('error') or report.get('observation', {}).get('error')
    if error:
        print('Error:', error)
    return int(bool(error or report.get('exit_code')))


if __name__ == '__main__':
    raise SystemExit(main())
