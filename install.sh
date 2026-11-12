#!/usr/bin/env bash
# Installs immutable application code and a read-only-by-default system daemon.
# Device policy migration is a separate, previewable action in the GUI.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Use sudo bash install.sh'; exit 1; }
if { command -v dpkg-query >/dev/null && dpkg-query -S /usr/bin/usb4-gpu-manager >/dev/null 2>&1; } ||
   { command -v rpm >/dev/null && rpm -qf /usr/bin/usb4-gpu-manager >/dev/null 2>&1; }; then
    echo 'The native package owns this installation. Use APT or DNF to manage it.' >&2
    exit 1
fi
source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
if [[ ${1:-} == --install-deps ]]; then
    apt-get update
    apt-get install -y python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 polkitd pkexec dbus initramfs-tools
fi
/usr/bin/python3 - <<'PY'
try:
 import gi
 gi.require_version('Gtk', '4.0')
 gi.require_version('Adw', '1')
 from gi.repository import Gtk, Adw, Gio
 if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4,12) or (Adw.get_major_version(), Adw.get_minor_version()) < (1,4):
  raise SystemExit('GTK 4.12+ and libadwaita 1.4+ are required.')
except (ImportError, ValueError):
 raise SystemExit('Missing desktop dependencies. Rerun: sudo bash install.sh --install-deps')
PY
if systemctl is-active --quiet usb4-gpu-manager.service; then
    /usr/bin/python3 -I - <<'PY'
import json, time
from gi.repository import Gio, GLib
bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
result = bus.call_sync('io.github.usb4gpumanager.Manager', '/io/github/usb4gpumanager/Manager',
 'io.github.usb4gpumanager.Manager1', 'GetStatus', None, GLib.VariantType.new('(s)'), Gio.DBusCallFlags.NONE, 5000, None)
status = json.loads(result.unpack()[0])
if status.get('operation', {}).get('state') in {'running', 'waiting', 'removing'}:
 if time.time() - status.get('timestamp', 0) > 30:
  raise SystemExit('The previous service is stalled. Save work, keep the eGPU disconnected, reboot, then rerun this installer.')
 raise SystemExit('A manager operation is active. Finish or cancel it before upgrading.')
PY
    systemctl stop usb4-gpu-manager.service
fi
install -d -m 0755 /usr/lib/usb4-gpu-manager/usb4_gpu_manager /usr/share/dbus-1/system-services /etc/dbus-1/system.d /usr/share/polkit-1/actions /usr/share/applications
for file in "$source_dir"/usb4_gpu_manager/*.py; do
    install -o root -g root -m 0644 "$file" /usr/lib/usb4-gpu-manager/usb4_gpu_manager/
done
cp -R "$source_dir/usb4_gpu_manager/locales" /usr/lib/usb4-gpu-manager/usb4_gpu_manager/
chown -R root:root /usr/lib/usb4-gpu-manager/usb4_gpu_manager/locales
for file in gui_entry.py daemon_entry.py cli_entry.py; do
    install -o root -g root -m 0644 "$source_dir/data/$file" /usr/lib/usb4-gpu-manager/
done
install -o root -g root -m 0644 "$source_dir/data/usb4-gpu-manager.service" /etc/systemd/system/
install -o root -g root -m 0644 "$source_dir/data/io.github.usb4gpumanager.Manager.conf" /etc/dbus-1/system.d/
install -o root -g root -m 0644 "$source_dir/data/io.github.usb4gpumanager.Manager.service" /usr/share/dbus-1/system-services/
install -o root -g root -m 0644 "$source_dir/data/io.github.usb4gpumanager.policy" /usr/share/polkit-1/actions/
install -o root -g root -m 0644 "$source_dir/data/io.github.usb4gpumanager.App.desktop" /usr/share/applications/
cat > /usr/bin/usb4-gpu-manager <<'SH'
#!/bin/sh
exec /usr/bin/python3 -I /usr/lib/usb4-gpu-manager/gui_entry.py "$@"
SH
cat > /usr/bin/usb4-gpu-managerctl <<'SH'
#!/bin/sh
exec /usr/bin/python3 -I /usr/lib/usb4-gpu-manager/cli_entry.py "$@"
SH
chmod 0755 /usr/bin/usb4-gpu-manager /usr/bin/usb4-gpu-managerctl
systemctl daemon-reload
systemctl reload dbus.service
systemctl enable --now usb4-gpu-manager.service
echo 'Application installed. Existing GPU policy has not been changed.'
echo 'Open USB4 GPU Manager, then use 部署与恢复 → 预览部署变更 to migrate your configuration.'
