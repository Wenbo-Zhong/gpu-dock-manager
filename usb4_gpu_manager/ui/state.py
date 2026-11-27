"""纯状态派生，界面刷新与退出决策不依赖 GTK 对象。"""

from ..contracts import BUSY_STATES, needs_session_cleanup


def operation_busy(data, pending=False, display_busy=False):
    # 服务尚未返回授权回执时也应禁用重复操作，不能只判断后台任务状态。
    return bool(pending or display_busy or data.get("operation", {}).get("state") in BUSY_STATES)


def removal_confirmed(data):
    operation, transport = data.get("operation", {}), data.get("transport", {})
    # 拔线或 PCI 消失本身不是安全移除成功，必须同时有服务完成回执且无残留清理。
    return bool(data.get("service_connected")
                and operation.get("state") == "done" and operation.get("action") in {"detach", "queue-detach"}
                and transport.get("state") in {"detached", "disconnected"}
                and not transport.get("pci_visible") and not transport.get("driver_cleanup_pending"))


def session_cleanup_pending(requests):
    # 后台任务无需保持 GUI 存活；仅会话资源拥有者需要等待本地 finally 执行完毕。
    return any(needs_session_cleanup(action) for action in requests.values())
