"""Drives the Gemini loop against a local fake OpenAI-compatible server (no network). Run: .venv/bin/python tests/test_gemini.py"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QCoreApplication  # noqa: E402

from csvviewer.assistant import gemini  # noqa: E402
from test_agent import FakeTools  # noqa: E402

# Each entry: ('sse', [chunk dicts]) | ('sse-hang', [chunk dicts]) | ('error', status, body) | ('json', body)
SCRIPT = []
REQUESTS = []
SENT = threading.Event()  # set once a hanging stream sent its chunks
RELEASE = threading.Event()  # lets a hanging handler exit at the end


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def handle_one(self, method):
        length = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        REQUESTS.append({'method': method, 'path': self.path, 'auth': self.headers.get('Authorization'), 'body': body})
        kind, *rest = SCRIPT.pop(0)
        if kind == 'error':
            status, payload = rest
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if kind == 'json':
            data = json.dumps(rest[0]).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for chunk in rest[0]:
            self.wfile.write(f'data: {json.dumps(chunk)}\n\n'.encode())
            self.wfile.flush()
        if kind == 'sse-hang':
            SENT.set()
            RELEASE.wait(30)
            return
        self.wfile.write(b'data: [DONE]\n\n')

    def do_GET(self):
        self.handle_one('GET')

    def do_POST(self):
        self.handle_one('POST')


def delta(content=None, toolCalls=None, finish=None):
    d = {}
    if content is not None:
        d['content'] = content
    if toolCalls is not None:
        d['tool_calls'] = toolCalls
    return {'choices': [{'index': 0, 'delta': d, 'finish_reason': finish}]}


def makeWorker(tools=None):
    tools = tools or FakeTools()
    msgs = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'How many rows?'}]
    worker = gemini.GeminiWorker(msgs, tools, 'gemini-3.8-flash', ' key123 ')
    ev = {'text': [], 'chips': [], 'done': [], 'errors': [], 'actions': [], 'finished': [], 'thinking': []}
    worker.textDelta.connect(ev['text'].append)
    worker.thinking.connect(ev['thinking'].append)
    worker.toolStarted.connect(lambda *a: ev['chips'].append(a))
    worker.toolFinished.connect(lambda *a: ev['done'].append(a))
    worker.failed.connect(ev['errors'].append)
    worker.uiAction.connect(ev['actions'].append)
    worker.finished.connect(lambda ok, msgs: ev['finished'].append((ok, msgs)))
    return worker, tools, ev


def runSync(script):
    SCRIPT[:] = script
    REQUESTS.clear()
    worker, tools, ev = makeWorker()
    worker.run()
    QCoreApplication.processEvents()
    return tools, ev


def main():
    app = QCoreApplication([])  # noqa: F841
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    gemini.BASE_URL = f'http://127.0.0.1:{server.server_port}/v1beta/openai/'

    # 0. Tool conversion drops strict/additionalProperties and keeps the schema
    conv = gemini.openaiTools([{'name': 'run_sql', 'description': 'd', 'strict': True, 'cache_control': {'type': 'ephemeral'},
                                'input_schema': {'type': 'object', 'properties': {'query': {'type': 'string'}},
                                                 'required': ['query'], 'additionalProperties': False}}])
    assert conv == [{'type': 'function', 'function': {'name': 'run_sql', 'description': 'd', 'parameters': {
        'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}}], conv

    # 1. Text + two tool calls (one split by index, one whole with a thought signature) + a bad-JSON call, then an answer
    sig = {'google': {'thought_signature': 'SIG=='}}
    turn1 = [
        delta('Let me '),
        delta('check.'),
        delta(toolCalls=[{'index': 0, 'id': 'c1', 'type': 'function', 'function': {'name': 'run_sql', 'arguments': '{"que'}}]),
        delta(toolCalls=[{'index': 0, 'function': {'arguments': 'ry": "SELECT 1"}'}}]),
        delta(toolCalls=[{'index': 1, 'id': 'c2', 'type': 'function', 'extra_content': sig,
                          'function': {'name': 'show_rows', 'arguments': '{"tabId": 1, "row": 5}'}}]),
        delta(toolCalls=[{'index': 2, 'id': 'c3', 'type': 'function', 'function': {'name': 'run_sql', 'arguments': '{oops'}}],
              finish='tool_calls'),
    ]
    turn2 = [delta('Calgary has '), delta('3 rows.', finish='stop')]
    tools, ev = runSync([('sse', turn1), ('sse', turn2)])
    assert not ev['errors'], ev['errors']
    ok, msgs = ev['finished'][0]
    assert ok and tools.closed
    assert tools.ran == [('run_sql', {'query': 'SELECT 1'}), ('show_rows', {'tabId': 1, 'row': 5})], tools.ran
    assert ''.join(ev['text']) == 'Let me check.Calgary has 3 rows.'
    assert [c[0] for c in ev['chips']] == ['c1', 'c2', 'c3'] and ev['chips'][0][1] == 'Running SQL'
    assert ev['done'][0] == ('c1', '3 rows · 1.2 s', True) and ev['done'][1][2] is True and ev['done'][2][2] is False
    assert ev['actions'] == [{'type': 'showRow', 'tabId': 1, 'row': 5, 'column': None}]
    assert ev['thinking'][0] is True and ev['thinking'][-1] is False
    roles = [m['role'] for m in msgs]
    assert roles == ['system', 'user', 'assistant', 'tool', 'tool', 'tool', 'assistant'], roles
    assert [m['tool_call_id'] for m in msgs[3:6]] == ['c1', 'c2', 'c3']
    assert msgs[5]['content'].startswith('Error:') and msgs[3]['content'] == '{"rowCount": 3, "seconds": 1.25}'
    assert msgs[6] == {'role': 'assistant', 'content': 'Calgary has 3 rows.'}
    # The first request: auth, model, stream, converted tools, reasoning effort
    first = REQUESTS[0]
    assert first['path'] == '/v1beta/openai/chat/completions' and first['auth'] == 'Bearer key123'
    body = first['body']
    assert body['model'] == 'gemini-3.8-flash' and body['stream'] is True and body['reasoning_effort'] == 'low'
    assert body['tool_choice'] == 'auto' and body['tools'][0]['type'] == 'function'
    assert body['tools'][0]['function']['name'] == 'run_sql' and 'strict' not in body['tools'][0]['function']
    # The second request echoes the assistant turn with the thought signature and the joined arguments
    sent = REQUESTS[1]['body']['messages']
    assistant = sent[2]
    assert assistant['content'] == 'Let me check.'
    calls = assistant['tool_calls']
    assert [c['id'] for c in calls] == ['c1', 'c2', 'c3'] and all('index' not in c for c in calls)
    assert calls[0]['function'] == {'name': 'run_sql', 'arguments': '{"query": "SELECT 1"}'}
    assert calls[1]['extra_content'] == sig, calls[1]
    assert [m['tool_call_id'] for m in sent[3:6]] == ['c1', 'c2', 'c3']

    # 2. 429 -> the free-limit message, not ok
    tools, ev = runSync([('error', 429, [{'error': {'code': 429, 'message': 'Resource exhausted', 'status': 'RESOURCE_EXHAUSTED'}}])])
    assert ev['errors'] and 'free Gemini limit' in ev['errors'][0] and 'Flash-Lite' in ev['errors'][0], ev['errors']
    assert ev['finished'][0][0] is False and tools.closed

    # 3. Invalid key (400 with API_KEY_INVALID) -> the key message
    bad = [{'error': {'code': 400, 'message': 'API key not valid. Please pass a valid API key.', 'status': 'INVALID_ARGUMENT',
                      'details': [{'reason': 'API_KEY_INVALID'}]}}]
    _, ev = runSync([('error', 400, bad)])
    assert ev['errors'] and ev['errors'][0].startswith('Google rejected the Gemini API key'), ev['errors']
    # 404 model and other errors carry Google's message
    _, ev = runSync([('error', 404, {'error': {'message': 'models/x is not found'}})])
    assert ev['errors'][0] == 'This Gemini model is not available to your key: models/x is not found', ev['errors']
    _, ev = runSync([('error', 500, {'error': {'message': 'Internal'}})])
    assert ev['errors'][0] == 'Gemini API error 500: Internal', ev['errors']

    # 4. Cancel mid-stream stops promptly
    SCRIPT[:] = [('sse-hang', [delta('partial ')])]
    REQUESTS.clear()
    worker, tools, ev = makeWorker()
    worker.start()
    assert SENT.wait(5)
    time.sleep(0.1)
    started = time.monotonic()
    worker.cancel()
    QCoreApplication.processEvents()
    elapsed = time.monotonic() - started
    # The question ends at once even where the blocked read can't be interrupted (Windows); the thread winds down alone
    assert ev['finished'] and ev['finished'][0][0] is False and elapsed < 2, (ev['finished'], elapsed)
    RELEASE.set()
    worker.thread.join(35)
    QCoreApplication.processEvents()
    assert len(ev['finished']) == 1 and not ev['errors'], ev
    QCoreApplication.processEvents()
    assert ev['finished'] and ev['finished'][0][0] is False and not ev['errors'] and tools.closed, ev
    RELEASE.set()

    # 5. checkKey ok / bad / unreachable
    SCRIPT[:] = [('json', {'data': [{'id': 'models/gemini-3.8-flash'}]}), ('error', 400, bad)]
    assert gemini.checkKey('good')[0] is True
    assert REQUESTS[-1]['path'] == '/v1beta/openai/models' and REQUESTS[-1]['auth'] == 'Bearer good'
    ok, message = gemini.checkKey('bad')
    assert not ok and 'rejected' in message, message
    gemini.BASE_URL = 'http://127.0.0.1:1/'
    ok, message = gemini.checkKey('x')
    assert not ok and message.startswith('Could not reach Google'), message
    server.shutdown()
    print('all gemini tests passed')


if __name__ == '__main__':
    main()
