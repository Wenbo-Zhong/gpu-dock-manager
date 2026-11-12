"""GTK4/libadwaita UI. The UI never writes sysfs or invokes privileged shells."""

from .i18n import tr, select_language
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import sys

# Keep GTK/libadwaita's own labels on the same language/fallback as our catalogs.
os.environ['LANGUAGE'] = select_language()

# Prefer the internal AMD panel's Mesa OpenGL renderer; avoid Vulkan enumeration.
from .rendering import configure_renderer
configure_renderer()

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 12) or (Adw.get_major_version(), Adw.get_minor_version()) < (1, 4):
    raise RuntimeError(tr('USB4 GPU Manager requires GTK 4.12+ and libadwaita 1.4+.'))

from . import __version__
from .client import Client
from .displays import INTERNAL_DISPLAY_ACTIONS
from .internal_gpu import ACTIONS as INTERNAL_ACTIONS, HOST_DRIVERS, VFIO, internal_switch_blocker, internal_switch_holders, releasable_internal_holder
from .presence import observe
from .system import gpu_holder_hint

INTERNAL_OPERATIONS = INTERNAL_ACTIONS | INTERNAL_DISPLAY_ACTIONS
EXTERNAL_OPERATIONS = {"attach", "detach", "queue-detach", "cancel-detach", "disable-displays", "restore-displays"}

CSS = """
.sidebar { background: alpha(@window_fg_color, .035); }
.gpu-panel { padding: 16px; border-radius: 16px; background: alpha(@window_fg_color,.045); }
.gpu-title { font-size: 19px; font-weight: 750; }
.gpu-section { font-weight: 700; }
.metric-number { font-size: 17px; font-weight: 700; }
.metric-box { padding: 8px 10px; border-radius: 9px; background: alpha(@window_fg_color,.045); }
.gpu-notice { padding: 9px 10px; border-radius: 8px; background: alpha(@accent_color,.10); }
.status-pill { padding: 5px 12px; border-radius: 18px; background: alpha(@accent_color,.15); color: @accent_color; font-weight: 700; }
.nav-item { padding: 11px 8px; }
.page-heading { font-size: 22px; font-weight: 700; }
.log-panel { padding: 14px; border-radius: 12px; }
"""


def label(text="", css=None, wrap=False):
    widget = Gtk.Label(label=text, xalign=0, wrap=wrap, selectable=True)
    if css:
        widget.add_css_class(css)
    return widget


def clear(box):
    while box.get_first_child():
        box.remove(box.get_first_child())


def row(title, subtitle="", value=""):
    result = Adw.ActionRow(title=title, subtitle=subtitle)
    result.set_use_markup(False)
    if value:
        suffix = label(value)
        suffix.add_css_class("dim-label")
        result.add_suffix(suffix)
    return result


def operation_summary(message):
    message = str(message)
    if "Traceback (most recent call last):" not in message:
        return message
    for line in reversed(message.splitlines()):
        if line.startswith(("RuntimeError: ", "OSError: ", "PermissionError: ")):
            return line.split(": ", 1)[1]
    return tr('Operation failed. See Events for details.')


def gpu_power_text(gpu):
    if not gpu or not (gpu.get("power_state") or gpu.get("runtime_status")):
        return tr('Not read')
    runtime = {"active": tr('Running'), "suspended": tr('Suspended'), "suspending": tr('Suspending'),
               "resuming": tr('Resuming')}.get(gpu.get("runtime_status"), tr('Unknown'))
    return f"{gpu.get('power_state') or tr('Unknown')} · {runtime}"


