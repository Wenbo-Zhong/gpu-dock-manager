"""自动管理的纯判断；不执行驱动操作，也不持有另一套状态或锁。"""


def router_ids(data):
    return {r["uuid"] for r in data["usb4"] if not r["is_host"]}


def transport_disconnected(data):
    # PCI 逻辑移除仍保留 USB4 链路，只有物理断开才复位手动移除抑制。
    return data.get("transport", {}).get("state") == "disconnected"


def automatic_attach_allowed(data, sleeping):
    return (not sleeping and data["profile"].get("enabled")
            and not data["deployment"]["restart_required"])


def bar_signature(gpu, resource):
    """未分配或格式错误的 BAR 不能推进稳定计时。"""
    rows = resource.splitlines()
    fields = rows[0].split() if rows else []
    try:
        assigned = len(fields) >= 2 and int(fields[0], 16) != 0
    except ValueError:
        assigned = False
    # 同时比较地址、路径和 BAR 范围，避免把重新枚举后的另一条路径当作稳定设备。
    return (gpu["pci"], gpu["path"], tuple(fields[:2])) if assigned else None


def internal_policy_state(data, sleeping):
    """无法可靠观测时返回 None，避免把缺失采样误认为拔线或断电。"""
    transport = data["transport"]
    if (sleeping or data.get("external_power_online") is None or data.get("status_stale")
            or transport.get("driver_cleanup_pending")
            or (transport["state"] == "detached" and not data.get("target_router_ids"))):
        return None
    # detached 表示驱动已移除但线缆仍连接，自动策略仍将其视为外接设备在场。
    return data["external_power_online"], transport["state"] in {"present", "detached"}


def external_profile(data):
    profile = data.get("profile") or data.get("suggested_profile") or {}
    known = data.get("last_known_target")
    # 手动移除后当前 PCI 目标可能为空；只从已确认的可移除 NVIDIA 外卡补齐型号。
    # 运行时 PCI 地址仍需重新解析，不能直接使用缓存中的旧地址执行操作。
    if (not profile.get("target") and known and known.get("external")
            and known.get("vendor") == "0x10de" and not known.get("boot_vga")):
        profile = dict(profile, target={key: known[key] for key in ("vendor", "device")})
    return profile
