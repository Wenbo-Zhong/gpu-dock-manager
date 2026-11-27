"""旧导入路径兼容入口；KDE 不依赖 GNOME 实现。"""
import sys
from .desktop import kde

# 使用模块别名保持旧导入路径可用，KDE 的实际实现仍只依赖桌面公共层。
sys.modules[__name__] = kde
