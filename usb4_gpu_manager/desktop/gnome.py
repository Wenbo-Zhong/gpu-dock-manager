"""GNOME 会话显示控制：只应用临时显示配置，设备移除仍由管理服务显式执行。

API reference: GNOME/mutter 50.1 data/dbus-interfaces/
org.gnome.Mutter.DisplayConfig.xml. Only temporary configurations are applied.
"""

from ..i18n import tr
import copy
import json

from gi.repository import Gio, GLib

from ..system import atomic_write, load_json
from .common import DisplaySession, BUSY, INTERNAL_DISPLAY_ACTIONS, internal_display_data, target_outputs, internal_outputs

NAME = "org.gnome.Mutter.DisplayConfig"
PATH = "/org/gnome/Mutter/DisplayConfig"
SIGNATURE = "(uua(iiduba(ssa{sv}))a{sv})"


def monitor_map(state):
    # GetCurrentState 中 state[1] 是物理屏幕，state[2] 是逻辑布局；两者不能混用。
    monitors = {}
    for spec, modes, properties in state[1]:
        name = spec[0]
        if name in monitors:
            raise RuntimeError(tr('The desktop reported duplicate connector names; monitor ownership is ambiguous.'))
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
                raise RuntimeError(tr('Monitor identity changed. Refresh and try again.'))
            current = [m for m in monitor["modes"] if m[6].get("is-current")]
            # 模式列表的第一项不一定是当前模式，必须按 Mutter 的属性明确识别。
            if len(current) != 1:
                raise RuntimeError(tr('Cannot uniquely identify the current display mode; configuration changes refused.'))
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
        raise RuntimeError(tr('A monitor is leased to another application. End the display lease first.'))
    return {"groups": groups, "properties": props}


def active_outputs(state):
    return {spec[0] for group in state[2] for spec in group[5]}


def disable_plan(state, data):
    DisplaySession.ready(data)
    outputs = target_outputs(data)
    monitors = monitor_map(state)
    if not outputs.issubset(monitors):
        raise RuntimeError(tr('Kernel and desktop monitor reports differ. Refresh and try again.'))
    if any(monitors[name]["properties"].get("is-builtin") for name in outputs):
        raise RuntimeError(tr('The desktop identifies the target connector as an internal panel; disabling refused.'))
    if not outputs.intersection(active_outputs(state)):
        raise RuntimeError(tr('This GPU has no active external monitor outputs.'))
    ready = internal_outputs(data).intersection(active_outputs(state))
    if not any(monitors[n]["properties"].get("is-builtin") for n in ready):
        raise RuntimeError(tr("Enable another GPU's internal panel in GNOME Display Settings before disabling external outputs."))
    before = configuration(state)
    after = copy.deepcopy(before)
    for group in after["groups"]:
        group[5] = [o for o in group[5] if o[0] not in outputs]
    after["groups"] = [g for g in after["groups"] if g[5]]
    # 移除左侧或中间输出会留下布局空洞；横向重排剩余逻辑组，
    # 同时保留各组的缩放、旋转和镜像成员。
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
    # 协议可能改变输出顺序，比较前规范化副本，避免误判或修改待应用的原布局。
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


class DesktopDisplays(DisplaySession):
    def _call(self, method, parameters=None, interface=NAME):
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return connection.call_sync(NAME, PATH, interface, method, parameters, None,
                                    Gio.DBusCallFlags.NONE, 3000, None).unpack()

    def read(self):
        allowed = self._call("Get", GLib.Variant("(ss)", (NAME, "ApplyMonitorsConfigAllowed")),
                             "org.freedesktop.DBus.Properties")[0]
        if not allowed:
            raise RuntimeError(tr('This GNOME session does not allow display configuration changes.'))
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
            # 恢复以停用后的布局为前提；用户手动调整过布局就不能再覆盖旧备份。
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
                    summary["restore_reason"] = tr('Restoration requires the same GPU, monitors and post-disable layout. If anything changed, enable outputs in GNOME Display Settings.')
                return summary
            except Exception as error:
                return {"supported": False, "can_disable": False, "can_restore": False, "monitors": [],
                        "reason": tr("Cannot control this desktop's monitors: ") + str(error)}

    def disable(self, data, expected_pci):
        with self.lock:
            if (data.get("target") or {}).get("pci") != expected_pci:
                raise RuntimeError(tr('The target GPU changed; monitor disabling cancelled.'))
            state = self.read()
            before, after, outputs = disable_plan(state, data)
            self._apply(state, after, 0)  # method=0 仅校验，method=1 才临时应用布局。
            gpu = data["target"]
            backup = {"target": {k: gpu[k] for k in ("pci", "vendor", "device")},
                      "topology": topology(state), "before": before, "after": after}
            # 在变更屏幕前落盘备份；应用失败或进程退出后仍保留人工恢复依据。
            atomic_write(self.backup_path, json.dumps(backup, ensure_ascii=False), 0o600)
            self._apply(state, after, 1)
            current = self.read()
            if set(outputs).intersection(active_outputs(current)) or config_key(configuration(current)) != config_key(after):
                raise RuntimeError(tr('The desktop has not confirmed the post-disable layout. Check Display Settings; the GPU has not been removed.'))
            return tr('External outputs disabled; the GPU remains connected. Continue computing or check safe-removal conditions separately.')

    def restore(self, data, expected_pci=None):
        with self.lock:
            if expected_pci is not None and (data.get("target") or {}).get("pci") != expected_pci:
                raise RuntimeError(tr('The target GPU changed; monitor restoration cancelled.'))
            state, backup = self.read(), load_json(self.backup_path)
            if not self._restorable(state, data, backup):
                raise RuntimeError(tr('Monitor connections or layout changed. Enable outputs in GNOME Display Settings to avoid overwriting your configuration.'))
            self._apply(state, backup["before"], 0)
            self._apply(state, backup["before"], 1)
            if config_key(configuration(self.read())) != config_key(backup["before"]):
                raise RuntimeError(tr('The desktop has not confirmed layout restoration. Check GNOME Display Settings.'))
            # 回读确认恢复成功后才删除备份，命令返回成功不能单独作为恢复凭据。
            self.backup_path.unlink(missing_ok=True)
            return tr('The previous external monitor layout was restored.')
