"""Display plans and session-bus calls are mocked; never change host outputs."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from usb4_gpu_manager import hardware
from usb4_gpu_manager.displays import (DesktopDisplays, active_outputs, apply_arguments,
                                      config_key, configuration, disable_plan, internal_display_data)
from tests.test_manager import gpu


def monitor(name, width=1920, height=1080, current=True, builtin=False, serial="one"):
    mode = [f"{width}x{height}@60", width, height, 60.0, 1.0, [1.0, 2.0], {"is-current": current}]
    return [[name, "vendor", "model", serial], [mode],
            {"is-builtin": builtin, "color-mode": 1, "rgb-range": 1, "is-underscanning": False}]


def fixture():
    panel, external = monitor("eDP-1", builtin=True), monitor("DP-9")
    state = [5, [panel, external],
             [[0, 0, 1.0, 0, True, [external[0]], {}],
              [1920, 0, 1.0, 0, False, [panel[0]], {}]],
             {"layout-mode": 1, "supports-changing-layout-mode": True}]
    target = gpu(external_display_state="connected", connectors=[
        {"name": "DP-9", "kind": "external", "status": "connected", "enabled": "enabled"}])
    internal = gpu(pci="0000:66:00.0", vendor="0x1002", external=False, driver="amdgpu", connectors=[
        {"name": "eDP-1", "kind": "internal", "status": "connected", "enabled": "enabled"}])
    data = {"target": target, "gpus": [target, internal], "can_mutate_devices": True,
            "service_connected": True, "operation": {"state": "idle"}}
    return state, data


def apply_to_state(state, config):
    result = copy.deepcopy(state)
    result[0] += 1
    by_name = {m[0][0]: m for m in result[1]}
    enabled = {o[0] for g in config["groups"] for o in g[5]}
    for m in result[1]:
        m[1][0][6]["is-current"] = m[0][0] in enabled
    result[2] = [[x, y, scale, transform, primary, [by_name[o[0]][0] for o in outputs], {}]
                 for x, y, scale, transform, primary, outputs in config["groups"]]
    return result


class DisplayPlanTests(unittest.TestCase):
    def test_4070_hdmi_can_be_disabled_without_disabling_external_gpu_output(self):
        state, data = fixture()
        hdmi = monitor("HDMI-A-1")
        state[1].append(hdmi)
        state[2].append([3840, 0, 1.0, 0, False, [hdmi[0]], {}])
        laptop_gpu = gpu(pci="0000:65:00.0", external=False, device="0x2820",
                         connectors=[{"name": "HDMI-A-1", "kind": "external",
                                      "status": "connected", "enabled": "enabled"}])
        data["gpus"].append(laptop_gpu)
        data["internal_gpu"] = {"pci": laptop_gpu["pci"], "reason": ""}
        _, after, outputs = disable_plan(state, internal_display_data(data))
        self.assertEqual(outputs, ["HDMI-A-1"])
        self.assertEqual({o[0] for g in after["groups"] for o in g[5]}, {"DP-9", "eDP-1"})
        self.assertEqual(data["target"]["pci"], "0000:43:00.0")

    def test_disable_only_target_keep_panel_and_original_for_restore(self):
        state, data = fixture()
        original = copy.deepcopy(state)
        before, after, outputs = disable_plan(state, data)
        self.assertEqual(outputs, ["DP-9"])
        self.assertEqual(after["groups"][0][:5], [0, 0, 1.0, 0, True])
        self.assertEqual(after["groups"][0][5][0][0], "eDP-1")
        self.assertEqual(before["groups"][1][0], 1920)
        self.assertEqual(state, original)

    def test_does_not_confuse_idle_dpms_with_disabled_desktop_panel(self):
        state, data = fixture()
        data["gpus"][1]["connectors"][0]["enabled"] = "disabled"
        disable_plan(state, data)  # Logical configuration remains active.

    def test_refuse_external_only_layout(self):
        state, data = fixture()
        state[2] = state[2][:1]
        with self.assertRaisesRegex(RuntimeError, "内屏"):
            disable_plan(state, data)

    def test_refuse_stale_busy_missing_target_and_ambiguous_ownership(self):
        for kind in ("stale", "busy", "missing", "ambiguous", "unknown", "desktop-mismatch"):
            state, data = fixture()
            if kind == "stale":
                data["can_mutate_devices"] = False
            elif kind == "busy":
                data["operation"] = {"state": "waiting"}
            elif kind == "missing":
                data["target"] = None
            elif kind == "ambiguous":
                data["gpus"][1]["connectors"].append(dict(data["target"]["connectors"][0]))
            elif kind == "unknown":
                data["target"]["connectors"][0]["status"] = "unknown"
            else:
                state[1].pop()
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                disable_plan(state, data)

    def test_clone_group_keeps_internal_member(self):
        state, data = fixture()
        state[2] = [[0, 0, 1.0, 0, True, [m[0] for m in state[1]], {}]]
        _, after, _ = disable_plan(state, data)
        self.assertEqual(len(after["groups"]), 1)
        self.assertEqual(after["groups"][0][5][0][0], "eDP-1")

    def test_scaled_rotated_other_gpu_screen_is_preserved_and_repacked(self):
        state, data = fixture()
        third = monitor("HDMI-1", width=3840, height=2160)
        state[1].append(third)
        state[2].insert(1, [1920, 0, 2.0, 1, False, [third[0]], {}])
        state[2][-1][0] = 3000
        _, after, _ = disable_plan(state, data)
        self.assertEqual(after["groups"][0][:4], [0, 0, 2.0, 1])
        self.assertEqual(after["groups"][1][0], 1080)
        self.assertTrue(after["groups"][1][4])
        self.assertEqual(after["groups"][0][5][0][2]["color-mode"], 1)

    def test_all_target_outputs_stop_together(self):
        state, data = fixture()
        extra = monitor("DP-10")
        state[1].append(extra)
        state[2].append([3840, 0, 1.0, 0, False, [extra[0]], {}])
        data["target"]["connectors"].append({"name": "DP-10", "kind": "external", "status": "connected", "enabled": "enabled"})
        _, after, names = disable_plan(state, data)
        self.assertEqual(names, ["DP-10", "DP-9"])
        self.assertEqual([o[0] for g in after["groups"] for o in g[5]], ["eDP-1"])

    def test_leased_monitor_and_duplicate_desktop_names_are_rejected(self):
        state, data = fixture()
        state[1][1][2]["is-for-lease"] = True
        with self.assertRaisesRegex(RuntimeError, "租赁"):
            disable_plan(state, data)
        state[1].append(copy.deepcopy(state[1][0]))
        with self.assertRaisesRegex(RuntimeError, "重复"):
            disable_plan(state, data)

    def test_connected_disabled_outputs_still_require_panel_and_no_holders(self):
        _, data = fixture()
        target = data["target"]
        target["connectors"][0]["enabled"] = "disabled"
        idle = {"complete": True, "processes": []}
        self.assertFalse(hardware.detach_check(target, idle)["safe"])
        self.assertTrue(hardware.detach_check(target, idle, other_display_ready=True)["safe"])
        occupied = {"complete": True, "processes": [{"name": "gnome-shell", "pid": 7}]}
        self.assertFalse(hardware.detach_check(target, occupied, other_display_ready=True)["safe"])
        target["connectors"][0]["enabled"] = "unknown"
        self.assertFalse(hardware.detach_check(target, idle, other_display_ready=True)["safe"])


class DesktopDisplaysTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.desktop = DesktopDisplays(Path(self.directory.name) / "layout.json")
        self.state, self.data = fixture()
        self.calls = []

    def fake_apply(self, state, config, method):
        self.calls.append((state[0], method, copy.deepcopy(config)))
        apply_arguments(state[0], method, config)  # Enforce real typed D-Bus encoding.
        if method == 1:
            self.state = apply_to_state(self.state, config)

    def test_disable_restore_uses_only_session_temporary_not_persistent(self):
        original = config_key(configuration(self.state))
        with patch.object(self.desktop, "read", side_effect=lambda: self.state), patch.object(self.desktop, "_apply", side_effect=self.fake_apply):
            self.assertTrue(self.desktop.status(self.data)["can_disable"])
            self.desktop.disable(self.data, self.data["target"]["pci"])
            self.assertEqual(active_outputs(self.state), {"eDP-1"})
            self.assertEqual(self.desktop.backup_path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(self.desktop.status(self.data)["can_restore"])
            self.desktop.restore(self.data)
            self.assertEqual(config_key(configuration(self.state)), original)
            self.assertEqual([c[1] for c in self.calls], [0, 1, 0, 1])
            self.assertFalse(self.desktop.backup_path.exists())

    def test_no_apply_when_target_changed(self):
        with patch.object(self.desktop, "_apply") as apply:
            with self.assertRaisesRegex(RuntimeError, "变化"):
                self.desktop.disable(self.data, "0000:44:00.0")
            apply.assert_not_called()

    def test_failed_validation_does_not_apply_or_save_backup(self):
        with patch.object(self.desktop, "read", return_value=self.state), patch.object(self.desktop, "_apply", side_effect=RuntimeError("invalid serial")) as apply:
            with self.assertRaisesRegex(RuntimeError, "serial"):
                self.desktop.disable(self.data, self.data["target"]["pci"])
            self.assertEqual(apply.call_count, 1)
            self.assertEqual(apply.call_args.args[2], 0)
            self.assertFalse(self.desktop.backup_path.exists())

    def test_restore_refuses_changed_topology_layout_or_busy_gpu(self):
        for kind in ("topology", "layout", "busy", "stale"):
            self.state, self.data = fixture()
            with patch.object(self.desktop, "read", side_effect=lambda: self.state), patch.object(self.desktop, "_apply", side_effect=self.fake_apply):
                self.desktop.disable(self.data, self.data["target"]["pci"])
                if kind == "topology":
                    self.state[1][1][0][3] = "different-monitor"
                elif kind == "layout":
                    self.state[2][0][0] = 7
                elif kind == "busy":
                    self.data["operation"]["state"] = "waiting"
                else:
                    self.data["can_mutate_devices"] = False
                self.calls.clear()
                with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                    self.desktop.restore(self.data)
                self.assertEqual(self.calls, [])

    def test_unsupported_compositor_is_explicit_without_fallback_writes(self):
        with patch.object(self.desktop, "read", side_effect=RuntimeError("no Mutter")), patch.object(self.desktop, "_apply") as apply:
            status = self.desktop.status(self.data)
            self.assertFalse(status["supported"])
            self.assertFalse(status["can_disable"])
            self.assertIn("no Mutter", status["reason"])
            apply.assert_not_called()
