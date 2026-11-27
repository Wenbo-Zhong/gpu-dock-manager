"""客户端启动、托盘与退出；退出不会取消后台预约或停止 systemd 服务。"""

import argparse
from pathlib import Path
import sys

from ..i18n import tr
from .gtk import Adw, Gio, GLib
from .state import session_cleanup_pending
from .window import Window


class Application(Adw.Application):
    def __init__(self, capture=None, page="overview", unique=True):
        super().__init__(application_id="io.github.usb4gpumanager.App",
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS if unique and not capture else Gio.ApplicationFlags.NON_UNIQUE)
        self.capture = capture
        self.page = page
        self.tray = None
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        action = Gio.SimpleAction.new("quit", None)
        action.connect("activate", lambda *_: self.request_quit())
        self.add_action(action)
        if not self.capture:
            watcher = Path("/usr/bin/usb4-gpu-mutter-watch")
            if not watcher.is_file():
                watcher = Path(__file__).resolve().parents[2] / "tools/mutter_update_watch.py"
            if watcher.is_file():
                try:
                    GLib.spawn_async([sys.executable, str(watcher), "check-installed"])
                except GLib.Error:
                    pass
            from ..tray import Tray
            try:
                self.tray = Tray(self.activate, self.hide_to_tray, self.request_quit)
            except GLib.Error:
                self.tray = None

    def hide_to_tray(self):
        window = self.window
        if window:
            if self.tray and self.tray.ready:
                window.set_visible(False)
            else:
                window.toast(tr('The desktop tray is unavailable; keeping the window accessible.'))

    def request_quit(self):
        window = self.window
        if window:
            # 先禁止新请求并清除自动注销意图，迟到的服务状态不能触发会话退出。
            window.closing = True
            window.pending_logout = False
            window.set_visible(False)
            if getattr(window, "refresh_source", 0):
                GLib.source_remove(window.refresh_source)
                window.refresh_source = 0
            # 已提交任务归服务所有。只等会话显示操作和 WirePlumber 的 finally。
            if session_cleanup_pending(window.pending_requests):
                return
        self.quit()

    def do_shutdown(self):
        if self.window:
            if self.window.refresh_source:
                GLib.source_remove(self.window.refresh_source)
            # 这里只收回客户端队列和托盘资源，不调用服务的取消或停止接口。
            self.window.pool.shutdown()
        if self.tray:
            self.tray.close()
        Adw.Application.do_shutdown(self)

    def do_activate(self):
        window = self.window
        if not window:
            window = Window(self, self.capture, self.page)
            self.window = window
        if window.closing:
            # 正在等待本地收尾时，托盘激活不能重新打开一个已进入退出流程的窗口。
            return
        window.unminimize()
        window.present()


def main():
    parser = argparse.ArgumentParser(description="USB4 GPU Manager")
    parser.add_argument("--page", choices=["overview", "devices", "deployment", "logs"], default="overview")
    parser.add_argument("--capture", metavar="PNG", help=argparse.SUPPRESS)
    args = parser.parse_args()
    return Application(args.capture, args.page).run([sys.argv[0]])
