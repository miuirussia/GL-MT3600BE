"""Regression checks for the network backport and first-boot defaults."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('network_builder', ROOT / 'scripts/builder.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class RSSPacketSteering(unittest.TestCase):
    def test_cpu_masks_disable_rps_for_rss(self):
        ucode = shutil.which('ucode')
        if not ucode:
            host_ucode = builder.SOURCE / 'staging_dir/host/bin/ucode'
            if not host_ucode.is_file():
                self.skipTest('Requires the host ucode interpreter.')
            ucode = str(host_ucode)

        source = (builder.SOURCE / 'package/network/config/netifd/files/usr/libexec/'
                  'network/packet-steering.uc').read_text()
        start = source.index('function cpu_mask(cpu)')
        end = source.index('\n}', start) + 2
        program = ('let cpus = [{id:0}, {id:1}, {id:2}, {id:3}];\n'
                   + source[start:end]
                   + '\nprint(sprintf("%J", [cpu_mask(null), cpu_mask(0), '
                     'cpu_mask(3), cpu_mask(-1)]));')
        masks = json.loads(subprocess.check_output([ucode, '-e', program], text=True))
        self.assertEqual(masks, ['0', '1', '8', 'f'])


class IWRadioArguments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('cc')
        if not compiler:
            raise unittest.SkipTest('Requires a C compiler.')

        # Compile the actual patched handlers. The iw patch retains complete
        # functions as context so this check needs no downloaded package tree.
        outer = (ROOT / 'patches/openwrt/180-plcharriere-iw.patch').read_text()
        inner = ''.join(line[1:] for line in outer.splitlines(True)
                        if line.startswith('+') and not line.startswith('+++'))
        source = ''.join(line[1:] for line in inner.splitlines(True)
                         if line.startswith((' ', '+')) and not line.startswith('+++'))

        def function(name):
            start = source.index('static int ' + name + '(')
            end = source.index('\n}', start) + 2
            return source[start:end]

        program = r'''
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
struct nl80211_state { int unused; };
struct nl_msg { int has_radio; unsigned radio; };
enum id_input { TEST_INPUT };
enum nl80211_tx_power_setting {
    NL80211_TX_POWER_AUTOMATIC, NL80211_TX_POWER_FIXED, NL80211_TX_POWER_LIMITED
};
#define NL80211_ATTR_WIPHY_RADIO_INDEX 1
#define NL80211_ATTR_WIPHY_TX_POWER_SETTING 2
#define NL80211_ATTR_WIPHY_TX_POWER_LEVEL 3
#define NL80211_ATTR_WIPHY_ANTENNA_TX 4
#define NL80211_ATTR_WIPHY_ANTENNA_RX 5
#define NLA_PUT_U8(msg, attr, value) do { \
    if (0) goto nla_put_failure; \
    (msg)->has_radio = 1; (msg)->radio = (uint8_t)(value); \
} while (0)
#define NLA_PUT_U32(msg, attr, value) do { \
    if (0) goto nla_put_failure; \
    (void)(msg); (void)(value); \
} while (0)
'''
        program += '\n'.join(function(name) for name in
                             ('put_radio_idx', 'handle_txpower', 'handle_antenna'))
        program += r'''
int main(int argc, char **argv) {
    struct nl_msg msg = {0};
    int result;
    if (!strcmp(argv[1], "txpower"))
        result = handle_txpower(NULL, &msg, argc - 2, argv + 2, TEST_INPUT);
    else
        result = handle_antenna(NULL, &msg, argc - 2, argv + 2, TEST_INPUT);
    printf("%d %d %u\n", result, msg.has_radio, msg.radio);
    return 0;
}
'''
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name)
        (directory / 'handlers.c').write_text(program)
        cls.executable = directory / 'handlers'
        subprocess.run([compiler, '-std=c99', '-Wall', '-Werror',
                        '-Wno-unused-parameter', str(directory / 'handlers.c'),
                        '-o', str(cls.executable)], check=True, capture_output=True)

    def invoke(self, *args):
        output = subprocess.check_output([str(self.executable), *args], text=True)
        return tuple(map(int, output.splitlines()[-1].split()))

    def test_radio_index_bounds(self):
        for command in [('txpower', 'auto'), ('txpower', 'fixed', '1000'),
                        ('antenna', 'all'), ('antenna', '3'), ('antenna', '3', '1')]:
            for radio in ['0', '1', '255']:
                with self.subTest(command=command, radio=radio):
                    self.assertEqual(self.invoke(*command, 'radio', radio),
                                     (0, 1, int(radio)))
            for radio in ['', '-1', '256', '4294967296', '999999999999999999999', '1x']:
                with self.subTest(command=command, radio=radio):
                    self.assertNotEqual(self.invoke(*command, 'radio', radio)[0], 0)

    def test_rejects_invalid_selectors_and_power(self):
        for args in [('txpower', 'auto', 'typo', '1'),
                     ('txpower', 'fixed', '1000', 'typo', '1'),
                     ('txpower', 'fixed', '1000junk', 'radio', '1'),
                     ('txpower', 'fixed', '', 'radio', '1'),
                     ('antenna', 'all', 'typo', '1')]:
            with self.subTest(args=args):
                self.assertNotEqual(self.invoke(*args)[0], 0)

    def test_commands_without_radio_remain_valid(self):
        for args in [('txpower', 'auto'), ('txpower', 'fixed', '1000'),
                     ('antenna', 'all'), ('antenna', '3'), ('antenna', '3', '1')]:
            with self.subTest(args=args):
                self.assertEqual(self.invoke(*args), (0, 0, 0))


class BoardDefaults(unittest.TestCase):
    def test_uci_failures_leave_defaults_for_retry(self):
        patch = (ROOT / 'patches/openwrt/200-plcharriere-board-integration.patch').read_text()
        stubs = r'''
board_name() { echo glinet,gl-mt3600be; }
config_load() { :; }
config_foreach() { "$1" radio0; }
config_get() {
    if [ "$3" = band ]; then eval "$1=5g"; else eval "$1=''"; fi
}
uci() {
    [ "$1" != -q ] || shift
    case "$1" in
        get|show) return 1;;
        batch) cat >/dev/null; [ "$UCI_FAIL" != batch ];;
        set|commit) [ "$UCI_FAIL" != "$1" ];;
    esac
}
'''
        for name, failures in [
            ('10_gl-mt3600be-wifi-tx-burst', ['set', 'commit']),
            ('11_gl-mt3600be-dns-dscp', ['batch']),
            ('12_gl-mt3600be-flow-offload', ['batch']),
        ]:
            marker = '/etc/uci-defaults/' + name + '\n'
            start = patch.index('+++ b/target/linux/mediatek/filogic/base-files' + marker)
            block = patch[start:]
            block = block.split('\ndiff --git ', 1)[0]
            script = ''.join(line[1:] + '\n' for line in block.splitlines()[2:]
                             if line.startswith('+'))
            script = stubs + script.replace('. /lib/functions.sh', ':')
            for failure in ['', *failures]:
                with self.subTest(script=name, failure=failure):
                    result = subprocess.run(['/bin/sh', '-c',
                                             'UCI_FAIL=' + failure + '\n' + script],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, not failure, result.stderr)


if __name__ == '__main__':
    unittest.main()
