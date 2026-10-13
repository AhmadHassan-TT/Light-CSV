"""Exercises the MCP HTTP transport and the stdio bridge without Qt. Run: .venv/bin/python tests/test_mcp_server.py"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, repo)
tmpConfig = tempfile.mkdtemp(prefix='csvview-mcp-test-')
os.environ['CSV_VIEWER_CONFIG_DIR'] = tmpConfig  # before any config call
from csvviewer.mcpserver import config  # noqa: E402
from csvviewer.mcpserver.server import McpServer, MAX_BODY  # noqa: E402

TOKEN = 'test-token-' + 'x' * 20


class FakeRegistry:
    def __init__(self):
        self.gate = threading.Event()

    def definitions(self):
        return [{'name': name, 'description': name, 'inputSchema': {'type': 'object'}}
                for name in ('echo', 'boom', 'slow')]

    def call(self, name, arguments):
        if name == 'boom':
            raise ValueError('kaboom')
        if name == 'slow':
            self.gate.wait(10)
        return {'content': [{'type': 'text', 'text': json.dumps(arguments, ensure_ascii=False)}], 'isError': False}


def freePort():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def request(server, body, method='POST', token=TOKEN, headers=None, path=None):
    """(status, headers dict, parsed body or None)."""
    conn = http.client.HTTPConnection('127.0.0.1', server.port, timeout=15)
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    h = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
    if token:
        h['Authorization'] = f'Bearer {token}'
    h.update(headers or {})
    try:
        conn.request(method, path or server.path, body=data, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, json.loads(raw) if raw else None
    finally:
        conn.close()


def rpc(method, msgId=1, params=None):
    msg = {'jsonrpc': '2.0', 'id': msgId, 'method': method}
    if params is not None:
        msg['params'] = params
    return msg


def testServer(server, registry):
    status, headers, body = request(server, rpc('initialize', params={'protocolVersion': '2025-03-26'}))
    assert status == 200 and headers['content-type'] == 'application/json', (status, headers)
    assert body['result']['protocolVersion'] == '2025-03-26'
    assert body['result']['serverInfo']['name'] == 'light-csv'
    assert body['result']['capabilities'] == {'tools': {'listChanged': False}}
    assert 'tabId' in body['result']['instructions']
    assert len(headers.get('mcp-session-id', '')) == 36
    _, _, body = request(server, rpc('initialize', params={'protocolVersion': '1999-01-01'}))
    assert body['result']['protocolVersion'] == '2025-11-25'

    status, _, body = request(server, {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                              headers={'Mcp-Session-Id': 'whatever', 'MCP-Protocol-Version': '2025-11-25'})
    assert status == 202 and body is None

    status, _, body = request(server, rpc('ping', 'p'))
    assert status == 200 and body == {'jsonrpc': '2.0', 'id': 'p', 'result': {}}

    _, _, body = request(server, rpc('tools/list', 2))
    assert [t['name'] for t in body['result']['tools']] == ['echo', 'boom', 'slow']

    _, _, body = request(server, rpc('tools/call', 3, {'name': 'echo', 'arguments': {'a': 1}}))
    assert body['result'] == {'content': [{'type': 'text', 'text': '{"a": 1}'}], 'isError': False}
    _, _, body = request(server, rpc('tools/call', 4, {'name': 'boom', 'arguments': {}}))
    assert body['result']['isError'] is True and 'kaboom' in body['result']['content'][0]['text']
    _, _, body = request(server, rpc('tools/call', 5, {'name': 'nope'}))
    assert body['result']['isError'] is True and 'Unknown tool' in body['result']['content'][0]['text']
    _, _, body = request(server, rpc('tools/call', 6, {'arguments': {}}))
    assert body['error']['code'] == -32602

    _, _, body = request(server, rpc('resources/list', 7))
    assert body['error']['code'] == -32601 and body['id'] == 7
    status, _, body = request(server, b'{not json')
    assert status == 400 and body['error']['code'] == -32700
    _, _, body = request(server, {'id': 8, 'method': 'ping'})
    assert body['error']['code'] == -32600 and body['id'] == 8
    status, _, body = request(server, [])
    assert status == 400 and body['error']['code'] == -32600

    status, _, body = request(server, [rpc('ping', 10), {'jsonrpc': '2.0', 'method': 'notifications/x'},
                                       rpc('tools/list', 11), 'junk'])
    assert status == 200 and [r.get('id') for r in body] == [10, 11, None], body
    assert body[2]['error']['code'] == -32600
    status, _, body = request(server, [{'jsonrpc': '2.0', 'method': 'notifications/initialized'}])
    assert status == 202 and body is None

    for token in (None, 'wrong', TOKEN + 'x'):
        status, headers, body = request(server, rpc('ping'), token=token)
        assert status == 401 and body['error'] and 'www-authenticate' in headers, (token, status)
    status, _, _ = request(server, rpc('ping'), headers={'Authorization': f'bearer {TOKEN}'})
    assert status == 200

    for origin in ('http://evil.example', 'null', 'https://localhost', 'http://localhost.evil.com',
                   'http://127.0.0.1.nip.io:80'):
        status, _, _ = request(server, rpc('ping'), headers={'Origin': origin})
        assert status == 403, origin
    status, _, _ = request(server, rpc('ping'), headers={'Host': 'evil.example:8765'})
    assert status == 403
    for origin in ('http://localhost', 'http://localhost:5173', 'http://127.0.0.1:8765'):
        status, _, _ = request(server, rpc('ping'), headers={'Origin': origin})
        assert status == 200, origin

    status, headers, _ = request(server, None, method='GET')
    assert status == 405 and headers['allow'] == 'POST'
    status, _, _ = request(server, None, method='DELETE')
    assert status == 405
    status, _, _ = request(server, rpc('ping'), path='/other')
    assert status == 404
    status, _, _ = request(server, rpc('ping'), headers={'Accept': 'text/html'})
    assert status == 406
    conn = http.client.HTTPConnection('127.0.0.1', server.port, timeout=15)
    conn.request('POST', server.path, body=b'{}', headers={'Authorization': f'Bearer {TOKEN}'})
    assert conn.getresponse().status == 200  # Accept missing is fine
    conn.close()

    # Only announce an oversized body: the server answers 413 from Content-Length and closes, so sending 2 MB would race that close
    conn = http.client.HTTPConnection('127.0.0.1', server.port, timeout=15)
    conn.putrequest('POST', server.path)
    for k, v in (('Content-Type', 'application/json'), ('Authorization', f'Bearer {TOKEN}'), ('Content-Length', str(MAX_BODY + 1))):
        conn.putheader(k, v)
    conn.endheaders()
    assert conn.getresponse().status == 413
    conn.close()

    # A slow tool call on one connection must not block a ping on another
    slowResult = {}
    worker = threading.Thread(target=lambda: slowResult.update(
        r=request(server, rpc('tools/call', 20, {'name': 'slow', 'arguments': {}}))))
    worker.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    status, _, _ = request(server, rpc('ping', 21))
    assert status == 200 and time.monotonic() - t0 < 2 and worker.is_alive()
    registry.gate.set()
    worker.join(10)
    assert slowResult['r'][2]['result']['isError'] is False
    print('server tests passed')


def runBridge(lines, env=None, timeout=30):
    """Feeds lines to csvview_mcp.py; returns its stdout messages (one JSON per line) and stderr."""
    proc = subprocess.run([sys.executable, os.path.join(repo, 'csvview_mcp.py')],
                          input='\n'.join(json.dumps(m) for m in lines) + '\n', capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, **(env or {})})
    out = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(out) == len(proc.stdout.splitlines())
    return out, proc.stderr


def testBridge(server):
    config.writeState(server.url, server.port, os.getpid())
    state = config.readState()
    assert state['token'] == TOKEN and state['url'] == server.url and state['port'] == server.port
    out, err = runBridge([rpc('initialize', 1, {'protocolVersion': '2025-06-18', 'capabilities': {},
                                                'clientInfo': {'name': 't', 'version': '0'}}),
                          {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                          rpc('tools/list', 2),
                          rpc('tools/call', 3, {'name': 'echo', 'arguments': {'q': 'é'}})])
    byId = {m['id']: m for m in out}
    assert sorted(byId) == [1, 2, 3], (out, err)
    assert byId[1]['result']['protocolVersion'] == '2025-06-18'
    assert len(byId[2]['result']['tools']) == 3
    assert byId[3]['result']['content'][0]['text'] == '{"q": "é"}'

    # App not running: state points at a closed port and auto-launch is off
    deadPort = freePort()
    config.writeState(f'http://127.0.0.1:{deadPort}/mcp', deadPort, os.getpid())
    t0 = time.monotonic()
    out, err = runBridge([rpc('ping', 1), {'jsonrpc': '2.0', 'method': 'notifications/initialized'}],
                         env={'CSV_VIEWER_MCP_NO_LAUNCH': '1'})
    assert len(out) == 1 and out[0]['id'] == 1 and out[0]['error']['code'] == -32000, out
    assert 'not running' in out[0]['error']['message'] and time.monotonic() - t0 < 10
    config.clearState()
    assert set(config.readState()) == {'token'}
    if os.name != 'nt':
        assert os.stat(config.configPath()).st_mode & 0o777 == 0o600
    print('bridge tests passed')


def main():
    os.makedirs(tmpConfig, exist_ok=True)
    with open(config.configPath(), 'w') as f:
        json.dump({'token': TOKEN}, f)
    assert config.loadToken() == TOKEN
    registry = FakeRegistry()
    server = McpServer(registry, TOKEN, port=freePort())
    server.start()
    blocker = McpServer(registry, TOKEN, port=server.port)  # busy port: moves to the next one
    blocker.start()
    try:
        assert blocker.port != server.port and blocker.url.endswith(f':{blocker.port}/mcp')
        testServer(server, registry)
        testBridge(server)
    finally:
        blocker.stop()
        server.stop()
        shutil.rmtree(tmpConfig, ignore_errors=True)
    print('all mcp tests passed')


if __name__ == '__main__':
    main()
