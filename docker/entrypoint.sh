#!/usr/bin/env bash
set -euo pipefail
uid="${BUILD_UID:-1000}"
gid="${BUILD_GID:-1000}"
# OpenWrt must run as an unprivileged user, even when invoked by host root.
if [[ "$uid" == 0 ]]; then uid=1000; fi
if [[ "$gid" == 0 ]]; then gid=1000; fi
groupmod -o -g "$gid" builder
usermod -o -u "$uid" -g "$gid" builder
mkdir -p /work/openwrt /work/downloads /work/ccache /artifacts /keys
chown "$uid:$gid" /work /work/openwrt /work/downloads /work/ccache /artifacts /keys /home/builder
export HOME=/home/builder USER=builder LOGNAME=builder
exec gosu builder python3 /inputs/scripts/builder.py "$@"
