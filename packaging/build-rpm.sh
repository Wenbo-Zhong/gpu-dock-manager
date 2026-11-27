#!/bin/sh
# RPM 打包入口；构建目录自动清理，版本和发行版依赖由 spec 描述。
set -eu
cd "$(dirname "$0")/.."
version=${1:?version required}
top=$(mktemp -d)
trap 'rm -rf -- "$top"' EXIT
rpmbuild -bb --define "_topdir $top" --define "_sourcedir $PWD/dist" \
    --define "app_version $version" packaging/usb4-gpu-manager.spec
cp "$top"/RPMS/noarch/*.rpm dist/
