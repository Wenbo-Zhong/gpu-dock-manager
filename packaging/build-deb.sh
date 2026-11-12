#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
version=${1:?version required}
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
sh packaging/stage.sh "$stage"
install -d "$stage/DEBIAN" dist
cat > "$stage/DEBIAN/control" <<CONTROL
Package: usb4-gpu-manager
Version: $version-1
Architecture: all
Maintainer: USB4 GPU Manager contributors <local@localhost>
Section: utils
Priority: optional
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, libgtk-4-1 (>= 4.12), gir1.2-adw-1, libadwaita-1-0 (>= 1.4), systemd, dbus, polkitd, kmod
Recommends: libnotify-bin
Suggests: initramfs-tools, kscreen
Description: Desktop and system service for USB4 external GPUs
 Manages display output and safe removal of external NVIDIA GPUs.
 Includes the optional Mutter patch, porting skill and update watcher.
CONTROL
install -m 0755 packaging/deb-postinst "$stage/DEBIAN/postinst"
install -m 0755 packaging/deb-prerm "$stage/DEBIAN/prerm"
install -m 0755 packaging/deb-postrm "$stage/DEBIAN/postrm"
dpkg-deb --root-owner-group --build "$stage" "dist/usb4-gpu-manager_${version}-1_all.deb"
