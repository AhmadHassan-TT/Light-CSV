"""MCP tool registry: full control of the viewer (except the assistant) for external apps; call() works from any thread."""
import codecs
import json
import os
import threading
import time
from array import array

from PyQt6.QtCore import QCoreApplication, QObject, Qt, QThread, pyqtSignal

from csvviewer.app import DELIMITERS, THEMES, samePath
from csvviewer.assistant.context import snapshotTabs
from csvviewer.assistant.tools import DEFINITIONS as CSV_DEFINITIONS, CsvTools, ToolError
from csvviewer.index import CsvIndex, Export

GUI_TIMEOUT = 30  # seconds a GUI-thread step may take
SLOW_GUI_TIMEOUT = 120  # opening / closing joins background threads
WAIT_TIMEOUT = 600  # waiting for indexing or a search
POLL = 0.1
MAX_VALUE = 500
MAX_SELECTION = 2000
FIRST_MATCHES = 20
DELEGATED = ('list_tabs', 'describe_tab', 'get_rows', 'search_rows', 'run_sql')
DELIMITER_NAMES = {'comma': ',', 'semicolon': ';', 'tab': '\t', 'pipe': '|', '\\t': '\t'}


class McpError(Exception):
    pass


def clip(value):
    return value[:MAX_VALUE] + '…' if isinstance(value, str) and len(value) > MAX_VALUE else value


def dump(obj):
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(',', ':'))


# ---------- schema ----------

TAB_ID = {'type': 'integer', 'minimum': 1, 'description': '1-based tab position, as in get_app_state / list_tabs.'}
ROW = {'type': 'integer', 'minimum': 1, 'description': '1-based data row number, as shown in the row header.'}
COLUMN = {'type': 'string', 'description': 'Column name (case-insensitive fallback).'}
OPTION_PROPS = {
    'delimiter': {'type': 'string', 'description': "One character (',', ';', '|', or a tab) or comma/semicolon/tab/pipe; omit to auto-detect."},
    'encoding': {'type': 'string', 'description': 'Text encoding such as utf-8, cp1252 or latin-1; omit to auto-detect.'},
    'hasHeader': {'type': 'boolean', 'description': 'First row is the header (default true).'},
    'quoteAware': {'type': 'boolean', 'description': 'Line breaks inside quoted values stay in the cell (default true).'},
}


def tool(name, title, description, props=None, required=(), readOnly=False, destructive=False, idempotent=False):
    return {'name': name, 'title': title, 'description': description,
            'inputSchema': {'type': 'object', 'properties': props or {}, 'required': list(required), 'additionalProperties': False},
            'annotations': {'readOnlyHint': readOnly, 'destructiveHint': destructive, 'idempotentHint': idempotent,
                            'openWorldHint': False}}


def delegated(name, title, extra=''):
    # Same schema and semantics as the assistant's data tool
    d = next(d for d in CSV_DEFINITIONS if d['name'] == name)
    schema = json.loads(json.dumps(d['input_schema']))
    for p in schema['properties'].values():
        if p.get('description', '').startswith('Tab number from list_tabs'):
            p.update(TAB_ID)
    return tool(name, title, d['description'] + extra, schema['properties'], schema['required'], readOnly=True, idempotent=True)