class GpuPanel(Gtk.Box):
    def __init__(self, category, name, link_label):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=300)
        self.add_css_class("gpu-panel")
        heading = Gtk.Box(spacing=6)
        category_label = label(category, "gpu-section")
        category_label.set_hexpand(True)
        heading.append(category_label)
        self.badge = label(tr('Detecting'), "status-pill")
        heading.append(self.badge)
        self.append(heading)
        self.title = label(name, "gpu-title", True)
        self.title.set_size_request(-1, 48)
        self.append(self.title)
        self.identity = label("", "dim-label", True)
        self.append(self.identity)
        self.power = label("", "dim-label", True)
        self.append(self.power)
        metrics = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True)
        self.metrics = {}
        for index, (key, title) in enumerate((("vram", tr('VRAM usage')), ("temp", tr('GPU temperature')),
                                               ("power", tr('GPU power')), ("link", link_label))):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            box.add_css_class("metric-box")
            box.append(label(title, "dim-label", True))
            number = label("—", "metric-number", True)
            self.metrics[key] = number
            box.append(number)
            metrics.attach(box, index % 2, index // 2, 1, 1)
        self.append(metrics)

        self.append(label(tr('Monitors'), "gpu-section"))
        self.display = label(tr('Detecting monitor ownership…'), "dim-label", True)
        self.append(self.display)
        display_buttons = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, min_children_per_line=1,
                                      max_children_per_line=2, column_spacing=6, row_spacing=6)
        self.disable = Gtk.Button(label=tr('Disable monitors'), sensitive=False)
        self.restore = Gtk.Button(label=tr('Restore monitors'), sensitive=False)
        display_buttons.append(self.disable)
        display_buttons.append(self.restore)
        self.append(display_buttons)

        self.append(label(tr('Drivers and device'), "gpu-section"))
        self.driver = label("", "dim-label", True)
        self.append(self.driver)
        self.buttons = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, min_children_per_line=1,
                                  max_children_per_line=3, column_spacing=6, row_spacing=6)
        self.append(self.buttons)
        self.blockers = label("", "dim-label", True)
        self.blockers.set_visible(False)
        self.append(self.blockers)
        self.notice = label("", "gpu-notice", True)
        self.notice.set_visible(False)
        self.append(self.notice)

    def add_button(self, title, callback, primary=False):
        button = Gtk.Button(label=title, sensitive=False)
        if primary:
            button.add_css_class("suggested-action")
        button.connect("clicked", lambda *_: callback())
        self.buttons.append(button)
        return button

    def update_metrics(self, metric, link):
        try:
            memory = f'{float(metric["memory_used_mib"]) / 1024:.1f} / {float(metric["memory_total_mib"]) / 1024:.0f} GB'
        except (KeyError, ValueError, TypeError):
            memory = "—"
        self.metrics["vram"].set_text(memory)
        for key, field, unit in (("temp", "temperature_c", "°C"), ("power", "power_w", "W")):
            value = metric.get(field)
            self.metrics[key].set_text(f"{value} {unit}" if value else "—")
        self.metrics["link"].set_text(link)

    def update_display(self, desktop, gpu, ready, empty):
        monitors = desktop.get("monitors", [])
        if monitors:
            summary = ", ".join(m["connector"] for m in monitors)
            summary += tr(' · {p0} active / {p1} connected').format(p0=sum((m['active'] for m in monitors)), p1=len(monitors))
            self.display.set_tooltip_text(", ".join(m["name"] for m in monitors))
        else:
            summary = tr('No monitor connected') if gpu and gpu.get("external_display_state") == "disconnected" else empty
            self.display.set_tooltip_text(None)
        self.display.set_text(summary)
        self.disable.set_sensitive(bool(ready and desktop.get("can_disable")))
        self.restore.set_sensitive(bool(ready and desktop.get("can_restore")))
        self.disable.set_tooltip_text(desktop.get("reason") or tr("Disable only this GPU's display outputs."))
        self.restore.set_tooltip_text(desktop.get("restore_reason") or tr('Restore the saved display layout.'))


