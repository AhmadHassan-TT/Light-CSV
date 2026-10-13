"""Claude loop (streaming + local tools) on a worker thread, and the controller wiring it to the panel."""
import json
import threading

from PyQt6.QtCore import QObject, QProcess, Qt, pyqtSignal
from PyQt6.QtWidgets import QMessageBox

from . import auth, history
from .providers import KEY_PROVIDERS
from .context import snapshotTabs

MODEL = 'claude-haiku-5-5'
MAX_TURNS = 25
SYSTEM = """You are the assistant inside Light-CSV, a desktop viewer for very large CSV files (up to tens of GB). \
The user asks questions about the files open in their tabs; answer from the data, using the tools.

How to work:
- The files are too big to read whole. Use describe_tab / get_rows to look at samples, search_rows to find text, \
and run_sql (DuckDB over the full file; tab N is the view tN) for anything that needs counts, sums, groupings, \
distinct values or exact answers. Never present a guess from a sample as a fact about the whole file - say when \
something is based on a sample.
- A full scan of a multi-GB file takes from seconds to a minute, so write one good aggregate query rather than many \
small ones, and quote column names with double quotes.
- Row numbers are 1-based data rows, as shown in the viewer. When you mention specific rows, link them as \
[row 123](csvrow://<tabId>/123) so the user can click to jump there. Use show_rows when the user asks to see or go to rows.
- If a tab is still indexing, say so and work with what is available or ask the user to wait.

How to answer: lead with the answer, keep it short, use a small markdown table when comparing numbers, \
and mention the SQL you ran only when it helps the user trust or reuse the result."""
TITLES = {'list_tabs': 'Listing tabs', 'describe_tab': 'Reading sample rows', 'get_rows': 'Reading rows',
          'search_rows': 'Searching', 'run_sql': 'Running SQL', 'show_rows': 'Showing rows'}


def contextBlock(tabs):
    """The open tabs, sent with each question (after the cached prefix)."""
    if not tabs:
        return '<open_tabs>none - no file is open</open_tabs>'
    lines = []
    for t in tabs:
        state = '' if t.indexed else ', still indexing'
        search = f', active search "{t.searchTerm}" ({t.searchHits:,} matching rows)' if t.searchTerm else ''
        lines.append(f'tab {t.tabId}: {t.name} - {t.index.dataRows:,} rows, {len(t.columns)} columns, '
                     f'SQL view t{t.tabId}{" (current tab)" if t.current else ""}{state}{search}')
    return '<open_tabs>\n' + '\n'.join(lines) + '\n</open_tabs>'


def summarize(result):
    """Short chip summary of a tool result."""
    try:
        data = json.loads(result)
    except (ValueError, TypeError):
        return 'done'
    if not isinstance(data, dict):
        return 'done'
    parts = []
    for key, label in (('totalMatches', 'matches'), ('rowCount', 'rows'), ('totalRows', 'rows')):
        if isinstance(data.get(key), int):
            parts.append(f'{data[key]:,} {label}')
            break
    if isinstance(data.get('seconds'), (int, float)):
        parts.append(f'{data["seconds"]:.1f} s')
    return ' · '.join(parts) or 'done'


