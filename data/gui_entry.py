"""安装后的图形入口：隔离模式下显式加载 root 管理的应用目录。"""

import sys
sys.path.insert(0, "/usr/lib/usb4-gpu-manager")
from usb4_gpu_manager.gui import main
raise SystemExit(main())
