#!/bin/sh
# Populate a package staging root. This script never starts a service.
set -eu
root=${1:?staging root required}
case "$root" in /*) ;; *) echo "Use an absolute staging root" >&2; exit 1;; esac
[ "$root" != / ] || exit 1
cd "$(dirname "$0")/.."

app="$root/usr/lib/usb4-gpu-manager"
share="$root/usr/share/usb4-gpu-manager"
install -d "$app/usb4_gpu_manager" "$root/usr/bin" "$share/mutter-poc/overlay" "$share/skills/mutter-egpu-port"
install -m 0644 usb4_gpu_manager/*.py "$app/usb4_gpu_manager/"
install -m 0644 data/gui_entry.py data/cli_entry.py data/daemon_entry.py "$app/"
for entry in 'usb4-gpu-manager gui_entry' 'usb4-gpu-managerctl cli_entry'; do
    set -- $entry
    printf '#!/bin/sh\nexec /usr/bin/python3 -I /usr/lib/usb4-gpu-manager/%s.py "$@"\n' "$2" > "$root/usr/bin/$1"
    chmod 0755 "$root/usr/bin/$1"
done
install -Dm 0755 tools/mutter_update_watch.py "$root/usr/bin/usb4-gpu-mutter-watch"
install -Dm 0644 data/usb4-gpu-manager.service "$root/usr/lib/systemd/system/usb4-gpu-manager.service"
for unit in data/usb4-gpu-mutter-watch.*; do
    install -Dm 0644 "$unit" "$root/usr/lib/systemd/user/${unit##*/}"
done
install -Dm 0644 data/io.github.usb4gpumanager.Manager.conf "$root/usr/share/dbus-1/system.d/io.github.usb4gpumanager.Manager.conf"
install -Dm 0644 data/io.github.usb4gpumanager.Manager.service "$root/usr/share/dbus-1/system-services/io.github.usb4gpumanager.Manager.service"
install -Dm 0644 data/io.github.usb4gpumanager.policy "$root/usr/share/polkit-1/actions/io.github.usb4gpumanager.policy"
install -Dm 0644 data/io.github.usb4gpumanager.App.desktop "$root/usr/share/applications/io.github.usb4gpumanager.App.desktop"
install -m 0644 experimental/mutter-poc/*.py experimental/mutter-poc/*.json experimental/mutter-poc/*.patch \
    experimental/mutter-poc/README.md experimental/mutter-poc/COPYING.GPL-2 "$share/mutter-poc/"
install -m 0644 experimental/mutter-poc/overlay/* "$share/mutter-poc/overlay/"
install -d "$share/mutter-poc/tests"
install -m 0644 experimental/mutter-poc/tests/*.[ch] "$share/mutter-poc/tests/"
install -m 0644 skills/mutter-egpu-port/SKILL.md "$share/skills/mutter-egpu-port/"
install -m 0644 docs/mutter-porting.md "$share/"
install -Dm 0644 LICENSE "$root/usr/share/doc/usb4-gpu-manager/copyright"
printf '\nThe Mutter patch and overlays are GPL-2.0-or-later. See\n/usr/share/usb4-gpu-manager/mutter-poc/COPYING.GPL-2 for the license.\n' \
    >> "$root/usr/share/doc/usb4-gpu-manager/copyright"

install -m 0644 NOTICE.md "$root/usr/share/doc/usb4-gpu-manager/NOTICE.md"
