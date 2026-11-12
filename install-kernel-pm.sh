#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run this script with sudo.' >&2; exit 1; }
source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
state=/var/lib/egpu-wayland-kernel-pm-test
[[ ! -e $state ]] || { echo "Saved installation already exists at $state; refusing to overwrite backup." >&2; exit 1; }
parameters=$(modinfo -p nvidia)
[[ $parameters == *NVreg_UseKernelSuspendNotifiers:* ]] || { echo 'NVIDIA kernel suspend notifiers are unavailable.' >&2; exit 1; }
[[ $(modinfo -F license nvidia) == *MIT* ]] || { echo 'This test requires NVIDIA open kernel modules.' >&2; exit 1; }
[[ -x /usr/local/sbin/egpu-nvidia-resume.sh && -f /etc/modprobe.d/zz-nvidia-egpu-manual.conf ]]
for dest in /etc/modprobe.d/zzzz-egpu-kernel-pm.conf \
    /usr/local/sbin/egpu-display-status \
    /etc/systemd/system/nvidia-suspend.service.d/egpu-display-status.conf \
    /etc/systemd/system/nvidia-resume.service.d/zz-egpu-display-status.conf; do
    [[ ! -e $dest && ! -L $dest ]] || { echo "Refusing to overwrite $dest" >&2; exit 1; }
done
install -d -m 0700 "$state"
install -m 0700 "$source_dir/rollback-kernel-pm.sh" "$state/rollback.sh"
if [[ -e /etc/systemd/system/nvidia-suspend.service.d/egpu-unload.conf ]]; then
    cp -a /etc/systemd/system/nvidia-suspend.service.d/egpu-unload.conf "$state/egpu-unload.conf"
fi
touch "$state/backup-ready"
trap 'bash "$state/rollback.sh"; exit 1' ERR
install -d /etc/systemd/system/nvidia-suspend.service.d /etc/systemd/system/nvidia-resume.service.d
install -m 0755 "$source_dir/egpu-display-status.py" /usr/local/sbin/egpu-display-status
cat > /etc/modprobe.d/zzzz-egpu-kernel-pm.conf <<'CONF'
# Preserve display outputs and CUDA; use the open driver's kernel PM notifier.
options nvidia NVreg_UseKernelSuspendNotifiers=1
CONF
cat > /etc/systemd/system/nvidia-suspend.service.d/egpu-display-status.conf <<'CONF'
[Service]
ExecStartPre=/usr/local/sbin/egpu-display-status --journal
CONF
cat > /etc/systemd/system/nvidia-resume.service.d/zz-egpu-display-status.conf <<'CONF'
[Service]
ExecStartPost=/usr/local/sbin/egpu-display-status --journal
CONF
# The old unload experiment failed because GNOME holds the eGPU's DRM node.
rm -f /etc/systemd/system/nvidia-suspend.service.d/egpu-unload.conf
systemctl daemon-reload
touch "$state/installed"
trap - ERR
/usr/local/sbin/egpu-display-status
echo 'Installed. Save work and reboot before testing suspend in Wayland.'
echo "Rollback: sudo bash $state/rollback.sh (then reboot)"
