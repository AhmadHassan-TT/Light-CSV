"""OpenAI-compatible providers (Gemini, Groq): chat streamed with urllib on a worker thread."""
import json
import socket
import threading
import urllib.error
import urllib.request

from PyQt6.QtCore import QObject, pyqtSignal

from . import agent

TIMEOUT = 120


def request(provider, path, key, body=None, timeout=TIMEOUT):
    """Open a GET (no body) or JSON POST to the provider; baseUrl is read at call time."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(provider.baseUrl + path, data=data, headers={'Authorization': f'Bearer {key}'})
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    return urllib.request.urlopen(req, timeout=timeout)


def errorInfo(e):
    """(provider's error message, raw body) from an HTTPError; the body is a dict or a list of dicts."""
    try:
        raw = e.read().decode(errors='replace')
    except Exception:
        raw = ''
    message = ''
    try:
        data = json.loads(raw)
        data = data[0] if isinstance(data, list) and data else data
        message = (data.get('error') or {}).get('message', '') if isinstance(data, dict) else ''
    except (ValueError, AttributeError):
        pass
    return message or raw.strip()[:300] or e.reason or '', raw


def keyRejected(e, raw):
    return e.code in (401, 403) or 'API_KEY_INVALID' in raw or 'invalid_api_key' in raw


def unreachable(provider, e):
    return f'Could not reach {provider.company}: {getattr(e, "reason", e)}. Check your internet connection.'


def httpMessage(provider, e):
    """User-facing message for an HTTP error from a chat request."""
    message, raw = errorInfo(e)
    if keyRejected(e, raw):
        return f'{provider.company} rejected the {provider.name} API key. Check it, or get a new one at {provider.keyUrl}'
    if e.code == 429:
        return f'The free {provider.name} limit is used up for now. {provider.limitHint}'.strip()
    if e.code == 404:
        return f'This {provider.name} model is not available to your key: {message}'
    if e.code == 400 and 'tool_use_failed' in raw:
        return 'The model produced an invalid tool call. Ask again, or try another model.'
    return f'{provider.name} API error {e.code}: {message}'


def checkKey(provider, key):
    """(ok, message) after listing models with the key; blocking, call off the GUI thread."""
    try:
        with request(provider, 'models', key.strip(), timeout=10) as resp:
            resp.read()
        return True, f'The {provider.name} API key works.'
    except urllib.error.HTTPError as e:
        message, raw = errorInfo(e)
        if keyRejected(e, raw):
            return False, f'{provider.company} rejected this API key. Copy it again from {provider.keyUrl}'
        if e.code == 429:
            return False, f'The key works, but the free {provider.name} limit is used up for now. {provider.limitHint}'.strip()
        return False, f'{provider.company} returned error {e.code}: {message}'
    except (urllib.error.URLError, OSError) as e:
        return False, unreachable(provider, e)


def cleanSchema(schema):
    """Copy of a JSON schema without additionalProperties, which some providers (Gemini) reject."""
    if isinstance(schema, dict):
        return {k: cleanSchema(v) for k, v in schema.items() if k != 'additionalProperties'}
    if isinstance(schema, list):
        return [cleanSchema(v) for v in schema]
    return schema


def openaiTools(definitions, provider):
    """Anthropic tool dicts -> OpenAI function tools (strict and cache fields dropped)."""
    def params(d):
        schema = d.get('input_schema') or {'type': 'object', 'properties': {}}
        return cleanSchema(schema) if provider.stripAdditionalProperties else schema
    return [{'type': 'function', 'function': {'name': d['name'], 'description': d.get('description', ''), 'parameters': params(d)}}
            for d in definitions]


def mergeCall(call, piece):
    """Fold one streamed tool-call piece into the call: arguments concatenate, other fields (thought signatures) are kept."""
    for k, v in piece.items():
        if k == 'index':
            continue
        if k == 'function':
            fn = call.setdefault('function', {})
            for fk, fv in (v or {}).items():
                if fk == 'arguments':
                    fn['arguments'] = fn.get('arguments', '') + (fv or '')
                elif fv is not None:
                    fn[fk] = fv
        elif v is not None and (k not in call or not call[k]):
            call[k] = v


class ChatWorker(QObject):
    """One question: streams the provider's answer and runs the tools it asks for. Same signals as agent.Worker."""

    textDelta = pyqtSignal(str)
    thinking = pyqtSignal(bool)
    toolStarted = pyqtSignal(str, str, str)
    toolFinished = pyqtSignal(str, str, bool)
    uiAction = pyqtSignal(dict)
    failed = pyqtSignal(str)
    finished = pyqtSignal(bool, list)  # ok, the conversation including this question's turns

    def __init__(self, provider, messages, tools, model, key):
        super().__init__()
        self.provider = provider
        self.messages = messages
        self.tools = tools
        self.model = model or provider.defaultModel
        self.key = key.strip()
        self.cancelled = False
        self.response = None
        self.done = False
        self.doneLock = threading.Lock()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    finish = agent.Worker.finish

    def cancel(self):
        self.cancelled = True
        self.tools.cancel()
        # End the question first: on Windows closing the connection waits on the blocked read's lock
        self.finish(False)
        resp = self.response
        if resp is not None:
            threading.Thread(target=self.closeResponse, args=(resp,), daemon=True).start()

    @staticmethod
    def closeResponse(resp):
        # Shut the socket down so a blocked read returns, then close
        try:
            resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            resp.close()
        except Exception:
            pass

    def run(self):
        ok = False
        try:
            ok = self.loop()
        except urllib.error.HTTPError as e:
            if not self.cancelled:
                self.failed.emit(httpMessage(self.provider, e))
        except (urllib.error.URLError, OSError) as e:
            if not self.cancelled:
                self.failed.emit(unreachable(self.provider, e))
        except Exception as e:
            if not self.cancelled:
                self.failed.emit(f'Error: {e}')
        finally:
            self.response = None
            self.tools.close()
            self.finish(ok)

    def loop(self):
        tools = openaiTools(self.tools.definitions(), self.provider)
        for _ in range(agent.MAX_TURNS):
            if self.cancelled:
                return False
            self.thinking.emit(True)
            body = {'model': self.model, 'messages': self.messages, 'tools': tools, 'tool_choice': 'auto',
                    'stream': True, **self.provider.extraBody(self.model)}
            text, calls, finish, extra = self.stream(body)
            if self.cancelled:
                return False
            self.thinking.emit(False)
            turn = {'role': 'assistant', 'content': text or None}
            if extra:
                turn['extra_content'] = extra
            if calls:
                turn['tool_calls'] = calls
            self.messages.append(turn)
            if not calls:
                if finish == 'length':
                    self.textDelta.emit('\n\n*(answer cut off at the length limit)*')
                return True
            for call in calls:
                self.messages.append(self.runTool(call))
                if self.cancelled:
                    return False
        self.failed.emit('Stopped after too many steps. Try a more specific question.')
        return False

    def stream(self, body):
        """One streamed request -> (text, tool calls in order, finish reason, message-level extra_content)."""
        text, calls, order, finish, extra = [], {}, [], None, None
        self.response = request(self.provider, 'chat/completions', self.key, body)
        try:
            for line in self.response:
                if self.cancelled:
                    break
                line = line.decode('utf-8', errors='replace').strip()
                if not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if data == '[DONE]':
                    break
                chunk = json.loads(data)
                if isinstance(chunk, dict) and chunk.get('error'):
                    err = chunk['error']
                    raise RuntimeError(err.get('message', err) if isinstance(err, dict) else err)
                for choice in chunk.get('choices') or []:
                    delta = choice.get('delta') or {}
                    if delta.get('content'):
                        self.thinking.emit(False)
                        text.append(delta['content'])
                        self.textDelta.emit(delta['content'])
                    if delta.get('extra_content'):
                        extra = delta['extra_content']
                    for piece in delta.get('tool_calls') or []:
                        self.thinking.emit(False)
                        # Pieces of one call share an index; a missing index with a new id starts a new call
                        index = piece.get('index')
                        if index is None:
                            index = len(order) if piece.get('id') or not order else order[-1]
                        if index not in calls:
                            calls[index] = {}
                            order.append(index)
                        mergeCall(calls[index], piece)
                    finish = choice.get('finish_reason') or finish
        finally:
            resp, self.response = self.response, None
            if resp is not None:
                resp.close()
        result = []
        for n, index in enumerate(order):
            call = calls[index]
            call.setdefault('id', f'call_{n}')
            call.setdefault('type', 'function')
            call.setdefault('function', {}).setdefault('arguments', '')
            result.append(call)
        return ''.join(text), result, finish, extra

    def runTool(self, call):
        from .tools import ToolError
        callId, fn = call['id'], call['function']
        name = fn.get('name', '')
        try:
            args = json.loads(fn.get('arguments') or '{}')
            if not isinstance(args, dict):
                raise ValueError('arguments are not a JSON object')
        except ValueError as e:
            title = agent.TITLES.get(name, name)
            self.toolStarted.emit(callId, title, fn.get('arguments') or '')
            self.toolFinished.emit(callId, 'invalid arguments', False)
            return {'role': 'tool', 'tool_call_id': callId, 'content': f'Error: the tool arguments are not valid JSON ({e}).'}
        detail = args.get('query') or args.get('text') or json.dumps(args, ensure_ascii=False)
        title = agent.TITLES.get(name, name)
        if name == 'search_rows' and args.get('text'):
            title = f'Searching "{args["text"]}"'
        self.toolStarted.emit(callId, title, detail)
        try:
            result, action = self.tools.run(name, args)
        except ToolError as e:
            self.toolFinished.emit(callId, str(e), False)
            return {'role': 'tool', 'tool_call_id': callId, 'content': f'Error: {e}'}
        except Exception as e:
            self.toolFinished.emit(callId, f'failed: {e}', False)
            return {'role': 'tool', 'tool_call_id': callId, 'content': f'Error: tool failed: {e}'}
        if action:
            self.uiAction.emit(action)
        self.toolFinished.emit(callId, agent.summarize(result), True)
        return {'role': 'tool', 'tool_call_id': callId, 'content': result}
