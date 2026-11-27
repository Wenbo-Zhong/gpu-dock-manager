"""前后端共享的请求契约；不导入 GTK、硬件探测或特权实现。"""

INTERNAL_ACTIONS = frozenset({"internal-attach", "internal-detach"})
INTERNAL_DISPLAY_ACTIONS = frozenset({"internal-disable-displays", "internal-restore-displays"})
DISPLAY_ACTIONS = frozenset({"disable-displays", "restore-displays"}) | INTERNAL_DISPLAY_ACTIONS
DEVICE_ACTIONS = frozenset({"attach", "detach", "queue-detach"}) | INTERNAL_ACTIONS
BUSY_STATES = frozenset({"running", "waiting", "removing"})
INTERNAL_OPERATIONS = INTERNAL_ACTIONS | INTERNAL_DISPLAY_ACTIONS
EXTERNAL_OPERATIONS = DEVICE_ACTIONS - INTERNAL_ACTIONS | frozenset({
    "cancel-detach", "disable-displays", "restore-displays"})

# 每个请求允许的字段只有一份定义；服务与控制器使用同一边界。
OPERATION_OPTIONS = {
    "attach": frozenset(),
    "detach": frozenset({"expected_pci"}),
    "queue-detach": frozenset({"expected_pci"}),
    "deploy": frozenset({"profile"}),
    "rollback": frozenset(),
    "set-automation": frozenset({"enabled"}),
    **dict.fromkeys(INTERNAL_ACTIONS, frozenset({"expected_pci"})),
}
SERVICE_ACTIONS = frozenset(OPERATION_OPTIONS) | {"cancel-detach"}


def valid_options(action, options):
    # 此处只约束动作和字段；字段类型、设备身份及实时安全条件由操作实现检查。
    allowed = frozenset() if action == "cancel-detach" else OPERATION_OPTIONS.get(action)
    return allowed is not None and isinstance(options, dict) and not (set(options) - allowed)


def needs_session_cleanup(action):
    """这些操作拥有会话资源，退出时必须等待其补偿完成。"""
    # 卸载内置显卡会临时暂停会话音频；加载驱动和普通远端预约没有该补偿作用域。
    return action in DISPLAY_ACTIONS or action == "internal-detach"
