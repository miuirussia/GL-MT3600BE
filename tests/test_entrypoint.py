"""Exercise entrypoint permission handling without Docker or root privileges."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent

# Stub privileged commands; gosu models access checks as the builder user.
STUBS = r'''
groupmod() { :; }
usermod() { :; }
mkdir() { :; }
chown() {
    printf '%s\n' "$*" >> "$COMMAND_LOG"
    case " $* " in
        *" $DENIED_CHOWN "*) return 1 ;;
    esac
    if [[ "$*" == *" $INACCESSIBLE_DIR" && -n "$INACCESSIBLE_DIR" ]]; then
        touch "$ACCESS_FIXED"
    fi
}
stat() { printf '%s\n' "$MOUNT_OWNER"; }
flock() { return "$LOCK_STATUS"; }
setsid() { "$@"; }
rsync() {
    printf 'rsync %s\n' "$*" >> "$COMMAND_LOG"
    if [[ "${@: -1}" == "$DENIED_EXPORT" ]]; then return 1; fi
}
'''

GOSU = r'''#!/bin/bash
shift
if [[ "$1" == test ]]; then
    if [[ "$3" == "$INACCESSIBLE_DIR" && "$2" == "$INACCESSIBLE_MODE" && ! -e "$ACCESS_FIXED" ]]; then
        exit 1
    fi
else
    printf '%s\n' "$*" >> "$COMMAND_LOG"
    if [[ "$EXPECT_STDIN" == 1 ]]; then
        read -r input
        printf 'stdin=%s\n' "$input" >> "$COMMAND_LOG"
    fi
    if [[ -n "${ROUTERFW_ARTIFACTS_DIR:-}" ]]; then
        printf 'artifacts=%s\n' "$ROUTERFW_ARTIFACTS_DIR" >> "$COMMAND_LOG"
    fi
    if [[ -n "${ROUTERFW_KEYS_DIR:-}" ]]; then
        printf 'keys=%s\n' "$ROUTERFW_KEYS_DIR" >> "$COMMAND_LOG"
    fi
    exit "$BUILDER_STATUS"
fi
'''


class EntrypointPermissions(unittest.TestCase):
    def run_entrypoint(self, inaccessible='', mode='-w', denied='/artifacts',
                       owner='1000:1000', builder_status=0, denied_export='', lock_status=0,
                       stdin=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stubs = root / 'stubs.sh'
            stubs.write_text(STUBS)
            gosu = root / 'gosu'
            gosu.write_text(GOSU)
            gosu.chmod(0o755)
            log = root / 'commands.log'
            env = dict(os.environ, BASH_ENV=str(stubs), COMMAND_LOG=str(log),
                       PATH=str(root) + os.pathsep + os.environ['PATH'],
                       BUILD_UID='1234', BUILD_GID='5678', DENIED_CHOWN=denied,
                       INACCESSIBLE_DIR=inaccessible, INACCESSIBLE_MODE=mode,
                       MOUNT_OWNER=owner, BUILDER_STATUS=str(builder_status),
                       ACCESS_FIXED=str(root / 'access-fixed'),
                       DENIED_EXPORT=denied_export, LOCK_STATUS=str(lock_status),
                       EXPECT_STDIN=str(int(stdin is not None)))
            # Keep the entrypoint's lock file inside the temporary test tree.
            entrypoint = root / 'entrypoint.sh'
            entrypoint.write_text((ROOT / 'docker/entrypoint.sh').read_text().replace(
                'exec 9>/work/.routerfw-mounts.lock', f'exec 9>"{root}/mounts.lock"'))
            result = subprocess.run(
                ['bash', str(entrypoint), 'make', '-j1'],
                env=env, capture_output=True, text=True, input=stdin)
            return result, log.read_text().splitlines()

    def test_writable_bind_mounts_do_not_require_chown(self):
        result, commands = self.run_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(commands, [
            '1234:5678 /work /work/openwrt /work/downloads /work/ccache /home/builder',
            'python3 /inputs/scripts/builder.py make -j1',
        ])

    def test_inaccessible_bind_mount_gets_chown(self):
        for directory in ('/artifacts', '/keys'):
            for mode in ('-w', '-x'):
                with self.subTest(directory=directory, mode=mode):
                    result, commands = self.run_entrypoint(directory, mode, denied='/none')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f'1234:5678 {directory}', commands)
                    self.assertEqual(commands[-1], 'python3 /inputs/scripts/builder.py make -j1')

    def test_inaccessible_bind_mount_with_denied_chown_stops_build(self):
        for directory in ('/artifacts', '/keys'):
            with self.subTest(directory=directory):
                result, commands = self.run_entrypoint(directory, denied=directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(command.startswith('python3 ') for command in commands))

    def test_work_volume_chown_failure_stops_build(self):
        result, commands = self.run_entrypoint(denied='/work')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(commands), 1)

    def test_root_owned_mount_with_denied_chown_stages_and_exports(self):
        for directory in ('/artifacts', '/keys'):
            for mode in ('-w', '-x'):
                with self.subTest(directory=directory, mode=mode):
                    result, commands = self.run_entrypoint(
                        directory, mode, denied=directory, owner='0:0')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(
                        f'rsync -a --no-owner --no-group {directory}/ /work/host{directory}/',
                        commands)
                    self.assertIn(f'-R 1234:5678 /work/host{directory}', commands)
                    variable = 'artifacts' if directory == '/artifacts' else 'keys'
                    self.assertIn(f'{variable}=/work/host{directory}', commands)
                    self.assertIn('python3 /inputs/scripts/builder.py make -j1', commands)
                    self.assertEqual(commands[-1],
                        f'rsync -a --no-owner --no-group --delete /work/host{directory}/ {directory}/')

    def test_failed_build_still_exports_and_preserves_exit_status(self):
        result, commands = self.run_entrypoint(
            '/keys', denied='/keys', owner='0:0', builder_status=42)
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(commands[-1],
            'rsync -a --no-owner --no-group --delete /work/host/keys/ /keys/')

    def test_export_failure_reports_retained_files(self):
        result, commands = self.run_entrypoint(
            '/artifacts', owner='0:0', denied_export='/artifacts/')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('files remain in /work/host/artifacts', result.stderr)
        self.assertIn('python3 /inputs/scripts/builder.py make -j1', commands)

    def test_staging_lock_conflict_stops_build(self):
        result, commands = self.run_entrypoint('/artifacts', owner='0:0', lock_status=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Another routerfw container', result.stderr)
        self.assertFalse(any(command.startswith('python3 ') for command in commands))

    def test_staged_build_preserves_standard_input(self):
        result, commands = self.run_entrypoint(
            '/artifacts', owner='0:0', stdin='input probe\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('stdin=input probe', commands)


if __name__ == '__main__':
    unittest.main()
