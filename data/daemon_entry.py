"""服务入口：cleanup 只撤销临时显示忽略规则，不改变永久设备策略。"""

import sys
sys.path.insert(0, "/usr/lib/usb4-gpu-manager")
if len(sys.argv) == 2 and sys.argv[1] == "cleanup":
    from usb4_gpu_manager.controller import Controller, IGNORE_RULE, IGNORE_STATE
    if IGNORE_RULE.exists() or IGNORE_STATE.exists():
        Controller().clear_ignore()
else:
    from usb4_gpu_manager.daemon import main
    main()
