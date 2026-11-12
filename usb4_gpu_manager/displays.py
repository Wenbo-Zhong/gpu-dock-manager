"""GNOME session display control. No root, PCI writes or implicit GPU removal.

API reference: GNOME/mutter 50.1 data/dbus-interfaces/
org.gnome.Mutter.DisplayConfig.xml. Only temporary configurations are applied.
"""
import copy
import json
import os
from pathlib import Path
import threading

from gi.repository import Gio, GLib

from .system import atomic_write, load_json

NAME = "org.gnome.Mutter.DisplayConfig"
PATH = "/org/gnome/Mutter/DisplayConfig"
SIGNATURE = "(uua(iiduba(ssa{sv}))a{sv})"
BUSY = {"running", "waiting", "removing"}
INTERNAL_DISPLAY_ACTIONS = {"internal-disable-displays", "internal-restore-displays"}


def internal_display_data(data):
    internal = data.get("internal_gpu") or {}
    gpu = next((item for item in data.get("gpus", []) if item["pci"] == internal.get("pci")
                and item["driver"] == "nvidia" and not internal.get("reason")), None)
    return dict(data, target=gpu)


def monitor_map(state):
    monitors = {}
    for spec, modes, properties in state[1]:
        name = spec[0]
        if name in monitors:
            raise RuntimeError("桌面报告了重复显示接口，无法确定显示器归属。")
        monitors[name] = {"spec": list(spec), "modes": modes, "properties": properties}
    return monitors


def configuration(state):
    """Translate GetCurrentState to ApplyMonitorsConfig, preserving active modes."""
    monitors = monitor_map(state)
    groups = []
    for x, y, scale, transform, primary, specs, _ in state[2]:
        outputs = []
        for spec in specs:
            monitor = monitors[spec[0]]
            if list(spec) != monitor["spec"]:
                raise RuntimeError("显示器身份发生变化，请刷新后重试。")
            current = [m for m in monitor["modes"] if m[6].get("is-current")]
            if len(current) != 1:
                raise RuntimeError("无法唯一读取当前显示模式，拒绝修改布局。")
            props = monitor["properties"]
            output_props = {k: props[k] for k in ("color-mode", "rgb-range") if k in props}
            if "is-underscanning" in props:
                output_props["underscanning"] = props["is-underscanning"]
            outputs.append([spec[0], current[0][0], output_props])
        groups.append([x, y, scale, transform, primary, outputs])
    props = {}
    if state[3].get("supports-changing-layout-mode"):
        props["layout-mode"] = state[3].get("layout-mode", 1)
    if any(m["properties"].get("is-for-lease") for m in monitors.values()):
        raise RuntimeError("存在租赁给其他应用的显示器，请先结束显示租赁。")
    return {"groups": groups, "properties": props}


def target_outputs(data):
    gpu = data.get("target")
    internal = data.get("internal_gpu") or {}
    supported_internal = (gpu and gpu["pci"] == internal.get("pci") and gpu["driver"] == "nvidia"
                          and not internal.get("reason"))
    if not gpu or gpu.get("boot_vga") or not (gpu.get("external") or supported_internal):
        raise RuntimeError("没有可唯一识别且已加载的目标显卡。")
    owners = {}
    for device in data.get("gpus", []):
        for connector in device["connectors"]:
            owners.setdefault(connector["name"], []).append(device["pci"])
    outputs = set()
    for connector in gpu["connectors"]:
        if connector["kind"] != "external":
            continue
        if owners.get(connector["name"]) != [gpu["pci"]]:
            raise RuntimeError("多个 GPU 的显示接口名称重复，拒绝修改归属不明的屏幕。")
        if connector["status"] not in {"connected", "disconnected"}:
            raise RuntimeError("外接显示器连接状态未知，请刷新后重试。")
        if connector["status"] == "connected":
            outputs.add(connector["name"])
    return outputs


def active_outputs(state):
    return {spec[0] for group in state[2] for spec in group[5]}