DEFINITIONS = [
    tool('get_app_state', 'Get viewer state',
         'Window title, theme, every open tab (tabId, file, size, rows, column count, indexing progress, frozen and hidden '
         'columns, active search) and the current cell. Start here.', readOnly=True, idempotent=True),
    tool('open_file', 'Open a CSV file',
         'Open a file in a new tab (or focus it if already open; options are then ignored) and return the tab. '
         'Indexing runs in the background; set waitForIndex to wait until the whole file is indexed (up to 10 min).',
         dict({'path': {'type': 'string', 'description': 'Absolute path of the file.'}}, **OPTION_PROPS,
              waitForIndex={'type': 'boolean', 'description': 'Wait until indexing finishes (default false).'}), ['path']),
    tool('close_tab', 'Close a tab', 'Close a tab; later tabs shift down by one tabId.', {'tabId': TAB_ID}, ['tabId'], destructive=True),
    tool('switch_tab', 'Switch tab', 'Make a tab the one shown.', {'tabId': TAB_ID}, ['tabId'], idempotent=True),
    tool('reopen_tab', 'Reopen tab with options',
         'Reopen a tab\'s file with other parse options (omitted ones keep their current value). Resets search, '
         'freeze and selection; re-indexes in the background.', dict({'tabId': TAB_ID}, **OPTION_PROPS), ['tabId']),
    delegated('list_tabs', 'List tabs', ' Each tab is also the SQL view t{tabId} for run_sql.'),
    delegated('describe_tab', 'Describe tab'),
    delegated('get_rows', 'Read rows'),
    delegated('search_rows', 'Search rows (no UI change)',
              ' Does not touch the tab\'s search bar; use set_search to drive the viewer\'s own search.'),
    delegated('run_sql', 'Run SQL'),
    tool('go_to_row', 'Go to row',
         'Show a tab, select a data row (and column) and scroll to it. Turns off "Only matches" first if it is on.',
         {'tabId': TAB_ID, 'row': ROW, 'column': COLUMN}, ['tabId', 'row'], idempotent=True),
    tool('set_search', 'Search in the viewer',
         "Run the tab's own search bar: substring match over the whole file or one column, highlighting matches. "
         'Needs an indexed tab (waits for indexing when wait is true). With wait (default true) returns the match '
         'count and the first 20 matching row numbers; otherwise returns at once while the search runs.',
         {'tabId': TAB_ID, 'text': {'type': 'string', 'minLength': 1, 'description': 'Text to find.'}, 'column': COLUMN,
          'caseSensitive': {'type': 'boolean', 'description': 'Default false.'},
          'onlyMatches': {'type': 'boolean', 'description': 'Show only matching rows; omit to keep the current setting.'},
          'wait': {'type': 'boolean', 'description': 'Wait for the search to finish (default true, up to 10 min).'}},
         ['tabId', 'text']),
    tool('clear_search', 'Clear search', "Stop and clear the tab's search and turn off \"Only matches\".",
         {'tabId': TAB_ID}, ['tabId'], idempotent=True),
    tool('set_frozen_columns', 'Freeze columns', 'Pin the first N columns on the left (0 unfreezes).',
         {'tabId': TAB_ID, 'count': {'type': 'integer', 'minimum': 0, 'description': 'Number of columns to pin.'}},
         ['tabId', 'count'], idempotent=True),
    tool('set_column_visibility', 'Show or hide columns', 'Hide or show the named columns of a tab.',
         {'tabId': TAB_ID, 'columns': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'description': 'Column names.'},
          'visible': {'type': 'boolean', 'description': 'true shows, false hides.'}},
         ['tabId', 'columns', 'visible'], idempotent=True),
    tool('show_all_columns', 'Show all columns', 'Unhide every column of a tab.', {'tabId': TAB_ID}, ['tabId'], idempotent=True),
    tool('export_matches', 'Export matching rows',
         "Write the header plus every row matching the tab's finished search (set_search) to a new CSV file, byte for "
         'byte. Overwrites an existing file at path, but never the file of an open tab. Returns the rows written.',
         {'tabId': TAB_ID, 'path': {'type': 'string', 'description': 'Absolute path of the file to write.'}},
         ['tabId', 'path'], idempotent=True),
    tool('get_selection', 'Get selection',
         'The current cell and the selected cells of a tab (default the shown tab) as rows, at most 2000 cells.',
         {'tabId': TAB_ID}, [], readOnly=True, idempotent=True),
    tool('set_theme', 'Set theme', 'Switch the colour theme.',
         {'theme': {'type': 'string', 'enum': list(THEMES), 'description': 'system, light or dark.'}}, ['theme'], idempotent=True),
]


def checkArgs(schema, args):
    props = schema['properties']
    bad = sorted(set(args) - set(props))
    missing = sorted(set(schema['required']) - set(args))
    if bad or missing:
        raise McpError(f'bad input: unknown {bad}, missing {missing}')
    for key, value in args.items():
        p = props[key]
        kind = p.get('type')
        ok = {'integer': lambda v: isinstance(v, int) and not isinstance(v, bool), 'boolean': lambda v: isinstance(v, bool),
              'string': lambda v: isinstance(v, str),
              'array': lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v)}.get(kind, lambda v: True)(value)
        if not ok:
            raise McpError(f'{key} must be {"an" if kind in ("integer", "array") else "a"} {kind}')
        if 'enum' in p and value not in p['enum']:
            raise McpError(f'{key} must be one of {p["enum"]}')
        if 'minimum' in p and value < p['minimum']:
            raise McpError(f'{key} must be at least {p["minimum"]}')
        if kind in ('string', 'array') and len(value) < p.get('minLength', p.get('minItems', 0)):
            raise McpError(f'{key} must not be empty')


