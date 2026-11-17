#!/usr/bin/env python3
"""Update APK download descriptors using their existing GitHub source URLs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
APK_NAME = r'[A-Za-z0-9][A-Za-z0-9._+~\-]*\.apk'


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


def checksum(url):
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={'User-Agent': 'routerfw-update'})
    with urllib.request.urlopen(request, timeout=60) as response:
        for chunk in iter(lambda: response.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def update(directory, check=False):
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError('APK directory must be a regular directory: ' + str(directory))
    descriptors = sorted(directory.glob('*.apk.txt'))
    github = GitHub()
    changes = []
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
        digest = checksum(latest_url)
        # Avoid changing commit pins when another file in the raw directory changes.
        if (urllib.parse.urlsplit(url).netloc == 'raw.githubusercontent.com'
                and target == descriptor and digest == old_digest.lower()):
            latest_url = url
        updated = f'{latest_url}\n{digest}\n'
        if target != descriptor or content != updated:
            changes.append((descriptor, target, updated))
            print(f'Update: {descriptor.name} -> {target.name} (SHA-256 {digest})', flush=True)

    # Resolve and download every package before modifying any descriptor.
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
    args = parser.parse_args()
    try:
        changed = update(args.directory, check=args.check)
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print('Error: ' + str(error), file=sys.stderr)
        return 2
    return 1 if args.check and changed else 0


if __name__ == '__main__':
    sys.exit(main())
