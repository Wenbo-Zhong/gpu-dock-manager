"""页面共用的 GTK 组件与文本格式；保持原有间距、尺寸和样式。"""

from ..i18n import tr
from .gtk import Adw, Gtk

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
    # 状态卡片只显示异常末尾的业务摘要；完整堆栈仍供事件/诊断信息使用。
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