def internal_outputs(data):
    gpu = data.get("target") or {}
    # Require an unambiguous internal panel on a different PCI GPU.
    counts = {}
    for device in data.get("gpus", []):
        for c in device["connectors"]:
            counts[c["name"]] = counts.get(c["name"], 0) + 1
    return {c["name"] for g in data.get("gpus", []) if g["pci"] != gpu.get("pci")
            for c in g["connectors"] if c["kind"] == "internal" and c["status"] == "connected"
            and counts[c["name"]] == 1}


def disable_plan(state, data):
    if not data.get("service_connected") or not data.get("can_mutate_devices"):
        raise RuntimeError("设备状态不可用或已过期，请先恢复状态采集。")
    if data.get("operation", {}).get("state") in BUSY:
        raise RuntimeError("显卡操作正在进行，请等待完成或取消预约。")
    outputs = target_outputs(data)
    monitors = monitor_map(state)
    if not outputs.issubset(monitors):
        raise RuntimeError("内核与桌面报告的外屏不一致，请刷新后重试。")
    if any(monitors[name]["properties"].get("is-builtin") for name in outputs):
        raise RuntimeError("目标接口被桌面识别为内屏，拒绝停用。")
    if not outputs.intersection(active_outputs(state)):
        raise RuntimeError("这张显卡没有正在输出的外接屏幕。")
    ready = internal_outputs(data).intersection(active_outputs(state))
    if not any(monitors[n]["properties"].get("is-builtin") for n in ready):
        raise RuntimeError("请先在 GNOME 显示设置中启用另一张显卡驱动的内屏，再停用外屏。")
    before = configuration(state)
    after = copy.deepcopy(before)
    for group in after["groups"]:
        group[5] = [o for o in group[5] if o[0] not in outputs]
    after["groups"] = [g for g in after["groups"] if g[5]]
    # Removing a middle/left screen can leave gaps or no origin. Repack the
    # remaining logical groups horizontally; retain scale, rotation and clones.
    after["groups"].sort(key=lambda g: (g[0], g[1]))
    primary_survives = any(g[4] for g in after["groups"])
    x = 0
    for group in after["groups"]:
        group[0], group[1] = x, 0
        if not primary_survives:
            group[4] = any(o[0] in ready for o in group[5])
            if group[4]:
                primary_survives = True
        mode = next(m for m in monitors[group[5][0][0]]["modes"] if m[0] == group[5][0][1])
        width = mode[2] if group[3] in {1, 3, 5, 7} else mode[1]
        scale = group[2] if state[3].get("layout-mode", 1) == 1 else 1
        x += round(width / scale)
    return before, after, sorted(outputs)


def topology(state):
    return sorted([list(spec) for spec, _, _ in state[1]])


def config_key(config):
    config = copy.deepcopy(config)
    for g in config["groups"]:
        g[5].sort(key=lambda o: o[0])
    config["groups"].sort(key=lambda g: (g[0], g[1]))
    return json.dumps(config, sort_keys=True)


def apply_arguments(serial, method, config):
    def variants(props):
        return {k: GLib.Variant("b" if isinstance(v, bool) else "u", v) for k, v in props.items()}
    groups = [(x, y, scale, transform, primary, [(n, mode, variants(p)) for n, mode, p in outputs])
              for x, y, scale, transform, primary, outputs in config["groups"]]
    return GLib.Variant(SIGNATURE, (serial, method, groups, variants(config["properties"])))


