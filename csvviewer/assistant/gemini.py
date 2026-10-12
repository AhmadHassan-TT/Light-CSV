"""Free alternative to Claude: Google Gemini through its OpenAI-compatible API, streamed with urllib on a worker thread."""
import json
import socket
import threading
import urllib.error
import urllib.request

from PyQt6.QtCore import QObject, QSettings, pyqtSignal

from . import agent

BASE_URL = 'https://generativelanguage.googleapis.com/v1beta/openai/'
MODELS = [('gemini-3.8-flash', 'Gemini 3.8 Flash'),
          ('gemini-3.5-flash-lite', 'Gemini 3.5 Flash-Lite (fastest, highest free limits)'),
          ('gemini-2.5-flash', 'Gemini 2.5 Flash')]
DEFAULT_MODEL = 'gemini-3.8-flash'
KEY_URL = 'https://aistudio.google.com/apikey'
KEY_SETTING = 'assistant/geminiKey'
MODEL_SETTING = 'assistant/geminiModel'
TIMEOUT = 120


def settings():
    return QSettings('csv-viewer', 'csv-viewer')


def savedKey():
    return settings().value(KEY_SETTING, '') or ''


def saveKey(key):
    settings().setValue(KEY_SETTING, key.strip())


def forgetKey():
    settings().remove(KEY_SETTING)


def savedModel():
    return settings().value(MODEL_SETTING, '') or DEFAULT_MODEL


def saveModel(model):
    settings().setValue(MODEL_SETTING, model)


def request(path, key, body=None, timeout=TIMEOUT):
    """Open a GET (no body) or JSON POST to the Gemini endpoint; read BASE_URL at call time so tests can point it elsewhere."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data, headers={'Authorization': f'Bearer {key}'})
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    return urllib.request.urlopen(req, timeout=timeout)


def errorInfo(e):
    """(Google's error message, raw body) from an HTTPError; the body is a dict or a list of dicts."""
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
    return e.code in (401, 403) or (e.code == 400 and 'API_KEY_INVALID' in raw)


def checkKey(key):
    """(ok, message) after listing models with the key; blocking, call off the GUI thread."""
    try:
        with request('models', key.strip(), timeout=10) as resp:
            resp.read()
        return True, 'The Gemini API key works.'
    except urllib.error.HTTPError as e:
        message, raw = errorInfo(e)
        if keyRejected(e, raw):
            return False, f'Google rejected this API key. Copy it again from {KEY_URL}'
        if e.code == 429:
            return False, 'The key works, but the free Gemini limit is used up for now. Wait a minute.'
        return False, f'Google returned error {e.code}: {message}'
    except (urllib.error.URLError, OSError) as e:
        return False, f'Could not reach Google: {getattr(e, "reason", e)}. Check your internet connection.'


def cleanSchema(schema):
    """Copy of a JSON schema without additionalProperties, which Gemini's function schemas may reject."""
    if isinstance(schema, dict):
        return {k: cleanSchema(v) for k, v in schema.items() if k != 'additionalProperties'}
    if isinstance(schema, list):
        return [cleanSchema(v) for v in schema]
    return schema


def openaiTools(definitions):
    """Anthropic tool dicts -> OpenAI function tools (strict and cache fields dropped)."""
    return [{'type': 'function', 'function': {'name': d['name'], 'description': d.get('description', ''),
                                              'parameters': cleanSchema(d.get('input_schema') or {'type': 'object', 'properties': {}})}}
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


class GeminiWorker(QObject):
    """One question: streams Gemini's answer and runs the tools it asks for. Same signals as agent.Worker."""

    textDelta = pyqtSignal(str)
    thinking = pyqtSignal(bool)
    toolStarted = pyqtSignal(str, str, str)
    toolFinished = pyqtSignal(str, str, bool)
    uiAction = pyqtSignal(dict)
    failed = pyqtSignal(str)
    finished = pyqtSignal(bool, list)  # ok, the conversation including this question's turns

    def __init__(self, messages, tools, model, key):
        super().__init__()
        self.messages = messages
        self.tools = tools
        self.model = model or DEFAULT_MODEL
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
        resp = self.response
        if resp is not None:
            # Shut the socket down so a blocked read returns at once, then close
            try:
                resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                resp.close()
            except Exception:
                pass
        self.tools.cancel()
        # On Windows the blocked read may only return at its timeout, so the question ends now
        self.finish(False)

    def run(self):
        ok = False
        try:
            ok = self.loop()
        except urllib.error.HTTPError as e:
            if not self.cancelled:
                self.failed.emit(self.httpMessage(e))
        except (urllib.error.URLError, OSError) as e:
            if not self.cancelled:
                self.failed.emit(f'Could not reach Google: {getattr(e, "reason", e)}. Check your internet connection.')
        except Exception as e:
            if not self.cancelled:
                self.failed.emit(f'Error: {e}')
        finally:
            self.response = None
            self.tools.close()
            self.finish(ok)

    def httpMessage(self, e):
        message, raw = errorInfo(e)
        if keyRejected(e, raw):
            return f'Google rejected the Gemini API key. Check it, or get a new one at {KEY_URL}'
        if e.code == 429:
            return ('The free Gemini limit is used up for now (requests per minute or per day). Wait a minute, '
                    'or pick Gemini 3.5 Flash-Lite, which has higher free limits.')
        if e.code == 404:
            return f'This Gemini model is not available to your key: {message}'
        return f'Gemini API error {e.code}: {message}'

    def loop(self):
        tools = openaiTools(self.tools.definitions())
        for _ in range(agent.MAX_TURNS):
            if self.cancelled:
                return False
            self.thinking.emit(True)
            body = {'model': self.model, 'messages': self.messages, 'tools': tools, 'tool_choice': 'auto',
                    'stream': True, 'reasoning_effort': 'low'}
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
        self.response = request('chat/completions', self.key, body)
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
