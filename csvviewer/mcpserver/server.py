"""MCP Streamable HTTP transport (server side): stateless, JSON responses only, no SSE.

Unknown tool names come back as a tool result with isError true (not a -32602 protocol error),
so the model sees the message and can correct itself."""
import hmac
import json
import os
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

SUPPORTED_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
LATEST_VERSION = SUPPORTED_VERSIONS[0]
MAX_BODY = 2 * 1024 * 1024
PORT_TRIES = 21
LOCAL_HOSTS = ('localhost', '127.0.0.1')
SERVER_INFO = {'name': 'light-csv', 'title': 'Light-CSV', 'version': '1.0'}
INSTRUCTIONS = ('Controls the Light-CSV desktop CSV viewer that is open on this computer. '
                'Open files are tabs identified by a numeric tabId; row numbers are 1-based data rows '
                '(the header is not a row). Use run_sql for counts, sums, grouping and other aggregates '
                'instead of reading rows one by one.')

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603


def rpcError(msgId, code, message):
    return {'jsonrpc': '2.0', 'id': msgId, 'error': {'code': code, 'message': message}}


def rpcResult(msgId, result):
    return {'jsonrpc': '2.0', 'id': msgId, 'result': result}


def toolError(text):
    return {'content': [{'type': 'text', 'text': text}], 'isError': True}


def localOrigin(origin):
    """True for http://localhost[:port] or http://127.0.0.1[:port]; 'null' and everything else is rejected."""
    try:
        parts = urlsplit(origin)
        parts.port  # raises on a bad port
    except ValueError:
        return False
    return parts.scheme == 'http' and parts.hostname in LOCAL_HOSTS and not parts.path.strip('/') \
        and not parts.username and not parts.query


def localHost(host):
    """Host header must name the loopback address (DNS-rebinding guard); missing is allowed."""
    if not host:
        return True
    try:
        return urlsplit('//' + host).hostname in LOCAL_HOSTS
    except ValueError:
        return False


