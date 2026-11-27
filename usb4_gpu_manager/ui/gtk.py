"""GTK 初始化顺序集中在此，语言和渲染器必须先于 GI 类型加载。"""

import os
from ..i18n import tr, select_language
from ..rendering import configure_renderer

os.environ["LANGUAGE"] = select_language()
configure_renderer()

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 12) or (Adw.get_major_version(), Adw.get_minor_version()) < (1, 4):
    raise RuntimeError(tr('USB4 GPU Manager requires GTK 4.12+ and libadwaita 1.4+.'))
