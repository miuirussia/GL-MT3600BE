"""APK update discovery, checksum pinning and safe descriptor replacement."""
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
              patch.object(updater.urllib.request, 'urlopen', return_value=io.BytesIO(data)) as download):
            self.assertEqual(updater.checksum('https://example.com/example.apk'),
                             hashlib.sha256(data).hexdigest())
        self.assertIsNone(download.call_args.args[0].get_header('Authorization'))


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
                update.assert_called_once_with(self.directory, check=True)

    def test_launcher_works_outside_repository_and_returns_error_status(self):
        self.descriptor.write_text('https://example.com/example.apk\nSKIP\n')
        result = subprocess.run(['bash', str(ROOT / 'update.sh'), '--check', str(self.directory)],
                                cwd=self.directory, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('Unsupported APK source', result.stderr)


if __name__ == '__main__':
    unittest.main()
