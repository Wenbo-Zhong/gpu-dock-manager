#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run this script with sudo.' >&2; exit 1; }
state=/var/lib/egpu-wayland-kernel-pm-test
[[ -f $state/backup-ready ]] || { echo 'No saved installation found.' >&2; exit 1; }
rm -f /etc/modprobe.d/zzzz-egpu-kernel-pm.conf
rm -f /etc/systemd/system/nvidia-suspend.service.d/egpu-display-status.conf
rm -f /etc/systemd/system/nvidia-resume.service.d/zz-egpu-display-status.conf
rm -f /usr/local/sbin/egpu-display-status
if [[ -f $state/egpu-unload.conf ]]; then
    install -d /etc/systemd/system/nvidia-suspend.service.d
    cp -a "$state/egpu-unload.conf" /etc/systemd/system/nvidia-suspend.service.d/egpu-unload.conf
fi
systemctl daemon-reload
touch "$state/rolled-back"
echo 'Previous configuration restored. Reboot to restore the previous NVIDIA module parameter.'
