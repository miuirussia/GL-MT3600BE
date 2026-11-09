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
chown "$uid:$gid" /work /work/openwrt /work/downloads /work/ccache /home/builder
# Host bind mounts can allow writes while rejecting ownership changes.
staged_mounts=()
for directory in /artifacts /keys; do
    if ! gosu builder test -w "$directory" || ! gosu builder test -x "$directory"; then
        if ! chown "$uid:$gid" "$directory" 2>/dev/null; then
            # Rootless Docker maps the host owner's UID/GID to container root.
            # Some shared filesystems cannot represent the builder's ownership.
            if [[ "$(stat -c '%u:%g' "$directory")" != 0:0 ]]; then
                echo "Cannot change ownership of $directory for builder ($uid:$gid)." >&2
                exit 1
            fi
            if [[ ${#staged_mounts[@]} == 0 ]]; then
                exec 9>/work/.routerfw-mounts.lock
                if ! flock -n 9; then
                    echo 'Another routerfw container is using the host mounts.' >&2
                    exit 1
                fi
            fi
            staging="/work/host${directory}"
            mkdir -p "$staging"
            # Keep staged files from an interrupted run when absent on the host.
            rsync -a --no-owner --no-group "$directory/" "$staging/"
            chown -R "$uid:$gid" "$staging"
            staged_mounts+=("$directory")
            case "$directory" in
                /artifacts) export ROUTERFW_ARTIFACTS_DIR="$staging" ;;
                /keys) export ROUTERFW_KEYS_DIR="$staging" ;;
            esac
            echo "Staging $directory in $staging; copying back when the command exits."
            continue
        fi
        if ! gosu builder test -w "$directory" || ! gosu builder test -x "$directory"; then
            echo "Builder cannot write to or traverse $directory after updating permissions." >&2
            exit 1
        fi
    fi
done
export HOME=/home/builder USER=builder LOGNAME=builder
if [[ ${#staged_mounts[@]} == 0 ]]; then
    exec gosu builder python3 /inputs/scripts/builder.py "$@"
fi

sync_staged_mounts() {
    status=$?
    trap - EXIT
    for directory in "${staged_mounts[@]}"; do
        if ! rsync -a --no-owner --no-group --delete "/work/host$directory/" "$directory/"; then
            echo "Failed to copy $directory back; files remain in /work/host$directory." >&2
            status=1
        fi
    done
    exit "$status"
}
trap sync_staged_mounts EXIT
# Keep interactive commands on their controlling terminal. Otherwise isolate the
# build process group so termination also reaches make and compiler subprocesses.
if [[ -t 0 ]]; then
    env --default-signal=INT,QUIT gosu builder python3 /inputs/scripts/builder.py "$@" <&0 &
    child=$!
    trap 'trap "" TERM INT; kill -TERM 0 2>/dev/null || true' TERM INT
else
    setsid gosu builder python3 /inputs/scripts/builder.py "$@" <&0 &
    child=$!
    trap 'kill -TERM -- "-$child" 2>/dev/null || true' TERM INT
fi
status=0
while :; do
    if wait "$child"; then break; else status=$?; fi
    if ! kill -0 "$child" 2>/dev/null; then break; fi
done
exit "$status"
