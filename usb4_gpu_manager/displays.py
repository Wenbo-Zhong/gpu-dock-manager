"""旧导入路径兼容入口；实现与测试补丁均指向 GNOME 适配模块。"""
import sys
from .desktop import gnome

# 保留原模块的公共函数与依赖对象，旧调用方无需在本次目录调整中同步迁移。
sys.modules[__name__] = gnome
