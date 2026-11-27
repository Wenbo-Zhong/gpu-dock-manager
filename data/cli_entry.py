"""安装后的命令行入口：使用固定应用路径，避免用户目录遮蔽特权模块。"""

import sys
sys.path.insert(0, "/usr/lib/usb4-gpu-manager")
from usb4_gpu_manager.cli import main
main()
