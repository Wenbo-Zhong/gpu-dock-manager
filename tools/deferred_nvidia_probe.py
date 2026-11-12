#!/usr/bin/python3
"""Compare AMD-only, NVIDIA GBM-only, and mixed EGL use in a disposable child.

Uses surfaceless GLES contexts. No windows, drawing, modesets or GNOME mutation.
"""
import argparse
import ctypes as C
from datetime import datetime
import json
import mmap
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gbm_context_probe import probe, snapshot


def card_for(pci):
    cards = [p for p in (Path('/sys/bus/pci/devices')/pci/'drm').glob('card[0-9]*') if '-' not in p.name]
    if len(cards) != 1:
        raise RuntimeError('Expected exactly one DRM card: ' + pci)
    return '/dev/dri/' + cards[0].name


def run_case(amd, nvidia, case):
    def observe(egl, display, context, gles):
        result = {'case': case, 'samples': [snapshot('amd-current-before-nvidia')]}
        egl.eglGetCurrentContext.restype = C.c_void_p
        egl.eglGetCurrentContext.argtypes = []
        if egl.eglGetCurrentContext() != context:
            raise RuntimeError('AMD context not current before experiment')
        expected_renderer = gles.glGetString(0x1F01)
        result['amd_renderer_before'] = expected_renderer.decode(errors='replace') if expected_renderer else None
        if case in ('amd-plus-nvidia-kms', 'amd-plus-nvidia-dumb-buffer'):
            class Resources(C.Structure):
                _fields_ = [('count_fbs', C.c_int), ('fbs', C.POINTER(C.c_uint32)),
                    ('count_crtcs', C.c_int), ('crtcs', C.POINTER(C.c_uint32)),
                    ('count_connectors', C.c_int), ('connectors', C.POINTER(C.c_uint32)),
                    ('count_encoders', C.c_int), ('encoders', C.POINTER(C.c_uint32)),
                    ('min_width', C.c_uint32), ('max_width', C.c_uint32),
                    ('min_height', C.c_uint32), ('max_height', C.c_uint32)]
            drm = C.CDLL('libdrm.so.2')
            drm.drmModeGetResources.restype, drm.drmModeGetResources.argtypes = C.POINTER(Resources), [C.c_int]
            drm.drmModeFreeResources.restype, drm.drmModeFreeResources.argtypes = None, [C.POINTER(Resources)]
            drm.drmModeGetConnectorCurrent.restype, drm.drmModeGetConnectorCurrent.argtypes = C.c_void_p, [C.c_int, C.c_uint32]
            drm.drmModeFreeConnector.restype, drm.drmModeFreeConnector.argtypes = None, [C.c_void_p]
            fd = os.open(card_for(nvidia), os.O_RDWR | os.O_CLOEXEC)
            resources = None
            try:
                resources = drm.drmModeGetResources(fd)
                if not resources:
                    raise RuntimeError('DRM get resources failed')
                count = resources.contents.count_connectors
                if not 0 <= count <= 128:
                    raise RuntimeError('Unexpected connector count')
                result['connectors_queried'] = []
                for index in range(count):
                    connector_id = resources.contents.connectors[index]
                    connector = drm.drmModeGetConnectorCurrent(fd, connector_id)
                    if not connector:
                        raise RuntimeError('DRM cached connector query failed')
                    drm.drmModeFreeConnector(connector)
                    result['connectors_queried'].append(connector_id)
                result['samples'].append(snapshot('after-nvidia-kms-query'))
                if case == 'amd-plus-nvidia-dumb-buffer':
                    drm.drmModeCreateDumbBuffer.restype = C.c_int
                    drm.drmModeCreateDumbBuffer.argtypes = [C.c_int, C.c_uint32, C.c_uint32, C.c_uint32,
                        C.c_uint32, C.POINTER(C.c_uint32), C.POINTER(C.c_uint32), C.POINTER(C.c_uint64)]
                    drm.drmModeDestroyDumbBuffer.restype = C.c_int
                    drm.drmModeDestroyDumbBuffer.argtypes = [C.c_int, C.c_uint32]
                    drm.drmModeMapDumbBuffer.restype = C.c_int
                    drm.drmModeMapDumbBuffer.argtypes = [C.c_int, C.c_uint32, C.POINTER(C.c_uint64)]
                    handle, pitch, size, offset = C.c_uint32(), C.c_uint32(), C.c_uint64(), C.c_uint64()
                    status = drm.drmModeCreateDumbBuffer(fd, 64, 64, 32, 0, C.byref(handle), C.byref(pitch), C.byref(size))
                    if status:
                        raise RuntimeError('DRM dumb allocation failed: ' + str(status))
                    try:
                        result['samples'].append(snapshot('after-private-dumb-allocation'))
                        if not 64 * 64 * 4 <= size.value <= 16 * 1024 * 1024:
                            raise RuntimeError('Unexpected private buffer size')
                        status = drm.drmModeMapDumbBuffer(fd, handle, C.byref(offset))
                        if status:
                            raise RuntimeError('DRM dumb mapping setup failed: ' + str(status))
                        with mmap.mmap(fd, size.value, flags=mmap.MAP_SHARED,
                                       prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=offset.value) as memory:
                            memory[:4] = b'\x00\x80\xff\x00'
                            result['private_buffer_write_verified'] = memory[:4] == b'\x00\x80\xff\x00'
                            result['samples'].append(snapshot('after-private-dumb-map-and-write'))
                        result['private_buffer'] = {'width': 64, 'height': 64, 'pitch': pitch.value,
                                                    'size': size.value, 'submitted_to_display': False}
                    finally:
                        result['dumb_destroy_status'] = drm.drmModeDestroyDumbBuffer(fd, handle)
                    if result['dumb_destroy_status']:
                        raise RuntimeError('DRM dumb destroy failed')
                    result['samples'].append(snapshot('after-private-dumb-destroy'))
            finally:
                if resources:
                    drm.drmModeFreeResources(resources)
                os.close(fd)
            result['samples'].append(snapshot('after-nvidia-kms-card-close'))
        elif case == 'amd-plus-nvidia-gbm':
            gbm = C.CDLL('libgbm.so.1')
            gbm.gbm_create_device.restype, gbm.gbm_create_device.argtypes = C.c_void_p, [C.c_int]
            gbm.gbm_device_destroy.restype, gbm.gbm_device_destroy.argtypes = None, [C.c_void_p]
            device = None
            fd = os.open(card_for(nvidia), os.O_RDWR | os.O_CLOEXEC)
            try:
                result['samples'].append(snapshot('after-nvidia-card-open'))
                device = gbm.gbm_create_device(fd)
                if not device:
                    raise RuntimeError('NVIDIA gbm_create_device failed')
                result['samples'].append(snapshot('after-nvidia-gbm-create'))
            finally:
                if device:
                    gbm.gbm_device_destroy(device)
                os.close(fd)
            result['samples'].append(snapshot('after-nvidia-gbm-and-card-close'))
        elif case == 'amd-plus-nvidia-egl':
            try:
                # This deliberately changes the current context of OUR child
                # thread, then checks explicit restoration of its AMD context.
                result['nvidia_probe'] = probe(nvidia, True)
            finally:
                bound = bool(egl.eglBindAPI(0x30A0))
                restored = bound and bool(egl.eglMakeCurrent(display, None, None, context))
                result['explicit_amd_restore_succeeded'] = restored
            if not restored:
                raise RuntimeError('AMD context restoration failed')
            if result['nvidia_probe'].get('error'):
                raise RuntimeError(result['nvidia_probe']['error'])
        result['amd_context_still_current'] = egl.eglGetCurrentContext() == context
        renderer = gles.glGetString(0x1F01)
        result['amd_renderer_after'] = renderer.decode(errors='replace') if renderer else None
        if not result['amd_context_still_current'] or not renderer or renderer != expected_renderer:
            raise RuntimeError('AMD context no longer usable')
        result['samples'].append(snapshot('amd-current-after-nvidia'))
        return result

    # An independent client need not be authenticated on the compositor's
    # primary node. The render node is the appropriate hardware-rendering path.
    return probe(amd, True, observe, render_node=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--amd-pci', required=True)
    parser.add_argument('--nvidia-pci', required=True)
    parser.add_argument('--case', choices=('amd-only', 'amd-plus-nvidia-kms', 'amd-plus-nvidia-dumb-buffer', 'amd-plus-nvidia-gbm', 'amd-plus-nvidia-egl'), required=True)
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    for pci, vendor in ((args.amd_pci, '0x1002'), (args.nvidia_pci, '0x10de')):
        path = Path('/sys/bus/pci/devices') / pci
        if not re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', pci) or not path.is_dir() or (path/'vendor').read_text().strip() != vendor:
            parser.error('Invalid GPU vendor or PCI address: ' + pci)
    if os.geteuid() == 0:
        parser.error('Run as the desktop user without sudo')
    if args.child:
        result = run_case(args.amd_pci, args.nvidia_pci, args.case)
        print(json.dumps(result))
        return int('error' in result)
    env = dict(os.environ)
    for key in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'DISPLAY', 'WAYLAND_DISPLAY',
                '__EGL_VENDOR_LIBRARY_FILENAMES', '__EGL_VENDOR_LIBRARY_DIRS',
                '__NV_PRIME_RENDER_OFFLOAD', 'DRI_PRIME', 'LIBGL_ALWAYS_SOFTWARE', 'GALLIUM_DRIVER'):
        env.pop(key, None)
    report = {'time': datetime.now().isoformat(), 'case': args.case,
              'amd_pci': args.amd_pci, 'nvidia_pci': args.nvidia_pci,
              'egl_vendor_policy': 'system-default; both vendors available',
              'scope': 'independent child; no GNOME mutation'}
    try:
        done = subprocess.run(['/usr/bin/python3', '-I', str(Path(__file__).resolve()),
            '--amd-pci', args.amd_pci, '--nvidia-pci', args.nvidia_pci, '--case', args.case, '--child'],
            env=env, capture_output=True, text=True, timeout=20)
        report.update(exit_code=done.returncode, stderr=done.stderr[-4000:])
        report['result'] = json.loads(done.stdout) if done.stdout else {}
    except (subprocess.TimeoutExpired, ValueError) as error:
        report['error'] = str(error)
    directory = Path(os.environ.get('XDG_STATE_HOME') or Path.home()/'.local/state')/'usb4-gpu-manager/poc-reports'
    directory.mkdir(parents=True, exist_ok=True)
    output = directory/('deferred-nvidia-' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8] + '.json')
    with output.open('x') as file:
        os.chmod(output, 0o600)
        json.dump(report, file, indent=2, ensure_ascii=False)
    print('Case:', args.case, '报告：', output)
    result = report.get('result', {})
    print('AMD renderer:', result.get('renderer'))
    print('AMD check:', result.get('current_context_observation', {}).get('amd_context_still_current'))
    print('Final GPU FDs:', result.get('samples', [{}])[-1].get('fds'))
    error = report.get('error') or result.get('error')
    if error:
        print('Error:', error)
    return int(bool(error or report.get('exit_code')))


if __name__ == '__main__':
    raise SystemExit(main())
