#!/usr/bin/env bash
# 先确认恢复完成且已重启，再移除应用；保留备份供后续人工检查。
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Use sudo bash uninstall.sh'; exit 1; }
if { command -v dpkg-query >/dev/null && dpkg-query -S /usr/bin/usb4-gpu-manager >/dev/null 2>&1; } ||
   { command -v rpm >/dev/null && rpm -qf /usr/bin/usb4-gpu-manager >/dev/null 2>&1; }; then
    echo 'The native package owns this installation. Use APT or DNF to manage it.' >&2
    exit 1
fi
if systemctl is-active --quiet usb4-gpu-manager.service; then
    /usr/bin/python3 -I - <<'PY'
import json
from gi.repository import Gio, GLib
bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
result = bus.call_sync('io.github.usb4gpumanager.Manager', '/io/github/usb4gpumanager/Manager',
 'io.github.usb4gpumanager.Manager1', 'GetStatus', None, GLib.VariantType.new('(s)'), Gio.DBusCallFlags.NONE, 5000, None)
if json.loads(result.unpack()[0]).get('operation', {}).get('state') in {'running', 'waiting', 'removing'}:
 raise SystemExit('Finish or cancel the active manager operation before uninstalling.')
PY
fi
# 不依赖 profile 存在性：首次回滚已经删掉它时仍可能欠一次恢复或重启。
/usr/bin/python3 -I - <<'PY'
import json
from pathlib import Path
state = Path('/var/lib/usb4-gpu-manager')
if (state / 'deployment-pending.json').exists():
 raise SystemExit('Deployment recovery is pending. Run recover before uninstalling.')
marker = state / 'restart-required.json'
if marker.exists():
 try:
  saved = json.loads(marker.read_text())
  boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
  required = not isinstance(saved, dict) or not saved.get('boot') or saved['boot'] == boot
 except (OSError, ValueError):
  required = True
 if required:
  raise SystemExit('Restart and verify the restored configuration before uninstalling.')
PY
if [[ -e /etc/usb4-gpu-manager/profile.json ]]; then
    echo 'Restore device policy first from the GUI, or run sudo usb4-gpu-managerctl recover.' >&2
    echo 'Reboot and verify the restored configuration before uninstalling.' >&2
    exit 1
fi
systemctl disable --now usb4-gpu-manager.service
rm -f /etc/systemd/system/usb4-gpu-manager.service
rm -f /etc/dbus-1/system.d/io.github.usb4gpumanager.Manager.conf
rm -f /usr/share/dbus-1/system-services/io.github.usb4gpumanager.Manager.service
rm -f /usr/share/polkit-1/actions/io.github.usb4gpumanager.policy
rm -f /usr/share/applications/io.github.usb4gpumanager.App.desktop
rm -f /usr/bin/usb4-gpu-manager /usr/bin/usb4-gpu-managerctl
rm -f /usr/bin/usb4-gpu-mutter-watch /usr/lib/systemd/user/usb4-gpu-mutter-watch.{service,timer}
rm -rf /usr/lib/usb4-gpu-manager /usr/share/usb4-gpu-manager /usr/share/doc/usb4-gpu-manager
systemctl daemon-reload
systemctl reload dbus.service
echo 'Application removed. Deployment backups in /var/lib/usb4-gpu-manager were retained.'
