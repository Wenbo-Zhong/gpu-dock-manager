"""GTK4-compatible StatusNotifierItem and DBusMenu, without GTK3 bindings."""

from .i18n import tr
from gi.repository import Gio, GLib

ITEM_XML = '''<node><interface name="org.kde.StatusNotifierItem">
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
<property name="Category" type="s" access="read"/>
<property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/>
<property name="Status" type="s" access="read"/>
<property name="IconName" type="s" access="read"/>
<property name="ItemIsMenu" type="b" access="read"/>
<property name="Menu" type="o" access="read"/>
</interface></node>'''
MENU_XML = '''<node><interface name="com.canonical.dbusmenu">
<method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/><arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
<method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/></method>
<method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
<method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
<method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
<method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
<method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
<property name="Version" type="u" access="read"/>
<property name="TextDirection" type="s" access="read"/>
<property name="Status" type="s" access="read"/>
<property name="IconThemePath" type="as" access="read"/>
</interface></node>'''


class Tray:
    def __init__(self, show, hide, quit):
        self.show, self.hide, self.quit = show, hide, quit
        self.ready = False
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.registrations = []
        for path, xml, handler, getter in (
                ('/StatusNotifierItem', ITEM_XML, self.item_call, self.item_property),
                ('/Menu', MENU_XML, self.menu_call, self.menu_property)):
            interface = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
            self.registrations.append(self.bus.register_object(path, interface, handler, getter, None))
        self.watch = Gio.bus_watch_name_on_connection(self.bus, 'org.kde.StatusNotifierWatcher',
            Gio.BusNameWatcherFlags.NONE, self.appeared, self.vanished)

    def appeared(self, connection, name, owner):
        def registered(bus, result):
            try:
                bus.call_finish(result)
                self.ready = True
            except GLib.Error:
                self.ready = False
        connection.call(name, '/StatusNotifierWatcher', 'org.kde.StatusNotifierWatcher',
                        'RegisterStatusNotifierItem', GLib.Variant('(s)', ('/StatusNotifierItem',)),
                        None, Gio.DBusCallFlags.NONE, 5000, None, registered)

    def vanished(self, *_):
        was_ready, self.ready = self.ready, False
        if was_ready:
            self.show()

    def item_property(self, connection, sender, path, interface, name):
        values = {'Category': ('s', 'Hardware'), 'Id': ('s', 'usb4-gpu-manager'),
                  'Title': ('s', 'USB4 GPU Manager'), 'Status': ('s', 'Active'),
                  'IconName': ('s', 'video-display'), 'ItemIsMenu': ('b', False), 'Menu': ('o', '/Menu')}
        return GLib.Variant(*values[name]) if name in values else None

    def item_call(self, connection, sender, path, interface, method, parameters, invocation):
        if method in {'Activate', 'SecondaryActivate', 'ContextMenu'}:
            self.show()
        invocation.return_value(None)

    @staticmethod
    def properties(item):
        if item == 0:
            return {'children-display': GLib.Variant('s', 'submenu')}
        title = {1: tr('Show window'), 2: tr('Hide in tray'), 3: tr('Quit')}[item]
        return {'label': GLib.Variant('s', title), 'enabled': GLib.Variant('b', True),
                'visible': GLib.Variant('b', True)}

    def event(self, item, event):
        if event == 'clicked':
            callback = {1: self.show, 2: self.hide, 3: self.quit}.get(item)
            if callback:
                callback()

    def menu_property(self, connection, sender, path, interface, name):
        values = {'Version': ('u', 3), 'TextDirection': ('s', 'ltr'),
                  'Status': ('s', 'normal'), 'IconThemePath': ('as', [])}
        return GLib.Variant(*values[name]) if name in values else None

    def menu_call(self, connection, sender, path, interface, method, parameters, invocation):
        args = parameters.unpack()
        if method == 'GetLayout':
            item, depth, names = args
            if item not in range(4):
                invocation.return_dbus_error('com.canonical.dbusmenu.Error', 'Unknown menu item')
                return
            children = [GLib.Variant('(ia{sv}av)', (i, self.properties(i), []))
                        for i in (1, 2, 3)] if item == 0 and depth != 0 else []
            value = GLib.Variant('(u(ia{sv}av))', (1, (item, self.properties(item), children)))
        elif method == 'GetGroupProperties':
            value = GLib.Variant('(a(ia{sv}))', ([(i, self.properties(i)) for i in (args[0] or range(4)) if i in range(4)],))
        elif method == 'GetProperty':
            prop = self.properties(args[0]).get(args[1]) if args[0] in range(4) else None
            if prop is None:
                invocation.return_dbus_error('com.canonical.dbusmenu.Error', 'Unknown property')
                return
            value = GLib.Variant('(v)', (prop,))
        elif method == 'AboutToShow':
            value = GLib.Variant('(b)', (False,))
        elif method == 'AboutToShowGroup':
            value = GLib.Variant('(aiai)', ([], []))
        elif method == 'Event':
            self.event(args[0], args[1])
            value = None
        elif method == 'EventGroup':
            for item, event, data, timestamp in args[0]:
                self.event(item, event)
            value = GLib.Variant('(ai)', ([],))
        else:
            invocation.return_dbus_error('com.canonical.dbusmenu.Error', 'Unknown method')
            return
        invocation.return_value(value)

    def close(self):
        Gio.bus_unwatch_name(self.watch)
        for registration in self.registrations:
            self.bus.unregister_object(registration)
        self.ready = False
