#!/usr/bin/env python3
"""Package an exported build for GitHub Releases; does not publish anything."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from builder import ARTIFACTS, INPUT


def package(destination):
    lock = json.loads((INPUT / 'openwrt.lock.json').read_text())
    version = lock['release']
    source = ARTIFACTS / version
    # Do not publish a stale export after changing the lock.
    if json.loads((source / 'openwrt.lock.json').read_text()) != lock:
        raise RuntimeError('Exported build does not match the current lock.')
    target = source / 'bin/targets' / lock['target']
    images = sorted(target.glob(f'*{lock["device"]}*.bin'))
    if not any(p.name.endswith('-sysupgrade.bin') for p in images):
        raise RuntimeError('GL-MT3600BE sysupgrade image is missing.')
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise RuntimeError('Release directory must be empty: ' + str(destination))
    for image in images:
        shutil.copy2(image, destination)
    shutil.copy2(source / 'build-manifest.json', destination)
    archive = destination / f'openwrt-{version}-{lock["device"]}-build.tar.zst'
    subprocess.run(['tar', '--zstd', '-cf', str(archive), '-C', str(ARTIFACTS), version], check=True)
    sums = []
    for path in sorted(destination.iterdir()):
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        sums.append(f'{digest}  {path.name}\n')
    (destination / 'SHA256SUMS').write_text(''.join(sums))


if __name__ == '__main__':
    package(INPUT / 'release')
