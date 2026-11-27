"""兼容原 GUI 入口；界面、请求队列和应用生命周期在 ui 包内实现。"""

from .ui.application import Application, main
from .ui.window import Window, GpuPanel, label, clear, row, operation_summary, gpu_power_text
from .ui.gtk import GLib
