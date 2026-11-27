"""页面布局与控件装配；回调和状态仍由唯一 Window 实例维护。"""

import json
from pathlib import Path

from ..i18n import tr
from .gtk import Adw, GObject, Gtk
from .components import GpuPanel, label, row


class Pages:
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
            # 用默认参数捕获本轮类别，避免循环结束后两个面板都操作外置 GPU。
            panel.disable.connect("clicked", lambda _, internal=internal: self.confirm_disable_displays(internal=internal))
            panel.restore.connect("clicked", lambda _, internal=internal:
                                  self.submit("internal-restore-displays", {"expected_pci": self.internal_pci()})
                                  if internal else self.submit("restore-displays"))
        self.operation_label = label("", "gpu-notice", True)
        self.operation_label.set_visible(False)
        page.append(self.operation_label)
        self.health = Adw.PreferencesGroup(title=tr('Runtime status'))
        page.append(self.health)

    def build_devices(self):
        page = self.page("devices", tr('Devices and monitors'), tr('Identify GPUs and monitors by PCI ownership; show connection and enable states separately.'))
        self.device_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        page.append(self.device_box)

    def build_deployment(self):
        page = self.page("deployment", tr('Deployment and recovery'), tr('After reinstalling Linux, select devices and deploy again. Existing configuration is backed up, and all changes can be previewed.'))
        self.deployment_banner = Adw.Banner(title="", revealed=True)
        page.append(self.deployment_banner)
        group = Adw.PreferencesGroup(title=tr('Device policy'))
        automation = row(tr('Automatic GPU management'),
                         tr('Control automatic GPU connection, switching and resume recovery. Manual controls stay available.'))
        self.automation_switch = Gtk.Switch(valign=Gtk.Align.CENTER, sensitive=False)
        self.automation_switch.connect("state-set", self.set_automation)
        automation.add_suffix(self.automation_switch)
        automation.set_activatable_widget(self.automation_switch)
        group.add(automation)
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
        source = Path(__file__).resolve().parents[2]
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
