"""Drives the assistant's Claude loop against a scripted fake client (no network). Run: .venv/bin/python tests/test_agent.py"""
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QCoreApplication  # noqa: E402

from csvviewer.assistant import agent  # noqa: E402
from csvviewer.assistant.tools import ToolError  # noqa: E402


def textBlock(text):
    return NS(type='text', text=text)


def toolBlock(id, name, input):
    return NS(type='tool_use', id=id, name=name, input=input)


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        yield NS(type='content_block_start', content_block=NS(type='thinking'))
        for block in self.message.content:
            yield NS(type='content_block_start', content_block=NS(type=block.type))
            if block.type == 'text':
                for word in block.text.split(' '):
                    yield NS(type='text', text=word + ' ')

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append({**kwargs, 'messages': list(kwargs['messages'])})  # snapshot, like the SDK serializing the request
        return FakeStream(self.script.pop(0))


class FakeTools:
    def __init__(self):
        self.ran = []
        self.closed = False

    def definitions(self):
        return [{'name': 'run_sql', 'description': 'sql', 'input_schema': {'type': 'object', 'properties': {}}}]

    def run(self, name, args):
        self.ran.append((name, args))
        if args.get('query') == 'bad':
            raise ToolError('Parser Error: syntax error')
        if name == 'show_rows':
            return '{"ok": true}', {'type': 'showRow', 'tabId': 1, 'row': 5, 'column': None}
        return '{"rowCount": 3, "seconds": 1.25}', None

    def cancel(self):
        pass

    def close(self):
        self.closed = True


def run(script, question='How many rows per city?'):
    client = FakeClient(script)
    agent.auth.makeClient = lambda: client
    tools = FakeTools()
    worker = agent.Worker([{'role': 'user', 'content': question}], tools)
    events = {'text': [], 'chips': [], 'done': [], 'errors': [], 'actions': [], 'finished': []}
    worker.textDelta.connect(events['text'].append)
    worker.toolStarted.connect(lambda *a: events['chips'].append(a))
    worker.toolFinished.connect(lambda *a: events['done'].append(a))
    worker.failed.connect(events['errors'].append)
    worker.uiAction.connect(events['actions'].append)
    worker.finished.connect(lambda ok, msgs: events['finished'].append((ok, msgs)))
    worker.run()  # synchronously, on this thread
    QCoreApplication.processEvents()
    return client, tools, events


def main():
    QCoreApplication([])
    # 1. Tool call, an error result, a UI action, then a final answer
    script = [
        NS(stop_reason='tool_use', content=[textBlock('Let me check.'), toolBlock('t1', 'run_sql', {'query': 'SELECT 1'}),
                                            toolBlock('t2', 'run_sql', {'query': 'bad'})]),
        NS(stop_reason='tool_use', content=[toolBlock('t3', 'show_rows', {'tabId': 1, 'row': 5})]),
        NS(stop_reason='end_turn', content=[textBlock('Calgary has 3 rows.')]),
    ]
    client, tools, ev = run(script)
    assert len(client.calls) == 3, len(client.calls)
    call = client.calls[0]
    assert call['model'] == 'claude-haiku-5-5' and call['cache_control'] == {'type': 'ephemeral'}
    assert call['output_config'] == {'effort': 'medium'} and 'thinking' not in call
    ok, msgs = ev['finished'][0]
    assert ok and tools.closed
    # user, assistant(tools), user(2 results in ONE message), assistant, user(1 result), assistant
    roles = [m['role'] for m in msgs]
    assert roles == ['user', 'assistant', 'user', 'assistant', 'user', 'assistant'], roles
    results = msgs[2]['content']
    assert [r['tool_use_id'] for r in results] == ['t1', 't2'] and results[1]['is_error'] and 'is_error' not in results[0]
    assert ''.join(ev['text']).split() == 'Let me check. Calgary has 3 rows.'.split()
    assert [c[0] for c in ev['chips']] == ['t1', 't2', 't3'] and ev['chips'][0][1] == 'Running SQL'
    assert ev['done'][0] == ('t1', '3 rows · 1.2 s', True) and ev['done'][1][2] is False
    assert ev['actions'] == [{'type': 'showRow', 'tabId': 1, 'row': 5, 'column': None}]
    assert not ev['errors']
    # Each request carries the whole growing history (append-only)
    assert len(client.calls[2]['messages']) == 5

    # 2. Refusal ends the turn with an error and not-ok
    _, _, ev = run([NS(stop_reason='refusal', content=[])])
    assert ev['errors'] and not ev['finished'][0][0]

    # 3. Truncated tool call is not run
    _, tools, ev = run([NS(stop_reason='max_tokens', content=[toolBlock('t9', 'run_sql', {'query': 'SEL'})])])
    assert not tools.ran and ev['errors'] and not ev['finished'][0][0]

    # 4. API errors become friendly messages
    import anthropic
    import httpx2 as httpx
    def boom():
        raise anthropic.RateLimitError('slow down', response=httpx.Response(429, request=httpx.Request('POST', 'https://x')), body=None)
    agent.auth.makeClient = boom
    worker = agent.Worker([{'role': 'user', 'content': 'q'}], FakeTools())
    errors = []
    worker.failed.connect(errors.append)
    worker.run()
    assert errors and 'Rate limited' in errors[0], errors

    # 5. Context block lists tabs with their SQL views
    tab = NS(tabId=2, name='big.csv', index=NS(dataRows=1234), columns=['a', 'b'], indexed=False, current=True, searchTerm='x', searchHits=7)
    block = agent.contextBlock([tab])
    assert 'tab 2: big.csv - 1,234 rows, 2 columns, SQL view t2 (current tab), still indexing' in block and '(7 matching rows)' in block
    print('all agent tests passed')


if __name__ == '__main__':
    main()
