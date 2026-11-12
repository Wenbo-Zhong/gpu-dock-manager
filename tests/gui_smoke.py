"""Explicit desktop smoke check. Opens dialogs, cancels them, never authorizes operations."""
from usb4_gpu_manager.gui import Application
from usb4_gpu_manager.deployment import plan
from gi.repository import GLib

app = Application(unique=False)
attempts = 0
submitted = []
errors = []


def exercise():
    global attempts
    window = app.get_active_window()
    attempts += 1
    if not window.data and attempts < 20:
        return True
    try:
        assert window.data.get("gpus"), "No GPU data rendered"
        window.submit = lambda *args: submitted.append(args)
        for page in window.page_names:
            window.stack.set_visible_child_name(page)
        if window.form_gpus:
            details = plan(window.profile_from_form(), window.data["gpus"])
            dialog = window.show_plan(details)
            dialog.response("cancel")
        if window.data.get("target"):
            for queued in [False, True]:
                window.confirm_detach(queued).response("cancel")
            window.confirm_disable_displays().response("cancel")
        if window.internal_pci():
            window.confirm_internal_switch("internal-detach", window.internal_pci()).response("cancel")
            window.confirm_disable_displays(internal=True).response("cancel")
        assert window.external_panel.display.get_text(), "Display controls must explain current state"
        assert window.external_panel.driver.get_text(), "Reconnect controls must explain prerequisites"
        window.show_operation("internal-detach", "Traceback (most recent call last):\nRuntimeError: 4070 正在使用")
        assert window.internal_panel.notice.get_text() == "4070 正在使用"
        assert not window.external_panel.notice.get_visible()
        assert not submitted, "Cancel must not submit privileged operations"
        if app.tray and app.tray.ready:
            app.hide_to_tray()
            assert not window.get_visible(), "Tray hide must hide the window"
            app.do_activate()
            assert window.get_visible() and app.window is window
            assert len(app.get_windows()) == 1, "Restoring must reuse the existing window"
            assert window.close_to_tray(), "Close must hide while tray is available"
            assert not window.get_visible()
            app.do_activate()
            window.operation_pending = True
            app.request_quit()
            assert window.get_visible(), "In-flight operations must keep the app available"
            window.operation_pending = False
            print("PASS: live tray registration, hide, restore, close-to-tray and in-flight quit guard")
        print("PASS: four pages, deployment/removal/display dialogs, disabled-button explanations, cancellation submits no operations")
    except Exception as error:
        errors.append(error)
    app.quit()
    return False


def activate(*_):
    GLib.timeout_add_seconds(2, exercise)


app.connect("activate", activate)
app.run(["gui-smoke"])
if errors:
    raise errors[0]
