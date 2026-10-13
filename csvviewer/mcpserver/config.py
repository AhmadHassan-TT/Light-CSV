"""MCP config file (token + running server's url/port/pid); pure stdlib, shared by the app and the stdio bridge.

CSV_VIEWER_CONFIG_DIR overrides the folder (used by tests)."""
import json
import os
import secrets
import sys
import tempfile
import time

APP_DIR = 'csv-viewer'


def configDir():
    """Per-user config folder, created if missing."""
    path = os.environ.get('CSV_VIEWER_CONFIG_DIR')
    if not path:
        if sys.platform == 'win32':
            base = os.environ.get('APPDATA') or os.path.expanduser('~\\AppData\\Roaming')
        elif sys.platform == 'darwin':
            base = os.path.expanduser('~/Library/Application Support')
        else:
            base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
        path = os.path.join(base, APP_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def configPath():
    return os.path.join(configDir(), 'mcp.json')


def readState():
    """mcp.json as a dict, or {} if missing or unreadable."""
    try:
        with open(configPath(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data):
    """Atomic write (temp file + replace), owner-only on POSIX."""
    folder = configDir()
    fd, tmp = tempfile.mkstemp(prefix='.mcp-', suffix='.json', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        if os.name != 'nt':
            os.chmod(tmp, 0o600)
        os.replace(tmp, configPath())
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def loadToken():
    """Stable per-user bearer token, created on first use."""
    data = readState()
    token = data.get('token')
    if not isinstance(token, str) or len(token) < 16:
        token = secrets.token_urlsafe(32)
        data['token'] = token
        save(data)
    return token


def writeState(url, port, pid):
    """Records the running server so the bridge can find it."""
    data = readState()
    data['token'] = data.get('token') or loadToken()
    data.update(url=url, port=port, pid=pid, updated=time.time())
    save(data)


def clearState():
    """Forgets the running server (keeps the token); leaves another live instance's entry alone."""
    data = readState()
    if data.get('pid') not in (None, os.getpid()):
        return
    if any(k in data for k in ('url', 'port', 'pid')):
        for k in ('url', 'port', 'pid', 'updated'):
            data.pop(k, None)
        save(data)