class Window(Adw.ApplicationWindow):
    def __init__(self, app, capture=None, initial_page="overview"):
        super().__init__(application=app, title="USB4 GPU Manager", default_width=1160, default_height=820)
        self.client = Client()
        self.pool = ThreadPoolExecutor(max_workers=3)
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
        GLib.timeout_add_seconds(3, self.refresh)
        if capture:
            GLib.timeout_add_seconds(5, self.capture)

    def page(self, name, title, subtitle):
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        for setter in (outer.set_margin_start, outer.set_margin_end):
            setter(28)
        outer.set_margin_top(24)
        outer.set_margin_bottom(28)
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading.append(label(title, "page-heading"))
        heading.append(label(subtitle, "dim-label", True))
        outer.append(heading)
        clamp = Adw.Clamp(maximum_size=1050)
        clamp.set_child(outer)
        scroll.set_child(clamp)
        self.stack.add_named(scroll, name)
        return outer

    def build_overview(self):
        page = self.page("overview", tr('GPU status'), tr('Connections, displays, compute and resume recovery in one place.'))
        self.banner = Adw.Banner(title=tr('Reading local status…'), revealed=True)
        page.append(self.banner)
        panels = Gtk.Box(spacing=14, homogeneous=True)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900px"))
        narrow.add_setter(panels, "orientation", GObject.Value(Gtk.Orientation, Gtk.Orientation.VERTICAL))
        self.add_breakpoint(narrow)
        self.internal_panel = GpuPanel(tr('Internal GPU'), "RTX 4070", tr('Internal PCIe'))
        self.external_panel = GpuPanel(tr('External GPU'), tr('External NVIDIA GPU'), tr('USB4 receive speed'))
        for panel in (self.internal_panel, self.external_panel):
            panels.append(panel)
        page.append(panels)

        self.internal_attach = self.internal_panel.add_button(tr('Load'),
            lambda: self.confirm_internal_switch("internal-attach", self.internal_pci()), primary=True)
        self.internal_detach = self.internal_panel.add_button(tr('Unload'),
            lambda: self.confirm_internal_switch("internal-detach", self.internal_pci()))
        self.internal_inspect = self.internal_panel.add_button(tr('Inspect holders'),
            lambda: self.confirm_internal_switch(None, self.internal_pci()))
        self.attach_button = self.external_panel.add_button(tr('Reconnect'), lambda: self.submit("attach"), primary=True)
        self.detach_button = self.external_panel.add_button(tr('Safely remove'), lambda: self.confirm_detach(False))
        self.queue_button = self.external_panel.add_button(tr('Remove after logout'), lambda: self.confirm_detach(True))
        self.cancel_button = Gtk.Button(label=tr('Cancel request'), visible=False, halign=Gtk.Align.START)
        self.cancel_button.connect("clicked", lambda *_: self.submit("cancel-detach"))
        self.external_panel.append(self.cancel_button)

        for panel, internal in ((self.internal_panel, True), (self.external_panel, False)):
            panel.disable.connect("clicked", lambda _, internal=internal: self.confirm_disable_displays(internal=internal))
            panel.restore.connect("clicked", lambda _, internal=internal:
                                  self.submit("internal-restore-displays", {"expected_pci": self.internal_pci()})
                                  if internal else self.submit("restore-displays"))
        self.operation_label = label("", "gpu-notice", True)
        self.operation_label.set_visible(False)
        page.append(self.operation_label)
        self.health = Adw.PreferencesGroup(title=tr('Runtime status'))
        page.append(self.health)

    def internal_pci(self):
        return (self.data.get("internal_gpu") or {}).get("pci")

    def build_devices(self):
        page = self.page("devices", tr('Devices and monitors'), tr('Identify GPUs and monitors by PCI ownership; show connection and enable states separately.'))
        self.device_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        page.append(self.device_box)

    def build_deployment(self):
        page = self.page("deployment", tr('Deployment and recovery'), tr('After reinstalling Linux, select devices and deploy again. Existing configuration is backed up, and all changes can be previewed.'))
        self.deployment_banner = Adw.Banner(title="", revealed=True)
        page.append(self.deployment_banner)
        group = Adw.PreferencesGroup(title=tr('Device policy'))
        target_row = row(tr('Target external GPU'), tr('NVIDIA supported initially; the selected target will not be isolated.'))
        self.target_select = Gtk.DropDown(model=Gtk.StringList.new([tr('No device detected yet')]), valign=Gtk.Align.CENTER)
        self.target_select.set_size_request(270, -1)
        target_row.add_suffix(self.target_select)
        group.add(target_row)
        page.append(group)
        self.protection_group = Adw.PreferencesGroup(title=tr('Internal NVIDIA GPU isolation'), description=tr('Optional. Devices using VFIO are selected by default. GPUs driving monitors cannot be isolated.'))
        page.append(self.protection_group)
        notes = Adw.PreferencesGroup(title=tr('Deployment contents'))
        for title, subtitle in [(tr('One custom management service'), tr('Manage device detection, delayed loading, resume recovery and events in one place.')),
                                (tr('Keep display outputs and CUDA'), tr('Enable NVIDIA kernel suspend notifiers; do not unload solely because no monitor is connected.')),
                                (tr('Back up and migrate existing configuration'), tr('Preview files to be replaced. A restart is required after updating initramfs.'))]:
            notes.add(row(title, subtitle))
        page.append(notes)
        actions = Gtk.Box(spacing=10)
        self.deploy_button = Gtk.Button(label=tr('Preview deployment changes'))
        self.deploy_button.add_css_class("suggested-action")
        self.deploy_button.connect("clicked", lambda *_: self.preview_deployment())
        self.rollback_button = Gtk.Button(label=tr('Restore previous configuration'))
        self.rollback_button.connect("clicked", lambda *_: self.confirm_rollback())
        actions.append(self.deploy_button)
        actions.append(self.rollback_button)
        page.append(actions)
        help_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        help_box.append(label(tr('Install background components'), "heading"))
        help_box.append(label(tr('If the management service is not installed, run this command from the source directory. You can deploy device policies here after installation.'), "dim-label", True))
        source = Path(__file__).resolve().parent.parent
        self.install_command = "sudo bash " + str(source / "install.sh") if (source / "install.sh").exists() else "sudo bash ./install.sh"
        help_box.append(label(self.install_command, "monospace", True))
        copy = Gtk.Button(label=tr('Copy install command'), halign=Gtk.Align.START)
        copy.connect("clicked", lambda *_: self.copy_text(self.install_command))
        help_box.append(copy)
        page.append(help_box)

    def build_logs(self):
        page = self.page("logs", tr('Events'), tr('Device, deployment and sleep events from the management service. Exported status excludes process command lines and environment variables.'))
        controls = Gtk.Box(spacing=10)
        copy = Gtk.Button(label=tr('Copy diagnostic status'), icon_name="edit-copy-symbolic")
        copy.connect("clicked", lambda *_: self.copy_text(json.dumps(self.data, ensure_ascii=False, indent=2)))
        controls.append(copy)
        page.append(controls)
        self.log_text = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_text.add_css_class("log-panel")
        log_scroll = Gtk.ScrolledWindow(min_content_height=420, vexpand=True)
        log_scroll.set_child(self.log_text)
        page.append(log_scroll)

    def refresh(self):
        if self.refreshing:
            if self.data:
                self.apply_status(observe(self.data), completed=False)
            return True
        self.refreshing = True
        def work():
            try:
                data = self.client.status()
                GLib.idle_add(self.apply_status, data)
            except Exception as error:
                GLib.idle_add(self.refresh_error, str(error))
        self.pool.submit(work)
        return True

    def refresh_error(self, error):
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
        self.attach_button.set_sensitive(bool(ready and can_attach and not deploy.get("restart_required")))
        self.detach_button.set_sensitive(bool(ready and gpu and data.get("detach", {}).get("safe")))
        self.queue_button.set_sensitive(bool(ready and gpu and gpu["external_display_state"] in {"connected", "disconnected"}
                                             and not gpu["boot_vga"] and data.get("other_internal_display_ready")))
        self.detach_button.set_visible(not removed)
        self.queue_button.set_visible(not removed)
        attach_hint = (tr('Configuration deployed; restart first.') if deploy.get("restart_required") else
                       tr('Scan PCIe and load the driver.') if can_attach else tr('Connect the external GPU first.'))
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
        if completed:
            self.refreshing = False
        self.data = data
        gpu, connected = data.get("target"), data.get("service_connected", False)
        deploy = data.get("deployment", {})
        operation = data.get("operation", {})
        busy = self.display_busy or operation.get("state") in {"running", "waiting", "removing"}
        fresh = data.get("can_mutate_devices", False)
        transport = data.get("transport", {})
        removal_done = operation.get("state") == "done" and operation.get("action") in {"detach", "queue-detach"}
        removed = (connected and removal_done and transport.get("state") in {"detached", "disconnected"}
                   and not transport.get("pci_visible") and not transport.get("driver_cleanup_pending"))
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

    def submit(self, action, options=None):
        self.operation_pending = True
        display_action = action in {"disable-displays", "restore-displays", *INTERNAL_DISPLAY_ACTIONS}
        self.display_busy = display_action
        self.display_action = action if display_action else ""
        self.display_message = tr("Changing this desktop's external outputs…") if display_action else ""
        if self.data:
            self.apply_status(self.data, completed=False)
        self.show_operation(action, self.display_message or tr('Submitting to the management service…'))
        def finished(message, failed=False):
            self.operation_pending = False
            self.display_busy = False
            if display_action:
                self.display_message = (tr('Display operation failed: ') if failed else "") + str(message)
            if failed:
                self.pending_logout = False
            self.toast(operation_summary(message))
            self.refresh()
            return False
        def work():
            try:
                message = self.client.perform(action, options)
                GLib.idle_add(finished, message)
            except Exception as error:
                GLib.idle_add(finished, str(error), True)
        self.pool.submit(work)

    def close_to_tray(self, *_):
        app = self.get_application()
        if app.tray and app.tray.ready:
            self.set_visible(False)
            return True
        return False

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


