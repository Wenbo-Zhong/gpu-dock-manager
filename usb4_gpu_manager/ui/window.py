"""GTK4/libadwaita 界面：展示服务状态，用户会话管理显示，特权操作交给服务。"""

from ..i18n import tr
from datetime import datetime
import json
import logging
import os
from pathlib import Path
from .gtk import Adw, Gdk, Gio, GLib, Gtk
from .pages import Pages
from .requests import RequestPool
from .state import operation_busy, removal_confirmed
from .. import __version__
from ..client import Client
from ..contracts import INTERNAL_DISPLAY_ACTIONS, DISPLAY_ACTIONS, INTERNAL_OPERATIONS, EXTERNAL_OPERATIONS
from ..internal_gpu import HOST_DRIVERS, VFIO, internal_switch_blocker, internal_switch_holders, releasable_internal_holder
from ..presence import observe
from ..system import gpu_holder_hint
from .components import CSS, GpuPanel, label, clear, row, operation_summary, gpu_power_text


class Window(Adw.ApplicationWindow, Pages):
    def __init__(self, app, capture=None, initial_page="overview"):
        super().__init__(application=app, title="USB4 GPU Manager", default_width=1160, default_height=820)
        self.client = Client()
        self.pool = RequestPool(max_workers=3)
        self.closing = False
        self.data = {}
        self.refreshing = False
        self.form_signature = None
        self.form_gpus = []
        self.protected_checks = []
        self.pending_logout = False
        self.last_operation = None
        self.display_busy = False
        self.display_message = ""
        self.display_action = ""
        self.operation_pending = False
        self.pending_requests = {}
        self.connect("close-request", self.close_to_tray)
        self.connect("realize", self.watch_minimize)
        self.capture_path = capture
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="USB4 GPU Manager", subtitle=tr('Internal and external GPUs · Local management')))
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=tr('Refresh status'))
        refresh.connect("clicked", lambda *_: self.refresh())
        header.pack_end(refresh)
        quit_button = Gtk.Button(icon_name="system-shutdown-symbolic", tooltip_text=tr('Quit application'))
        quit_button.connect("clicked", lambda *_: self.get_application().request_quit())
        header.pack_end(quit_button)
        layout.append(header)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=185, spacing=12)
        sidebar.add_css_class("sidebar")
        sidebar.set_margin_top(12)
        nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        nav.add_css_class("navigation-sidebar")
        nav.set_margin_start(10)
        nav.set_margin_end(10)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True, vexpand=True)
        self.page_names = ["overview", "devices", "deployment", "logs"]
        for name, title, icon in [("overview", tr('Overview'), "view-grid-symbolic"),
                                  ("devices", tr('Devices and monitors'), "video-display-symbolic"),
                                  ("deployment", tr('Deployment and recovery'), "preferences-system-symbolic"),
                                  ("logs", tr('Events'), "text-x-generic-symbolic")]:
            item = Gtk.Box(spacing=10)
            item.add_css_class("nav-item")
            item.append(Gtk.Image(icon_name=icon))
            item.append(Gtk.Label(label=title))
            nav.append(item)
        nav.connect("row-selected", lambda _, selected: self.stack.set_visible_child_name(self.page_names[selected.get_index()]) if selected else None)
        sidebar.append(nav)
        spacer = Gtk.Box(vexpand=True)
        sidebar.append(spacer)
        foot = label(tr('v{p0} · Local management').format(p0=__version__), "dim-label")
        foot.set_margin_start(22)
        foot.set_margin_bottom(22)
        sidebar.append(foot)
        body.append(sidebar)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        body.append(self.stack)
        layout.append(body)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(layout)
        self.set_content(self.toasts)

        self.build_overview()
        self.build_devices()
        self.build_deployment()
        self.build_logs()
        nav.select_row(nav.get_row_at_index(self.page_names.index(initial_page)))
        self.refresh()
        self.refresh_source = GLib.timeout_add_seconds(3, self.refresh)
        if capture:
            GLib.timeout_add_seconds(5, self.capture)

    def internal_pci(self):
        return (self.data.get("internal_gpu") or {}).get("pci")

    def refresh(self):
        if self.closing:
            return False
        if self.refreshing:
            if self.data:
                self.apply_status(observe(self.data), completed=False)
            return True
        self.refreshing = True
        def work():
            try:
                data = self.client.status()
                # GTK 控件只能由主循环更新；工作线程负责阻塞 RPC 和数据采集。
                GLib.idle_add(self.apply_status, data)
            except Exception as error:
                GLib.idle_add(self.refresh_error, str(error))
        self.pool.submit(work)
        return True

    def refresh_error(self, error):
        if self.closing:
            return False
        self.refreshing = False
        self.banner.set_title(tr('Failed to read status: ') + operation_summary(error))
        self.banner.set_revealed(True)

    def update_gpu_panels(self, data, gpu, metric, removed, busy):
        ready = data.get("service_connected") and data.get("can_mutate_devices") and not busy
        transport = data.get("transport", {})
        state = transport.get("state")
        previous = gpu or data.get("last_known_target") or {}
        panel = self.external_panel
        panel.title.set_text(metric.get("name") or previous.get("name") or tr('External NVIDIA GPU'))
        panel.identity.set_text(f"{previous.get('pci', tr('PCI not identified'))} · {(gpu['driver'] if gpu else tr('Driver unbound'))}")
        panel.power.set_text(tr('PCI power · ') + gpu_power_text(gpu))
        badge = (tr('Unexpected disconnect') if transport.get("driver_cleanup_pending") else tr('Driver error') if data.get("blocked_io")
                 else tr('Status stale') if data.get("status_stale") else tr('Removed') if state == "detached"
                 else tr('Connected') if gpu and gpu["driver"] == "nvidia" else tr('Waiting for driver') if gpu else tr('Disconnected'))
        panel.badge.set_text(badge)
        router = next((r for r in data.get("usb4", []) if not r["is_host"]), {})
        speed = router.get("rx_speed", "—")
        panel.update_metrics(metric, "—" if speed == "unknown" else speed)
        panel.update_display(data.get("desktop_displays", {}), gpu, ready,
                             tr('External output disabled') if removed else tr('No controllable external monitors'))
        deploy = data.get("deployment", {})
        can_attach = deploy.get("managed") or (data.get("internal_gpu") and previous
                                               and state in {"present", "detached"})
        self.attach_button.set_sensitive(bool(ready and can_attach and not deploy.get("restart_required")
                                              and not (gpu and gpu.get("boot_vga"))))
        self.detach_button.set_sensitive(bool(ready and gpu and data.get("detach", {}).get("safe")))
        self.queue_button.set_sensitive(bool(ready and gpu and gpu["external_display_state"] in {"connected", "disconnected"}
                                             and not gpu["boot_vga"] and data.get("other_internal_display_ready")))
        self.detach_button.set_visible(not removed)
        self.queue_button.set_visible(not removed)
        attach_hint = (tr('Configuration deployed; restart first.') if deploy.get("restart_required") else
                       tr('Scan PCIe and load the driver.') if can_attach else tr('Connect the external GPU first.'))
        if gpu and gpu.get("boot_vga"):
            attach_hint = tr('This GPU is the boot GPU or drives an internal panel and cannot be safely removed.')
        self.attach_button.set_tooltip_text(attach_hint)
        panel.driver.set_text((tr('Safely removed · USB4 disconnected') if state == "disconnected" else
                               tr('Safely removed · You may unplug USB4')) if removed else attach_hint)
        reasons = list(data.get("detach", {}).get("blockers", [])) if gpu else []
        if gpu and not data.get("other_internal_display_ready"):
            reasons.append(tr('Removal after logout requires an internal panel driven by another GPU.'))
        panel.blockers.set_text("\n".join(reasons))
        panel.blockers.set_visible(bool(reasons))

        internal = data.get("internal_gpu") or {}
        card = next((g for g in data.get("gpus", []) if g["pci"] == internal.get("pci")), None)
        panel = self.internal_panel
        graphics, audio = internal.get("graphics_driver"), internal.get("audio_driver")
        drivers = graphics, audio
        reason = internal.get("reason")
        can_switch = bool(ready and card and not reason)
        panel.title.set_text("GeForce RTX 4070 Laptop GPU")
        panel.identity.set_text(f"{internal['pci']} · {graphics or tr('Unknown driver')}" if internal.get("pci") else tr('No supported internal GPU detected'))
        panel.power.set_text(tr('PCI power · ') + gpu_power_text(card))
        panel.badge.set_text(tr('Isolated') if drivers == (VFIO, VFIO) else tr('Loaded') if drivers == HOST_DRIVERS
                             else tr('Partially loaded') if graphics == "nvidia" else tr('Waiting for driver') if card else tr('Not detected'))
        internal_metric = next((item for item in data.get("metrics", []) if card and item["pci"] == card["pci"]), {})
        panel.update_metrics(internal_metric, tr('Fixed connection'))
        sleeping = bool(card and card.get("runtime_status") == "suspended")
        for key in ("vram", "temp", "power"):
            if sleeping:
                panel.metrics[key].set_text(tr('Sleeping'))
            panel.metrics[key].set_tooltip_text(None if internal_metric else
                tr('Sampling pauses while the GPU sleeps. When the driver is loaded and the GPU is active in D0, metrics refresh every 10 seconds.'))
        panel.update_display(data.get("internal_desktop_displays", {}), card, can_switch,
                             tr('Load the driver to detect HDMI monitors'))
        panel.driver.set_text(tr('HDMI audio · {p0}   IOMMU group · {p1}').format(p0=audio, p1=internal['iommu_group'])
                              if audio else tr('Load or unload the GPU and HDMI audio drivers.'))
        panel.blockers.set_text(reason or "")
        panel.blockers.set_visible(bool(reason))
        self.internal_attach.set_sensitive(can_switch and drivers != HOST_DRIVERS)
        self.internal_detach.set_sensitive(can_switch and drivers != (VFIO, VFIO))
        self.internal_inspect.set_sensitive(bool(data.get("service_connected") and card and not busy and not reason))

    def show_operation(self, action, message):
        target = (self.internal_panel.notice if action in INTERNAL_OPERATIONS else
                  self.external_panel.notice if action in EXTERNAL_OPERATIONS else self.operation_label)
        for widget in (self.internal_panel.notice, self.external_panel.notice, self.operation_label):
            widget.set_text(operation_summary(message) if widget is target else "")
            widget.set_visible(widget is target and bool(message))

    def apply_status(self, data, completed=True):
        if self.closing:
            return False
        if completed:
            self.refreshing = False
        self.data = data
        gpu, connected = data.get("target"), data.get("service_connected", False)
        deploy = data.get("deployment", {})
        operation = data.get("operation", {})
        busy = operation_busy(data, self.operation_pending, self.display_busy)
        fresh = data.get("can_mutate_devices", False)
        transport = data.get("transport", {})
        removal_done = operation.get("state") == "done" and operation.get("action") in {"detach", "queue-detach"}
        removed = removal_confirmed(data)
        stale = data.get("status_stale", False)
        self.banner.set_revealed(not connected or bool(data.get("last_error")) or stale or deploy.get("restart_required", False))
        if not connected:
            banner_text = tr('Management service disconnected · Read-only preview')
        elif stale:
            banner_text = data.get("last_error") or (tr('Detailed status is stale ({p0:.0f} seconds since the last update). Showing independent USB4 connectivity.').format(p0=data.get('status_age_seconds', 0)) if data.get("sample_available") else tr('Fetching detailed device status; USB4 connectivity is monitored independently.'))
        elif deploy.get("restart_required"):
            banner_text = tr('Configuration deployed; save your work and restart')
        else:
            banner_text = data.get("last_error", "")
        self.banner.set_title(operation_summary(banner_text))
        metric = next((m for m in data.get("metrics", []) if gpu and m["pci"] == gpu["pci"]), {})
        self.update_gpu_panels(data, gpu, metric, removed, busy)

        for old in getattr(self, "health_rows", []):
            self.health.remove(old)
        parameters = data.get("parameters", {})
        connections = tr('External monitor connected') if gpu and gpu["external_display_state"] == "connected" else tr('No external monitor connected') if gpu and gpu["external_display_state"] == "disconnected" else tr('Connection state unknown')
        internal = [g for g in data.get("gpus", []) if g["driver"] == "vfio-pci"]
        resume = data.get("last_resume")
        resume_text = {"ready": tr('Resume complete'), "waiting": tr('Waiting for USB4 to stabilize'), "absent": tr('System resumed; no GPU connected')}.get(resume["state"], tr('Unknown')) if resume else tr('No resume event recorded by this service yet')
        self.health_rows = [row(tr('NVIDIA driver'), value=metric.get("driver_version", tr('Not read'))),
                            row(tr('External monitors'), tr("Based on this eGPU's DRM connectors"), connections),
                            row(tr('Suspend and resume'), tr('Preserve VRAM and display outputs'), tr('Kernel suspend notifiers enabled') if parameters.get("UseKernelSuspendNotifiers") == "1" else tr('Not configured / driver not loaded')),
                            row(tr('Internal GPU isolation'), value=", ".join(g["name"] for g in internal) or tr('Not isolated')),
                            row(tr('Last resume'), value=resume_text)]
        for item in self.health_rows:
            self.health.add(item)
        action = self.display_action if self.display_message else operation.get("action", "")
        message = self.display_message or ("" if removal_done else operation.get("message", ""))
        self.show_operation(action, message)
        self.cancel_button.set_visible(connected and operation.get("action") == "queue-detach" and operation.get("state") == "waiting")
        self.deploy_button.set_sensitive(connected and fresh and bool(self.form_gpus or data.get("gpus")) and not busy)
        self.rollback_button.set_sensitive(connected and deploy.get("rollback_available", False) and not busy)
        self.deployment_banner.set_title(tr('Configuration deployed · Restart required') if deploy.get("restart_required") else tr('Unified management enabled') if deploy.get("managed") else tr('Device policy not migrated; existing system configuration remains active'))
        automatic = data.get("automation_enabled")
        self.automation_switch.handler_block_by_func(self.set_automation)
        try:
            self.automation_switch.set_active(automatic is True)
            self.automation_switch.set_state(automatic is True)
        finally:
            self.automation_switch.handler_unblock_by_func(self.set_automation)
        self.automation_switch.set_sensitive(connected and isinstance(automatic, bool) and not busy and not self.operation_pending)
        self.update_devices(data)
        self.update_form(data)
        events = data.get("events", [])
        self.log_text.get_buffer().set_text("\n\n".join(datetime.fromtimestamp(e["time"]).strftime("%m-%d %H:%M:%S") + "  " + e["message"] for e in events) or tr('Waiting for management events. Before installation, use journalctl to inspect existing service logs.'))
        operation_key = (operation.get("state"), operation.get("message"))
        if operation_key != self.last_operation and operation.get("state") in {"done", "error"}:
            if removed and self.stack.get_visible_child_name() != "overview":
                self.toast(tr('GPU safely removed'))
            elif not removal_done and (self.stack.get_visible_child_name() != "overview"
                                       or operation.get("action") not in INTERNAL_OPERATIONS | EXTERNAL_OPERATIONS):
                self.toast(operation_summary(operation.get("message", "")))
        self.last_operation = operation_key
        if self.pending_logout and operation.get("action") == "queue-detach":
            if operation.get("state") == "waiting" and fresh:
                self.pending_logout = False
                self.request_logout()
            elif operation.get("state") in {"error", "done"}:
                self.pending_logout = False
        return False

    def update_devices(self, data):
        signature = json.dumps({key: data.get(key) for key in
                               ("gpus", "desktop_displays", "internal_desktop_displays", "services", "usb4")}, sort_keys=True)
        if signature == getattr(self, "device_signature", None):
            return
        self.device_signature = signature
        clear(self.device_box)
        words = {"connected": tr('Connected'), "disconnected": tr('Disconnected'), "unknown": tr('Unknown')}
        desktop_monitors = {m["connector"]: m for m in data.get("desktop_displays", {}).get("monitors", [])}
        internal = data.get("internal_gpu") or {}
        for gpu in data.get("gpus", []):
            group = Adw.PreferencesGroup(title=gpu["name"], description=f'{gpu["pci"]} · {gpu["driver"]}')
            gpu_monitors = desktop_monitors if gpu == data.get("target") else {}
            if internal.get("pci") == gpu["pci"]:
                desktop = data.get("internal_desktop_displays") or {}
                gpu_monitors = {m["connector"]: m for m in desktop.get("monitors", [])}
            if gpu.get("power_state") or gpu.get("runtime_status"):
                group.add(row(tr('PCI power state'), tr('Runtime power management'), gpu_power_text(gpu)))
            for c in gpu["connectors"]:
                category = {"internal": tr('Internal panel'), "external": tr('External connector'), "virtual": tr('Virtual connector')}[c["kind"]]
                desktop = gpu_monitors.get(c["name"])
                enabled = (tr(' · desktop output active') if desktop["active"] else tr(' · desktop output disabled')) if desktop else (tr(' · enabled in kernel') if c["enabled"] == "enabled" else tr(' · disabled in kernel'))
                group.add(row(c["name"], category, words.get(c["status"], c["status"]) + enabled))
            if not gpu["connectors"]:
                group.add(row(tr('No DRM connectors yet'), tr('Missing connectors do not mean no monitor is connected when the driver is unbound.')))
            self.device_box.append(group)
        group = Adw.PreferencesGroup(title="USB4 / Thunderbolt")
        for router in data.get("usb4", []):
            if not router["is_host"]:
                group.add(row(router["name"], f'{router["vendor"]} · {router["id"]}', router["rx_speed"]))
        self.device_box.append(group)
        services = Adw.PreferencesGroup(title=tr('Service integration'), description=tr('Inactive is normal after a one-shot task completes.'))
        for unit in data.get("services", []):
            state = (tr('Not installed') if unit.get("LoadState") == "not-found" else
                     tr('Running') if unit.get("ActiveState") == "active" else
                     tr('Failed') if unit.get("ActiveState") == "failed" else tr('Waiting for an event / completed'))
            services.add(row(unit["Id"], unit.get("UnitFileState", "") + " · " + unit.get("Result", ""), state))
        self.device_box.append(services)

    def update_form(self, data):
        signature = tuple((g["pci"], g["driver"]) for g in data.get("gpus", []))
        # 普通状态刷新不重建表单，保留用户尚未提交的设备选择和隔离勾选。
        if signature == self.form_signature:
            return
        self.form_signature = signature
        self.form_gpus = [g for g in data.get("gpus", []) if g["vendor"] == "0x10de" and not g["boot_vga"] and g.get("external")]
        metric_names = {m["pci"]: m["name"] for m in data.get("metrics", [])}
        self.target_select.set_model(Gtk.StringList.new([metric_names.get(g["pci"], g["name"]) + " · " + g["pci"] for g in self.form_gpus] or [tr('No NVIDIA device found')]))
        suggested = data.get("profile") or data.get("suggested_profile", {})
        selector = suggested.get("target", {})
        for i, gpu in enumerate(self.form_gpus):
            if all(gpu.get(k) == v for k, v in selector.items()) and selector:
                self.target_select.set_selected(i)
        for item, _ in self.protected_checks:
            self.protection_group.remove(item)
        self.protected_checks = []
        for gpu in data.get("gpus", []):
            if gpu["vendor"] != "0x10de" or gpu.get("external"):
                continue
            item = row(gpu["name"], gpu["pci"])
            check = Gtk.CheckButton(active=gpu["driver"] == "vfio-pci", valign=Gtk.Align.CENTER)
            item.add_suffix(check)
            self.protection_group.add(item)
            self.protected_checks.append((item, (gpu, check)))

    def profile_from_form(self):
        index = self.target_select.get_selected()
        if index >= len(self.form_gpus):
            raise ValueError(tr('Connect and select the target external GPU.'))
        gpu = self.form_gpus[index]
        protected = [{"vendor": g["vendor"], "device": g["device"]} for _, (g, check) in self.protected_checks if check.get_active()]
        return {"schema": 1, "enabled": True, "target": {"vendor": gpu["vendor"], "device": gpu["device"]},
                "protected": protected, "stable_seconds": 12, "keep_path_awake": True}

    def preview_deployment(self):
        try:
            profile = self.profile_from_form()
        except Exception as error:
            self.toast(str(error))
            return
        def work():
            try:
                details = self.client.preview(profile)
                GLib.idle_add(self.show_plan, details)
            except Exception as error:
                GLib.idle_add(self.toast, str(error))
        self.pool.submit(work)

    def show_plan(self, details):
        dialog = Adw.MessageDialog.new(self, tr('Deploy this device policy?'), details["summary"])
        dialog.add_response("cancel", tr('Cancel'))
        dialog.add_response("deploy", tr('Authorize and deploy'))
        dialog.set_response_appearance("deploy", Adw.ResponseAppearance.SUGGESTED)
        text = "\n\n".join(path + "\n" + body for path, body in details["files"].items())
        if details["migrate"]:
            text += tr('\nBack up and migrate:\n') + "\n".join(details["migrate"])
        view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(text)
        scroll = Gtk.ScrolledWindow(min_content_height=270, max_content_height=360)
        scroll.set_child(view)
        dialog.set_extra_child(scroll)
        dialog.connect("response", lambda _, answer: self.submit("deploy", {"profile": details["profile"]}) if answer == "deploy" else None)
        dialog.present()
        return dialog

    def confirm_detach(self, queued):
        gpu = self.data.get("target")
        if not gpu:
            return
        text = (tr("Save your work and enable the internal panel. This GPU's external outputs will stop; monitor cables may stay connected. The manager will queue removal and open logout confirmation. Sign in using the internal panel, and unplug only after confirmation. The request expires after 180 seconds if the GPU remains held.") if queued else
                tr('The manager will recheck monitors and holders, then remove the GPU and its audio function. Unplug USB4 only after safe-removal confirmation.'))
        if queued and 'KDE' in os.environ.get('XDG_CURRENT_DESKTOP', '').upper().split(':'):
            text = (tr('Save your work and enable the internal panel. The manager will queue removal and open KDE logout confirmation. Wait at the login screen after logout; immediately signing back into KDE may reacquire the GPU. The existing GPU-ignore rule applies only to GNOME. KDE logout removal still needs hardware testing. The request expires after 180 seconds. Unplug only after the manager confirms safe removal.'))
        dialog = Adw.MessageDialog.new(self, tr('Log out and safely remove?') if queued else tr('Safely remove the external GPU?'), text)
        dialog.add_response("cancel", tr('Cancel'))
        dialog.add_response("confirm", tr('Queue removal and open logout confirmation') if queued else tr('Check and remove'))
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        def respond(_, answer):
            if answer == "confirm":
                self.pending_logout = queued
                self.submit("queue-detach" if queued else "detach", {"expected_pci": gpu["pci"]})
        dialog.connect("response", respond)
        dialog.present()
        return dialog

    def confirm_disable_displays(self, *, internal=False):
        target = self.data.get("target")
        action = "internal-disable-displays" if internal else "disable-displays"
        pci = self.internal_pci() if internal else (target or {}).get("pci")
        if not pci:
            return
        dialog = Adw.MessageDialog.new(self, tr("Disable this GPU's external monitors?"),
            tr("Save your work and enable another GPU's internal panel. Only the target GPU's external outputs will stop; the GPU and compute tasks stay connected. The desktop may reposition other monitors. Use Restore monitors to recover the layout. Check removal conditions separately afterward."))
        dialog.add_response("cancel", tr('Cancel'))
        dialog.add_response("confirm", tr('Disable monitors'))
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _, answer: self.submit(action, {"expected_pci": pci}) if answer == "confirm" else None)
        dialog.present()
        return dialog

    def confirm_internal_switch(self, action, pci):
        if not pci:
            return
        attach = action == "internal-attach"
        title = (tr('Load the internal 4070?') if attach else tr('Unload the internal 4070?')) if action else tr('Internal 4070 holders')
        dialog = Adw.MessageDialog.new(self, title, tr('Reading current drivers and holders…'))
        dialog.add_response("cancel", tr('Cancel') if action else tr('Close'))
        if action:
            dialog.add_response("confirm", tr('Confirm switch'))
            dialog.set_response_enabled("confirm", False)
            dialog.set_response_appearance("confirm", Adw.ResponseAppearance.SUGGESTED if attach else Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect("response", lambda _, answer: self.submit(action, {"expected_pci": pci}) if answer == "confirm" else None)
        dialog.present()

        def show(status, error=None):
            if not dialog.get_visible():
                return False
            if error:
                dialog.set_body(tr('Cannot inspect the 4070: ') + error)
                return False
            graphics, audio = status["graphics_driver"], status["audio_driver"]
            current = (tr('Loaded') if (graphics, audio) == HOST_DRIVERS else tr('Unloaded and isolated')
                       if (graphics, audio) == (VFIO, VFIO) else tr('Graphics loaded; audio driver differs')
                       if graphics == "nvidia" else tr('Partially bound'))
            lines = [tr('Current state · {p0}').format(p0=current), tr('Graphics driver {p0}   HDMI audio {p1}').format(p0=graphics, p1=audio)]
            occupied = status["holders"]
            blocker = internal_switch_blocker(status, action) if action else ""
            scoped = bool(action and graphics == ("nvidia" if attach else VFIO)
                          and audio != ("snd_hda_intel" if attach else VFIO) and "audio_holders" in status)
            blocking_pids = {p["pid"] for p in internal_switch_holders(status, action)["processes"]} if action else set()
            if scoped:
                lines.extend(("", tr('This action switches only the HDMI audio driver; checking audio and VFIO holders.')))
            if action == "internal-detach" and any(releasable_internal_holder(p, action) for p in occupied["processes"]):
                lines.extend(("", tr('Before unloading, pause nvidia-powerd and, if needed, briefly stop WirePlumber (audio may be interrupted). Restore WirePlumber after the operation. Restore nvidia-powerd if unloading fails or when the GPU is loaded again.')))
            if blocker:
                lines.extend(("", blocker))
            if occupied["processes"]:
                entries = []
                for process in occupied["processes"]:
                    direct = process["direct_nodes"]
                    kind = tr('This device') if direct else tr('Shared or other NVIDIA nodes')
                    if scoped:
                        kind += tr(' · release required') if process["pid"] in blocking_pids else tr(' · no release needed for this action')
                    if releasable_internal_holder(process, action):
                        kind += tr(' · released automatically')
                    entries.append(f'{process["name"]} (PID {process["pid"]})· {kind}\n'
                                   + ", ".join(direct or process["nodes"])
                                   + ("\n" + gpu_holder_hint(process) if not action and gpu_holder_hint(process) else ""))
                processes = label("\n\n".join(entries), wrap=True)
                scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                            max_content_height=220, propagate_natural_height=True)
                scroll.set_child(processes)
                dialog.set_extra_child(scroll)
            elif occupied["complete"]:
                lines.extend(("", tr('No holders found.')))
            if not occupied["complete"] and not blocker:
                lines.extend(("", tr('Holder scan is incomplete.')))
            if action and not blocker:
                lines.extend(("", tr('Keep the AMD internal panel enabled. Holders will be checked again before switching.')))
                dialog.set_response_enabled("confirm", True)
            dialog.set_body("\n".join(lines))
            return False

        def work():
            try:
                GLib.idle_add(show, self.client.internal_gpu_status(pci))
            except Exception as error:
                GLib.idle_add(show, None, operation_summary(error))
        self.pool.submit(work)
        return dialog

    def confirm_rollback(self):
        dialog = Adw.MessageDialog.new(self, tr('Restore the pre-deployment configuration?'), tr('Restore backed-up files and rebuild initramfs. A restart is required.'))
        dialog.add_response("cancel", tr('Cancel'))
        dialog.add_response("confirm", tr('Authorize and restore'))
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _, answer: self.submit("rollback") if answer == "confirm" else None)
        dialog.present()

    def set_automation(self, switch, enabled):
        self.submit("set-automation", {"enabled": enabled})
        return True

    def submit(self, action, options=None):
        if getattr(self, "closing", False):
            return
        # 授权弹窗和本地显示操作也属于未完成请求；仅允许额外提交队列取消。
        busy = operation_busy(self.data, self.operation_pending)
        if busy and (action != "cancel-detach" or action in self.pending_requests.values()):
            self.toast(tr('Another operation is in progress. Wait or cancel the queued request.'))
            return
        request = object()
        # 按请求身份收尾，取消请求的回执不能清除另一条未完成请求的忙碌状态。
        self.pending_requests[request] = action
        self.operation_pending = True
        display_action = action in DISPLAY_ACTIONS
        if display_action:
            self.display_busy = True
            self.display_action = action
            self.display_message = tr("Changing this desktop's external outputs…")
        elif not self.display_busy:
            self.display_action, self.display_message = "", ""
        if self.data:
            self.apply_status(self.data, completed=False)
        self.show_operation(action, self.display_message or tr('Submitting to the management service…'))
        def finished(message, failed=False):
            self.pending_requests.pop(request, None)
            self.operation_pending = bool(self.pending_requests)
            self.display_busy = any(a in DISPLAY_ACTIONS
                                    for a in self.pending_requests.values())
            if getattr(self, "closing", False):
                if failed:
                    logging.getLogger("usb4-gpu-manager").error(
                        "Session request %s failed during client shutdown: %s", action, message)
                # 本地请求已走完补偿，再次评估退出；不刷新隐藏窗口或自动注销。
                self.get_application().request_quit()
                return False
            if display_action:
                self.display_message = (tr('Display operation failed: ') if failed else "") + str(message)
            if failed and action != "cancel-detach":
                self.pending_logout = False
            self.toast(operation_summary(message))
            # 远端回执可能只表示预约成功，实际任务进度仍从 GetStatus 获取。
            self.refresh()
            return False
        def work():
            try:
                message = self.client.perform(action, options)
                GLib.idle_add(finished, message)
            except Exception as error:
                GLib.idle_add(finished, str(error), True)
        try:
            self.pool.submit(work)
        except Exception as error:
            finished(str(error), True)

    def close_to_tray(self, *_):
        app = self.get_application()
        if app.tray and app.tray.ready:
            self.set_visible(False)
            return True
        # 无托盘时退出客户端；系统服务和已提交任务仍独立运行。
        app.request_quit()
        return True

    def watch_minimize(self, *_):
        surface = self.get_surface()
        if isinstance(surface, Gdk.Toplevel):
            surface.connect("notify::state", lambda surface, _: self.get_application().hide_to_tray()
                            if surface.get_state() & Gdk.ToplevelState.MINIMIZED else None)

    def request_logout(self):
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            if 'KDE' in os.environ.get('XDG_CURRENT_DESKTOP', '').upper().split(':'):
                connection.call('org.kde.LogoutPrompt', '/LogoutPrompt', 'org.kde.LogoutPrompt',
                                'promptLogout', None, None, Gio.DBusCallFlags.NONE, 5000, None, None)
                return
            connection.call("org.gnome.SessionManager", "/org/gnome/SessionManager", "org.gnome.SessionManager",
                            "Logout", GLib.Variant("(u)", (0,)), None, Gio.DBusCallFlags.NONE, 5000, None, None)
        except GLib.Error:
            self.toast(tr('Removal queued. Log out using the desktop menu, or cancel the request.'))

    def copy_text(self, text):
        Gdk.Display.get_default().get_clipboard().set(text)
        self.toast(tr('Copied.'))

    def toast(self, message):
        self.toasts.add_toast(Adw.Toast.new(str(message)[:180]))
        return False

    def capture(self):
        from gi.repository import Gsk
        paintable = Gtk.WidgetPaintable.new(self)
        capture = Gtk.Snapshot()
        paintable.snapshot(capture, self.get_width(), self.get_height())
        node = capture.to_node()
        if node:
            renderer = Gsk.Renderer.new_for_surface(self.get_surface())
            texture = renderer.render_texture(node, None)
            texture.save_to_png(self.capture_path)
            renderer.unrealize()
        self.get_application().quit()
        return False
