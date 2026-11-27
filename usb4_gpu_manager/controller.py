"""控制器旧路径兼容入口；状态和锁只有 core.controller 一份实现。"""
import sys
from .core import controller

# 兼容入口使用同一模块对象，避免旧路径导入和测试补丁指向两套全局依赖。
sys.modules[__name__] = controller
