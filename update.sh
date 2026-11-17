#!/usr/bin/env bash
set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
    echo 'Python 3 is required to update APK descriptors.' >&2
    exit 2
fi
exec python3 "$(dirname -- "${BASH_SOURCE[0]}")/scripts/update-apk.py" "$@"
