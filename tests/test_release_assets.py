import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class ReleaseAssets(unittest.TestCase):
    def test_native_packaging_and_checksums(self):
        lock = json.loads((ROOT / 'openwrt.lock.json').read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / 'inputs'
            inputs.mkdir()
            (inputs / 'openwrt.lock.json').write_text(json.dumps(lock))
            artifacts = root / 'artifacts'
            export = artifacts / lock['release']
            target = export / 'bin/targets' / lock['target']
            target.mkdir(parents=True)
            image = f'openwrt-{lock["device"]}-squashfs-sysupgrade.bin'
            (target / image).write_bytes(b'test firmware\n')
            (export / 'openwrt.lock.json').write_text(json.dumps(lock))
            (export / 'build-manifest.json').write_text('{}\n')
            env = dict(os.environ, ROUTERFW_INPUT_DIR=str(inputs),
                       ROUTERFW_ARTIFACTS_DIR=str(artifacts))
            command = [sys.executable, str(ROOT / 'scripts/release-assets.py')]
            subprocess.run(command, env=env, check=True)
            release = inputs / 'release'
            for line in (release / 'SHA256SUMS').read_text().splitlines():
                digest, filename = line.split('  ', 1)
                self.assertEqual(digest, hashlib.sha256((release / filename).read_bytes()).hexdigest())
            self.assertEqual((release / image).read_bytes(), b'test firmware\n')
            archive = next(release.glob('*.tar.zst'))
            contents = subprocess.check_output(['tar', '-tf', str(archive)], text=True)
            self.assertIn(f'{lock["release"]}/build-manifest.json', contents)
            # A rerun must not accidentally include an old SHA256SUMS in new sums.
            result = subprocess.run(command, env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