class Application(Adw.Application):
    def __init__(self, capture=None, page="overview", unique=True):
        super().__init__(application_id="io.github.usb4gpumanager.App",
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS if unique and not capture else Gio.ApplicationFlags.NON_UNIQUE)
        self.capture = capture
        self.page = page
        self.tray = None
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        action = Gio.SimpleAction.new("quit", None)
        action.connect("activate", lambda *_: self.request_quit())
        self.add_action(action)
        if not self.capture:
            from .tray import Tray
            try:
                self.tray = Tray(self.activate, self.hide_to_tray, self.request_quit)
            except GLib.Error:
                self.tray = None

    def hide_to_tray(self):
        window = self.window
        if window:
            if self.tray and self.tray.ready:
                window.set_visible(False)
            else:
                window.toast(tr('The desktop tray is unavailable; keeping the window accessible.'))

    def request_quit(self):
        window = self.window
        if window and (window.operation_pending or window.data.get("operation", {}).get("state") in {"running", "waiting", "removing"}):
            window.present()
            window.toast(tr('Wait for the current operation to finish, or cancel the queued request, before exiting.'))
            return
        self.quit()

    def do_shutdown(self):
        if self.tray:
            self.tray.close()
        Adw.Application.do_shutdown(self)

    def do_activate(self):
        window = self.window
        if not window:
            window = Window(self, self.capture, self.page)
            self.window = window
        window.unminimize()
        window.present()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="USB4 GPU Manager")
    parser.add_argument("--page", choices=["overview", "devices", "deployment", "logs"], default="overview")
    parser.add_argument("--capture", metavar="PNG", help=argparse.SUPPRESS)
    args = parser.parse_args()
    return Application(args.capture, args.page).run([sys.argv[0]])