class Worker(QObject):
    """One question: streams Claude's answer and runs the tools it asks for."""

    textDelta = pyqtSignal(str)
    thinking = pyqtSignal(bool)
    toolStarted = pyqtSignal(str, str, str)
    toolFinished = pyqtSignal(str, str, bool)
    uiAction = pyqtSignal(dict)
    failed = pyqtSignal(str)
    finished = pyqtSignal(bool, list)  # ok, the conversation including this question's turns

    def __init__(self, messages, tools):
        super().__init__()
        self.messages = messages
        self.tools = tools
        self.cancelled = False
        self.done = False
        self.doneLock = threading.Lock()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def finish(self, ok):
        # Emitted once: by Stop right away, or when the loop ends
        with self.doneLock:
            if self.done:
                return
            self.done = True
        self.thinking.emit(False)
        self.finished.emit(ok and not self.cancelled, self.messages)

    def cancel(self):
        self.cancelled = True
        self.tools.cancel()
        # A read blocked on the network can't always be interrupted, so the question ends now and the thread winds down alone
        self.finish(False)

    def run(self):
        import anthropic
        ok = False
        try:
            ok = self.loop(auth.makeClient())
        except anthropic.AuthenticationError:
            self.failed.emit('Claude rejected the sign-in. Sign in again (View ▸ Assistant ▸ Sign out, then sign in).')
        except anthropic.PermissionDeniedError as e:
            self.failed.emit(f'Your Claude account cannot use this model: {e.message}')
        except anthropic.RateLimitError:
            self.failed.emit('Rate limited by the Claude API. Wait a moment and ask again.')
        except anthropic.APIConnectionError:
            self.failed.emit('Could not reach the Claude API. Check your internet connection.')
        except anthropic.APIStatusError as e:
            self.failed.emit(f'Claude API error {e.status_code}: {e.message}')
        except TypeError as e:
            # The SDK raises TypeError when no credentials resolve
            self.failed.emit('Not signed in to Claude.' if 'authentication' in str(e) else f'Error: {e}')
        except Exception as e:
            self.failed.emit(f'Error: {e}')
        finally:
            self.tools.close()
            self.finish(ok)

    def loop(self, client):
        definitions = self.tools.definitions()
        for _ in range(MAX_TURNS):
            if self.cancelled:
                return False
            self.thinking.emit(True)
            with client.messages.stream(
                model=MODEL, max_tokens=16000, system=SYSTEM, tools=definitions, messages=self.messages,
                output_config={'effort': 'medium'},
                cache_control={'type': 'ephemeral'},  # system + tools + earlier turns are reused every call
            ) as stream:
                for event in stream:
                    if self.cancelled:
                        return False
                    if event.type == 'text':
                        self.thinking.emit(False)
                        self.textDelta.emit(event.text)
                    elif event.type == 'content_block_start' and event.content_block.type == 'tool_use':
                        self.thinking.emit(False)
                response = stream.get_final_message()
            self.thinking.emit(False)
            # Append the turn unchanged; the history is append-only
            self.messages.append({'role': 'assistant', 'content': response.content})
            if response.stop_reason == 'refusal':
                self.failed.emit('Claude declined to answer this request.')
                return False
            toolUses = [b for b in response.content if b.type == 'tool_use']
            if not toolUses:
                if response.stop_reason == 'max_tokens':
                    self.textDelta.emit('\n\n*(answer cut off at the length limit)*')
                return True
            if response.stop_reason == 'max_tokens':
                self.failed.emit('The answer hit the length limit before a tool call finished. Try a narrower question.')
                return False
            results = [self.runTool(block) for block in toolUses]
            # All results for one turn go back in a single message
            self.messages.append({'role': 'user', 'content': results})
        self.failed.emit('Stopped after too many steps. Try a more specific question.')
        return False

    def runTool(self, block):
        from .tools import ToolError
        args = block.input if isinstance(block.input, dict) else {}
        detail = args.get('query') or args.get('text') or json.dumps(args, ensure_ascii=False)
        title = TITLES.get(block.name, block.name)
        if block.name == 'search_rows' and args.get('text'):
            title = f'Searching "{args["text"]}"'
        self.toolStarted.emit(block.id, title, detail)
        try:
            result, action = self.tools.run(block.name, args)
        except ToolError as e:
            self.toolFinished.emit(block.id, str(e), False)
            return {'type': 'tool_result', 'tool_use_id': block.id, 'content': str(e), 'is_error': True}
        except Exception as e:
            self.toolFinished.emit(block.id, f'failed: {e}', False)
            return {'type': 'tool_result', 'tool_use_id': block.id, 'content': f'Tool failed: {e}', 'is_error': True}
        if action:
            self.uiAction.emit(action)
        self.toolFinished.emit(block.id, summarize(result), True)
        return {'type': 'tool_result', 'tool_use_id': block.id, 'content': result}