class Job:
    def __init__(self, fn):
        self.fn, self.result, self.error = fn, None, None
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.state = 'pending'


class McpTools(QObject):
    """Tool definitions plus call(); GUI work is queued to the GUI thread, waiting happens on the caller's thread."""

    invoke = pyqtSignal(object)

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.invoke.connect(self.runJob, Qt.ConnectionType.QueuedConnection)

    def definitions(self):
        return DEFINITIONS

    # ---------- threading ----------

    def onGuiThread(self):
        return QThread.currentThread() == self.thread()

    def gui(self, fn, timeout=GUI_TIMEOUT):
        """Run fn on the GUI thread and return its result (directly when already there)."""
        if self.onGuiThread():
            return fn()
        job = Job(fn)
        self.invoke.emit(job)
        if not job.event.wait(timeout):
            with job.lock:
                if job.state == 'pending':
                    job.state = 'abandoned'
            if job.state == 'abandoned':
                raise McpError(f'the viewer did not respond within {timeout} s (busy or showing a dialog)')
            job.event.wait()
        if job.error:
            raise job.error
        return job.result

    def runJob(self, job):
        with job.lock:
            if job.state == 'abandoned':
                return
            job.state = 'running'
        try:
            job.result = job.fn()
        except Exception as e:
            job.error = e
        job.event.set()

    def sleep(self):
        # A call made on the GUI thread keeps the UI alive while waiting
        if self.onGuiThread():
            QCoreApplication.processEvents()
        time.sleep(POLL)

    def waitFor(self, done, timeout=WAIT_TIMEOUT):
        end = time.monotonic() + timeout
        while not done():
            if time.monotonic() > end:
                return False
            self.sleep()
        return True

    # ---------- entry ----------

    def call(self, name, arguments):
        try:
            d = next((d for d in DEFINITIONS if d['name'] == name), None)
            if d is None:
                raise McpError(f'unknown tool {name!r}')
            args = arguments or {}
            if not isinstance(args, dict):
                raise McpError('arguments must be an object')
            checkArgs(d['inputSchema'], args)
            if name in DELEGATED:
                data = self.delegate(name, args)
            else:
                data = getattr(self, 'tool_' + name)(**args)
            return {'content': [{'type': 'text', 'text': dump(data)}], 'structuredContent': data, 'isError': False}
        except (McpError, ToolError) as e:
            return self.error(str(e))
        except Exception as e:
            return self.error(f'{type(e).__name__}: {e}')

    def error(self, msg):
        return {'content': [{'type': 'text', 'text': msg}], 'structuredContent': {'error': msg}, 'isError': True}

    def delegate(self, name, args):
        # The assistant's data tools on a fresh snapshot; they read the index off the GUI thread
        tools = CsvTools(self.gui(lambda: snapshotTabs(self.window)))
        try:
            text, _ = tools.run(name, args)
        finally:
            tools.close()
        data = json.loads(text)
        return {'tabs': data} if isinstance(data, list) else data

    # ---------- GUI-thread helpers ----------

    def tabAt(self, tabId, needIndex=True):
        tabs = self.window.tabs
        if not 1 <= tabId <= tabs.count():
            raise McpError(f'no tab {tabId}; open tabs: ' + (f'1-{tabs.count()}' if tabs.count() else 'none'))
        tab = tabs.widget(tabId - 1)
        if needIndex and not tab.index:
            raise McpError(f'tab {tabId} has no file open')
        return tab

    def ready(self, tab, tabId):
        # Columns are set up by the periodic tick; run it now so a fresh tab answers at once
        tab.tick()
        if not tab.columnsReady:
            raise McpError(f'tab {tabId} is still reading its first rows, try again in a moment')

    def columnIndex(self, tab, name):
        cols = tab.model.columns
        if name in cols:
            return cols.index(name)
        low = [c.strip().lower() for c in cols]
        if name.strip().lower() in low:
            return low.index(name.strip().lower())
        raise McpError(f'no column {name!r}; columns: {cols[:50]}')

    def searchInfo(self, tab):
        if not tab.lastQuery:
            return None
        term, cs, col = tab.lastQuery
        s = tab.search
        return {'text': term, 'column': tab.model.columns[col] if col is not None and col < len(tab.model.columns) else None,
                'caseSensitive': cs, 'onlyMatches': tab.onlyMatches.isChecked(),
                'matches': len(s.hits) if s else 0, 'done': bool(s and s.done)}

    def tabInfo(self, i):
        tab = self.window.tabs.widget(i)
        idx = tab.index
        if not idx:
            return {'tabId': i + 1, 'name': self.window.tabs.tabText(i), 'path': None, 'empty': True}
        tab.tick()
        cols = tab.model.columns
        return {'tabId': i + 1, 'name': os.path.basename(idx.path), 'path': idx.path, 'sizeBytes': idx.size,
                'rows': idx.dataRows, 'columns': len(cols), 'indexed': idx.done, 'progress': round(idx.progress, 4),
                'current': tab is self.window.current(), 'delimiter': idx.delimiter, 'encoding': idx.encoding,
                'hasHeader': idx.hasHeader, 'quoteAware': idx.quoteAware, 'frozenColumns': tab.table.frozenCount,
                'hiddenColumns': [c for n, c in enumerate(cols) if tab.table.isColumnHidden(n)],
                'activeSearch': self.searchInfo(tab)}

    def tabIdOf(self, tab):
        i = self.window.tabs.indexOf(tab)
        if i < 0:
            raise McpError('the tab was closed')
        return i + 1

    def currentCell(self, tab):
        cur = tab.table.currentIndex()
        if not tab.index or not cur.isValid() or cur.row() >= tab.model.rowCount():
            return None
        col = cur.column()
        return {'tabId': self.tabIdOf(tab), 'row': tab.model.dataRow(cur.row()) + 1,
                'column': tab.model.columns[col] if col < len(tab.model.columns) else None,
                'value': clip(tab.model.cell(cur.row(), col))}

    # ---------- options ----------

    def options(self, delimiter=None, encoding=None, hasHeader=None, quoteAware=None, base=None):
        """CsvIndex keyword options; omitted values come from base (a CsvIndex) or the defaults."""
        if delimiter is not None:
            if delimiter.lower() in ('auto', ''):
                delimiter = None
            else:
                delimiter = DELIMITER_NAMES.get(delimiter.lower(), DELIMITERS.get(delimiter, delimiter))
                if len(delimiter) != 1 or delimiter in '"\r\n':
                    raise McpError('delimiter must be one character (not a quote or line break) or comma/semicolon/tab/pipe/auto')
        elif base:
            delimiter = base.delimiter
        if encoding is not None:
            if encoding.lower() in ('auto', ''):
                encoding = None
            else:
                try:
                    codecs.lookup(encoding)
                except LookupError:
                    raise McpError(f'unknown encoding {encoding!r}')
        elif base:
            encoding = base.encoding
        return dict(delimiter=delimiter, encoding=encoding,
                    hasHeader=hasHeader if hasHeader is not None else (base.hasHeader if base else True),
                    quoteAware=quoteAware if quoteAware is not None else (base.quoteAware if base else True))

    def probe(self, path, opts):
        # Opening in the viewer shows a modal error box on failure, so try it here first
        if not os.path.isfile(path):
            raise McpError(f'not a file: {path}' if os.path.exists(path) else f'file not found: {path}')
        try:
            CsvIndex(path, **opts).close()
        except (OSError, ValueError) as e:
            raise McpError(f'cannot open {path}: {e}')

    def waitIndexed(self, index, tabId):
        if not self.waitFor(lambda: index.done or index.cancelled):
            raise McpError(f'tab {tabId} is still indexing after {WAIT_TIMEOUT} s ({index.progress:.0%})')
        if index.cancelled and not index.done:
            raise McpError(f'tab {tabId} was closed or reopened while indexing')

    # ---------- tools ----------

    def tool_get_app_state(self):
        def state():
            w = self.window
            tab = w.current()
            return {'windowTitle': w.windowTitle(), 'theme': w.settings.value('view/theme', 'system'),
                    'tabs': [self.tabInfo(i) for i in range(w.tabs.count())],
                    'currentTabId': w.tabs.currentIndex() + 1 if w.tabs.count() else None,
                    'currentCell': self.currentCell(tab) if tab else None}
        return self.gui(state)

    def tool_open_file(self, path, waitForIndex=False, **options):
        path = os.path.abspath(os.path.expanduser(path))
        opts = self.options(**options)
        self.probe(path, opts)

        def openTab():
            w = self.window
            for i in range(w.tabs.count()):
                tab = w.tabs.widget(i)
                if tab.index and samePath(tab.index.path, path):
                    w.tabs.setCurrentIndex(i)
                    return tab, True
            before = w.tabs.count()
            w.openPath(path, **opts)
            if w.tabs.count() == before:
                raise McpError(f'could not open {path}')
            return w.tabs.widget(w.tabs.count() - 1), False
        tab, already = self.gui(openTab, SLOW_GUI_TIMEOUT)
        index = tab.index
        if waitForIndex:
            self.waitIndexed(index, self.gui(lambda: self.tabIdOf(tab)))
        info = self.gui(lambda: self.tabInfo(self.tabIdOf(tab) - 1))
        info['alreadyOpen'] = already
        return info

    def tool_close_tab(self, tabId):
        def close():
            tab = self.tabAt(tabId, needIndex=False)
            name = os.path.basename(tab.index.path) if tab.index else ''
            self.window.closeTab(tabId - 1)
            return {'closed': tabId, 'name': name, 'remainingTabs': self.window.tabs.count()}
        return self.gui(close, SLOW_GUI_TIMEOUT)

    def tool_switch_tab(self, tabId):
        def switch():
            self.tabAt(tabId, needIndex=False)
            self.window.tabs.setCurrentIndex(tabId - 1)
            return self.tabInfo(tabId - 1)
        return self.gui(switch)

    def tool_reopen_tab(self, tabId, **options):
        def take():
            tab = self.tabAt(tabId)
            return tab, tab.index
        tab, index = self.gui(take)
        opts = self.options(base=index, **options)
        self.probe(index.path, opts)

        def reopen():
            if tab.index is not index:
                raise McpError(f'tab {tabId} changed meanwhile, try again')
            if not tab.openFile(index.path, **opts):
                raise McpError(f'could not reopen {index.path}')
            return self.tabInfo(self.tabIdOf(tab) - 1)
        return self.gui(reopen, SLOW_GUI_TIMEOUT)

    def tool_go_to_row(self, tabId, row, column=None):
        def go():
            tab = self.tabAt(tabId)
            self.ready(tab, tabId)
            rows = tab.index.dataRows
            if row > rows:
                more = '' if tab.index.done else ' so far (still indexing)'
                raise McpError(f'row {row} is out of range; tab {tabId} has rows 1-{rows}{more}')
            col = self.columnIndex(tab, column) if column else None
            if tab.onlyMatches.isChecked():
                tab.onlyMatches.setChecked(False)
            self.window.tabs.setCurrentIndex(tabId - 1)
            tab.model.sync()
            tab.jumpToDataRow(row - 1, col)
            return {'tabId': tabId, 'current': self.currentCell(tab)}
        return self.gui(go)

    def tool_set_search(self, tabId, text, column=None, caseSensitive=False, onlyMatches=None, wait=True):
        index = self.gui(lambda: self.tabAt(tabId).index)
        if not index.done:
            if not wait:
                raise McpError(f'tab {tabId} is still indexing ({index.progress:.0%}); search needs the whole file, '
                               'retry later or pass wait=true')
            self.waitIndexed(index, tabId)

        def start():
            tab = self.tabAt(tabId)
            if tab.index is not index:
                raise McpError(f'tab {tabId} changed meanwhile, try again')
            self.ready(tab, tabId)
            col = self.columnIndex(tab, column) if column else None
            if onlyMatches is False:
                tab.onlyMatches.setChecked(False)
            tab.searchBox.setText(text)
            tab.columnBox.setCurrentIndex(0 if col is None else col + 1)
            tab.caseBox.setChecked(caseSensitive)
            tab.startSearch()
            if onlyMatches:
                tab.onlyMatches.setChecked(True)
            if not tab.search:
                raise McpError(f'search did not start on tab {tabId}')
            return tab, tab.search
        tab, search = self.gui(start)
        if wait:
            self.waitFor(lambda: search.done or search.cancelled)

        def result():
            if search.cancelled and not search.done:
                raise McpError('the search was stopped or replaced before it finished')
            info = self.searchInfo(tab) if tab.search is search else None
            out = {'tabId': self.tabIdOf(tab), 'text': text, 'matches': len(search.hits), 'done': search.done,
                   'firstRows': [h + 1 for h in search.hits[:FIRST_MATCHES]],
                   'onlyMatches': tab.onlyMatches.isChecked(), 'progress': round(search.progress, 4)}
            if info:
                out['column'] = info['column']
            if wait and not search.done:
                out['note'] = f'still searching after {WAIT_TIMEOUT} s; check get_app_state later'
            return out
        return self.gui(result)

    def tool_clear_search(self, tabId):
        def clear():
            tab = self.tabAt(tabId)
            tab.clearSearch()
            tab.searchBox.clear()
            return {'tabId': tabId, 'cleared': True}
        return self.gui(clear)

    def tool_set_frozen_columns(self, tabId, count):
        def freeze():
            tab = self.tabAt(tabId)
            self.ready(tab, tabId)
            width = tab.model.columnCount()
            if count > width:
                raise McpError(f'count {count} is out of range; tab {tabId} has {width} columns')
            tab.freezeSpin.setValue(count)
            return {'tabId': tabId, 'frozenColumns': tab.table.frozenCount}
        return self.gui(freeze)

    def tool_set_column_visibility(self, tabId, columns, visible):
        def apply():
            tab = self.tabAt(tabId)
            self.ready(tab, tabId)
            picks = [self.columnIndex(tab, c) for c in columns]
            for c in picks:
                tab.table.setColumnHidden(c, not visible)
            tab.table.refreshFrozen()
            cols = tab.model.columns
            return {'tabId': tabId, 'changed': [cols[c] for c in picks],
                    'hiddenColumns': [c for n, c in enumerate(cols) if tab.table.isColumnHidden(n)]}
        return self.gui(apply)

    def tool_show_all_columns(self, tabId):
        def show():
            tab = self.tabAt(tabId)
            tab.showAllColumns()
            return {'tabId': tabId, 'hiddenColumns': []}
        return self.gui(show)

    def tool_export_matches(self, tabId, path):
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(path):
            raise McpError(f'{path} is a folder')
        if not os.path.isdir(os.path.dirname(path)):
            raise McpError(f'folder does not exist: {os.path.dirname(path)}')

        def take():
            tab = self.tabAt(tabId)
            w = self.window
            for i in range(w.tabs.count()):
                other = w.tabs.widget(i).index
                if other and samePath(other.path, path):
                    which = 'the source file' if other is tab.index else f'the file open in tab {i + 1}'
                    raise McpError(f'refusing to overwrite {which}: {path}')
            s = tab.search
            if not s:
                raise McpError(f'tab {tabId} has no search; run set_search first')
            if not s.done:
                raise McpError(f'the search on tab {tabId} is still running ({s.progress:.0%}); wait for it to finish')
            if not len(s.hits):
                raise McpError(f'the search on tab {tabId} has no matches')
            return tab.index, array('q', s.hits)
        index, rows = self.gui(take)
        # Writing happens here, off the GUI thread
        export = Export(index, rows, path)
        export.run()
        if export.error:
            raise McpError(f'export failed: {export.error}')
        return {'tabId': tabId, 'path': path, 'rowsWritten': len(rows), 'sizeBytes': os.path.getsize(path)}

    def tool_get_selection(self, tabId=None):
        def selection():
            w = self.window
            if tabId is None:
                if not w.tabs.count():
                    raise McpError('no tab is open')
                tab, tid = w.current(), w.tabs.currentIndex() + 1
            else:
                tab, tid = self.tabAt(tabId), tabId
            if not tab.index:
                raise McpError(f'tab {tid} has no file open')
            model, table = tab.model, tab.table
            ranges = table.selectionModel().selection()
            total = sum(r.height() * sum(not table.isColumnHidden(c) for c in range(r.left(), r.right() + 1)) for r in ranges)
            picked = {}
            cells = ((row, col) for r in ranges for row in range(r.top(), r.bottom() + 1)
                     for col in range(r.left(), r.right() + 1) if not table.isColumnHidden(col))
            for n, (row, col) in enumerate(cells):
                if n >= MAX_SELECTION:
                    break
                picked.setdefault(row, {})[col] = clip(model.cell(row, col))
            n = sum(len(v) for v in picked.values())
            cols = sorted({c for vals in picked.values() for c in vals})
            rows = [[model.dataRow(r) + 1] + [picked[r].get(c) for c in cols] for r in sorted(picked)]
            return {'tabId': tid, 'current': self.currentCell(tab), 'columns': ['row'] + [model.columns[c] for c in cols],
                    'rows': rows, 'selectedCells': total, 'truncated': total > n}
        return self.gui(selection)

    def tool_set_theme(self, theme):
        def apply():
            self.window.setTheme(theme)
            return {'theme': self.window.settings.value('view/theme', 'system')}
        return self.gui(apply)
