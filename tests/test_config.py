"""Configuration fragments can be loaded without a prepared OpenWrt tree."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('config_builder', ROOT / 'scripts/builder.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ConfigFragments(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        mocked = patch.object(builder, 'INPUT', self.root)
        mocked.start()
        self.addCleanup(mocked.stop)

    def fragment(self, name, text):
        path = self.root / 'configs' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_filename_order_and_section_precedence(self):
        self.fragment('device/z.config', 'CONFIG_DEVICE=y\nCONFIG_ORDER=m\n')
        self.fragment('device/a.config', 'CONFIG_ORDER=y\nCONFIG_SHARED=y')
        self.fragment('packages/a.config', 'CONFIG_PACKAGE_example=y\nCONFIG_SHARED=m')
        self.fragment('custom/z.config', '# CONFIG_SHARED is not set\n')
        self.fragment('custom/a.config', 'CONFIG_SHARED=y\n')
        self.fragment('kernel/a.config', 'CONFIG_LINUX_ONLY=y\n')
        self.fragment('packages/notes.txt', 'CONFIG_IGNORED=y\n')
        self.fragment('packages/example.config.swp', 'CONFIG_IGNORED=y\n')

        self.assertEqual(builder.requested_config(), {
            'CONFIG_DEVICE': 'y', 'CONFIG_ORDER': 'm',
            'CONFIG_PACKAGE_example': 'y', 'CONFIG_SHARED': 'n',
        })

    def test_configure_merges_kernel_fragments_with_line_boundaries(self):
        lock = {'device': 'glinet_gl-mt3600be', 'kernel': {'version': 'test-kernel'}}
        device = 'CONFIG_TARGET_mediatek_filogic_DEVICE_' + lock['device']
        self.fragment('device/target.config', device + '=y\n')
        self.fragment('kernel/z.config', '# CONFIG_TLS is not set')
        self.fragment('kernel/a.config', 'CONFIG_TLS=y')
        source = self.root / 'openwrt'
        source.mkdir()

        with patch.object(builder, 'SOURCE', source), patch.object(
                builder, 'run', side_effect=[None, lock['kernel']['version']]):
            builder.configure(lock)

        self.assertEqual((source / 'env/kernel-config').read_text(),
                         'CONFIG_TLS=y\n# CONFIG_TLS is not set\n')
        self.assertEqual((source / '.config').read_text(), device + '=y\n')

    def test_empty_optional_sections(self):
        self.fragment('device/target.config', 'CONFIG_TARGET_mediatek=y\n')
        self.assertEqual(builder.requested_config(), {'CONFIG_TARGET_mediatek': 'y'})
        self.assertEqual(builder.config_text('kernel'), '')


if __name__ == '__main__':
    unittest.main()
