"""APK discovery, dependency checks, checksum pinning and safe descriptor replacement."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('update_apk', ROOT / 'scripts/update-apk.py')
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class Discovery(unittest.TestCase):
    def test_matches_versions_without_changing_package_variant_or_architecture(self):
        cases = [
            ('luci-theme-outline-1.4.1-r20261116.apk', 'luci-theme-outline-1.5.0-r1.apk'),
            ('forkop_2.0.19_all.apk', 'forkop_2.1.0_all.apk'),
            ('luci-i18n-forkop-ru_2.0.19_all.apk', 'luci-i18n-forkop-ru_2.1.0_all.apk'),
            ('sing-box-forkop_1.14.4-r5_base_aarch64_cortex-a53.apk',
             'sing-box-forkop_1.15.0-r1_base_aarch64_cortex-a53.apk'),
            ('luci-i18n-temp-status-ru-0.8.1-r1.apk', 'luci-i18n-temp-status-ru-0.9.0-r1.apk'),
            ('example_latest_all.apk', 'example_latest_all.apk'),
        ]
        distractors = [
            'forkop_2.1.0_all.ipk', 'luci-app-forkop_2.1.0_all.apk',
            'sing-box-forkop_1.15.0-r1_full_aarch64_cortex-a53.apk',
            'sing-box-forkop_1.15.0-r1_base_x86_64.apk',
            'luci-app-fancontrol_latest_all.apk',
        ]
        for old, new in cases:
            with self.subTest(old=old):
                assets = [{'name': name} for name in [*distractors, new]]
                self.assertEqual(updater.select_asset(old, assets)['name'], new)

    def test_missing_or_ambiguous_assets_fail(self):
        for names in ([], ['example-2.0.apk', 'example-3.0.apk'], ['example-2.0_x86_64.apk']):
            with self.subTest(names=names), self.assertRaisesRegex(RuntimeError, 'expected one matching'):
                updater.select_asset('example-1.0.apk', [{'name': name} for name in names])

    def test_release_discovery_handles_stable_and_rolling_releases(self):
        for tag, endpoint in [('v1.0', 'latest'), ('continuous', 'tags/continuous'),
                              ('nightly', 'tags/nightly')]:
            with self.subTest(tag=tag):
                github = Mock()
                github.get.return_value = {'assets': [{
                    'name': 'example-2.0.apk', 'browser_download_url': 'https://example.com/new.apk'}]}
                result = updater.resolve(
                    f'https://github.com/owner/repo/releases/download/{tag}/example-1.0.apk', github)
                github.get.assert_called_once_with('owner/repo/releases/' + endpoint)
                self.assertEqual(result, ('example-2.0.apk', 'https://example.com/new.apk'))

    def test_raw_discovery_pins_commit_and_keeps_openwrt_directory(self):
        github = Mock()
        commit = 'a' * 40
        github.get.side_effect = [
            [{'sha': commit}],
            [{'name': 'example-2.0.apk', 'type': 'file'},
             {'name': 'example-3.0.apk', 'type': 'dir'}],
        ]
        result = updater.resolve(
            'https://raw.githubusercontent.com/owner/repo/old-commit/25.12/example-1.0.apk', github)
        self.assertEqual(result, ('example-2.0.apk',
                                 f'https://raw.githubusercontent.com/owner/repo/{commit}/25.12/example-2.0.apk'))
        self.assertEqual(github.get.call_args_list[0].args,
                         ('owner/repo/commits?path=25.12&per_page=1',))
        self.assertEqual(github.get.call_args_list[1].args,
                         (f'owner/repo/contents/25.12?ref={commit}',))

    def test_unsupported_sources_fail(self):
        for url in ['https://example.com/example-1.0.apk', 'file:///tmp/example.apk',
                    'http://github.com/owner/repo/releases/download/v1/example.apk',
                    'https://github.com/owner/repo/releases/download/v1/example.apk?token=abc']:
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                updater.resolve(url, Mock())

    def test_api_requests_are_cached_and_authenticated(self):
        with (patch.dict(os.environ, {'GH_TOKEN': 'test-token'}),
              patch.object(updater.urllib.request, 'urlopen',
                           return_value=io.BytesIO(json.dumps({'assets': []}).encode())) as download):
            github = updater.GitHub()
            self.assertEqual(github.get('owner/repo/releases/latest'), {'assets': []})
            self.assertEqual(github.get('owner/repo/releases/latest'), {'assets': []})
        download.assert_called_once()
        request = download.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.github.com/repos/owner/repo/releases/latest')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-token')

    def test_checksum_streams_package_without_sending_api_token(self):
        data = b'apk payload' * 200000
        with (patch.dict(os.environ, {'GH_TOKEN': 'test-token'}),
              patch.object(updater.urllib.request, 'urlopen', return_value=io.BytesIO(data)) as download,
              tempfile.TemporaryFile() as package):
            self.assertEqual(updater.checksum('https://example.com/example.apk', package),
                             hashlib.sha256(data).hexdigest())
            package.seek(0)
            self.assertEqual(package.read(), data)
        self.assertIsNone(download.call_args.args[0].get_header('Authorization'))


class Dependencies(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.configs = Path(temporary.name)
        (self.configs / 'packages').mkdir()
        (self.configs / 'packages/runtime.config').write_text(
            'CONFIG_PACKAGE_ucode=y\nCONFIG_PACKAGE_ip-tiny=y\n'
            'CONFIG_PACKAGE_nftables-json=m\nCONFIG_PACKAGE_unused=n\n')
        self.repositories = patch.object(updater, 'repository_packages', return_value=[])
        self.repository_packages = self.repositories.start()
        self.addCleanup(self.repositories.stop)

    def test_checks_configured_packages_variants_base_packages_and_local_providers(self):
        self.repository_packages.return_value = [
            {'name': 'ip-tiny', 'provides': ['ip=1']},
            {'name': 'nftables-json', 'provides': ['nftables=1']},
        ]
        updater.check_dependencies([
            {'name': 'app', 'depends': ['libc', 'kernel', 'base-files', 'ucode>=1',
                                       'ip', 'nftables', 'addon>=2', 'virtual-addon=2', '!conflict']},
            {'name': 'addon', 'version': '2', 'provides': ['virtual-addon=2']},
        ], self.configs)

    def test_discovers_arbitrary_virtual_dependencies_from_configured_packages_only(self):
        self.repository_packages.return_value = [
            {'name': 'ucode', 'provides': ['new-virtual-package=3']},
            {'name': 'not-selected', 'provides': ['missing-virtual-package=2']},
        ]
        updater.check_dependencies([
            {'name': 'app', 'depends': ['new-virtual-package>=2']},
        ], self.configs)
        with self.assertRaisesRegex(RuntimeError, 'app: missing-virtual-package'):
            updater.check_dependencies([
                {'name': 'app', 'depends': ['missing-virtual-package']},
            ], self.configs)

    def test_exact_and_local_dependencies_do_not_fetch_repository_indexes(self):
        updater.check_dependencies([{'name': 'app', 'depends': ['libc', 'ucode']}], self.configs)
        self.repository_packages.assert_not_called()

    def test_configured_abi_libraries_supply_real_names_and_virtual_dependencies(self):
        (self.configs / 'packages/libraries.config').write_text(
            'CONFIG_PACKAGE_libubus=y\nCONFIG_PACKAGE_libsqlite3=m\n')
        self.repository_packages.return_value = [
            {'name': 'libubus20251202', 'provides': ['libubus', 'libubus-any']},
            {'name': 'libsqlite3-0', 'provides': ['libsqlite3', 'libsqlite3-any']},
        ]
        updater.check_dependencies([
            {'name': 'app', 'depends': ['libubus20251202>=1', 'libubus-any',
                                       'libsqlite3-0', 'libsqlite3-any']},
        ], self.configs)

    def test_disabled_abi_library_does_not_supply_dependencies(self):
        (self.configs / 'packages/libraries.config').write_text('CONFIG_PACKAGE_libubus=n\n')
        self.repository_packages.return_value = [
            {'name': 'libubus20251202', 'provides': ['libubus', 'libubus-any']},
        ]
        with self.assertRaisesRegex(RuntimeError, 'app: libubus-any\napp: libubus20251202'):
            updater.check_dependencies([
                {'name': 'app', 'depends': ['libubus20251202', 'libubus-any']},
            ], self.configs)

    def test_shared_provides_do_not_select_other_package_variants(self):
        self.repository_packages.return_value = [
            {'name': 'ucode-alternative', 'provides': ['ucode=1', 'alternative-only']},
            {'name': 'ip-full', 'provides': ['ip=1', 'full-only']},
        ]
        with self.assertRaisesRegex(RuntimeError, 'app: alternative-only\napp: full-only'):
            updater.check_dependencies([
                {'name': 'app', 'depends': ['alternative-only', 'full-only']},
            ], self.configs)

    def test_missing_and_disabled_dependencies_report_package_and_version_constraint(self):
        with self.assertRaisesRegex(RuntimeError, 'app: new-library>=2\napp: unused'):
            updater.check_dependencies([
                {'name': 'app', 'depends': ['unused', 'new-library>=2']},
            ], self.configs)

    def test_later_config_fragments_override_earlier_selections(self):
        (self.configs / 'custom').mkdir()
        for override in ('CONFIG_PACKAGE_ucode=n\n', '# CONFIG_PACKAGE_ucode is not set\n'):
            with self.subTest(override=override):
                (self.configs / 'custom/override.config').write_text(override)
                with self.assertRaisesRegex(RuntimeError, 'app: ucode'):
                    updater.check_dependencies([{'name': 'app', 'depends': ['ucode']}], self.configs)

    def test_missing_config_directory_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, 'Configuration directory does not exist'):
            updater.check_dependencies([{'name': 'app'}], self.configs / 'missing')

    def test_reads_dependency_metadata_without_executing_package_scripts(self):
        info = {'name': 'app', 'version': '2', 'depends': ['ucode'], 'provides': ['virtual=2']}
        with (tempfile.TemporaryFile() as package,
              patch.object(updater, 'apk', return_value=json.dumps({'info': info})) as apk):
            package.write(b'APK data')
            self.assertEqual(updater.package_info(package), info)
            self.assertEqual(package.tell(), 0)
            apk.assert_called_once_with('adbdump', '--format', 'json', '/dev/stdin', stdin=package)

    def test_corrupt_apk_and_invalid_metadata_fail(self):
        for info in (None, [], {}, {'name': 'app', 'depends': 'library'},
                     {'name': 'app', 'provides': [None]}):
            with (self.subTest(info=info), tempfile.TemporaryFile() as package,
                  patch.object(updater, 'apk', return_value=json.dumps({'info': info})),
                  self.assertRaisesRegex(RuntimeError, 'metadata')):
                updater.package_info(package)

    def test_apk_command_uses_native_tool_or_pinned_docker_image(self):
        for native in (None, '/usr/bin/apk'):
            with (self.subTest(native=native), patch.dict(os.environ, {}, clear=True),
                  patch.object(updater.shutil, 'which', side_effect=[native, '/usr/bin/docker']
                               if native is None else [native]),
                  patch.object(updater.subprocess, 'run', return_value=Mock(
                      returncode=0, stdout='{}')) as run):
                self.assertEqual(updater.apk('adbdump'), '{}')
                expected = ([native] if native else
                            ['docker', 'run', '--rm', '-i', updater.APK_IMAGE, 'apk'])
                self.assertEqual(run.call_args.args[0], [*expected, 'adbdump'])

    def test_missing_tool_and_failed_inspection_are_errors(self):
        with (patch.dict(os.environ, {}, clear=True),
              patch.object(updater.shutil, 'which', return_value=None),
              self.assertRaisesRegex(RuntimeError, 'require apk-tools 3')):
            updater.apk('adbdump')
        with (patch.dict(os.environ, {'ROUTERFW_APK': '/test/apk'}),
              patch.object(updater.subprocess, 'run', return_value=Mock(
                  returncode=1, stderr='corrupt package')),
              self.assertRaisesRegex(RuntimeError, 'corrupt package')):
            updater.apk('adbdump')


class RepositoryMetadata(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = json.dumps({'version_number': '25.12.6', 'target': 'mediatek/filogic',
                                   'arch_packages': 'aarch64_cortex-a53'}).encode()
        self.reference = 'https://downloads.openwrt.org/releases/25.12.6/targets/mediatek/filogic/profiles.json'
        self.lock = {'release': '25.12.6', 'target': 'mediatek/filogic',
                     'reference': {'url': self.reference,
                                   'sha256': hashlib.sha256(self.profile).hexdigest()},
                     'feeds': [{'name': 'luci'}, {'name': 'packages'}]}
        self.write_lock()
        root = patch.object(updater, 'ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)

    def write_lock(self):
        (self.root / 'openwrt.lock.json').write_text(json.dumps(self.lock))

    def test_reads_providers_from_locked_target_architecture_and_feed_indexes(self):
        info = {'name': 'custom-variant', 'provides': ['custom-virtual=2']}
        def download(url, package):
            data = self.profile if url == self.reference else b'index data'
            package.write(data)
            return hashlib.sha256(data).hexdigest()
        with (patch.object(updater, 'checksum', side_effect=download) as fetch,
              patch.object(updater, 'apk', return_value=json.dumps({'packages': [info]}))):
            self.assertEqual(list(updater.repository_packages()), [info] * 4)
        release = 'https://downloads.openwrt.org/releases/25.12.6/'
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [
            self.reference,
            release + 'targets/mediatek/filogic/packages/packages.adb',
            *(release + f'packages/aarch64_cortex-a53/{feed}/packages.adb'
              for feed in ('base', 'luci', 'packages')),
        ])

    def test_wrong_profile_checksum_or_release_stops_inspection(self):
        for digest, message in [('0' * 64, 'checksum mismatch'),
                                (hashlib.sha256(self.profile).hexdigest(), 'does not match')]:
            self.lock['release'] = 'different-release'
            self.write_lock()
            def download(url, package):
                package.write(self.profile)
                return digest
            with (self.subTest(message=message),
                  patch.object(updater, 'checksum', side_effect=download),
                  patch.object(updater, 'apk') as apk,
                  self.assertRaisesRegex(RuntimeError, message)):
                list(updater.repository_packages())
            apk.assert_not_called()


class Updates(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.url = 'https://github.com/owner/repo/releases/download/v1/example-1.0.apk'
        self.descriptor = self.directory / 'example-1.0.apk.txt'
        self.original = self.url + '\n' + '0' * 64 + '\n'
        self.descriptor.write_text(self.original)
        self.new_url = self.url.replace('v1/', 'v2/').replace('1.0.apk', '2.0.apk')
        self.resolver = patch.object(updater, 'resolve', return_value=('example-2.0.apk', self.new_url))
        self.resolver.start()
        self.addCleanup(self.resolver.stop)
        self.digest = '1' * 64
        self.hasher = patch.object(updater, 'checksum', return_value=self.digest)
        self.hasher.start()
        self.addCleanup(self.hasher.stop)
        self.configs = self.directory / 'configs'
        (self.configs / 'packages').mkdir(parents=True)
        (self.configs / 'packages/runtime.config').write_text('CONFIG_PACKAGE_library=y\n')
        self.root = patch.object(updater, 'ROOT', self.directory)
        self.root.start()
        self.addCleanup(self.root.stop)
        self.metadata = patch.object(updater, 'package_info', return_value={
            'name': 'example', 'version': '2.0', 'depends': ['libc', 'library']})
        self.package_info = self.metadata.start()
        self.addCleanup(self.metadata.stop)
        self.repositories = patch.object(updater, 'repository_packages', return_value=[])
        self.repositories.start()
        self.addCleanup(self.repositories.stop)

    def test_renames_descriptor_updates_url_and_pins_checksum(self):
        (self.directory / '.gitkeep').touch()
        self.assertTrue(updater.update(self.directory))
        self.assertFalse(self.descriptor.exists())
        self.assertEqual((self.directory / 'example-2.0.apk.txt').read_text(),
                         self.new_url + '\n' + self.digest + '\n')
        self.assertTrue((self.directory / '.gitkeep').exists())

    def test_check_does_not_write(self):
        self.assertTrue(updater.update(self.directory, check=True))
        self.assertEqual(self.descriptor.read_text(), self.original)
        self.assertFalse((self.directory / 'example-2.0.apk.txt').exists())

    def test_new_dependency_stops_update_without_changing_descriptors_or_configs(self):
        other = self.directory / 'other-1.0.apk.txt'
        other.write_text(self.original)
        originals = {path: path.read_bytes() for path in self.directory.rglob('*') if path.is_file()}
        for check in (False, True):
            with (self.subTest(check=check),
                  patch.object(updater, 'resolve', side_effect=[
                      ('example-2.0.apk', self.new_url), ('other-2.0.apk', self.new_url)]),
                  patch.object(updater, 'package_info', side_effect=[
                      {'name': 'example', 'depends': ['library']},
                      {'name': 'other', 'depends': ['new-library>=2']}]),
                  self.assertRaisesRegex(RuntimeError, 'other: new-library>=2')):
                updater.update(self.directory, check=check)
            self.assertEqual({path: path.read_bytes() for path in self.directory.rglob('*')
                              if path.is_file()}, originals)

    def test_unchanged_package_still_checks_dependencies(self):
        self.descriptor.write_text(self.url + '\n' + self.digest + '\n')
        with (patch.object(updater, 'resolve', return_value=('example-1.0.apk', self.url)),
              patch.object(updater, 'package_info', return_value={
                  'name': 'example', 'depends': ['missing-library']}),
              self.assertRaisesRegex(RuntimeError, 'missing-library')):
            updater.update(self.directory)

    def test_repository_download_failure_does_not_change_descriptors(self):
        with (patch.object(updater, 'package_info', return_value={
                  'name': 'example', 'depends': ['virtual-library']}),
              patch.object(updater, 'repository_packages', side_effect=urllib.error.URLError('offline')),
              self.assertRaises(urllib.error.URLError)):
            updater.update(self.directory)
        self.assertEqual(self.descriptor.read_text(), self.original)
        self.assertFalse((self.directory / 'example-2.0.apk.txt').exists())

    def test_local_binary_can_satisfy_dependencies_and_is_not_modified(self):
        binary = self.directory / 'addon.apk'
        binary.write_bytes(b'local APK')
        with patch.object(updater, 'package_info', side_effect=[
                {'name': 'example', 'depends': ['virtual-addon']},
                {'name': 'addon', 'depends': ['library'], 'provides': ['virtual-addon=1']}]):
            self.assertTrue(updater.update(self.directory))
        self.assertEqual(binary.read_bytes(), b'local APK')

    def test_explicit_config_directory_is_used(self):
        self.assertTrue(updater.update(self.directory, configs=self.configs))
        with self.assertRaisesRegex(RuntimeError, 'Configuration directory does not exist'):
            updater.update(self.directory, configs=self.configs / 'missing')

    def test_up_to_date_is_idempotent(self):
        self.descriptor.write_text(self.url + '\n' + self.digest + '\n')
        with patch.object(updater, 'resolve', return_value=('example-1.0.apk', self.url)):
            self.assertFalse(updater.update(self.directory))

    def test_replaced_rolling_asset_updates_hash_even_when_url_is_unchanged(self):
        with patch.object(updater, 'resolve', return_value=('example-1.0.apk', self.url)):
            self.assertTrue(updater.update(self.directory))
        self.assertEqual(self.descriptor.read_text(), self.url + '\n' + self.digest + '\n')

    def test_unchanged_raw_package_keeps_its_commit_pin(self):
        old = 'https://raw.githubusercontent.com/owner/repo/old/25.12/example-1.0.apk'
        self.descriptor.write_text(old + '\n' + self.digest + '\n')
        with patch.object(updater, 'resolve', return_value=('example-1.0.apk', old.replace('/old/', '/new/'))):
            self.assertFalse(updater.update(self.directory))

    def test_failure_in_later_package_leaves_all_descriptors_unchanged(self):
        other = self.directory / 'other-1.0.apk.txt'
        other.write_text(self.original)
        with (patch.object(updater, 'resolve', side_effect=[
                ('example-2.0.apk', self.new_url), ('other-2.0.apk', self.new_url)]),
              patch.object(updater, 'checksum', side_effect=[self.digest, urllib.error.URLError('offline')])):
            with self.assertRaises(urllib.error.URLError):
                updater.update(self.directory)
        self.assertEqual(self.descriptor.read_text(), self.original)
        self.assertEqual(other.read_text(), self.original)
        self.assertFalse((self.directory / 'example-2.0.apk.txt').exists())

    def test_conflicting_binary_or_descriptor_is_not_overwritten(self):
        for name in ('example-2.0.apk', 'example-2.0.apk.txt'):
            with self.subTest(name=name):
                target = self.directory / name
                target.write_text('existing')
                with self.assertRaisesRegex(RuntimeError, 'Conflicting'):
                    updater.update(self.directory)
                self.assertEqual(target.read_text(), 'existing')
                self.assertEqual(self.descriptor.read_text(), self.original)
                target.unlink()

    def test_invalid_or_symlink_descriptor_is_rejected(self):
        for content in ('', self.url + '\n', self.url + '\ninvalid\n'):
            self.descriptor.write_text(content)
            with self.assertRaisesRegex(RuntimeError, 'expected a URL'):
                updater.update(self.directory)
        self.descriptor.unlink()
        self.descriptor.symlink_to(self.directory / 'missing')
        with self.assertRaisesRegex(RuntimeError, 'regular descriptor'):
            updater.update(self.directory)

    def test_check_exit_codes(self):
        for changed, expected in [(False, 0), (True, 1)]:
            with (patch.object(updater, 'update', return_value=changed) as update,
                  patch.object(updater.sys, 'argv', ['update-apk.py', '--check', str(self.directory)])):
                self.assertEqual(updater.main(), expected)
                update.assert_called_once_with(self.directory, check=True, configs=None)

    def test_dependency_error_exit_code(self):
        with (patch.object(updater, 'update', side_effect=RuntimeError('missing dependency')),
              patch.object(updater.sys, 'argv', ['update-apk.py', '--check', str(self.directory)])):
            self.assertEqual(updater.main(), 2)

    def test_launcher_works_outside_repository_and_returns_error_status(self):
        self.descriptor.write_text('https://example.com/example.apk\nSKIP\n')
        result = subprocess.run(['bash', str(ROOT / 'update.sh'), '--check', str(self.directory)],
                                cwd=self.directory, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('Unsupported APK source', result.stderr)


if __name__ == '__main__':
    unittest.main()
