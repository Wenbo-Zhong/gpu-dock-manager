"""Strict identity checks for the explicitly enrolled internal RTX 4070."""
from pathlib import Path
import re


def validate_internal(pci, root=Path('/sys/bus/pci/devices')):
    if not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.0', pci or ''):
        raise RuntimeError('Invalid internal GPU PCI address')
    gpu, audio = root / pci, root / (pci[:-1] + '1')
    for path, product, cls in ((gpu, '0x2820', '0x03'), (audio, '0x22bd', '0x0403')):
        if ((path / 'vendor').read_text().strip() != '0x10de'
                or (path / 'device').read_text().strip() != product
                or not (path / 'class').read_text().strip().startswith(cls)):
            raise RuntimeError('Internal RTX 4070 identity changed')
        if (path / 'removable').exists() and (path / 'removable').read_text().strip() == 'removable':
            raise RuntimeError('Internal GPU must not be a removable device')
    if (gpu / 'boot_vga').read_text().strip() != '0':
        raise RuntimeError('The boot GPU cannot use the internal experiment')
    group = (gpu / 'iommu_group').resolve()
    if (not group.name.isdecimal() or (audio / 'iommu_group').resolve() != group
            or {p.resolve() for p in (group / 'devices').iterdir()} != {gpu.resolve(), audio.resolve()}):
        raise RuntimeError('Internal GPU IOMMU group must contain only graphics and audio')
    return gpu
