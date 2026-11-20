#!/usr/bin/env python3
"""Update GitHub APK descriptors after checking dependencies against the build config."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
APK_NAME = r'[A-Za-z0-9][A-Za-z0-9._+~\-]*\.apk'
APK_IMAGE = 'alpine:3.23@sha256:469c9ea410b9215eaee7976ba6c0639039b444be8e5d638f7abbada189971eb2'


class GitHub:
    def __init__(self):
        self.cache = {}

    def get(self, path):
        if path not in self.cache:
            headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'routerfw-update'}
            token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
            if token:
                headers['Authorization'] = 'Bearer ' + token
            request = urllib.request.Request('https://api.github.com/repos/' + path,
                                             headers=headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                self.cache[path] = json.load(response)
        return self.cache[path]


def select_asset(name, assets):
    # Keep the package name, separator, architecture and variant; replace only the version.
    versioned = re.fullmatch(r'(.+?)([-_])([0-9][A-Za-z0-9.+~\-]*)(_.+)?\.apk', name)
    pattern = re.escape(name)
    if versioned:
        package, separator, _, suffix = versioned.groups()
        pattern = (re.escape(package + separator) + r'[0-9][A-Za-z0-9.+~\-]*'
                   + re.escape(suffix or '') + r'\.apk')
    matches = [asset for asset in assets if re.fullmatch(pattern, asset['name'])]
    if len(matches) != 1:
        raise RuntimeError(f'{name}: expected one matching APK, found {len(matches)}: '
                           + ', '.join(asset['name'] for asset in matches))
    asset = matches[0]
    if not re.fullmatch(APK_NAME, asset['name']):
        raise RuntimeError('Unsafe APK filename: ' + asset['name'])
    return asset


def resolve(url, github):
    parsed = urllib.parse.urlsplit(url)
    parts = [urllib.parse.unquote(part) for part in parsed.path.strip('/').split('/')]
    if parsed.scheme != 'https' or parsed.query or parsed.fragment:
        raise RuntimeError('Expected a plain GitHub HTTPS download URL: ' + url)
    if (parsed.netloc == 'github.com' and len(parts) == 6
            and parts[2:4] == ['releases', 'download']):
        owner, repo, _, _, tag, name = parts
        repository = f'{owner}/{repo}'
        # Named rolling releases such as continuous can be marked as prereleases.
        endpoint = 'latest' if re.search(r'\d', tag) else 'tags/' + urllib.parse.quote(tag, safe='')
        release = github.get(f'{repository}/releases/{endpoint}')
        asset = select_asset(name, release['assets'])
        return asset['name'], asset['browser_download_url']
    if parsed.netloc == 'raw.githubusercontent.com' and len(parts) >= 4:
        owner, repo, _, *filepath = parts
        repository = f'{owner}/{repo}'
        directory = '/'.join(filepath[:-1])
        # Resolve the most recent directory change on the default branch once, then pin it.
        query = urllib.parse.urlencode({'path': directory, 'per_page': 1})
        commits = github.get(f'{repository}/commits?{query}')
        if not commits:
            raise RuntimeError('No commits for APK directory: ' + url)
        commit = commits[0]['sha']
        path = urllib.parse.quote(directory, safe='/')
        entries = github.get(f'{repository}/contents/{path}?ref={commit}')
        asset = select_asset(filepath[-1], [entry for entry in entries if entry['type'] == 'file'])
        path = urllib.parse.quote('/'.join([*filepath[:-1], asset['name']]), safe='/')
        return asset['name'], f'https://raw.githubusercontent.com/{repository}/{commit}/{path}'
    raise RuntimeError('Unsupported APK source (use GitHub Releases or raw.githubusercontent.com): ' + url)


def apk(*args, stdin=None):
    command = [os.environ.get('ROUTERFW_APK') or shutil.which('apk') or '']
    if not command[0]:
        if not shutil.which('docker'):
            raise RuntimeError('Dependency checks require apk-tools 3 (ROUTERFW_APK) or Docker.')
        command = ['docker', 'run', '--rm', '-i', APK_IMAGE, 'apk']
    result = subprocess.run([*command, *args], stdin=stdin, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError('APK dependency inspection failed: ' + result.stderr.strip())
    return result.stdout


def package_info(package):
    package.seek(0)
    info = json.loads(apk('adbdump', '--format', 'json', '/dev/stdin', stdin=package))['info']
    if not isinstance(info, dict) or not isinstance(info.get('name'), str) or not info['name']:
        raise RuntimeError('APK metadata has no package name.')
    for field in ('depends', 'provides'):
        if not isinstance(info.get(field, []), list) or not all(
                isinstance(value, str) for value in info.get(field, [])):
            raise RuntimeError('Invalid APK metadata: ' + field)
    return info


def configured_packages(directory):
    if not directory.is_dir():
        raise RuntimeError('Configuration directory does not exist: ' + str(directory))
    settings = {}
    # Match the builder's fragment order, including explicit later overrides.
    for group in ('device', 'packages', 'custom'):
        for fragment in sorted((directory / group).glob('*.config')):
            for line in fragment.read_text().splitlines():
                match = re.fullmatch(r'CONFIG_PACKAGE_([A-Za-z0-9_+.-]+)=(.+)', line)
                if match:
                    settings[match[1]] = match[2]
                match = re.fullmatch(r'# CONFIG_PACKAGE_([A-Za-z0-9_+.-]+) is not set', line)
                if match:
                    settings[match[1]] = 'n'
    return {name for name, value in settings.items() if value in ('y', 'm')}


def repository_packages():
    lock = json.loads((ROOT / 'openwrt.lock.json').read_text())
    reference = lock['reference']
    with tempfile.TemporaryFile() as profile:
        if checksum(reference['url'], profile) != reference['sha256']:
            raise RuntimeError('Official profiles.json checksum mismatch.')
        profile.seek(0)
        metadata = json.load(profile)
    if metadata['version_number'] != lock['release'] or metadata['target'] != lock['target']:
        raise RuntimeError('Official package metadata does not match openwrt.lock.json.')
    release_url = reference['url'].split('/targets/', 1)[0]
    target_url = urllib.parse.urljoin(reference['url'], 'packages/packages.adb')
    feeds = ['base', *(feed['name'] for feed in lock['feeds'])]
    urls = [target_url, *(f"{release_url}/packages/{metadata['arch_packages']}/{feed}/packages.adb"
                          for feed in dict.fromkeys(feeds))]
    for url in urls:
        with tempfile.TemporaryFile() as index:
            try:
                checksum(url, index)
            except urllib.error.HTTPError as error:
                # Custom feeds in the lock may not have an official binary repository.
                if error.code == 404:
                    continue
                raise
            index.seek(0)
            yield from json.loads(apk('adbdump', '--format', 'json', '/dev/stdin',
                                      stdin=index))['packages']


def dependency_name(dependency):
    return re.split(r'[<>=~]', dependency, maxsplit=1)[0]


def check_dependencies(packages, configs):
    selected = configured_packages(configs)
    # The rootfs installer always requests these packages, independently of fragments.
    available = selected | {'base-files', 'libc', 'kernel'}
    for info in packages:
        available.add(info['name'])
        available.update(dependency_name(provided) for provided in info.get('provides', []))
    required = {dependency_name(dependency) for info in packages
                for dependency in info.get('depends', []) if not dependency.startswith('!')}
    if required - available and selected:
        # Only configured packages may supply virtual dependencies from release indexes.
        for info in repository_packages():
            provides = {dependency_name(provided) for provided in info.get('provides', [])}
            # ABI-versioned APKs provide both their Kconfig name and <name>-any.
            names = {info['name']}
            names.update(name for name in info.get('provides', [])
                         if name + '-any' in provides and info['name'].startswith(name))
            if names & selected:
                available.add(info['name'])
                available.update(provides)
            if required <= available:
                break
    missing = []
    for info in packages:
        for dependency in info.get('depends', []):
            if dependency.startswith('!'):
                continue
            name = dependency_name(dependency)
            if name not in available:
                missing.append(f"{info['name']}: {dependency}")
    if missing:
        raise RuntimeError('APK dependencies missing from the build configuration or local APKs:\n'
                           + '\n'.join(sorted(set(missing))))
    print('APK dependencies are present in the build configuration or local APKs.', flush=True)


def checksum(url, package=None):
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={'User-Agent': 'routerfw-update'})
    with urllib.request.urlopen(request, timeout=60) as response:
        for chunk in iter(lambda: response.read(1024 * 1024), b''):
            digest.update(chunk)
            if package is not None:
                package.write(chunk)
    return digest.hexdigest()


def update(directory, check=False, configs=None):
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError('APK directory must be a regular directory: ' + str(directory))
    descriptors = sorted(directory.glob('*.apk.txt'))
    github = GitHub()
    changes = []
    packages = []
    targets = set()
    for descriptor in descriptors:
        if descriptor.is_symlink() or not descriptor.is_file():
            raise RuntimeError('Expected a regular descriptor: ' + str(descriptor))
        content = descriptor.read_text(encoding='utf-8-sig')
        lines = content.splitlines()
        if len(lines) != 2 or not re.fullmatch(r'[A-Fa-f0-9]{64}|SKIP', lines[1].strip()):
            raise RuntimeError(f'{descriptor.name}: expected a URL and SHA-256 or SKIP on two lines.')
        url, old_digest = (line.strip() for line in lines)
        print('Checking ' + descriptor.name, flush=True)
        name, latest_url = resolve(url, github)
        target = directory / (name + '.txt')
        if target in targets or (target != descriptor and (target.exists() or target.is_symlink())):
            raise RuntimeError('Conflicting APK descriptor: ' + target.name)
        binary = directory / name
        if binary.exists() or binary.is_symlink():
            raise RuntimeError('Conflicting local APK: ' + name)
        targets.add(target)
        with tempfile.TemporaryFile() as package:
            digest = checksum(latest_url, package)
            packages.append(package_info(package))
        # Avoid changing commit pins when another file in the raw directory changes.
        if (urllib.parse.urlsplit(url).netloc == 'raw.githubusercontent.com'
                and target == descriptor and digest == old_digest.lower()):
            latest_url = url
        updated = f'{latest_url}\n{digest}\n'
        if target != descriptor or content != updated:
            changes.append((descriptor, target, updated))
            print(f'Update: {descriptor.name} -> {target.name} (SHA-256 {digest})', flush=True)

    for binary in sorted(directory.glob('*.apk')):
        if binary.is_symlink() or not binary.is_file():
            raise RuntimeError('Expected a regular APK: ' + str(binary))
        with binary.open('rb') as package:
            packages.append(package_info(package))
    if packages:
        check_dependencies(packages, configs if configs is not None else ROOT / 'configs')

    # Resolve, download and check every package before modifying any descriptor.
    if not check:
        for descriptor, target, content in changes:
            target.write_text(content, encoding='utf-8')
            if target != descriptor:
                descriptor.unlink()
    print(f'{len(changes)} descriptor(s) ' + ('would be updated.' if check else 'updated.'), flush=True)
    return bool(changes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true',
                        help='Do not write files; exit 1 if updates are available, 2 on error.')
    parser.add_argument('directory', nargs='?', type=Path, default=ROOT / 'apk',
                        help='APK descriptor directory (default: repository apk/).')
    parser.add_argument('--configs', type=Path,
                        help='Build configuration directory (default: repository configs/).')
    args = parser.parse_args()
    try:
        changed = update(args.directory, check=args.check, configs=args.configs)
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print('Error: ' + str(error), file=sys.stderr)
        return 2
    return 1 if args.check and changed else 0


if __name__ == '__main__':
    sys.exit(main())
