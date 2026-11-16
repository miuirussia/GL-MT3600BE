"""Prebuilt APK inputs and installation with the prepared OpenWrt recipe."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('apk_builder', ROOT / 'scripts/builder.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class APKInputs(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'apk').mkdir()
        self.destination = self.root / 'openwrt/apk'
        mocked = patch.object(builder, 'INPUT', self.root)
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_copies_binary_packages_and_ignores_gitkeep(self):
        (self.root / 'apk/.gitkeep').touch()
        data = b'ADB\x00\x80\r\n\x00'
        (self.root / 'apk/example-1.0-r1.apk').write_bytes(data)
        builder.install_apk_packages(self.destination)
        self.assertEqual(list(self.destination.iterdir()),
                         [self.destination / 'example-1.0-r1.apk'])
        self.assertEqual((self.destination / 'example-1.0-r1.apk').read_bytes(), data)

    def test_downloads_packages_and_checks_sha256(self):
        data = b'ADB\x00\x80\r\n\x00'
        url = 'https://example.com/download?id=123'
        descriptor = self.root / 'apk/example.apk.txt'
        (self.root / 'apk/local.apk').write_bytes(b'local')
        for digest in (builder.sha(data), builder.sha(data).upper(), 'SKIP'):
            with self.subTest(digest=digest):
                descriptor.write_text(f'\ufeff{url}\r\n{digest}\r\n', encoding='utf-8')
                with patch.object(builder.urllib.request, 'urlopen', return_value=io.BytesIO(data)) as download:
                    builder.install_apk_packages(self.destination)
                download.assert_called_once_with(url, timeout=60)
                self.assertEqual((self.destination / 'example.apk').read_bytes(), data)
                self.assertEqual((self.destination / 'local.apk').read_bytes(), b'local')
                self.assertFalse((self.destination / descriptor.name).exists())

    def test_checksum_mismatch_does_not_install_package(self):
        (self.root / 'apk/example.apk.txt').write_text('https://example.com/example.apk\n' + '0' * 64 + '\n')
        with patch.object(builder.urllib.request, 'urlopen', return_value=io.BytesIO(b'wrong package')):
            with self.assertRaisesRegex(RuntimeError, 'SHA-256 checksum mismatch'):
                builder.install_apk_packages(self.destination)
        self.assertFalse((self.destination / 'example.apk').exists())

    def test_rejects_invalid_download_descriptors(self):
        for content in ('', 'https://example.com/example.apk\n',
                        'https://example.com/example.apk\ninvalid\n',
                        'https://example.com/example.apk\nSKIP\nextra\n',
                        'https://example.com/example.apk\nskip\n',
                        'file:///tmp/example.apk\nSKIP\n',
                        'https:///example.apk\nSKIP\n',
                        'https://example.com/a b.apk\nSKIP\n'):
            with self.subTest(content=content):
                (self.root / 'apk/example.apk.txt').write_text(content)
                with patch.object(builder.urllib.request, 'urlopen') as download:
                    with self.assertRaises(RuntimeError):
                        builder.install_apk_packages(self.destination)
                download.assert_not_called()

    def test_rejects_conflicting_local_and_downloaded_package(self):
        (self.root / 'apk/example.apk').write_bytes(b'local')
        (self.root / 'apk/example.apk.txt').write_text('https://example.com/example.apk\nSKIP\n')
        with patch.object(builder.urllib.request, 'urlopen') as download:
            with self.assertRaisesRegex(RuntimeError, 'Conflicting APK inputs'):
                builder.install_apk_packages(self.destination)
        download.assert_not_called()
        self.assertEqual((self.destination / 'example.apk').read_bytes(), b'local')

    def test_download_failure_does_not_install_package(self):
        (self.root / 'apk/example.apk.txt').write_text('https://example.com/example.apk\nSKIP\n')
        with patch.object(builder.urllib.request, 'urlopen', side_effect=urllib.error.URLError('unavailable')):
            with self.assertRaises(urllib.error.URLError):
                builder.install_apk_packages(self.destination)
        self.assertFalse((self.destination / 'example.apk').exists())

    def test_rejects_invalid_inputs(self):
        for name in ('nested/example.apk', 'example package.apk', 'example.ipk', '$(id).apk',
                     'nested/example.apk.txt', 'example package.apk.txt', '$(id).apk.txt'):
            with self.subTest(name=name):
                package = self.root / 'apk' / name
                package.parent.mkdir(parents=True, exist_ok=True)
                package.touch()
                try:
                    with self.assertRaisesRegex(RuntimeError, 'regular .apk files'):
                        builder.install_apk_packages(self.destination)
                finally:
                    package.unlink()

    def test_rejects_symlink(self):
        (self.root / 'apk/example.apk').symlink_to('/missing.apk')
        with self.assertRaisesRegex(RuntimeError, 'symlinks'):
            builder.install_apk_packages(self.destination)

    def test_package_change_invalidates_preparation_stamp(self):
        package = self.root / 'apk/example.apk'
        package.write_bytes(b'original')
        original = builder.source_inputs()
        stamp = self.root / 'stamp.json'
        stamp.write_text(json.dumps({'inputs': original}))
        package.write_bytes(b'replaced')
        with patch.object(builder, 'STAMP', stamp):
            with self.assertRaisesRegex(RuntimeError, 'reset-sources'):
                builder.prepare({})

    def test_descriptor_change_invalidates_preparation_stamp(self):
        descriptor = self.root / 'apk/example.apk.txt'
        descriptor.write_text('https://example.com/example.apk\nSKIP\n')
        stamp = self.root / 'stamp.json'
        stamp.write_text(json.dumps({'inputs': builder.source_inputs()}))
        for content in ('https://example.com/updated.apk\nSKIP\n',
                        'https://example.com/example.apk\n' + '0' * 64 + '\n'):
            with self.subTest(content=content):
                descriptor.write_text(content)
                with patch.object(builder, 'STAMP', stamp):
                    with self.assertRaisesRegex(RuntimeError, 'reset-sources'):
                        builder.prepare({})

    def test_prebuilt_packages_require_apk_enabled(self):
        (self.root / 'apk/example.apk').touch()
        self.destination.parent.mkdir()
        lock = json.loads((ROOT / 'openwrt.lock.json').read_text())
        settings = {'CONFIG_TARGET_mediatek_filogic_DEVICE_' + lock['device']: 'y',
                    'CONFIG_USE_APK': 'n'}
        with (patch.object(builder, 'SOURCE', self.destination.parent),
              patch.object(builder, 'requested_config', return_value=settings),
              patch.object(builder, 'run')):
            with self.assertRaisesRegex(RuntimeError, 'require CONFIG_USE_APK=y'):
                builder.configure(lock)


class APKInstallation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.apk = Path(os.environ.get('ROUTERFW_APK', builder.SOURCE / 'staging_dir/host/bin/apk'))
        cls.fakeroot = Path(shutil.which('fakeroot') or builder.SOURCE / 'staging_dir/host/bin/fakeroot')
        if not cls.apk.is_file() or not cls.fakeroot.is_file():
            raise unittest.SkipTest('Requires the host APK tool and fakeroot after prepare/build.')
        cls.package_makefile = (builder.SOURCE / 'package/Makefile').read_text()
        cls.rootfs_makefile = (builder.SOURCE / 'include/rootfs.mk').read_text()
        cls.ucode = Path(os.environ.get('ROUTERFW_UCODE', builder.SOURCE / 'staging_dir/hostpkg/bin/ucode'))

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ('apk', 'repo', 'tmp', 'host/bin', 'hostpkg/bin', 'staging', 'staging-root'):
            (self.root / name).mkdir(parents=True)
        (self.root / 'host/bin/apk').symlink_to(self.apk)
        for name in ('gzip', 'tar'):
            (self.root / 'host/bin' / name).symlink_to(shutil.which(name))
        if self.ucode.is_file():
            (self.root / 'hostpkg/bin/ucode').symlink_to(self.ucode)
        self.command('openssl', 'ecparam', '-name', 'prime256v1', '-genkey',
                     '-noout', '-out', self.root / 'private-key.pem')
        public = self.command('openssl', 'ec', '-in', self.root / 'private-key.pem', '-pubout')
        (self.root / 'public-key.pem').write_text(public.stdout)
        for name in ('base-files', 'libc', 'kernel'):
            self.make_package(name, self.root / 'repo', signed=True)
            (self.root / 'staging' / (name + '.version')).write_text('1.0-r1\n')
        self.make_package('test-library', self.root / 'repo', signed=True)
        self.command(self.apk, 'mkndx', '--keys-dir', self.root,
                     '--sign', self.root / 'private-key.pem',
                     '--output', self.root / 'repo/packages.adb',
                     *sorted((self.root / 'repo').glob('*.apk')))
        (self.root / 'tmp/.build').touch()
        start = self.package_makefile.index('$(curdir)/install:')
        end = self.package_makefile.index('\n\trm -rf $(TARGET_DIR)/run', start)
        apk_start = self.rootfs_makefile.index('apk =')
        apk_end = self.rootfs_makefile.index('\nTARGET_DIR_ORIG', apk_start)
        prepare_start = self.rootfs_makefile.index('define prepare_rootfs\n')
        prepare_end = self.rootfs_makefile.index('\nendef', prepare_start) + len('\nendef')
        makefile = (
            f'TOPDIR := {self.root}\n'
            'curdir := package\n'
            'TMP_DIR := $(TOPDIR)/tmp\n'
            'TARGET_DIR := $(TOPDIR)/rootfs\n'
            'TARGET_DIR_ORIG := $(TOPDIR)/rootfs.orig\n'
            'STAGING_DIR := $(TOPDIR)/staging\n'
            'STAGING_DIR_ROOT := $(TOPDIR)/staging-root\n'
            'STAGING_DIR_HOST := $(TOPDIR)/host\n'
            'STAGING_DIR_HOSTPKG := $(TOPDIR)/hostpkg\n'
            'PACKAGE_DIR_ALL := $(TOPDIR)/repo\n'
            'BUILD_KEY_APK_SEC := $(TOPDIR)/private-key.pem\n'
            'CONFIG_USE_APK := y\nCONFIG_SIGNED_PACKAGES := y\n'
            'ARCH_PACKAGES := aarch64_cortex-a53\n'
            f'FAKEROOT := {self.fakeroot}\nXARGS := xargs\n'
            + self.rootfs_makefile[apk_start:apk_end] + '\n'
            + self.rootfs_makefile[prepare_start:prepare_end] + '\n'
            + self.package_makefile[start:end] + '\nendif\n'
            'package/merge package/merge-index:\n\t@:\n'
            'prepare-rootfs:\n\t$(call prepare_rootfs,$(TARGET_DIR))\n')
        (self.root / 'Makefile').write_text(makefile)

    def command(self, *args, check=True):
        return subprocess.run(list(map(str, args)), cwd=self.root, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)

    def make_package(self, name, destination, arch='aarch64_cortex-a53', depends=None,
                     signed=False, post_install=None):
        files = self.root / 'payload' / name
        files.mkdir(parents=True)
        (files / name).write_text(name + '\n')
        package = destination / (name + '-1.0-r1.apk')
        args = [self.apk, 'mkpkg', '--info', 'name:' + name,
                '--info', 'version:1.0-r1', '--info', 'arch:' + arch,
                '--files', files, '--output', package]
        if depends:
            args += ['--info', 'depends:' + depends]
        if signed:
            args += ['--sign', self.root / 'private-key.pem']
        if post_install is not None:
            script = self.root / (name + '.post-install')
            script.write_text(post_install)
            args += ['--script', 'post-install:' + str(script)]
        self.command(*args)
        return package

    def install(self, success=True):
        result = self.command('make', '-s', 'package/install', check=False)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_installs_local_and_built_dependencies_with_signature_checks(self):
        original = self.root / 'inputs/apk'
        original.mkdir(parents=True)
        package = self.make_package('test-app', original, depends='test-addon test-library')
        data = package.read_bytes()
        with patch.object(builder, 'INPUT', original.parent):
            builder.install_apk_packages(self.root / 'apk')
        self.make_package('test-addon', self.root / 'apk', arch='noarch')
        self.install()
        for name in ('test-app', 'test-addon', 'test-library'):
            self.assertEqual((self.root / 'rootfs' / name).read_text(), name + '\n')
        self.assertEqual(package.read_bytes(), data)
        self.command(self.apk, 'verify', '--keys-dir', self.root,
                     *sorted((self.root / 'apk').glob('*.apk')))

    def test_missing_dependency_stops_installation(self):
        self.make_package('test-app', self.root / 'apk', depends='missing-library')
        result = self.install(success=False)
        self.assertIn('missing-library', result.stderr)

    def test_wrong_architecture_stops_installation(self):
        self.make_package('test-app', self.root / 'apk', arch='x86_64')
        self.install(success=False)

    def test_corrupt_package_stops_installation(self):
        (self.root / 'apk/broken.apk').write_bytes(b'not an APK')
        self.install(success=False)

    def test_empty_directory_preserves_normal_installation(self):
        self.install()
        self.assertTrue((self.root / 'rootfs/base-files').is_file())

    def test_rootfs_runs_shell_and_ucode_postinstall_with_offline_root(self):
        if not self.ucode.is_file():
            self.skipTest('Requires the host ucode interpreter.')
        self.make_package('test-shell', self.root / 'apk', post_install=(
            '#!/bin/sh\nprintf shell > "$IPKG_INSTROOT/shell-ran"\n'))
        self.make_package('test-ucode', self.root / 'apk', post_install=(
            '#!/usr/bin/ucode\n'
            'import { writefile } from "fs";\n'
            'let root = getenv("IPKG_INSTROOT");\n'
            'if (root == null || root == "") exit(99);\n'
            'writefile(root + "/ucode-ran", "ucode");\n'))
        self.install()
        rootfs = self.root / 'rootfs'
        self.assertFalse((rootfs / 'shell-ran').exists())
        self.assertFalse((rootfs / 'ucode-ran').exists())
        result = self.command('make', '-s', 'prepare-rootfs', check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((rootfs / 'shell-ran').read_text(), 'shell')
        self.assertEqual((rootfs / 'ucode-ran').read_text(), 'ucode')
        archive = self.command('tar', '-tzf', rootfs / 'lib/apk/db/scripts.tar.gz')
        self.assertNotIn('.post-install', archive.stdout)

    def assert_postinstall_failure(self, script, exit_code):
        self.make_package('test-failure', self.root / 'apk', post_install=script)
        self.install()
        result = self.command('make', '-s', 'prepare-rootfs', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('postinst script', result.stderr)
        self.assertIn(f'exit code {exit_code}', result.stderr)

    def test_rootfs_stops_on_failed_shell_postinstall(self):
        self.assert_postinstall_failure('#!/bin/sh\nexit 17\n', 17)

    def test_rootfs_stops_on_failed_ucode_postinstall(self):
        if not self.ucode.is_file():
            self.skipTest('Requires the host ucode interpreter.')
        self.assert_postinstall_failure('#!/usr/bin/ucode\nexit(23);\n', 23)


if __name__ == '__main__':
    unittest.main()
