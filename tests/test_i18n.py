import ast
import gettext
import os
from pathlib import Path
from string import Formatter
import unittest
from unittest.mock import patch

from usb4_gpu_manager.i18n import LOCALES, DOMAIN, select_language, tr, translate_text, localize_payload, catalog


class I18nTests(unittest.TestCase):
    def test_system_language_selection_and_english_fallback(self):
        cases = {'zh_CN.UTF-8': 'zh_CN', 'zh_SG': 'zh_CN', 'zh-Hans': 'zh_CN', 'zh': 'zh_CN',
                 'zh_TW.UTF-8': 'zh_TW', 'zh_HK': 'zh_TW', 'zh_MO': 'zh_TW', 'zh-Hant': 'zh_TW',
                 'en_US.UTF-8': 'en', 'en_GB': 'en', 'zz_ZZ.UTF-8': 'en', 'C.UTF-8': 'en'}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(select_language({'LANG': raw}), expected)
        self.assertEqual(select_language({'LANG': 'en_US', 'LANGUAGE': 'zh_TW:en'}), 'zh_TW')
        self.assertEqual(select_language({'LANG': 'zh_CN', 'LANGUAGE': 'zz:zh_CN'}), 'en')
        self.assertEqual(select_language({'LANG': 'zh_CN', 'LC_ALL': 'en_US'}), 'en')
        self.assertEqual(select_language({'LANG': 'en_US', 'LC_MESSAGES': 'zh_HK'}), 'zh_TW')

    def test_interface_translations(self):
        for language, expected in [('en', 'GPU status'), ('zh_CN', '显卡状态'), ('zh_TW', '顯示卡狀態'), ('zz', 'GPU status')]:
            with patch.dict(os.environ, {'GPU_DOCK_LANGUAGE': language}):
                self.assertEqual(tr('GPU status'), expected)
                self.assertEqual(tr('Untranslated English fallback'), 'Untranslated English fallback')

    def test_service_parameters_and_identifiers_are_preserved(self):
        message = 'Unknown (PID 123) is holding the GPU.'
        self.assertEqual(translate_text(message, 'zh_CN'), 'Unknown（PID 123）正在占用显卡。')
        payload = {'reason': message, 'pci': '0000:65:00.0', 'name': 'Unknown', 'driver': 'Loaded',
                   'state': 'running', 'nodes': ['/dev/dri/card0'],
                   'detach': {'blockers': [message]}, 'events': [{'message': 'GPU safely removed'}]}
        with patch.dict(os.environ, {'GPU_DOCK_LANGUAGE': 'zh_TW'}):
            translated = localize_payload(payload)
        for key in ('pci', 'name', 'driver', 'state', 'nodes'):
            self.assertEqual(payload[key], translated[key])
        self.assertIn('顯示卡', translated['reason'])
        self.assertEqual(translated['reason'], translated['detach']['blockers'][0])
        self.assertEqual(translated['events'][0]['message'], '顯示卡已安全移除')

    def test_formatted_numbers_prefixes_nested_and_legacy_messages(self):
        message = 'Detailed status is stale (12 seconds since the last update). Showing independent USB4 connectivity.'
        self.assertIn('12', translate_text(message, 'zh_TW'))
        self.assertNotIn('Detailed status', translate_text(message, 'zh_TW'))
        message = 'Failed to read status: This GPU has no active external monitor outputs.'
        translated = translate_text(message, 'zh_CN')
        self.assertIn('读取状态失败', translated)
        self.assertNotIn('This GPU', translated)
        self.assertEqual(translate_text('显卡已安全移除', 'en'), 'GPU safely removed')
        nested = 'GDBus.Error:io.github.usb4gpumanager.Error: No unique target GPU. (36)'
        self.assertIn('没有唯一目标显卡', translate_text(nested, 'zh_CN'))
        self.assertEqual(translate_text('/dev/dri/card0', 'zh_TW'), '/dev/dri/card0')

    def test_all_application_messages_have_complete_matching_catalogs(self):
        expected = {key for key in catalog('en')._catalog if isinstance(key, str) and key}
        self.assertGreater(len(expected), 350)
        for language in ('en', 'zh_CN', 'zh_TW'):
            with (LOCALES / language / 'LC_MESSAGES' / (DOMAIN + '.mo')).open('rb') as file:
                entries = gettext.GNUTranslations(file)._catalog
            self.assertEqual(expected, {key for key in entries if isinstance(key, str) and key})
            for key in expected:
                source = {field for _, field, _, _ in Formatter().parse(key) if field is not None}
                target = {field for _, field, _, _ in Formatter().parse(entries[key]) if field is not None}
                self.assertEqual(source, target, (language, key))
        for path in LOCALES.parent.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'tr'
                        and node.args and isinstance(node.args[0], ast.Constant)):
                    self.assertIn(node.args[0].value, expected, (path.name, node.lineno))
