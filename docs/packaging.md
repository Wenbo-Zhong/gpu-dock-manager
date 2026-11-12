# USB4 GPU Manager native packages

Build the application package without root from the source checkout.

| Target | Build tools | Result |
| --- | --- | --- |
| `make deb` | GNU make, dpkg-deb | `dist/usb4-gpu-manager_0.1.2-1_all.deb` |
| `make rpm` | GNU make, rpmbuild | `dist/usb4-gpu-manager-0.1.2-1*.noarch.rpm` |

Install the result using `sudo apt install ./dist/<file>.deb` or `sudo dnf install ./dist/<file>.rpm`. Install from a session with no active removal, connection or deployment operation. The service starts on demand via D-Bus. For background handling from boot, enable it with `sudo systemctl enable --now usb4-gpu-manager.service`. The package manager owns the application files, service, policy, desktop entry and patch kit. Boot policy migration is a separate GUI operation and currently requires Debian-style initramfs-tools. Fedora users can use display management and removal with an already configured NVIDIA/USB4 stack.

If upgrading from the old `install.sh` install, confirm there are no operations in progress and inspect the old service unit. The legacy installer put the unit under `/etc/systemd/system`, where it overrides the package's unit. After native package installation, remove the old unit only if it matches the shipped unit, and reload systemd.

```bash
sudo cmp /etc/systemd/system/usb4-gpu-manager.service /usr/lib/systemd/system/usb4-gpu-manager.service
# Run only when cmp confirms the units match.
sudo rm /etc/systemd/system/usb4-gpu-manager.service
sudo systemctl daemon-reload
```

The package uses the vendor directory for the D-Bus policy. The legacy copy under `/etc/dbus-1/system.d` can likewise be removed after confirming it matches the packaged policy. Retain site-specific configuration. Use APT or DNF for future updates and removal. If the application has deployed a GPU boot policy, use the application's rollback before uninstalling when that policy should also be removed.

See [the Mutter porting runbook](mutter-porting.md) for building distribution Mutter packages, setting a verified build as the system default, rollback and update reminders.
