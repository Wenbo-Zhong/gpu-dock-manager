# Distribution support and porting

GPU Dock Manager is experimental. A package building successfully does not
establish hardware or desktop compatibility.

| Environment | Current status |
| --- | --- |
| Ubuntu 26.04, GNOME 50.1, NVIDIA open 595 driver | Development environment; selected hardware workflows tested |
| Fedora / other RPM distributions | RPM packaging exists; deployment and hardware behavior not validated |
| Debian / other DEB distributions | Package generation exists; versions, deployment and hardware behavior not validated |
| KDE Plasma 6.6 Wayland | Tray and read-only KScreen status verified; display changes implemented with mocked tests, real external-output tests pending |
| Other desktops | Standard tray protocol implemented; compositor-specific monitor adapters still needed |

## Current constraints

- Python 3.10+, GTK 4.12+, libadwaita 1.4+, PyGObject, systemd, D-Bus and Polkit.
- NVIDIA telemetry requires nvidia-smi; driver deployment expects NVIDIA open
  kernel modules and supported suspend-notifier parameters.
- System deployment currently uses Ubuntu/Debian initramfs-tools and paths.
  Fedora/dracut and other deployment backends still need implementation and tests.
  Inspect the deployment preview; do not deploy Ubuntu-specific boot configuration
  on another distribution without porting it first.
- Internal GPU switching currently targets only the validated RTX 4070 PCI IDs
  10de:2820 / 10de:22bd, with strict IOMMU and AMD internal-display checks.
- Display changes use GNOME Mutter session D-Bus or KDE KScreen (`kscreen-doctor`).
  KDE requires the optional kscreen package. Mirrored KDE layouts are currently rejected.
  See kde-compatibility.md for the tested scope and remaining KWin holder issue.
- Optional Mutter patches are version-specific and must be reviewed, ported and
  rebuilt against the exact distribution source. The application package does
  not replace the system compositor. See mutter-porting.md.
- Tray support needs a StatusNotifierItem host (GNOME usually needs an extension).
- Manual internal-GPU detach can briefly interrupt audio while WirePlumber is
  stopped. Real-world behavior on additional systems remains untested.

## Reporting a new platform

Include distribution/version, kernel, desktop/compositor, GPU PCI IDs, NVIDIA
version, connection type, IOMMU topology and monitor routing. Report automated
checks separately from real attach/detach and suspend/resume tests. Use sanitized
reports; exclude full journals, usernames, device UUIDs and authentication data.

Keep distribution-specific deployment backends separate. First establish read-only
status, then monitor control, then reversible device operations, then boot setup.
Do not relax device identity, holder or active-display checks to make tests pass.

## Development checks

Run `make check` and `make poc-check`. `make gui-check` needs a real graphical
session and opens/cancels dialogs without authorizing device mutations.
`make deb` and `make rpm` build packages without installing them.

The installed command names and service identifiers remain usb4-gpu-manager for
upgrade compatibility, even though the repository is named gpu-dock-manager.
