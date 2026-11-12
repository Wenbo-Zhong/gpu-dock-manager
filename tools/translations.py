#!/usr/bin/python3
"""GPL-3.0-only. Extract gettext messages and maintain standard PO/MO catalogs."""
import argparse
import ast
import json
from pathlib import Path
import subprocess
import tempfile
from string import Formatter

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / 'usb4_gpu_manager/locales'
DOMAIN = 'gpu-dock-manager'


def extract():
    messages = {}
    paths = sorted((ROOT / 'usb4_gpu_manager').glob('*.py')) + [ROOT / 'tools/mutter_update_watch.py']
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'tr'
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                messages.setdefault(node.args[0].value, []).append(f'{path.relative_to(ROOT)}:{node.lineno}')
    header = ('Project-Id-Version: GPU Dock Manager\nMIME-Version: 1.0\n'
              'Content-Type: text/plain; charset=UTF-8\nContent-Transfer-Encoding: 8bit\n')
    text = '# GPU Dock Manager translation template. GPL-3.0-only.\nmsgid ""\nmsgstr ' + json.dumps(header) + '\n\n'
    for message, refs in sorted(messages.items()):
        text += '#: ' + ' '.join(refs) + '\n'
        if any(field is not None for _, field, _, _ in Formatter().parse(message)):
            text += '#, python-brace-format\n'
        text += 'msgid ' + json.dumps(message, ensure_ascii=False) + '\nmsgstr ""\n\n'
    template = LOCALES / (DOMAIN + '.pot')
    template.write_text(text)
    for po in sorted(LOCALES.glob('*/LC_MESSAGES/*.po')):
        subprocess.run(['msgmerge', '--update', '--backup=none', str(po), str(template)], check=True)
    print(f'{len(messages)} messages extracted; PO files merged. Review fuzzy entries before compiling.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('extract', 'compile', 'check'))
    args = parser.parse_args()
    if args.action == 'extract':
        extract()
        return
    for po in sorted(LOCALES.glob('*/LC_MESSAGES/*.po')):
        with tempfile.TemporaryDirectory() as temporary:
            output = po.with_suffix('.mo') if args.action == 'compile' else Path(temporary) / 'check.mo'
            subprocess.run(['msgfmt', '--check', '--check-format', '-o', str(output), str(po)], check=True)
        print(args.action + ': ' + str(po.relative_to(ROOT)))


if __name__ == '__main__':
    main()
