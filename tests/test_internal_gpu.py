from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from usb4_gpu_manager import controller, hardware, internal_gpu

PCI = "0000:65:00.0"


class InternalGpuTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.sysfs = Path(temporary.name)
        self.bus = self.sysfs / "bus/pci"
        self.group = self.sysfs / "kernel/iommu_groups/22"
        (self.group / "devices").mkdir(parents=True)
        self.gpu = self.bus / "devices" / PCI
        self.audio = self.bus / "devices" / "0000:65:00.1"
        for device, product, cls in ((self.gpu, "0x2820", "0x030000"),
                                     (self.audio, "0x22bd", "0x040300")):
            device.mkdir(parents=True)
            for attr, value in {"vendor": "0x10de", "device": product, "class": cls,
                                "boot_vga": "0", "driver_override": "(null)"}.items():
                (device / attr).write_text(value)
            (device / "iommu_group").symlink_to(self.group)
            (self.group / "devices" / device.name).symlink_to(device)
        for name in ("nvidia", "vfio-pci", "snd_hda_intel"):
            (self.bus / "drivers" / name).mkdir(parents=True)
        self.bind(self.gpu, "nvidia", self.bus)
        self.bind(self.audio, "vfio-pci", self.bus)

    @staticmethod
    def bind(device, desired, bus):
        link = device / "driver"
        link.unlink(missing_ok=True)
        if desired:
            link.symlink_to(bus / "drivers" / desired)

    def drivers(self):
        return tuple(internal_gpu.driver(device) for device in (self.gpu, self.audio))

    def test_switches_both_functions_in_both_directions(self):
        with (patch.object(internal_gpu, "check_idle"), patch.object(internal_gpu, "command"),
              patch.object(internal_gpu, "bind_driver", side_effect=self.bind) as bind):
            internal_gpu.switch_internal_gpu(PCI, "internal-detach", self.sysfs)
            self.assertEqual(self.drivers(), ("vfio-pci", "vfio-pci"))
            internal_gpu.switch_internal_gpu(PCI, "internal-attach", self.sysfs)
            self.assertEqual(self.drivers(), ("nvidia", "snd_hda_intel"))
            self.assertEqual(bind.call_count, 3)
            internal_gpu.switch_internal_gpu(PCI, "internal-attach", self.sysfs)
            self.assertEqual(bind.call_count, 3)
        for device in (self.gpu, self.audio):
            self.assertEqual((device / "driver_override").read_text().strip(), "")

    def test_module_loading_reserves_original_bindings(self):
        self.bind(self.gpu, "vfio-pci", self.bus)
        def load(*args, **kwargs):
            self.assertEqual(self.drivers(), ("vfio-pci", "vfio-pci"))
            for device in (self.gpu, self.audio):
                self.assertEqual((device / "driver_override").read_text().strip(), "vfio-pci")
        with (patch.object(internal_gpu, "check_idle"),
              patch.object(internal_gpu, "command", side_effect=load),
              patch.object(internal_gpu, "bind_driver", side_effect=self.bind)):
            internal_gpu.switch_internal_gpu(PCI, "internal-attach", self.sysfs)
        self.assertEqual(self.drivers(), internal_gpu.HOST_DRIVERS)

    def test_module_failure_releases_reservations_without_switching(self):
        self.bind(self.gpu, "vfio-pci", self.bus)
        with (patch.object(internal_gpu, "check_idle"),
              patch.object(internal_gpu, "command", side_effect=RuntimeError("module failed")),
              patch.object(internal_gpu, "bind_driver") as bind,
              self.assertRaisesRegex(RuntimeError, "module failed")):
            internal_gpu.switch_internal_gpu(PCI, "internal-attach", self.sysfs)
        bind.assert_not_called()
        self.assertEqual(self.drivers(), ("vfio-pci", "vfio-pci"))
        for device in (self.gpu, self.audio):
            self.assertEqual((device / "driver_override").read_text().strip(), "")

    def test_second_idle_check_failure_keeps_original_pair(self):
        self.bind(self.gpu, "vfio-pci", self.bus)
        with (patch.object(internal_gpu, "check_idle", side_effect=[None, RuntimeError("new holder")]),
              patch.object(internal_gpu, "command"),
              patch.object(internal_gpu, "bind_driver") as bind,
              self.assertRaisesRegex(RuntimeError, "new holder")):
            internal_gpu.switch_internal_gpu(PCI, "internal-attach", self.sysfs)
        bind.assert_not_called()
        self.assertEqual(self.drivers(), ("vfio-pci", "vfio-pci"))
        for device in (self.gpu, self.audio):
            self.assertEqual((device / "driver_override").read_text().strip(), "")

    def test_binding_failure_restores_original_pair(self):
        self.bind(self.gpu, "vfio-pci", self.bus)

        def fail_gpu_load(device, desired, bus):
            if desired == "nvidia":
                self.bind(device, None, bus)
                raise OSError("simulated driver failure")
            self.bind(device, desired, bus)

        with (patch.object(internal_gpu, "check_idle"), patch.object(internal_gpu, "command"),
              patch.object(internal_gpu, "bind_driver", side_effect=fail_gpu_load),
              self.assertRaisesRegex(RuntimeError, "已恢复原驱动绑定")):
            internal_gpu.switch_internal_gpu(PCI, "internal-attach", self.sysfs)
        self.assertEqual(self.drivers(), ("vfio-pci", "vfio-pci"))
        for device in (self.gpu, self.audio):
            self.assertEqual((device / "driver_override").read_text().strip(), "")

    def test_rejects_other_group_members_before_loading_modules(self):
        (self.group / "devices" / "0000:65:00.2").mkdir()
        with (patch.object(internal_gpu, "command") as run,
              self.assertRaisesRegex(RuntimeError, "IOMMU")):
            internal_gpu.switch_internal_gpu(PCI, "internal-detach", self.sysfs)
        run.assert_not_called()
        self.assertEqual(self.drivers(), ("nvidia", "vfio-pci"))

    def test_checks_vfio_and_gpu_use_before_mutating(self):
        selected = {"pci": PCI, "vendor": "0x10de", "driver": "nvidia",
                    "external_display_state": "disconnected", "connectors": [], "nodes": ["/dev/dri/card0"]}
        amd = {"pci": "0000:66:00.0", "vendor": "0x1002", "driver": "amdgpu",
               "connectors": [{"kind": "internal", "status": "connected", "enabled": "enabled"}]}
        with (patch.object(hardware, "gpu_inventory", return_value=[selected, amd]),
              patch.object(hardware, "holders", return_value={"complete": True,
                  "processes": [{"name": "qemu", "pid": 12}]}) as scan,
              patch.object(internal_gpu, "command"), patch.object(internal_gpu, "bind_driver") as bind,
              self.assertRaisesRegex(RuntimeError, "qemu")):
            internal_gpu.switch_internal_gpu(PCI, "internal-detach", self.sysfs)
        self.assertIn("/dev/vfio/22", scan.call_args.args[0]["nodes"])
        bind.assert_not_called()
        self.assertEqual((self.gpu / "driver_override").read_text(), "(null)")

    def test_service_routes_to_internal_worker_without_external_profile(self):
        manager = controller.Controller()
        with (patch.object(controller, "internal_device_action") as action,
              patch.object(manager, "_profile_and_gpu") as external):
            manager.perform("internal-detach", {"expected_pci": PCI})
        action.assert_called_once_with("internal-detach", PCI, protect_connected=False)
        external.assert_not_called()
        self.assertEqual(manager.operation["state"], "done")

    def test_connected_hdmi_is_protected_automatically_even_after_display_disable(self):
        connector = {"kind": "external", "name": "HDMI-A-1", "status": "connected", "enabled": "disabled"}
        selected = {"pci": PCI, "vendor": "0x10de", "driver": "nvidia",
                    "external_display_state": "connected", "connectors": [connector], "nodes": []}
        amd = {"pci": "0000:66:00.0", "vendor": "0x1002", "driver": "amdgpu",
               "connectors": [{"kind": "internal", "status": "connected", "enabled": "enabled"}]}
        with (patch.object(hardware, "gpu_inventory", return_value=[selected, amd]),
              patch.object(hardware, "holders", return_value={"complete": True, "processes": []})):
            with self.assertRaisesRegex(RuntimeError, "仍接有显示器"):
                internal_gpu.check_idle(self.gpu, self.audio, self.group, self.sysfs, True)
            internal_gpu.check_idle(self.gpu, self.audio, self.group, self.sysfs, False)

    def test_preview_distinguishes_direct_and_shared_nvidia_holders(self):
        selected = {"pci": PCI, "vendor": "0x10de", "device": "0x2820", "external": False,
                    "boot_vga": False, "nodes": ["/dev/dri/renderD129"]}
        occupied = {"complete": True, "processes": [
            {"pid": 12, "name": "terminal", "nodes": ["/dev/dri/renderD129", "/dev/nvidiactl"]},
            {"pid": 13, "name": "monitor", "nodes": ["/dev/nvidia-uvm"]},
        ]}
        with (patch.object(hardware, "gpu_inventory", return_value=[selected]),
              patch.object(hardware, "holders", return_value=occupied)):
            status = internal_gpu.inspect_internal_gpu(PCI, self.sysfs)
        self.assertEqual((status["graphics_driver"], status["audio_driver"]), ("nvidia", "vfio-pci"))
        direct, shared = status["holders"]["processes"]
        self.assertEqual(direct["direct_nodes"], ["/dev/dri/renderD129"])
        self.assertEqual(shared["direct_nodes"], [])
        self.assertEqual(internal_gpu.internal_switch_blocker(status, "internal-attach"), "")
        self.assertIn("释放", internal_gpu.internal_switch_blocker(status, "internal-detach"))
        status.update(graphics_driver="vfio-pci", audio_driver="vfio-pci")
        self.assertIn("无需", internal_gpu.internal_switch_blocker(status, "internal-detach"))

    def test_audio_only_attach_scans_audio_and_vfio_without_touching_graphics(self):
        # Even an active HDMI output does not need to be torn down to add audio.
        selected = {"pci": PCI, "vendor": "0x10de", "driver": "nvidia",
                    "external_display_state": "connected", "nodes": ["/dev/dri/renderD129"],
                    "connectors": [{"kind": "external", "status": "connected", "enabled": "enabled"}]}
        amd = {"vendor": "0x1002", "driver": "amdgpu", "pci": "0000:66:00.0",
               "connectors": [{"kind": "internal", "status": "connected", "enabled": "enabled"}]}
        (self.audio / "sound/card7").mkdir(parents=True)
        with (patch.object(hardware, "gpu_inventory", return_value=[selected, amd]),
              patch.object(hardware, "holders", return_value={"complete": True, "processes": []}) as scan,
              patch.object(internal_gpu, "command") as run,
              patch.object(internal_gpu, "bind_driver", side_effect=self.bind) as bind):
            internal_gpu.switch_internal_gpu(PCI, "internal-attach", self.sysfs)
        for call in scan.call_args_list:
            spec = call.args[0]
            self.assertEqual(spec["vendor"], "vfio")
            self.assertIn("/dev/vfio/22", spec["nodes"])
            self.assertIn("/dev/snd/controlC7", spec["nodes"])
            self.assertNotIn("/dev/dri/renderD129", spec["nodes"])
        bind.assert_called_once_with(self.audio, "snd_hda_intel", self.bus)
        self.assertEqual(run.call_args.args[0], ["modprobe", "--ignore-install", "snd_hda_intel"])
        self.assertEqual(self.drivers(), internal_gpu.HOST_DRIVERS)

    def test_audio_only_attach_blocks_vfio_users_and_incomplete_scans(self):
        selected = {"pci": PCI, "vendor": "0x10de", "driver": "nvidia", "nodes": []}
        amd = {"vendor": "0x1002", "driver": "amdgpu", "pci": "0000:66:00.0",
               "connectors": [{"kind": "internal", "status": "connected", "enabled": "enabled"}]}
        for occupied, message in (({"complete": True, "processes": [{"pid": 12, "name": "qemu"}]}, "qemu"),
                                  ({"complete": False, "processes": []}, "不完整")):
            with (self.subTest(occupied=occupied),
                  patch.object(hardware, "gpu_inventory", return_value=[selected, amd]),
                  patch.object(hardware, "holders", return_value=occupied),
                  patch.object(internal_gpu, "command") as run,
                  patch.object(internal_gpu, "bind_driver") as bind,
                  self.assertRaisesRegex(RuntimeError, message)):
                internal_gpu.switch_internal_gpu(PCI, "internal-attach", self.sysfs)
            run.assert_not_called()
            bind.assert_not_called()

    def test_preview_audio_scope_keeps_audio_and_vfio_blockers(self):
        selected = {"pci": PCI, "vendor": "0x10de", "device": "0x2820", "external": False,
                    "boot_vga": False, "nodes": ["/dev/dri/renderD129"]}
        (self.audio / "sound/card7").mkdir(parents=True)
        occupied = {"complete": True, "processes": [
            {"pid": 1, "name": "terminal", "nodes": ["/dev/dri/renderD129"]},
            {"pid": 2, "name": "audio", "nodes": ["/dev/snd/controlC7"]},
            {"pid": 3, "name": "qemu", "nodes": ["/dev/vfio/22"]}]}
        with (patch.object(hardware, "gpu_inventory", return_value=[selected]),
              patch.object(hardware, "holders", return_value=occupied)):
            status = internal_gpu.inspect_internal_gpu(PCI, self.sysfs)
        self.assertEqual([p["pid"] for p in status["audio_holders"]["processes"]], [2, 3])
        self.assertIn("释放", internal_gpu.internal_switch_blocker(status, "internal-attach"))
        del status["audio_holders"]
        self.assertIn("释放", internal_gpu.internal_switch_blocker(status, "internal-attach"))
