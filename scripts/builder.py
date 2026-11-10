#!/usr/bin/env python3
"""Locked OpenWrt source build for Docker and native Linux CI."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

INPUT = Path(os.environ.get('ROUTERFW_INPUT_DIR', Path(__file__).resolve().parent.parent))
WORK = Path(os.environ.get('ROUTERFW_WORK_DIR', '/work'))
SOURCE = WORK / 'openwrt'
ARTIFACTS = Path(os.environ.get('ROUTERFW_ARTIFACTS_DIR', '/artifacts'))
KEYS = Path(os.environ.get('ROUTERFW_KEYS_DIR', '/keys'))
STAMP = SOURCE / '.routerfw-prepared.json'


def run(*args, cwd=SOURCE, capture=False):
    print('+', shlex.join(map(str, args)), flush=True)
    result = subprocess.run(list(map(str, args)), cwd=cwd, check=True,
                            text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fail(message):
    raise RuntimeError(message)


def load_lock():
    lock = json.loads((INPUT / 'openwrt.lock.json').read_text())
    if (lock['target'], lock['device']) != ('mediatek/filogic', 'glinet_gl-mt3600be'):
        fail('This environment is configured for mediatek/filogic GL-MT3600BE.')
    if lock['kernel']['vermagic_mode'] not in ('official', 'native'):
        fail('kernel.vermagic_mode must be official or native.')
    if not re.fullmatch('[0-9a-f]{32}', lock['kernel']['vermagic']):
        fail('Invalid vermagic: expected 32 lowercase hex digits.')
    names = set()
    for repo in [lock, *lock['feeds']]:
        if not re.fullmatch('[0-9a-f]{40}', repo['commit']):
            fail('Each repository requires a full 40-character commit, never a branch/tag.')
    for feed in lock['feeds']:
        if not re.fullmatch('[a-z][a-z0-9_]*', feed['name']) or feed['name'] in names:
            fail('Invalid or duplicate feed name: ' + feed['name'])
        names.add(feed['name'])
        # feeds.conf is parsed by upstream Perl and later used in shell commands.
        if not re.fullmatch(r'https://[A-Za-z0-9./_~:@%+\-]+', feed['url']):
            fail('Feed URLs must be plain HTTPS Git URLs without branch selectors.')
    return lock


def input_files(*roots):
    for root in roots:
        path = INPUT / root
        paths = [path] if path.is_file() else sorted(path.rglob('*'))
        for item in paths:
            if any(part in ('.git', '.gitkeep', '__pycache__') for part in item.parts):
                continue
            if item.is_symlink() or item.is_file():
                yield item


def inventory(*roots):
    result = {}
    for path in input_files(*roots):
        result[str(path.relative_to(INPUT))] = {
            'sha256': sha(os.readlink(path).encode() if path.is_symlink() else path.read_bytes()),
            'mode': oct(path.lstat().st_mode & 0o777),
            'symlink': path.is_symlink(),
        }
    return result


def source_inputs():
    return inventory('openwrt.lock.json', 'patches', 'packages', 'apk', 'files', 'uci-defaults')


def checkout(path, url, commit):
    path.mkdir(parents=True, exist_ok=True)
    run('git', 'init', '-q', path, cwd=WORK)
    run('git', 'remote', 'add', 'origin', url, cwd=path)
    run('git', 'fetch', '--depth=1', 'origin', commit, cwd=path)
    run('git', 'checkout', '--detach', 'FETCH_HEAD', cwd=path)


def apply_patches(directory, destination):
    for patch in sorted(directory.glob('*.patch')):
        # Fail on conflicts; never silently skip a patch.
        run('git', 'apply', '--check', patch, cwd=destination)
        run('git', 'apply', patch, cwd=destination)


def copy_inputs(name, destination):
    shutil.copytree(INPUT / name, destination, dirs_exist_ok=True, symlinks=True,
                    ignore=shutil.ignore_patterns('.git', '.gitkeep', '__pycache__'))


def install_uci_defaults(destination):
    destination.mkdir(parents=True, exist_ok=True)
    for script in input_files('uci-defaults'):
        if script.parent != INPUT / 'uci-defaults' or script.is_symlink():
            fail('uci-defaults/ must contain regular shell script files without subdirectories or symlinks.')
        target = destination / script.name
        if target.exists() or target.is_symlink():
            fail('Conflicting uci-defaults script in files/ overlay: ' + script.name)
        run('/bin/sh', '-n', script)
        shutil.copy2(script, target)
        target.chmod(0o755)


def install_apk_packages(destination):
    names = set()
    for package in input_files('apk'):
        if (package.parent != INPUT / 'apk' or package.is_symlink() or
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+~\-]*\.apk(?:\.txt)?', package.name)):
            fail('apk/ must contain regular .apk files or .apk.txt files directly, without subdirectories, symlinks or special characters in filenames.')
        name = package.stem if package.suffix == '.txt' else package.name
        if name in names:
            fail('Conflicting APK inputs for: ' + name)
        names.add(name)
        if package.suffix == '.txt':
            lines = package.read_text(encoding='utf-8-sig').splitlines()
            if (len(lines) != 2 or
                    not re.fullmatch(r'(?:[A-Fa-f0-9]{64}|SKIP)', lines[1].strip())):
                fail(f'{package.name}: expected a URL on the first line and SHA-256 or SKIP on the second line.')
            url, digest = (line.strip() for line in lines)
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme not in ('http', 'https') or not parsed.netloc or
                    any(char.isspace() for char in url)):
                fail(f'{package.name}: expected an HTTP(S) URL.')
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read()
            if digest != 'SKIP' and sha(data) != digest.lower():
                fail(f'{package.name}: APK SHA-256 checksum mismatch.')
            destination.mkdir(parents=True, exist_ok=True)
            (destination / name).write_bytes(data)
            continue
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(package, destination / package.name)


def repo_state(lock):
    states = {}
    for name, path, commit in [
        ('openwrt', SOURCE, lock['commit']),
        *((f['name'], SOURCE / 'feeds' / f['name'], f['commit']) for f in lock['feeds'])
    ]:
        head = run('git', 'rev-parse', 'HEAD', cwd=path, capture=True)
        if head != commit:
            fail(f'{name}: HEAD does not match openwrt.lock.json.')
        diff = subprocess.check_output(['git', 'diff', '--binary', 'HEAD'], cwd=path)
        states[name] = {'commit': head, 'diff_sha256': sha(diff)}
    return states


def check_reference(lock):
    reference = WORK / 'profiles.json'
    if not reference.exists() or sha(reference.read_bytes()) != lock['reference']['sha256']:
        with urllib.request.urlopen(lock['reference']['url'], timeout=60) as response:
            data = response.read()
        if sha(data) != lock['reference']['sha256']:
            fail('Official profiles.json checksum mismatch.')
        reference.write_bytes(data)
    metadata = json.loads(reference.read_bytes())
    expected = lock['kernel']
    if (metadata['version_number'] != lock['release'] or
            metadata['target'] != lock['target'] or
            lock['device'] not in metadata['profiles'] or
            metadata['linux_kernel']['version'] != expected['version'] or
            metadata['linux_kernel']['vermagic'] != expected['vermagic']):
        fail('Release, device or kernel metadata does not match official profiles.json.')


def prepare(lock):
    inputs = source_inputs()
    if STAMP.exists():
        stamp = json.loads(STAMP.read_text())
        if stamp['inputs'] != inputs:
            fail('Sources/feeds/patches/local packages changed. Run ./build.sh reset-sources, then rebuild.')
        if stamp['repositories'] != repo_state(lock):
            fail('Tracked sources were edited inside Docker. Save a patch, then run reset-sources.')
        return
    if any(SOURCE.iterdir()):
        fail('Incomplete source preparation. Run ./build.sh reset-sources and retry.')
    check_reference(lock)
    checkout(SOURCE, lock['repository'], lock['commit'])
    apply_patches(INPUT / 'patches/openwrt', SOURCE)
    # Verify the locked kernel version before allowing a forced ABI hash.
    series = '.'.join(lock['kernel']['version'].split('.')[:2])
    kernel_versions = (SOURCE / f'target/linux/generic/kernel-{series}').read_text()
    patchlevel = lock['kernel']['version'][len(series):]
    if f'LINUX_VERSION-{series} = {patchlevel}' not in kernel_versions:
        fail('Locked kernel version differs from the OpenWrt source.')
    kernel_dir = SOURCE / f'target/linux/mediatek/patches-{series}'
    kernel_dir.mkdir(exist_ok=True)
    for patch in sorted((INPUT / 'patches/kernel').glob('*.patch')):
        dest = kernel_dir / ('9999-routerfw-' + patch.name)
        if dest.exists():
            fail('Kernel patch already exists: ' + str(dest))
        shutil.copy2(patch, dest)
    feeds = lock['feeds']
    (SOURCE / 'feeds.conf').write_text(''.join(
        f"src-git {f['name']} {f['url']}^{f['commit']}\n" for f in feeds))
    for feed in feeds:
        destination = SOURCE / 'feeds' / feed['name']
        checkout(destination, feed['url'], feed['commit'])
        apply_patches(INPUT / 'patches/feeds' / feed['name'], destination)
    # Index the already checked out commits; do not resolve moving branch heads.
    run('./scripts/feeds', 'update', '-i')
    run('./scripts/feeds', 'install', '-a')
    copy_inputs('packages', SOURCE / 'package/local')
    install_apk_packages(SOURCE / 'apk')
    copy_inputs('files', SOURCE / 'files')
    install_uci_defaults(SOURCE / 'files/etc/uci-defaults')
    (SOURCE / 'dl').symlink_to(WORK / 'downloads', target_is_directory=True)
    (SOURCE / 'version').write_text(lock['revision'] + '\n')
    (SOURCE / 'version.date').write_text(str(lock['source_date_epoch']) + '\n')
    if lock['kernel']['vermagic_mode'] == 'official':
        (SOURCE / '.vermagic').write_text(lock['kernel']['vermagic'] + '\n')
    STAMP.write_text(json.dumps({'inputs': inputs, 'repositories': repo_state(lock)}, indent=2) + '\n')


def ensure_keys():
    private = KEYS / 'private-key.pem'
    public = KEYS / 'public-key.pem'
    if not private.exists():
        if public.exists():
            fail('public-key.pem exists without private-key.pem in .keys/.')
        run('openssl', 'ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', private)
        private.chmod(0o600)
    derived = subprocess.check_output(['openssl', 'ec', '-in', str(private), '-pubout'], stderr=subprocess.DEVNULL)
    if public.exists() and public.read_bytes() != derived:
        fail('The APK public/private keys in .keys/ do not match.')
    if not public.exists():
        public.write_bytes(derived)
    for name in ('private-key.pem', 'public-key.pem'):
        destination = SOURCE / name
        if not destination.exists():
            destination.symlink_to(KEYS / name)


def requested_config():
    settings = {}
    for name in ('device', 'packages', 'custom'):
        for line in (INPUT / f'configs/{name}.config').read_text().splitlines():
            if re.fullmatch(r'CONFIG_[A-Za-z0-9_\-]+=.+', line):
                key, value = line.split('=', 1)
                settings[key] = value
            elif re.fullmatch(r'# CONFIG_[A-Za-z0-9_\-]+ is not set', line):
                settings[line.split()[1]] = 'n'
    return settings


def configure(lock):
    settings = requested_config()
    device = 'CONFIG_TARGET_mediatek_filogic_DEVICE_' + lock['device']
    if settings.get(device) != 'y':
        fail('The GL-MT3600BE device selection must remain enabled.')
    (SOURCE / '.config').write_text(''.join(
        f'# {k} is not set\n' if v == 'n' else f'{k}={v}\n' for k, v in settings.items()))
    run('make', 'defconfig')
    resolved = (SOURCE / '.config').read_text().splitlines()
    mismatches = [f'{k}={v}' for k, v in settings.items()
                  if (f'# {k} is not set' if v == 'n' else f'{k}={v}') not in resolved
                  and not (v == 'n' and not any(l.startswith(k + '=') for l in resolved))]
    if mismatches:
        fail('Kconfig dropped/changed requested options (missing package or dependency?):\n' + '\n'.join(mismatches))
    if list(input_files('apk')) and 'CONFIG_USE_APK=y' not in resolved:
        fail('Prebuilt packages in apk/ require CONFIG_USE_APK=y.')
    # Source patches may change kernel selection independently of the lock.
    kernel = run('make', '-s', '--no-print-directory', '-C', 'target/linux',
                 f'TOPDIR={SOURCE}', 'val.LINUX_VERSION', capture=True)
    if kernel != lock['kernel']['version']:
        fail('Resolved LINUX_VERSION differs from the locked kernel.')


def export_artifacts(lock):
    target = SOURCE / 'bin/targets' / lock['target']
    # OpenWrt's filename prefix may omit the release version.
    images = target.glob(f'*-{lock["device"]}-squashfs-sysupgrade.bin')
    if not any(image.is_file() for image in images):
        fail('Build succeeded but the expected GL-MT3600BE sysupgrade image is missing.')
    hashes = list((SOURCE / 'build_dir').glob('target-*/linux-mediatek_filogic/linux-*/.vermagic'))
    if len(hashes) != 1:
        fail('Cannot uniquely locate the compiled kernel .vermagic.')
    actual = hashes[0].read_text().strip()
    if lock['kernel']['vermagic_mode'] == 'official' and actual != lock['kernel']['vermagic']:
        fail('Compiled kernel .vermagic differs from the lock.')
    destination = ARTIFACTS / lock['release']
    temporary = ARTIFACTS / ('.' + lock['release'] + '.tmp')
    if temporary.exists():
        shutil.rmtree(temporary)
    # Materialize package symlinks so exports also work on Windows bind mounts.
    shutil.copytree(SOURCE / 'bin', temporary / 'bin', symlinks=False)
    if (SOURCE / 'apk').is_dir():
        shutil.copytree(SOURCE / 'apk', temporary / 'apk')
    shutil.copy2(SOURCE / '.config', temporary / 'config.full')
    (temporary / 'config.seed').write_text(run('./scripts/diffconfig.sh', capture=True) + '\n')
    shutil.copy2(INPUT / 'openwrt.lock.json', temporary)
    shutil.copy2(WORK / 'profiles.json', temporary / 'reference-profiles.json')
    shutil.copy2(KEYS / 'public-key.pem', temporary)
    dpkg = run('dpkg-query', '-W', '-f=${Package}=${Version}\n', capture=True)
    (temporary / 'builder-packages.txt').write_text(dpkg + '\n')
    manifest = {
        'lock': lock, 'vermagic': actual, 'repositories': repo_state(lock),
        'inputs': inventory('openwrt.lock.json', 'configs', 'patches', 'packages', 'apk', 'files', 'uci-defaults', 'docker', 'scripts', 'compose.yaml', 'build.sh', 'build.ps1', '.gitattributes'),
        'config_sha256': sha((SOURCE / '.config').read_bytes()),
        'public_key_sha256': sha((KEYS / 'public-key.pem').read_bytes()),
        'builder_platform': 'linux/' + {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(os.uname().machine, os.uname().machine),
        'builder_os': Path('/etc/os-release').read_text(),
        'github_sha': os.environ.get('GITHUB_SHA'),
        'runner_image_version': os.environ.get('ImageVersion'),
    }
    (temporary / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    files = sorted(p for p in temporary.rglob('*') if p.is_file())
    (temporary / 'SHA256SUMS').write_text(''.join(
        f'{sha(p.read_bytes())}  {p.relative_to(temporary)}\n' for p in files))
    if destination.exists():
        shutil.rmtree(destination)
    temporary.rename(destination)
    print(f'Firmware and manifest: artifacts/{lock["release"]}/', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='build', choices=(
        'prepare', 'configure', 'menuconfig', 'download', 'build', 'make', 'shell', 'reset-sources'))
    parser.add_argument('make_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command != 'make' and args.make_args:
        parser.error('Extra arguments are only supported by the make command.')
    for directory in (WORK, SOURCE, WORK / 'downloads', WORK / 'ccache', ARTIFACTS, KEYS):
        directory.mkdir(parents=True, exist_ok=True)
    # Prevent two containers mutating the same named work volume.
    with (WORK / '.routerfw.lock').open('w') as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail('Another routerfw container is using this work volume.')
        if args.command == 'reset-sources':
            # Preserve downloads, ccache and host keys. Never erase a host checkout.
            os.chdir(WORK)
            shutil.rmtree(SOURCE)
            SOURCE.mkdir()
            print('Source/build tree removed; download cache, ccache, keys and artifacts preserved.')
            return
        lock = load_lock()
        os.environ.update(SOURCE_DATE_EPOCH=str(lock['source_date_epoch']),
                          KBUILD_BUILD_TIMESTAMP='@' + str(lock['source_date_epoch']),
                          KBUILD_BUILD_USER='builder', KBUILD_BUILD_HOST='openwrt')
        if args.command == 'shell' and STAMP.exists():
            run('/bin/bash')
            return
        prepare(lock)
        ensure_keys()
        if args.command == 'prepare':
            return
        if args.command == 'shell':
            run('/bin/bash')
            return
        configure(lock)
        jobs = int(os.environ.get('JOBS', '4'))
        if jobs < 1:
            fail('JOBS must be a positive integer.')
        if args.command == 'configure':
            return
        if args.command == 'menuconfig':
            run('make', 'menuconfig')
            (ARTIFACTS / 'menuconfig.config').write_text(run('./scripts/diffconfig.sh', capture=True) + '\n')
            print('Save on host: cp artifacts/menuconfig.config configs/custom.config')
        elif args.command == 'make':
            run('make', *args.make_args)
        else:
            run('make', f'-j{jobs}', 'download', 'V=s')
            if args.command == 'build':
                run('make', f'-j{jobs}', 'V=s')
                export_artifacts(lock)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
