#!/bin/sh
# 统一组装 DEB/RPM 安装树；这里只复制文件，不启动服务或部署设备策略。
# Populate a package staging root. This script never starts a service.
set -eu
root=${1:?staging root required}
case "$root" in /*) ;; *) echo "Use an absolute staging root" >&2; exit 1;; esac
[ "$root" != / ] || exit 1
cd "$(dirname "$0")/.."

python3 -I packaging/files.py --root "$root"
