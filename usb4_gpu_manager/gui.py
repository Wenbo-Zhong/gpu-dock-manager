"""GTK4/libadwaita UI. The UI never writes sysfs or invokes privileged shells."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import sys

# A removal utility must not keep GPU device files open for its own UI.
# This small dashboard uses GTK's CPU renderer instead of enumerating Vulkan GPUs.
os.environ.setdefault("GSK_RENDERER", "cairo")

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 12) or (Adw.get_major_version(), Adw.get_minor_version()) < (1, 4):
    raise RuntimeError("USB4 GPU Manager requires GTK 4.12+ and libadwaita 1.4+.")

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
    return "操作失败，详情见事件记录。"


def gpu_power_text(gpu):
    if not gpu or not (gpu.get("power_state") or gpu.get("runtime_status")):
        return "未读取"
    runtime = {"active": "运行中", "suspended": "已休眠", "suspending": "正在休眠",
               "resuming": "正在唤醒"}.get(gpu.get("runtime_status"), "未知")
    return f'{gpu.get("power_state") or "未知"} · {runtime}'


class GpuPanel(Gtk.Box):
    def __init__(self, category, name, link_label):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=300)
        self.add_css_class("gpu-panel")
        heading = Gtk.Box(spacing=6)
        category_label = label(category, "gpu-section")
        category_label.set_hexpand(True)
        heading.append(category_label)
        self.badge = label("检测中", "status-pill")
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
        for index, (key, title) in enumerate((("vram", "显存占用"), ("temp", "核心温度"),
                                               ("power", "显卡功耗"), ("link", link_label))):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            box.add_css_class("metric-box")
            box.append(label(title, "dim-label"))
            number = label("—", "metric-number", True)
            self.metrics[key] = number
            box.append(number)
            metrics.attach(box, index % 2, index // 2, 1, 1)
        self.append(metrics)

        self.append(label("显示器", "gpu-section"))
        self.display = label("正在检测屏幕归属…", "dim-label", True)
        self.append(self.display)
        display_buttons = Gtk.Box(spacing=6)
        self.disable = Gtk.Button(label="停用外屏", sensitive=False)
        self.restore = Gtk.Button(label="恢复外屏", sensitive=False)
        display_buttons.append(self.disable)
        display_buttons.append(self.restore)
        self.append(display_buttons)

        self.append(label("驱动与设备", "gpu-section"))
        self.driver = label("", "dim-label", True)
        self.append(self.driver)
        self.buttons = Gtk.Box(spacing=6)
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
            summary = "、".join(m["connector"] for m in monitors)
            summary += f' · {sum(m["active"] for m in monitors)} 正在输出 / {len(monitors)} 已接屏'
            self.display.set_tooltip_text("、".join(m["name"] for m in monitors))
        else:
            summary = "未接显示器" if gpu and gpu.get("external_display_state") == "disconnected" else empty
            self.display.set_tooltip_text(None)
        self.display.set_text(summary)
        self.disable.set_sensitive(bool(ready and desktop.get("can_disable")))
        self.restore.set_sensitive(bool(ready and desktop.get("can_restore")))
        self.disable.set_tooltip_text(desktop.get("reason") or "仅停用这张显卡的显示输出。")
        self.restore.set_tooltip_text(desktop.get("restore_reason") or "恢复此前保存的显示布局。")


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
        header.set_title_widget(Adw.WindowTitle(title="USB4 GPU Manager", subtitle="内置与外置显卡 · 本地管理"))
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="刷新状态")
        refresh.connect("clicked", lambda *_: self.refresh())
        header.pack_end(refresh)
        quit_button = Gtk.Button(icon_name="system-shutdown-symbolic", tooltip_text="退出程序")
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
        for name, title, icon in [("overview", "状态总览", "view-grid-symbolic"),
                                  ("devices", "设备与显示器", "video-display-symbolic"),
                                  ("deployment", "部署与恢复", "preferences-system-symbolic"),
                                  ("logs", "事件记录", "text-x-generic-symbolic")]:
            item = Gtk.Box(spacing=10)
            item.add_css_class("nav-item")
            item.append(Gtk.Image(icon_name=icon))
            item.append(Gtk.Label(label=title))
            nav.append(item)
        nav.connect("row-selected", lambda _, selected: self.stack.set_visible_child_name(self.page_names[selected.get_index()]) if selected else None)
        sidebar.append(nav)
        spacer = Gtk.Box(vexpand=True)
        sidebar.append(spacer)
        foot = label(f"v{__version__} · 本地运行", "dim-label")
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
        page = self.page("overview", "显卡状态", "连接、显示、计算与睡眠恢复，一处查看。")
        self.banner = Adw.Banner(title="正在读取本机状态…", revealed=True)
        page.append(self.banner)
        panels = Gtk.Box(spacing=14, homogeneous=True)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900px"))
        narrow.add_setter(panels, "orientation", GObject.Value(Gtk.Orientation, Gtk.Orientation.VERTICAL))
        self.add_breakpoint(narrow)
        self.internal_panel = GpuPanel("内置显卡", "RTX 4070", "内置 PCIe")
        self.external_panel = GpuPanel("外置显卡", "外置 NVIDIA 显卡", "USB4 接收速率")
        for panel in (self.internal_panel, self.external_panel):
            panels.append(panel)
        page.append(panels)

        self.internal_attach = self.internal_panel.add_button("加载",
            lambda: self.confirm_internal_switch("internal-attach", self.internal_pci()), primary=True)
        self.internal_detach = self.internal_panel.add_button("卸载",
            lambda: self.confirm_internal_switch("internal-detach", self.internal_pci()))
        self.internal_inspect = self.internal_panel.add_button("检查占用",
            lambda: self.confirm_internal_switch(None, self.internal_pci()))
        self.attach_button = self.external_panel.add_button("重新连接", lambda: self.submit("attach"), primary=True)
        self.detach_button = self.external_panel.add_button("安全移除", lambda: self.confirm_detach(False))
        self.queue_button = self.external_panel.add_button("注销后移除", lambda: self.confirm_detach(True))
        self.cancel_button = Gtk.Button(label="取消预约", visible=False, halign=Gtk.Align.START)
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
        self.health = Adw.PreferencesGroup(title="运行状态")
        page.append(self.health)

    def internal_pci(self):
        return (self.data.get("internal_gpu") or {}).get("pci")

    def build_devices(self):
        page = self.page("devices", "设备与显示器", "按 PCI 归属识别显卡和显示器，连接状态与启用状态分别展示。")
        self.device_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        page.append(self.device_box)

    def build_deployment(self):
        page = self.page("deployment", "部署与恢复", "重装 Linux 后重新选择设备并部署；已有配置先备份，所有变更可预览。")
        self.deployment_banner = Adw.Banner(title="", revealed=True)
        page.append(self.deployment_banner)
        group = Adw.PreferencesGroup(title="设备策略")
        target_row = row("目标外接显卡", "首版支持 NVIDIA；不会隔离所选目标卡。")
        self.target_select = Gtk.DropDown(model=Gtk.StringList.new(["尚未检测到设备"]), valign=Gtk.Align.CENTER)
        self.target_select.set_size_request(270, -1)
        target_row.add_suffix(self.target_select)
        group.add(target_row)
        page.append(group)
        self.protection_group = Adw.PreferencesGroup(title="内置 NVIDIA 显卡隔离", description="可选。当前使用 VFIO 的设备默认勾选；正在驱动显示器的设备会被拒绝隔离。")
        page.append(self.protection_group)
        notes = Adw.PreferencesGroup(title="部署内容")
        for title, subtitle in [("一个自定义管理服务", "统一处理设备检测、延迟加载、唤醒恢复和事件记录。"),
                                ("保留显示输出与 CUDA", "启用 NVIDIA 内核休眠通知器，不按“未接屏”强制卸载驱动。"),
                                ("备份并迁移现有配置", "预览将替换的文件；更新 initramfs 后需要重启。")]:
            notes.add(row(title, subtitle))
        page.append(notes)
        actions = Gtk.Box(spacing=10)
        self.deploy_button = Gtk.Button(label="预览部署变更")
        self.deploy_button.add_css_class("suggested-action")
        self.deploy_button.connect("clicked", lambda *_: self.preview_deployment())
        self.rollback_button = Gtk.Button(label="恢复部署前配置")
        self.rollback_button.connect("clicked", lambda *_: self.confirm_rollback())
        actions.append(self.deploy_button)
        actions.append(self.rollback_button)
        page.append(actions)
        help_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        help_box.append(label("首次安装后台组件", "heading"))
        help_box.append(label("若尚未安装管理服务，在源码目录执行以下命令。安装完成后可在本页部署设备策略。", "dim-label", True))
        source = Path(__file__).resolve().parent.parent
        self.install_command = "sudo bash " + str(source / "install.sh") if (source / "install.sh").exists() else "sudo bash ./install.sh"
        help_box.append(label(self.install_command, "monospace", True))
        copy = Gtk.Button(label="复制安装命令", halign=Gtk.Align.START)
        copy.connect("clicked", lambda *_: self.copy_text(self.install_command))
        help_box.append(copy)
        page.append(help_box)

    def build_logs(self):
        page = self.page("logs", "事件记录", "管理服务的设备、部署和睡眠事件。导出状态不包含进程命令行或环境变量。")
        controls = Gtk.Box(spacing=10)
        copy = Gtk.Button(label="复制诊断状态", icon_name="edit-copy-symbolic")
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
        self.banner.set_title("读取状态失败：" + operation_summary(error))
        self.banner.set_revealed(True)

    def update_gpu_panels(self, data, gpu, metric, removed, busy):
        ready = data.get("service_connected") and data.get("can_mutate_devices") and not busy
        transport = data.get("transport", {})
        state = transport.get("state")
        previous = gpu or data.get("last_known_target") or {}
        panel = self.external_panel
        panel.title.set_text(metric.get("name") or previous.get("name") or "外置 NVIDIA 显卡")
        panel.identity.set_text(f'{previous.get("pci", "PCI 未识别")} · {gpu["driver"] if gpu else "驱动未绑定"}')
        panel.power.set_text("PCI 电源 · " + gpu_power_text(gpu))
        badge = ("异常断开" if transport.get("driver_cleanup_pending") else "驱动异常" if data.get("blocked_io")
                 else "状态过期" if data.get("status_stale") else "已移除" if state == "detached"
                 else "已连接" if gpu and gpu["driver"] == "nvidia" else "等待驱动" if gpu else "未连接")
        panel.badge.set_text(badge)
        router = next((r for r in data.get("usb4", []) if not r["is_host"]), {})
        speed = router.get("rx_speed", "—")
        panel.update_metrics(metric, "—" if speed == "unknown" else speed)
        panel.update_display(data.get("desktop_displays", {}), gpu, ready,
                             "外屏输出已关闭" if removed else "未发现可控制的外屏")
        deploy = data.get("deployment", {})
        can_attach = deploy.get("managed") or (data.get("internal_gpu") and previous
                                               and state in {"present", "detached"})
        self.attach_button.set_sensitive(bool(ready and can_attach and not deploy.get("restart_required")))
        self.detach_button.set_sensitive(bool(ready and gpu and data.get("detach", {}).get("safe")))
        self.queue_button.set_sensitive(bool(ready and gpu and gpu["external_display_state"] in {"connected", "disconnected"}
                                             and not gpu["boot_vga"] and data.get("other_internal_display_ready")))
        self.detach_button.set_visible(not removed)
        self.queue_button.set_visible(not removed)
        attach_hint = ("配置已部署，请先重启。" if deploy.get("restart_required") else
                       "扫描 PCIe 并加载驱动。" if can_attach else "请先接入外置显卡。")
        self.attach_button.set_tooltip_text(attach_hint)
        panel.driver.set_text(("已安全移除 · USB4 已断开" if state == "disconnected" else
                               "已安全移除 · 可以拔下 USB4 线缆") if removed else attach_hint)
        reasons = list(data.get("detach", {}).get("blockers", [])) if gpu else []
        if gpu and not data.get("other_internal_display_ready"):
            reasons.append("注销后移除需要另一张显卡驱动的内屏。")
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
        panel.identity.set_text(f'{internal["pci"]} · {graphics or "驱动未知"}' if internal.get("pci") else "未检测到受支持的内置显卡")
        panel.power.set_text("PCI 电源 · " + gpu_power_text(card))
        panel.badge.set_text("已隔离" if drivers == (VFIO, VFIO) else "已加载" if drivers == HOST_DRIVERS
                             else "部分加载" if graphics == "nvidia" else "等待驱动" if card else "未检测到")
        internal_metric = next((item for item in data.get("metrics", []) if card and item["pci"] == card["pci"]), {})
        panel.update_metrics(internal_metric, "固定连接")
        sleeping = bool(card and card.get("runtime_status") == "suspended")
        for key in ("vram", "temp", "power"):
            if sleeping:
                panel.metrics[key].set_text("休眠中")
            panel.metrics[key].set_tooltip_text(None if internal_metric else
                "显卡休眠时暂停采样；驱动已加载且显卡处于 D0 活跃状态时，每 10 秒读取指标。")
        panel.update_display(data.get("internal_desktop_displays", {}), card, can_switch,
                             "加载驱动后可识别 HDMI 显示器")
        panel.driver.set_text(f'HDMI 音频 · {audio}　 IOMMU 组 · {internal["iommu_group"]}'
                              if audio else "加载或卸载显卡与 HDMI 音频驱动。")
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
            banner_text = "管理服务未连接 · 当前为只读预览"
        elif stale:
            banner_text = data.get("last_error") or (f'详细状态已过期（{data.get("status_age_seconds", 0):.0f} 秒未更新），显示独立的 USB4 连接状态。' if data.get("sample_available") else "正在获取设备详细状态；USB4 连接独立监测。")
        elif deploy.get("restart_required"):
            banner_text = "配置已部署，请保存工作并重启"
        else:
            banner_text = data.get("last_error", "")
        self.banner.set_title(operation_summary(banner_text))
        metric = next((m for m in data.get("metrics", []) if gpu and m["pci"] == gpu["pci"]), {})
        self.update_gpu_panels(data, gpu, metric, removed, busy)

        for old in getattr(self, "health_rows", []):
            self.health.remove(old)
        parameters = data.get("parameters", {})
        connections = "已连接外屏" if gpu and gpu["external_display_state"] == "connected" else "未连接外屏" if gpu and gpu["external_display_state"] == "disconnected" else "连接状态未知"
        internal = [g for g in data.get("gpus", []) if g["driver"] == "vfio-pci"]
        resume = data.get("last_resume")
        resume_text = {"ready": "恢复完成", "waiting": "等待 USB4 稳定", "absent": "系统已唤醒，未接显卡"}.get(resume["state"], "未知") if resume else "本次管理服务尚无唤醒事件"
        self.health_rows = [row("NVIDIA 驱动", value=metric.get("driver_version", "未读取")),
                            row("外接显示器", "按 eGPU 自身的 DRM 接口判断", connections),
                            row("睡眠恢复机制", "保留显存与显示输出", "内核通知器已启用" if parameters.get("UseKernelSuspendNotifiers") == "1" else "待配置 / 未加载驱动"),
                            row("内置独显隔离", value="、".join(g["name"] for g in internal) or "未隔离"),
                            row("最近一次唤醒", value=resume_text)]
        for item in self.health_rows:
            self.health.add(item)
        action = self.display_action if self.display_message else operation.get("action", "")
        message = self.display_message or ("" if removal_done else operation.get("message", ""))
        self.show_operation(action, message)
        self.cancel_button.set_visible(connected and operation.get("action") == "queue-detach" and operation.get("state") == "waiting")
        self.deploy_button.set_sensitive(connected and fresh and bool(self.form_gpus or data.get("gpus")) and not busy)
        self.rollback_button.set_sensitive(connected and deploy.get("rollback_available", False) and not busy)
        self.deployment_banner.set_title("管理配置已部署 · 待重启" if deploy.get("restart_required") else "统一管理已启用" if deploy.get("managed") else "尚未迁移设备策略；现有系统配置保持运行")
        self.update_devices(data)
        self.update_form(data)
        events = data.get("events", [])
        self.log_text.get_buffer().set_text("\n\n".join(datetime.fromtimestamp(e["time"]).strftime("%m-%d %H:%M:%S") + "  " + e["message"] for e in events) or "等待管理服务事件。安装前可使用 journalctl 查看现有服务日志。")
        operation_key = (operation.get("state"), operation.get("message"))
        if operation_key != self.last_operation and operation.get("state") in {"done", "error"}:
            if removed and self.stack.get_visible_child_name() != "overview":
                self.toast("显卡已安全移除")
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
        clear(self.device_box)
        words = {"connected": "已连接", "disconnected": "未连接", "unknown": "未知"}
        desktop_monitors = {m["connector"]: m for m in data.get("desktop_displays", {}).get("monitors", [])}
        internal = data.get("internal_gpu") or {}
        for gpu in data.get("gpus", []):
            group = Adw.PreferencesGroup(title=gpu["name"], description=f'{gpu["pci"]} · {gpu["driver"]}')
            gpu_monitors = desktop_monitors if gpu == data.get("target") else {}
            if internal.get("pci") == gpu["pci"]:
                desktop = data.get("internal_desktop_displays") or {}
                gpu_monitors = {m["connector"]: m for m in desktop.get("monitors", [])}
            if gpu.get("power_state") or gpu.get("runtime_status"):
                group.add(row("PCI 电源状态", "运行时电源管理", gpu_power_text(gpu)))
            for c in gpu["connectors"]:
                category = {"internal": "内屏", "external": "外接接口", "virtual": "虚拟接口"}[c["kind"]]
                desktop = gpu_monitors.get(c["name"])
                enabled = (" · 桌面正在输出" if desktop["active"] else " · 桌面已停用") if desktop else (" · 内核已启用" if c["enabled"] == "enabled" else " · 内核未启用")
                group.add(row(c["name"], category, words.get(c["status"], c["status"]) + enabled))
            if not gpu["connectors"]:
                group.add(row("尚无 DRM 接口", "驱动未绑定时，不把接口缺失视为未接屏。"))
            self.device_box.append(group)
        group = Adw.PreferencesGroup(title="USB4 / Thunderbolt")
        for router in data.get("usb4", []):
            if not router["is_host"]:
                group.add(row(router["name"], f'{router["vendor"]} · {router["id"]}', router["rx_speed"]))
        self.device_box.append(group)
        services = Adw.PreferencesGroup(title="服务集成", description="一次性任务完成后 inactive 属于正常状态。")
        for unit in data.get("services", []):
            state = ("未安装" if unit.get("LoadState") == "not-found" else
                     "运行中" if unit.get("ActiveState") == "active" else
                     "失败" if unit.get("ActiveState") == "failed" else "等待事件 / 已完成")
            services.add(row(unit["Id"], unit.get("UnitFileState", "") + " · " + unit.get("Result", ""), state))
        self.device_box.append(services)

    def update_form(self, data):
        signature = tuple((g["pci"], g["driver"]) for g in data.get("gpus", []))
        if signature == self.form_signature:
            return
        self.form_signature = signature
        self.form_gpus = [g for g in data.get("gpus", []) if g["vendor"] == "0x10de" and not g["boot_vga"] and g.get("external")]
        metric_names = {m["pci"]: m["name"] for m in data.get("metrics", [])}
        self.target_select.set_model(Gtk.StringList.new([metric_names.get(g["pci"], g["name"]) + " · " + g["pci"] for g in self.form_gpus] or ["未发现 NVIDIA 设备"]))
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
            raise ValueError("请连接并选择目标外接显卡。")
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
        dialog = Adw.MessageDialog.new(self, "部署这套设备策略？", details["summary"])
        dialog.add_response("cancel", "取消")
        dialog.add_response("deploy", "授权并部署")
        dialog.set_response_appearance("deploy", Adw.ResponseAppearance.SUGGESTED)
        text = "\n\n".join(path + "\n" + body for path, body in details["files"].items())
        if details["migrate"]:
            text += "\n备份并迁移：\n" + "\n".join(details["migrate"])
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
        text = ("保存工作并打开内屏。接在这张显卡上的外屏会停止输出，显示器线可以留在显卡上。管理器将预约移除并打开注销确认；请通过内屏重新登录，确认“可以拔线”后再拔线。180 秒内未释放 GPU 会自动取消。" if queued else
                "管理器将再次检查显示器和进程占用，然后移除目标 GPU 及其音频功能。收到“可以拔线”提示后再拔 USB4 线。")
        dialog = Adw.MessageDialog.new(self, "注销后安全移除？" if queued else "安全移除外接显卡？", text)
        dialog.add_response("cancel", "取消")
        dialog.add_response("confirm", "预约并打开注销确认" if queued else "检查并移除")
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
        dialog = Adw.MessageDialog.new(self, "停用这张显卡的外屏？",
            "请先保存工作并启用另一张显卡驱动的内屏。仅停用 eGPU 的外屏输出，显卡和计算任务保持连接。其余屏幕会临时横向排列，可用“恢复外屏”还原。停用后仍需单独检查显卡移除条件。")
        dialog.add_response("cancel", "取消")
        dialog.add_response("confirm", "停用外屏")
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _, answer: self.submit(action, {"expected_pci": pci}) if answer == "confirm" else None)
        dialog.present()
        return dialog

    def confirm_internal_switch(self, action, pci):
        if not pci:
            return
        attach = action == "internal-attach"
        title = ("加载内置 4070？" if attach else "卸载内置 4070？") if action else "内置 4070 占用"
        dialog = Adw.MessageDialog.new(self, title, "正在读取当前驱动和进程占用…")
        dialog.add_response("cancel", "取消" if action else "关闭")
        if action:
            dialog.add_response("confirm", "确认切换")
            dialog.set_response_enabled("confirm", False)
            dialog.set_response_appearance("confirm", Adw.ResponseAppearance.SUGGESTED if attach else Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect("response", lambda _, answer: self.submit(action, {"expected_pci": pci}) if answer == "confirm" else None)
        dialog.present()

        def show(status, error=None):
            if not dialog.get_visible():
                return False
            if error:
                dialog.set_body("无法检查 4070：" + error)
                return False
            graphics, audio = status["graphics_driver"], status["audio_driver"]
            current = ("已加载" if (graphics, audio) == HOST_DRIVERS else "已卸载并隔离"
                       if (graphics, audio) == (VFIO, VFIO) else "图形已加载，音频驱动不同"
                       if graphics == "nvidia" else "部分绑定")
            lines = [f"当前状态 · {current}", f"图形驱动 {graphics}　HDMI 音频 {audio}"]
            occupied = status["holders"]
            blocker = internal_switch_blocker(status, action) if action else ""
            scoped = bool(action and graphics == ("nvidia" if attach else VFIO)
                          and audio != ("snd_hda_intel" if attach else VFIO) and "audio_holders" in status)
            blocking_pids = {p["pid"] for p in internal_switch_holders(status, action)["processes"]} if action else set()
            if scoped:
                lines.extend(("", "本次仅切换 HDMI 音频驱动；检查音频和 VFIO 占用。"))
            if action == "internal-detach" and any(releasable_internal_holder(p, action) for p in occupied["processes"]):
                lines.extend(("", "卸载前自动暂停 nvidia-powerd，并按需短暂停止 WirePlumber（声音可能中断）。"
                              "操作结束后恢复 WirePlumber；卸载失败或重新加载显卡时恢复 nvidia-powerd。"))
            if blocker:
                lines.extend(("", blocker))
            if occupied["processes"]:
                entries = []
                for process in occupied["processes"]:
                    direct = process["direct_nodes"]
                    kind = "本设备" if direct else "共享或其他 NVIDIA 节点"
                    if scoped:
                        kind += " · 需释放" if process["pid"] in blocking_pids else " · 本次无需释放"
                    if releasable_internal_holder(process, action):
                        kind += " · 自动释放"
                    entries.append(f'{process["name"]}（PID {process["pid"]}）· {kind}\n'
                                   + "、".join(direct or process["nodes"])
                                   + ("\n" + gpu_holder_hint(process) if not action and gpu_holder_hint(process) else ""))
                processes = label("\n\n".join(entries), wrap=True)
                scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                            max_content_height=220, propagate_natural_height=True)
                scroll.set_child(processes)
                dialog.set_extra_child(scroll)
            elif occupied["complete"]:
                lines.extend(("", "未发现占用进程。"))
            if not occupied["complete"] and not blocker:
                lines.extend(("", "进程扫描不完整。"))
            if action and not blocker:
                lines.extend(("", "保持 AMD 内屏启用。切换前会再次检查占用。"))
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
        dialog = Adw.MessageDialog.new(self, "恢复部署前的配置？", "恢复备份文件并重建 initramfs。完成后需要重启。")
        dialog.add_response("cancel", "取消")
        dialog.add_response("confirm", "授权并恢复")
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _, answer: self.submit("rollback") if answer == "confirm" else None)
        dialog.present()

    def submit(self, action, options=None):
        self.operation_pending = True
        display_action = action in {"disable-displays", "restore-displays", *INTERNAL_DISPLAY_ACTIONS}
        self.display_busy = display_action
        self.display_action = action if display_action else ""
        self.display_message = "正在更改当前桌面的外屏输出…" if display_action else ""
        if self.data:
            self.apply_status(self.data, completed=False)
        self.show_operation(action, self.display_message or "正在提交后台服务…")
        def finished(message, failed=False):
            self.operation_pending = False
            self.display_busy = False
            if display_action:
                self.display_message = ("外屏操作未完成：" if failed else "") + str(message)
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
            connection.call("org.gnome.SessionManager", "/org/gnome/SessionManager", "org.gnome.SessionManager",
                            "Logout", GLib.Variant("(u)", (0,)), None, Gio.DBusCallFlags.NONE, 5000, None, None)
        except GLib.Error:
            self.toast("移除已预约。请使用桌面菜单手动注销；也可以取消预约。")

    def copy_text(self, text):
        Gdk.Display.get_default().get_clipboard().set(text)
        self.toast("已复制。")

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
                window.toast("当前桌面托盘不可用，窗口继续保留。")

    def request_quit(self):
        window = self.window
        if window and (window.operation_pending or window.data.get("operation", {}).get("state") in {"running", "waiting", "removing"}):
            window.present()
            window.toast("请等待当前操作完成，或先取消预约，再退出。")
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