class DesktopDisplays:
    def __init__(self, backup_path=None, *, internal=False):
        state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        name = "display-layout-internal.json" if internal else "display-layout.json"
        self.backup_path = Path(backup_path) if backup_path else state_home / "usb4-gpu-manager" / name
        self.lock = threading.RLock()

    def _call(self, method, parameters=None, interface=NAME):
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return connection.call_sync(NAME, PATH, interface, method, parameters, None,
                                    Gio.DBusCallFlags.NONE, 3000, None).unpack()

    def read(self):
        allowed = self._call("Get", GLib.Variant("(ss)", (NAME, "ApplyMonitorsConfigAllowed")),
                             "org.freedesktop.DBus.Properties")[0]
        if not allowed:
            raise RuntimeError("当前 GNOME 会话不允许修改显示配置。")
        return self._call("GetCurrentState")

    def _apply(self, state, config, method):
        self._call("ApplyMonitorsConfig", apply_arguments(state[0], method, config))

    def _restorable(self, state, data, backup):
        gpu = data.get("target")
        if not gpu or not isinstance(backup, dict) or not data.get("can_mutate_devices") or not data.get("service_connected"):
            return False
        if data.get("operation", {}).get("state") in BUSY:
            return False
        try:
            return (backup.get("target") == {k: gpu[k] for k in ("pci", "vendor", "device")}
                    and backup.get("topology") == topology(state)
                    and config_key(backup["after"]) == config_key(configuration(state)))
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            return False

    def status(self, data):
        with self.lock:
            try:
                state = self.read()
                monitors, active = monitor_map(state), active_outputs(state)
                outputs = target_outputs(data)
                summary = {"supported": True, "can_disable": False, "can_restore": False,
                           "monitors": [{"connector": n, "name": monitors[n]["properties"].get("display-name", n),
                                         "active": n in active} for n in sorted(outputs) if n in monitors],
                           "reason": ""}
                try:
                    disable_plan(state, data)
                    summary["can_disable"] = True
                except RuntimeError as error:
                    summary["reason"] = str(error)
                backup = load_json(self.backup_path)
                summary["can_restore"] = self._restorable(state, data, backup)
                if backup and not summary["can_restore"]:
                    summary["restore_reason"] = "恢复要求同一张显卡、相同屏幕及停用后的布局；如已改动，请用 GNOME 显示设置启用外屏。"
                return summary
            except Exception as error:
                return {"supported": False, "can_disable": False, "can_restore": False, "monitors": [],
                        "reason": "无法控制当前桌面的外屏：" + str(error)}

    def disable(self, data, expected_pci):
        with self.lock:
            if (data.get("target") or {}).get("pci") != expected_pci:
                raise RuntimeError("目标显卡已变化，取消停用外屏。")
            state = self.read()
            before, after, outputs = disable_plan(state, data)
            self._apply(state, after, 0)  # Mutter validation, no display changes.
            gpu = data["target"]
            backup = {"target": {k: gpu[k] for k in ("pci", "vendor", "device")},
                      "topology": topology(state), "before": before, "after": after}
            atomic_write(self.backup_path, json.dumps(backup, ensure_ascii=False), 0o600)
            self._apply(state, after, 1)
            current = self.read()
            if set(outputs).intersection(active_outputs(current)) or config_key(configuration(current)) != config_key(after):
                raise RuntimeError("桌面尚未确认外屏停用后的布局，请查看显示设置；显卡尚未移除。")
            return "外屏已停用，显卡仍保持连接；可以继续计算，或单独检查安全移除条件。"

    def restore(self, data, expected_pci=None):
        with self.lock:
            if expected_pci is not None and (data.get("target") or {}).get("pci") != expected_pci:
                raise RuntimeError("目标显卡已变化，取消恢复外屏。")
            state, backup = self.read(), load_json(self.backup_path)
            if not self._restorable(state, data, backup):
                raise RuntimeError("屏幕连接或布局已变化，不能覆盖当前配置；请用 GNOME 显示设置启用外屏。")
            self._apply(state, backup["before"], 0)
            self._apply(state, backup["before"], 1)
            if config_key(configuration(self.read())) != config_key(backup["before"]):
                raise RuntimeError("桌面尚未确认恢复原布局，请检查 GNOME 显示设置。")
            self.backup_path.unlink(missing_ok=True)
            return "已恢复停用前的外屏布局。"
