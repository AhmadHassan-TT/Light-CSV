"""Claude API sign-in: the user's own `ant auth login` profile, ANTHROPIC_API_KEY, or a pasted key."""
import hashlib
import io
import json
import os
import platform
import shutil
import sys
import tarfile
import urllib.request
import zipfile

from PyQt6.QtCore import QSettings, QStandardPaths

KEY_SETTING = 'assistant/apiKey'
RELEASES = 'https://api.github.com/repos/anthropics/anthropic-cli/releases/latest'


def settings():
    return QSettings('csv-viewer', 'csv-viewer')


def savedKey():
    return settings().value(KEY_SETTING, '') or ''


def saveKey(key):
    settings().setValue(KEY_SETTING, key.strip())


def forgetKey():
    settings().remove(KEY_SETTING)


def makeClient():
    """Anthropic client from a pasted key, else the SDK's own resolution (env key, then the ant profile)."""
    import anthropic
    key = savedKey()
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def authState():
    """(signedIn, label) without any network call."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, 'The anthropic package is not installed'
    if savedKey():
        return True, 'API key'
    if os.environ.get('ANTHROPIC_API_KEY'):
        return True, 'API key (environment)'
    try:
        client = makeClient()
    except Exception:
        return False, ''
    if client.api_key or client.auth_token or client.credentials:
        return True, 'Claude account'
    return False, ''


def antDir():
    return os.path.join(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation), 'bin')


def findAnt():
    exe = 'ant.exe' if sys.platform == 'win32' else 'ant'
    local = os.path.join(antDir(), exe)
    return shutil.which('ant') or (local if os.path.exists(local) else None)


def assetSuffix():
    system = {'win32': 'windows', 'darwin': 'darwin'}.get(sys.platform, 'linux')
    arch = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower(), 'amd64')
    return f'{system}_{arch}.' + ('zip' if system == 'windows' else 'tar.gz')


def downloadAnt():
    """Fetch the official ant CLI for this platform from GitHub, verify its checksum, return its path."""
    with urllib.request.urlopen(RELEASES, timeout=30) as r:
        release = json.load(r)
    assets = {a['name']: a['browser_download_url'] for a in release['assets']}
    name = next(n for n in assets if n.endswith(assetSuffix()) and n.startswith('ant_'))
    sums = next(u for n, u in assets.items() if n.endswith('checksums.txt'))
    with urllib.request.urlopen(sums, timeout=30) as r:
        expected = dict(line.split()[::-1] for line in r.read().decode().splitlines() if line.strip())[name]
    with urllib.request.urlopen(assets[name], timeout=120) as r:
        data = r.read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError('Checksum mismatch for the downloaded ant CLI')
    os.makedirs(antDir(), exist_ok=True)
    exe = 'ant.exe' if sys.platform == 'win32' else 'ant'
    target = os.path.join(antDir(), exe)
    if name.endswith('.zip'):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next(m for m in z.namelist() if os.path.basename(m) == exe)
            blob = z.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            member = next(m for m in t.getmembers() if os.path.basename(m.name) == exe)
            blob = t.extractfile(member).read()
    with open(target, 'wb') as f:
        f.write(blob)
    os.chmod(target, 0o755)
    return target
