#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    echo 'Docker with Compose is required (Docker Desktop on macOS).' >&2
    exit 1
fi
export BUILD_UID="$(id -u)" BUILD_GID="$(id -g)"
mkdir -p artifacts .keys
if [[ "${1:-}" == image ]]; then
    exec docker compose build --pull builder
fi
docker compose build builder
if [[ $# -eq 0 ]]; then set -- build; fi
exec docker compose run --rm builder "$@"
