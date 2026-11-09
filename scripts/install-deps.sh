#!/usr/bin/env bash
set -euo pipefail
if [[ "$EUID" -ne 0 ]]; then
    echo 'Run with sudo (Debian/Ubuntu).' >&2
    exit 1
fi
export DEBIAN_FRONTEND=noninteractive
package_list="$(dirname -- "${BASH_SOURCE[0]}")/build-packages.txt"
apt-get -o Acquire::Retries=3 update
xargs -r apt-get install -y --no-install-recommends < "$package_list"