class Assistant(QObject):
    """Owns the Assistant dock: conversation state, sign-in, and the per-question worker."""

    antReady = pyqtSignal(str)  # path of the downloaded ant CLI, or "error:..."
    keyChecked = pyqtSignal(str, bool, str, str)  # free-key check: providerId, ok, message, key

    def __init__(self, window, dock):
        super().__init__(window)
        from .panel import AssistantPanel
        self.window = window
        self.messages = []
        self.worker = None
        self.tabWidgets = {}
        self.login = None
        self.provider = auth.settings().value('assistant/provider', 'claude')
        self.chat = None  # the saved chat being shown (history.newChat dict)
        self.panel = AssistantPanel()
        self.dock = dock  # the shared side panel; the main window puts self.panel in its Assistant tab
        self.panel.askRequested.connect(self.ask)
        self.panel.stopRequested.connect(self.stop)
        self.panel.newChatRequested.connect(self.newChat)
        self.panel.signInRequested.connect(self.signIn)
        self.panel.apiKeyEntered.connect(self.useKey)
        self.panel.rowLinkClicked.connect(self.showRow)
        self.antReady.connect(self.runLogin)
        self.panel.providerChanged.connect(self.setProvider)
        self.panel.providerKeyEntered.connect(self.useProviderKey)
        self.panel.setProviders([('claude', 'Claude')] + [(p.id, f'{p.name} (free)') for p in KEY_PROVIDERS.values()])
        self.panel.modelChosen.connect(self.chooseModel)
        self.keyChecked.connect(self.onKeyChecked)
        self.panel.chatOpened.connect(self.openChat)
        self.panel.chatDeleted.connect(self.deleteChat)
        self.panel.historyCleared.connect(self.clearHistory)
        self.panel.expandRequested.connect(self.toggleExpanded)
        self.dock.topLevelChanged.connect(self.panel.setExpanded)
        self.refreshHistory()
        self.panel.setProvider(self.provider)
        window.tabs.currentChanged.connect(self.updateContext)
        self.dock.visibilityChanged.connect(lambda shown: shown and (self.refreshAuth(), self.updateContext()))
        self.refreshAuth()

    def menuActions(self, menu):
        menu.addAction('Sign out of the assistant', self.signOut)

    # ---------- sign-in ----------

    def keyProvider(self):
        return KEY_PROVIDERS.get(self.provider)

    def refreshAuth(self):
        provider = self.keyProvider()
        if provider:
            self.panel.setKeyProvider({'id': provider.id, 'name': provider.name, 'signupText': provider.signupText,
                                       'keyUrl': provider.keyUrl, 'keyPlaceholder': provider.keyPlaceholder})
            signedIn, label = bool(provider.savedKey()), f'{provider.name} key (free)'
            self.panel.setModelChoices(provider.models, provider.savedModel())
        else:
            signedIn, label = auth.authState()
            self.panel.setModelChoices([], 'Haiku 5.5')
        self.panel.setAuthState(signedIn, label)
        return signedIn

    def setProvider(self, provider):
        if provider == self.provider:
            return
        # Histories are in each provider's own format, so switching starts a new chat
        self.newChat()
        self.provider = provider
        auth.settings().setValue('assistant/provider', provider)
        self.panel.setProvider(provider)
        self.refreshAuth()

    def useProviderKey(self, providerId, key):
        from . import gemini
        provider = KEY_PROVIDERS[providerId]
        self.panel.setAuthState(False, f'Checking the key with {provider.company}…')
        threading.Thread(target=lambda: self.keyChecked.emit(providerId, *gemini.checkKey(provider, key), key), daemon=True).start()

    def onKeyChecked(self, providerId, ok, message, key):
        if ok:
            KEY_PROVIDERS[providerId].saveKey(key)
            self.refreshAuth()
        elif providerId == self.provider:
            self.panel.setAuthState(False, message)

    def chooseModel(self, model):
        if self.keyProvider():
            self.keyProvider().saveModel(model)

    def useKey(self, key):
        if not key.strip().startswith('sk-'):
            self.panel.setAuthState(False, 'That does not look like an API key (they start with sk-).')
            return
        auth.saveKey(key)
        self.refreshAuth()

    def signIn(self):
        ant = auth.findAnt()
        if not ant:
            self.panel.setAuthState(False, 'Downloading the Claude sign-in tool…')

            def fetch():
                try:
                    self.antReady.emit(auth.downloadAnt())
                except Exception as e:
                    self.antReady.emit(f'error:{e}')
            threading.Thread(target=fetch, daemon=True).start()
            return
        self.runLogin(ant)

    def runLogin(self, ant):
        if ant.startswith('error:'):
            self.panel.setAuthState(False, f'Could not download the sign-in tool: {ant[6:]}. Paste an API key instead.')
            return
        self.panel.setAuthState(False, 'Finish signing in in your browser…')
        self.login = QProcess(self)
        self.login.finished.connect(lambda code, _status: self.loginDone(code))
        self.login.start(ant, ['auth', 'login'])

    def loginDone(self, code):
        output = bytes(self.login.readAllStandardError()).decode(errors='replace').strip()
        if not self.refreshAuth():
            self.panel.setAuthState(False, f'Sign-in did not finish{": " + output.splitlines()[-1] if output else "."}')

    def signOut(self):
        if self.keyProvider():
            self.keyProvider().forgetKey()
            self.refreshAuth()
            return
        auth.forgetKey()
        ant = auth.findAnt()
        if ant:
            QProcess.execute(ant, ['auth', 'logout'])
        self.refreshAuth()

    # ---------- questions ----------

    def updateContext(self, *_):
        tabs = snapshotTabs(self.window)
        current = next((t.name for t in tabs if t.current), None)
        text = f'{len(tabs)} tab{"s" if len(tabs) != 1 else ""} open' + (f' · current: {current}' if current else '')
        self.panel.setContextInfo(text if tabs else 'Open a CSV file to ask about it')

    def ask(self, question):
        from .tools import CsvTools
        if self.worker or not question.strip():
            return
        if not self.refreshAuth():
            return
        tabs = snapshotTabs(self.window)
        self.tabWidgets = {i + 1: self.window.tabs.widget(i) for i in range(self.window.tabs.count())}
        self.panel.addUserMessage(question)
        self.panel.startAssistantMessage()
        self.panel.setBusy(True)
        # The worker gets its own copy; the conversation only advances when a question completes
        content = f'{contextBlock(tabs)}\n\n{question}'
        provider = self.keyProvider()
        if self.chat is None:
            self.chat = history.newChat(self.provider, provider.savedModel() if provider else 'Haiku 5.5', question)
        self.record('user', question)
        self.record('start')
        if provider:
            from . import gemini
            earlier = self.messages or [{'role': 'system', 'content': SYSTEM}]
            self.worker = gemini.ChatWorker(provider, earlier + [{'role': 'user', 'content': content}], CsvTools(tabs),
                                            provider.savedModel(), provider.savedKey())
        else:
            self.worker = Worker(self.messages + [{'role': 'user', 'content': content}], CsvTools(tabs))
        self.worker.textDelta.connect(self.panel.appendText)
        self.worker.thinking.connect(self.panel.setThinking)
        self.worker.toolStarted.connect(self.panel.addToolChip)
        self.worker.toolFinished.connect(self.panel.finishToolChip)
        self.worker.uiAction.connect(self.applyAction)
        self.worker.failed.connect(self.panel.showError)
        self.worker.textDelta.connect(lambda t: self.record('text', t))
        self.worker.toolStarted.connect(lambda *a: self.record('chip', *a))
        self.worker.toolFinished.connect(lambda *a: self.record('chipDone', *a))
        self.worker.failed.connect(lambda t: self.record('error', t))
        self.worker.finished.connect(self.onFinished)
        self.worker.start()

    def onFinished(self, ok, messages):
        if ok:
            self.messages = messages
        worker, self.worker = self.worker, None
        if worker:
            # A stopped worker may still be winding down; nothing it sends later should reach the panel
            for signal in (worker.textDelta, worker.thinking, worker.toolStarted, worker.toolFinished, worker.uiAction, worker.failed, worker.finished):
                try:
                    signal.disconnect()
                except TypeError:
                    pass
        self.panel.endAssistantMessage()
        self.panel.setBusy(False)
        if self.chat is not None:
            self.record('end')
            self.chat['messages'] = self.messages
            try:
                history.save(self.chat)
            except OSError as e:
                self.panel.showError(f'Could not save this chat: {e}')
            self.refreshHistory()

    def stop(self):
        if self.worker:
            self.worker.cancel()

    def newChat(self):
        self.stop()
        self.messages = []
        self.chat = None
        self.panel.clear()
        self.panel.setCurrentChat('')
        self.updateContext()

    # ---------- history ----------

    def record(self, kind, *args):
        """Keep what the panel showed, so a saved chat can be replayed."""
        if self.chat is None:
            return
        display = self.chat['display']
        if kind == 'text' and display and display[-1][0] == 'text':
            display[-1][1] += args[0]  # one entry per streamed answer, not per token
        else:
            display.append([kind, *args])

    def refreshHistory(self):
        items = [{'id': c['id'], 'title': c['title'], 'when': history.whenText(c['updated']), 'provider': c['provider']}
                 for c in history.listChats()]
        self.panel.setHistory(items)
        self.panel.setCurrentChat(self.chat['id'] if self.chat else '')

    def openChat(self, chatId):
        try:
            chat = history.load(chatId)
        except (OSError, ValueError) as e:
            self.panel.showError(f'Could not open that chat: {e}')
            return
        self.stop()
        if chat.get('provider', 'claude') != self.provider:
            self.provider = chat.get('provider', 'claude')
            auth.settings().setValue('assistant/provider', self.provider)
            self.panel.setProvider(self.provider)
            self.refreshAuth()
        self.chat = chat
        self.messages = chat.get('messages', [])
        self.panel.clear()
        replay = {'user': self.panel.addUserMessage, 'start': self.panel.startAssistantMessage, 'text': self.panel.appendText,
                  'chip': self.panel.addToolChip, 'chipDone': self.panel.finishToolChip, 'error': self.panel.showError,
                  'end': self.panel.endAssistantMessage}
        for kind, *args in chat.get('display', []):
            if kind in replay:
                replay[kind](*args)
        self.panel.setCurrentChat(chatId)
        self.updateContext()

    def deleteChat(self, chatId):
        history.delete(chatId)
        if self.chat and self.chat['id'] == chatId:
            self.newChat()
        self.refreshHistory()

    def clearHistory(self):
        history.clear()
        self.newChat()
        self.refreshHistory()

    # ---------- larger window ----------

    def toggleExpanded(self):
        if self.dock.isFloating():
            self.dock.setFloating(False)
            return
        self.dock.setFloating(True)
        # A roomy window over the main one, for reading long reports
        area = self.window.screen().availableGeometry() if self.window.screen() else self.window.geometry()
        width, height = min(1100, int(area.width() * 0.7)), int(area.height() * 0.85)
        self.dock.setGeometry(area.x() + (area.width() - width) // 2, area.y() + (area.height() - height) // 2, width, height)

    # ---------- viewer actions ----------

    def applyAction(self, action):
        if action.get('type') == 'showRow':
            self.showRow(action['tabId'], action['row'], action.get('column'))

    def showRow(self, tabId, row, column=None):
        tab = self.tabWidgets.get(tabId)
        if tab is None or self.window.tabs.indexOf(tab) < 0:
            # Fall back to the tab currently at that position
            tab = self.window.tabs.widget(tabId - 1)
        if tab is None or not tab.index:
            return
        self.window.tabs.setCurrentWidget(tab)
        if tab.model.rowMap is not None:
            tab.onlyMatches.setChecked(False)
        col = tab.model.columns.index(column) if column in tab.model.columns else None
        tab.jumpTo(row - 1, col)

    def shutdown(self):
        if self.worker:
            self.worker.cancel()
            self.worker.thread.join(5)
