#!/bin/sh
set -eu

keys_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/.keys
private_key="$keys_dir/private-key.pem"

if [ ! -f "$private_key" ]; then
    if [ -e "$keys_dir/public-key.pem" ]; then
        printf '%s\n' 'Restore .keys/private-key.pem to match the existing public key.' >&2
        exit 1
    fi

    umask 077
    mkdir -p "$keys_dir"
    temporary=$(mktemp -d "$keys_dir/.signing-key.XXXXXXXX")
    trap 'rm -f "$temporary/private-key.pem"; rmdir "$temporary"' EXIT
    openssl ecparam -name prime256v1 -genkey -noout -out "$temporary/private-key.pem"
    ln "$temporary/private-key.pem" "$keys_dir/"
fi

printf '%s\n' "$private_key"
