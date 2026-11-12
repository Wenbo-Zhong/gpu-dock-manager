import unittest
from pathlib import Path
from unittest.mock import Mock
import xml.etree.ElementTree as ET
from gi.repository import GLib
from usb4_gpu_manager.tray import Tray, ITEM_XML, MENU_XML


class TrayTests(unittest.TestCase):
    def tray(self):
        tray = Tray.__new__(Tray)
        tray.show, tray.hide, tray.quit = Mock(), Mock(), Mock()
        return tray

    def test_menu_layout_and_actions(self):
        tray = self.tray()
        invocation = Mock()
        tray.menu_call(None, None, None, None, 'GetLayout', GLib.Variant('(iias)', (0, -1, [])), invocation)
        layout = invocation.return_value.call_args.args[0].unpack()
        self.assertEqual(layout[0], 1)
        self.assertEqual([child[0] for child in layout[1][2]], [1, 2, 3])
        for item, callback in ((1, tray.show), (2, tray.hide), (3, tray.quit)):
            tray.menu_call(None, None, None, None, 'Event',
                           GLib.Variant('(isvu)', (item, 'clicked', GLib.Variant('s', ''), 0)), invocation)
            callback.assert_called_once()
        tray.event(3, 'hovered')
        tray.quit.assert_called_once()

    def test_watcher_loss_restores_hidden_window(self):
        tray = self.tray()
        tray.ready = True
        tray.vanished()
        self.assertFalse(tray.ready)
        tray.show.assert_called_once()

    def test_activation_restores_window_and_protocol_xml_parses(self):
        from gi.repository import Gio
        for xml in (ITEM_XML, MENU_XML):
            self.assertTrue(Gio.DBusNodeInfo.new_for_xml(xml).interfaces)
        tray = self.tray()
        invocation = Mock()
        tray.item_call(None, None, None, None, 'Activate', GLib.Variant('(ii)', (0, 0)), invocation)
        tray.show.assert_called_once()
        invocation.return_value.assert_called_once_with(None)

    def test_privileged_device_operations_require_active_session(self):
        root = ET.fromstring(Path('data/io.github.usb4gpumanager.policy').read_text())
        for action in root.findall('action'):
            defaults = action.find('defaults')
            self.assertEqual(defaults.findtext('allow_any'), 'no')
            self.assertEqual(defaults.findtext('allow_inactive'), 'no')
            expected = 'auth_admin_keep' if action.attrib['id'].endswith('.configure') else 'yes'
            self.assertEqual(defaults.findtext('allow_active'), expected)
