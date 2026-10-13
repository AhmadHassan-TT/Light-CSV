#!/usr/bin/env python3
"""MCP stdio bridge: relays newline-delimited JSON-RPC between an MCP client and the running Light-CSV app.

Reads the app's url + token from the MCP config (mcp.json); starts the app with --mcp if it is not running.
stdout carries protocol messages only; logs go to stderr.
Env: CSV_VIEWER_CONFIG_DIR (config folder override), CSV_VIEWER_MCP_NO_LAUNCH=1 (never start the app)."""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from csvviewer.mcpserver import config, server  # noqa: E402

TIMEOUT = 600  # tool calls (searches, SQL) can take minutes
LAUNCH_WAIT = 20
USER_AGENT = 'Light-CSV-MCP-Bridge/1.0'
NOT_RUNNING = 'Light-CSV is not running and could not be started: open CSV Viewer (MCP server is on by default)'
APP_NAMES = ('CSV Viewer.exe', 'csv-viewer.exe', 'csv-viewer', 'CSV Viewer')

outLock = threading.Lock()
launchLock = threading.Lock()
launched = False


class NotRunning(Exception):
    pass


def log(text):
    print(f'csvview-mcp: {text}', file=sys.stderr, flush=True)


def write(message):
    """One JSON message per line on stdout."""
    data = (json.dumps(message, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    with outLock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def post(body, timeout=TIMEOUT):
    """POSTs raw JSON to the app; returns the parsed reply or None (202). Raises NotRunning if unreachable."""
    state = config.readState()
    url, token = state.get('url'), state.get('token')
    if not url or not token:
        raise NotRunning('no running server recorded')
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream', 'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if resp.status != 202 and raw.strip() else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotRunning(f'{url} is not the Light-CSV server')  # stale state: another program on that port
        raw = e.read()
        try:
            return json.loads(raw)
        except ValueError:
            return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32000, 'message': f'HTTP {e.code}: {e.reason}'}}
    except (urllib.error.URLError, ConnectionError) as e:
        raise NotRunning(str(getattr(e, 'reason', e)))


def appCommand():
    """Command that starts the viewer with its MCP server, or None if no launcher is found."""
    if getattr(sys, 'frozen', False):
        folder = os.path.dirname(sys.executable)
        for name in APP_NAMES:
            path = os.path.join(folder, name)
            if os.path.isfile(path) and os.path.realpath(path) != os.path.realpath(sys.executable):
                return [path, '--mcp']
        return None
    script = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'csvview')
    return [sys.executable, script, '--mcp'] if os.path.isfile(script) else None


def launchApp():
    """Starts the app detached from our stdio; True if a process was started."""
    cmd = appCommand()
    if not cmd:
        log('could not find the CSV Viewer launcher')
        return False
    kwargs = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL,
              'close_fds': True}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    try:
        subprocess.Popen(cmd, **kwargs)
    except OSError as e:
        log(f'could not start {cmd[0]}: {e}')
        return False
    log(f'started {" ".join(cmd)}')
    return True


def ensureRunning():
    """Launches the app once per bridge process and waits for its server to answer a ping."""
    global launched
    with launchLock:
        ping = json.dumps({'jsonrpc': '2.0', 'id': 'bridge-ping', 'method': 'ping'}).encode()
        try:
            post(ping, timeout=5)
            return True
        except NotRunning:
            pass
        if launched or os.environ.get('CSV_VIEWER_MCP_NO_LAUNCH') == '1':
            return False
        launched = True
        if not launchApp():
            return False
        deadline = time.monotonic() + LAUNCH_WAIT
        while time.monotonic() < deadline:
            time.sleep(0.25)
            try:
                post(ping, timeout=2)
                return True
            except NotRunning:
                pass
        log(f'the app did not answer within {LAUNCH_WAIT} s')
        return False


def errorReply(message, text):
    """-32000 errors for every request (not notification) in the message, or None."""
    items = message if isinstance(message, list) else [message]
    errors = [{'jsonrpc': '2.0', 'id': m['id'], 'error': {'code': -32000, 'message': text}}
              for m in items if isinstance(m, dict) and 'id' in m and 'method' in m]
    if not errors:
        return None
    return errors if isinstance(message, list) else errors[0]


def answerLocally(message):
    """Reply to handshake/listing messages without the app, so connecting never opens a window.
    Returns (handled, reply); the app is started only when a tool is actually called."""
    if not isinstance(message, dict):
        return False, None
    method, msgId = message.get('method', ''), message.get('id')
    if method.startswith('notifications/'):
        return True, None
    if method == 'initialize':
        params = message.get('params') or {}
        requested = params.get('protocolVersion')
        return True, {'jsonrpc': '2.0', 'id': msgId, 'result': {
            'protocolVersion': requested if requested in server.SUPPORTED_VERSIONS else server.LATEST_VERSION,
            'capabilities': {'tools': {'listChanged': False}}, 'serverInfo': server.SERVER_INFO,
            'instructions': server.INSTRUCTIONS}}
    if method == 'ping':
        return True, {'jsonrpc': '2.0', 'id': msgId, 'result': {}}
    if method == 'tools/list':
        tools = config.loadToolsCache()
        if tools:
            return True, {'jsonrpc': '2.0', 'id': msgId, 'result': {'tools': tools}}
    return False, None


def appRunning():
    try:
        post(json.dumps({'jsonrpc': '2.0', 'id': 'bridge-ping', 'method': 'ping'}).encode(), timeout=2)
        return True
    except NotRunning:
        return False


def relay(line):
    try:
        message = json.loads(line)
    except ValueError:
        write({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error: invalid JSON'}})
        return
    if not appRunning():
        handled, reply = answerLocally(message)
        if handled:
            if reply is not None:
                write(reply)
            return
    body = json.dumps(message).encode()
    try:
        try:
            reply = post(body)
        except NotRunning as e:
            log(f'app not reachable ({e})')
            reply = post(body) if ensureRunning() else errorReply(message, NOT_RUNNING)
    except NotRunning as e:
        log(f'app not reachable ({e})')
        reply = errorReply(message, NOT_RUNNING)
    except Exception as e:
        log(f'relay failed: {e}')
        reply = errorReply(message, f'Bridge error: {e}')
    if reply is not None:
        write(reply)


def main():
    workers = []
    for raw in sys.stdin.buffer:
        line = raw.decode('utf-8', errors='replace').strip()
        if not line:
            continue
        # One thread per message: a long tool call must not hold up pings or other calls
        worker = threading.Thread(target=relay, args=(line,), daemon=True)
        worker.start()
        workers.append(worker)
        workers = [w for w in workers if w.is_alive()]
    for worker in workers:
        worker.join()


if __name__ == '__main__':
    main()