class Dispatcher:
    """JSON-RPC message handling, independent of HTTP."""

    def __init__(self, registry):
        self.registry = registry

    def handle(self, msg):
        """Response dict for a request, or None for a notification / client response."""
        if not isinstance(msg, dict):
            return rpcError(None, INVALID_REQUEST, 'Invalid Request: expected a JSON object')
        msgId = msg.get('id')
        isRequest = 'id' in msg
        method = msg.get('method')
        if 'method' not in msg and ('result' in msg or 'error' in msg):
            return None  # a response to a server request; we never send any
        validId = isinstance(msgId, (str, int)) and not isinstance(msgId, bool)
        if msg.get('jsonrpc') != '2.0' or not isinstance(method, str) or (isRequest and not validId):
            return rpcError(msgId if validId else None, INVALID_REQUEST, 'Invalid Request')
        if not isRequest:
            return None  # notifications/initialized, notifications/cancelled, ...
        params = msg.get('params')
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return rpcError(msgId, INVALID_PARAMS, 'params must be an object')
        try:
            if method == 'initialize':
                return rpcResult(msgId, self.initialize(params))
            if method == 'ping':
                return rpcResult(msgId, {})
            if method == 'tools/list':
                return rpcResult(msgId, {'tools': self.registry.definitions()})
            if method == 'tools/call':
                name = params.get('name')
                args = params.get('arguments')
                if not isinstance(name, str) or not (args is None or isinstance(args, dict)):
                    return rpcError(msgId, INVALID_PARAMS, 'tools/call needs a string name and object arguments')
                return rpcResult(msgId, self.callTool(name, args or {}))
        except Exception as e:
            traceback.print_exc()
            return rpcError(msgId, INTERNAL_ERROR, f'Internal error: {e}')
        return rpcError(msgId, METHOD_NOT_FOUND, f'Method not found: {method}')

    def initialize(self, params):
        requested = params.get('protocolVersion')
        return {'protocolVersion': requested if requested in SUPPORTED_VERSIONS else LATEST_VERSION,
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': SERVER_INFO,
                'instructions': INSTRUCTIONS}

    def callTool(self, name, args):
        # Tool failures are results the model can read, not protocol errors
        if name not in {d.get('name') for d in self.registry.definitions()}:
            return toolError(f'Unknown tool: {name}')
        try:
            result = self.registry.call(name, args)
        except Exception as e:
            return toolError(f'{type(e).__name__}: {e}' if str(e) else type(e).__name__)
        if not isinstance(result, dict) or 'content' not in result:
            return toolError(f'Tool {name} returned an invalid result')
        return result


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'Light-CSV-MCP/1.0'

    def log_message(self, format, *args):
        pass  # quiet: stdout may be someone's protocol channel

    def send(self, status, body=None, headers=()):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else b''
        self.send_response(status)
        if body is not None:
            self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.end_headers()
        if data:
            self.wfile.write(data)

    def checkRequest(self):
        """Path, Origin and token checks shared by every method; sends the error and returns False on failure."""
        mcp = self.server.mcp
        keepAlive = not self.close_connection
        self.close_connection = True  # until the checks pass: a rejected body is never read
        if urlsplit(self.path).path.rstrip('/') != mcp.path.rstrip('/'):
            self.send(404, rpcError(None, INVALID_REQUEST, 'Not found'))
            return False
        origin = self.headers.get('Origin')
        if (origin is not None and not localOrigin(origin)) or not localHost(self.headers.get('Host')):
            self.send(403, rpcError(None, INVALID_REQUEST, 'Forbidden origin'))
            return False
        auth = self.headers.get('Authorization') or ''
        given = auth[7:].strip() if auth[:7].lower() == 'bearer ' else ''
        if not hmac.compare_digest(given.encode(), mcp.token.encode()):
            self.send(401, rpcError(None, -32001, 'Unauthorized: missing or wrong bearer token'),
                      [('WWW-Authenticate', 'Bearer realm="light-csv"')])
            return False
        self.close_connection = not keepAlive
        return True

    def do_GET(self):
        # We never open an SSE stream
        if self.checkRequest():
            self.send(405, rpcError(None, INVALID_REQUEST, 'Method not allowed: use POST'), [('Allow', 'POST')])

    def do_DELETE(self):
        # Stateless: there is no session to end
        if self.checkRequest():
            self.send(405, rpcError(None, INVALID_REQUEST, 'Method not allowed: sessions are stateless'),
                      [('Allow', 'POST')])

    def do_POST(self):
        if not self.checkRequest():
            return
        accept = self.headers.get('Accept')
        if accept and not any(t in accept for t in ('application/json', 'text/event-stream', '*/*', 'application/*')):
            self.send(406, rpcError(None, INVALID_REQUEST, 'Not acceptable: responses are application/json'))
            return
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            self.close_connection = True
            self.send(411, rpcError(None, INVALID_REQUEST, 'Content-Length required'))
            return
        if length > MAX_BODY or length < 0:
            self.close_connection = True  # the body is left unread
            self.send(413, rpcError(None, INVALID_REQUEST, f'Request body over {MAX_BODY} bytes'))
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except ValueError:
            self.send(400, rpcError(None, PARSE_ERROR, 'Parse error: invalid JSON'))
            return
        dispatcher = self.server.mcp.dispatcher
        isBatch = isinstance(payload, list)
        if isBatch and not payload:
            self.send(400, rpcError(None, INVALID_REQUEST, 'Invalid Request: empty batch'))
            return
        messages = payload if isBatch else [payload]
        replies = [r for r in map(dispatcher.handle, messages) if r is not None]
        if not replies:
            self.send(202)  # notifications / responses only
            return
        headers = []
        if any(isinstance(m, dict) and m.get('method') == 'initialize' for m in messages):
            headers.append(('Mcp-Session-Id', str(uuid.uuid4())))
        version = self.headers.get('MCP-Protocol-Version')
        if version in SUPPORTED_VERSIONS:
            headers.append(('MCP-Protocol-Version', version))
        self.send(200, replies if isBatch else replies[0], headers)


class HttpServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second socket steal a busy port, so the port scan would never move on
    allow_reuse_address = os.name != 'nt'


class McpServer:
    """Localhost-only MCP endpoint; registry.call runs on the request's worker thread."""

    def __init__(self, registry, token, host='127.0.0.1', port=8765, path='/mcp'):
        self.registry = registry
        self.token = token
        self.host = host
        self.requestedPort = port
        self.port = None
        self.path = path if path.startswith('/') else '/' + path
        self.dispatcher = Dispatcher(registry)
        self.httpd = None
        self.thread = None

    @property
    def url(self):
        return f'http://{self.host}:{self.port}{self.path}'

    def start(self):
        """Binds the first free port from the requested one (+20) and serves on a daemon thread."""
        if self.httpd:
            return
        lastError = None
        for port in range(self.requestedPort, self.requestedPort + PORT_TRIES):
            try:
                self.httpd = HttpServer((self.host, port), Handler)
                break
            except OSError as e:
                lastError = e
        if not self.httpd:
            raise OSError(f'No free port in {self.requestedPort}-{self.requestedPort + PORT_TRIES - 1}: {lastError}')
        self.httpd.mcp = self
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name='mcp-http', daemon=True)
        self.thread.start()

    def stop(self):
        if not self.httpd:
            return
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.httpd = self.thread = None
