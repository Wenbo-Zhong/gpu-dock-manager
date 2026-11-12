#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
version=${1:?version required}
top=$(mktemp -d)
trap 'rm -rf -- "$top"' EXIT
rpmbuild -bb --define "_topdir $top" --define "_sourcedir $PWD/dist" \
    --define "app_version $version" packaging/usb4-gpu-manager.spec
cp "$top"/RPMS/noarch/*.rpm dist/
