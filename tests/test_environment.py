"""Run inside the builder after prepare: python3 -m unittest discover -s /inputs/tests."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('builder', ROOT / 'scripts/builder.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class LockValidation(unittest.TestCase):
    def test_device_config_symbol_with_hyphen_is_recognized(self):
        self.assertEqual(builder.requested_config().get(
            'CONFIG_TARGET_mediatek_filogic_DEVICE_glinet_gl-mt3600be'), 'y')

    def changed_lock(self, mutate):
        lock = json.loads((ROOT / 'openwrt.lock.json').read_text())
        mutate(lock)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'openwrt.lock.json').write_text(json.dumps(lock))
            with patch.object(builder, 'INPUT', root):
                with self.assertRaises(RuntimeError):
                    builder.load_lock()

    def test_moving_feed_branch_is_rejected(self):
        self.changed_lock(lambda lock: lock['feeds'][0].update(commit='openwrt-25.12'))

    def test_duplicate_feed_is_rejected(self):
        self.changed_lock(lambda lock: lock['feeds'].append(lock['feeds'][0]))

    def test_invalid_vermagic_is_rejected(self):
        self.changed_lock(lambda lock: lock['kernel'].update(vermagic='123'))

    def test_source_change_requires_reset(self):
        lock = builder.load_lock()
        with tempfile.TemporaryDirectory() as directory:
            stamp = Path(directory) / 'stamp.json'
            stamp.write_text(json.dumps({'inputs': {'old': {}}}))
            with patch.object(builder, 'STAMP', stamp):
                with self.assertRaisesRegex(RuntimeError, 'reset-sources'):
                    builder.prepare(lock)


class ArtifactExport(unittest.TestCase):
    def setUp(self):
        self.lock = builder.load_lock()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / 'openwrt'
        self.target = self.source / 'bin/targets' / self.lock['target']
        self.target.mkdir(parents=True)
        self.artifacts = self.root / 'artifacts'
        self.artifacts.mkdir()
        keys = self.root / 'keys'
        keys.mkdir()
        (keys / 'public-key.pem').write_text('test public key\n')
        (self.source / '.config').write_text('CONFIG_TARGET_mediatek=y\n')
        kernel = self.source / 'build_dir/target-test/linux-mediatek_filogic' / ('linux-' + self.lock['kernel']['version'])
        kernel.mkdir(parents=True)
        (kernel / '.vermagic').write_text(self.lock['kernel']['vermagic'] + '\n')
        (self.root / 'profiles.json').write_text('{}\n')
        for mocked in (
            patch.multiple(builder, SOURCE=self.source, WORK=self.root,
                           ARTIFACTS=self.artifacts, KEYS=keys),
            patch.object(builder, 'run', return_value=''),
            patch.object(builder, 'repo_state', return_value={}),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_exports_versioned_and_unversioned_images(self):
        for prefix in ('openwrt', 'openwrt-' + self.lock['release']):
            with self.subTest(prefix=prefix):
                image = f'{prefix}-mediatek-filogic-{self.lock["device"]}-squashfs-sysupgrade.bin'
                (self.target / image).write_bytes(b'test firmware\n')
                try:
                    builder.export_artifacts(self.lock)
                    exported = self.artifacts / self.lock['release']
                    relative = Path('bin/targets') / self.lock['target'] / image
                    self.assertEqual((exported / relative).read_bytes(), b'test firmware\n')
                    digest = hashlib.sha256(b'test firmware\n').hexdigest()
                    self.assertIn(f'{digest}  {relative}\n', (exported / 'SHA256SUMS').read_text())
                finally:
                    (self.target / image).unlink()

    def test_rejects_missing_sysupgrade_image(self):
        (self.target / f'openwrt-mediatek-filogic-{self.lock["device"]}-initramfs-kernel.bin').touch()
        (self.target / 'openwrt-mediatek-filogic-other-device-squashfs-sysupgrade.bin').touch()
        # A directory with the expected filename is not a firmware image.
        (self.target / f'openwrt-mediatek-filogic-{self.lock["device"]}-squashfs-sysupgrade.bin').mkdir()
        with self.assertRaisesRegex(RuntimeError, 'sysupgrade image is missing'):
            builder.export_artifacts(self.lock)
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_package_symlinks_are_exported_as_regular_files(self):
        (self.target / f'openwrt-{self.lock["device"]}-squashfs-sysupgrade.bin').write_bytes(b'firmware')
        packages = self.target / 'packages'
        packages.mkdir()
        (packages / 'example.apk').write_bytes(b'package')
        shared = self.source / 'bin/packages/test'
        shared.mkdir(parents=True)
        (shared / 'base').symlink_to(packages, target_is_directory=True)
        (packages / 'alias.apk').symlink_to('example.apk')

        builder.export_artifacts(self.lock)

        exported = self.artifacts / self.lock['release']
        for name in ('example.apk', 'alias.apk'):
            package = exported / 'bin/packages/test/base' / name
            self.assertEqual(package.read_bytes(), b'package')
            self.assertFalse(package.is_symlink())
        self.assertFalse((exported / 'bin/packages/test/base').is_symlink())
        self.assertIn('bin/packages/test/base/alias.apk', (exported / 'SHA256SUMS').read_text())


class VermagicRecipe(unittest.TestCase):
    def test_override_and_native_fallback_in_patched_upstream_recipe(self):
        # Exercise the actual patched Make recipe, including make's $ expansion.
        source = (builder.SOURCE / 'include/kernel-defaults.mk').read_text()
        start = source.index('\tif [ -s $(TOPDIR)/.vermagic ]; then')
        end = source.index('\n\tfi', start) + len('\n\tfi')
        recipe = source[start:end]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            linux = root / 'linux'
            linux.mkdir()
            config = b'CONFIG_Z=y\nCONFIG_A=m\n# CONFIG_B is not set\n'
            (linux / '.config.set').write_bytes(config)
            mkhash = root / 'mkhash'
            mkhash.write_text('#!/bin/sh\nmd5sum | cut -d " " -f 1\n')
            mkhash.chmod(0o755)
            makefile = root / 'Makefile'
            makefile.write_text(f'TOPDIR := {root}\nLINUX_DIR := {linux}\n'
                                f'MKHASH := {mkhash}\nall:\n{recipe}\n')
            expected = builder.load_lock()['kernel']['vermagic']
            (root / '.vermagic').write_text(expected + '\n')
            subprocess.run(['make', '-s', '-f', str(makefile)], check=True)
            self.assertEqual((linux / '.vermagic').read_text(), expected + '\n')
            (root / '.vermagic').unlink()
            subprocess.run(['make', '-s', '-f', str(makefile)], check=True)
            native = hashlib.md5(b'CONFIG_A=m\nCONFIG_Z=y\n').hexdigest()
            self.assertEqual((linux / '.vermagic').read_text(), native + '\n')


class UCIDefaults(unittest.TestCase):
    def test_script_is_installed_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'uci-defaults').mkdir()
            script = root / 'uci-defaults/99-custom'
            script.write_text('#!/bin/sh\nuci set system.@system[0].hostname=Beryl7\nuci commit system\nexit 0\n')
            script.chmod(0o644)
            destination = root / 'overlay/etc/uci-defaults'
            with patch.object(builder, 'INPUT', root):
                builder.install_uci_defaults(destination)
            installed = destination / script.name
            self.assertEqual(installed.read_bytes(), script.read_bytes())
            self.assertEqual(installed.stat().st_mode & 0o777, 0o755)

    def test_overlay_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'uci-defaults').mkdir()
            (root / 'uci-defaults/99-custom').write_text('#!/bin/sh\nexit 0\n')
            destination = root / 'overlay/etc/uci-defaults'
            destination.mkdir(parents=True)
            (destination / '99-custom').write_text('original\n')
            with patch.object(builder, 'INPUT', root):
                with self.assertRaisesRegex(RuntimeError, 'Conflicting'):
                    builder.install_uci_defaults(destination)
            self.assertEqual((destination / '99-custom').read_text(), 'original\n')


if __name__ == '__main__':
    unittest.main()
