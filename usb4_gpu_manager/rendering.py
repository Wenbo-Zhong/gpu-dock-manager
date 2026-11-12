"""Force the built-in panel's AMD/Intel Mesa GPU for the manager UI."""
from pathlib import Path
import os


def configure_renderer(environ=None, sysfs=Path('/sys'), mesa=Path('/usr/share/glvnd/egl_vendor.d/50_mesa.json')):
    env = os.environ if environ is None else environ
    candidates = []
    for device in (sysfs / 'bus/pci/devices').glob('*'):
        try:
            vendor = (device / 'vendor').read_text().strip()
            driver = (device / 'driver').resolve().name
            if (vendor, driver) not in {('0x1002', 'amdgpu'), ('0x8086', 'i915'), ('0x8086', 'xe')}:
                continue
            if any((sysfs / 'class/drm' / connector.name.split('-', 1)[0] / 'device').resolve() == device.resolve()
                   and (connector / 'status').read_text().strip() == 'connected'
                   for pattern in ('card*-eDP-*', 'card*-LVDS-*', 'card*-DSI-*')
                   for connector in (sysfs / 'class/drm').glob(pattern)):
                candidates.append(device.name)
        except OSError:
            continue
    if len(candidates) != 1 or not mesa.is_file():
        env['GSK_RENDERER'] = 'cairo'
        return 'cairo'
    for key in ('__NV_PRIME_RENDER_OFFLOAD', '__NV_PRIME_RENDER_OFFLOAD_PROVIDER', '__VK_LAYER_NV_optimus', 'GBM_BACKEND'):
        env.pop(key, None)
    env.update({'GSK_RENDERER': 'gl', 'DRI_PRIME': 'pci-' + candidates[0].replace(':', '_').replace('.', '_'),
                '__EGL_VENDOR_LIBRARY_FILENAMES': str(mesa), '__GLX_VENDOR_LIBRARY_NAME': 'mesa'})
    return 'gl'
