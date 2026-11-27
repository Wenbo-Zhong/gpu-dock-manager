"""logind 生命周期锁：驱动操作与部署共用同一文件描述符释放协议。"""

import os
from contextlib import contextmanager


@contextmanager
def sleep_inhibitor(connection=None, reason="Changing GPU drivers"):
    """仅在作用域内阻止睡眠/关机；失败和正常退出均释放描述符。"""
    # 延迟导入 GI，纯状态模块及隔离 worker 导入系统工具时不必初始化总线依赖。
    from gi.repository import Gio, GLib
    bus = connection if connection is not None else Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    reply, descriptors = bus.call_with_unix_fd_list_sync(
        "org.freedesktop.login1", "/org/freedesktop/login1",
        "org.freedesktop.login1.Manager", "Inhibit",
        GLib.Variant("(ssss)", ("sleep:shutdown", "USB4 GPU Manager", reason, "block")),
        GLib.VariantType.new("(h)"), Gio.DBusCallFlags.NONE, 5000, None, None)
    # D-Bus 的 h 值是返回描述符列表的索引，不是本进程可直接 close 的文件描述符。
    descriptor = descriptors.get(reply.unpack()[0])
    try:
        yield
    finally:
        os.close(descriptor)
