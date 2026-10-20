"""Drives the MCP tool registry from a worker thread while Qt runs on the main thread.
Run: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_mcp_tools.py"""
import csv
import json
import os
import random
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
TMP = tempfile.mkdtemp(prefix='mcptools')
# Keep the user's real settings untouched
for var in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
    os.environ.setdefault(var, os.path.join(TMP, var.lower()))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QItemSelection, QItemSelectionModel  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from csvviewer.app import MainWindow  # noqa: E402
from csvviewer.index import CsvIndex, Search  # noqa: E402
from csvviewer.mcpserver.tools import McpTools  # noqa: E402

HEADER = ['id', 'name', 'city', 'note', 'amount', 'qty']
CITIES = ['Toronto', 'Calgary, AB', 'Montréal', 'calgary west', 'Regina']
N = 4000


def writeCsv(path, n, seed):
    rng = random.Random(seed)
    rows = [[i, f'name{i}', rng.choice(CITIES), rng.choice(['plain', 'has, comma', 'multi\nline', 'quote "in"', '']),
             round(rng.uniform(0, 100), 2), rng.randint(1, 9)] for i in range(n)]
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    return [[str(v) for v in r] for r in rows]


class Steps:
    def __init__(self, tools, window):
        self.tools, self.window, self.count = tools, window, 0

    def ok(self, name, **args):
        r = self.tools.call(name, args)
        assert not r['isError'], (name, args, r['content'][0]['text'])
        assert json.loads(r['content'][0]['text']) == r['structuredContent']
        self.count += 1
        return r['structuredContent']

    def bad(self, name, needle, **args):
        r = self.tools.call(name, args)
        assert r['isError'], (name, args, r)
        text = r['content'][0]['text']
        assert needle.lower() in text.lower(), (name, args, text)
        self.count += 1
        return text

    def state(self):
        return self.ok('get_app_state')

    def tabState(self, tabId):
        return self.state()['tabs'][tabId - 1]


def checkDefinitions(tools):
    defs = tools.definitions()
    names = [d['name'] for d in defs]
    assert len(names) == len(set(names)), names
    assert json.dumps(defs) == json.dumps(tools.definitions())
    want = {'get_app_state', 'open_file', 'close_tab', 'switch_tab', 'list_tabs', 'describe_tab', 'get_rows', 'search_rows',
            'run_sql', 'go_to_row', 'set_search', 'clear_search', 'set_frozen_columns', 'set_column_visibility',
            'show_all_columns', 'export_matches', 'get_selection', 'set_theme', 'reopen_tab', 'show_query'}
    assert set(names) == want, set(names) ^ want
    for d in defs:
        assert not any(w in d['name'] for w in ('ask', 'assistant', 'chat')), d['name']
        assert d['title'] and d['description']
        s = d['inputSchema']
        assert s['type'] == 'object' and s['additionalProperties'] is False and isinstance(s['required'], list)
        assert set(s['required']) <= set(s['properties']), d['name']
        for p in s['properties'].values():
            assert p['type'] in ('string', 'integer', 'boolean', 'array') and p.get('description')
        a = d['annotations']
        assert set(a) == {'readOnlyHint', 'destructiveHint', 'idempotentHint', 'openWorldHint'} and a['openWorldHint'] is False
    byName = {d['name']: d['annotations'] for d in defs}
    for n in ('get_app_state', 'list_tabs', 'describe_tab', 'get_rows', 'search_rows', 'run_sql', 'get_selection'):
        assert byName[n]['readOnlyHint'], n
    assert byName['close_tab']['destructiveHint'] and not byName['export_matches']['readOnlyHint']
    assert 't{tabId}' in next(d for d in defs if d['name'] == 'run_sql')['description']


def run(tools, window, steps):
    s = steps
    rows = writeCsv(os.path.join(TMP, 'data.csv'), N, 1)
    other = writeCsv(os.path.join(TMP, 'other.csv'), 300, 2)
    data, otherPath = os.path.join(TMP, 'data.csv'), os.path.join(TMP, 'other.csv')

    # Work really lands on the GUI thread
    assert threading.current_thread() is not threading.main_thread()
    assert tools.gui(lambda: threading.current_thread() is threading.main_thread())

    # Bad calls
    s.bad('nope', 'unknown tool')
    s.bad('get_app_state', 'unknown', extra=1)
    s.bad('close_tab', 'missing')
    s.bad('close_tab', 'must be an integer', tabId='1')
    s.bad('close_tab', 'at least 1', tabId=0)
    s.bad('open_file', 'not found', path=os.path.join(TMP, 'missing.csv'))
    s.bad('open_file', 'not a file', path=TMP)
    s.bad('open_file', 'delimiter', path=data, delimiter='::')
    s.bad('open_file', 'encoding', path=data, encoding='klingon')
    assert s.state()['tabs'] == []

    # open_file waits for the index
    t1 = s.ok('open_file', path=data, waitForIndex=True)
    assert t1['tabId'] == 1 and t1['indexed'] and t1['rows'] == N and t1['columns'] == len(HEADER), t1
    assert t1['progress'] == 1 and t1['current'] and not t1['alreadyOpen'] and t1['sizeBytes'] == os.path.getsize(data)
    assert s.ok('open_file', path=data)['alreadyOpen']
    t2 = s.ok('open_file', path=otherPath, delimiter='comma')
    assert t2['tabId'] == 2 and t2['current']
    end = time.time() + 30
    while not s.tabState(2)['indexed'] and time.time() < end:
        time.sleep(0.05)
    st = s.state()
    assert st['tabs'][1]['rows'] == 300 and st['currentTabId'] == 2 and 'CSV Viewer' in st['windowTitle']

    # switch_tab
    assert s.ok('switch_tab', tabId=1)['current']
    assert s.state()['currentTabId'] == 1
    s.bad('switch_tab', 'no tab 9', tabId=9)

    # Delegated data tools
    tabs = s.ok('list_tabs')['tabs']
    assert [t['tabId'] for t in tabs] == [1, 2] and tabs[0]['columns'] == HEADER
    desc = s.ok('describe_tab', tabId=1)
    assert desc['columns'] == HEADER and len(desc['samples']) == 15
    got = s.ok('get_rows', tabId=1, startRow=10, count=3, columns=['note', 'city'])
    assert got['rows'] == [[r, rows[r - 1][3], rows[r - 1][2]] for r in (10, 11, 12)], got
    s.bad('get_rows', 'out of range', tabId=1, startRow=N + 1, count=1)
    s.bad('describe_tab', 'no tab 7', tabId=7)
    found = s.ok('search_rows', tabId=1, text='Calgary', column='city')
    truth = [i for i, r in enumerate(rows) if 'calgary' in r[2].lower()]
    assert found['totalMatches'] == len(truth)
    sql = s.ok('run_sql', query='SELECT city, count(*) AS n, sum(qty) AS q FROM t1 GROUP BY 1 ORDER BY 1')
    want = Counter(r[2] for r in rows)
    assert {r[0]: r[1] for r in sql['rows']} == dict(want), sql
    assert sum(r[2] for r in sql['rows']) == sum(int(r[5]) for r in rows)
    assert s.ok('run_sql', query='SELECT count(*) FROM t2')['rows'] == [[300]]
    s.bad('run_sql', 'read-only', query='DROP TABLE t1')
    # Table names work in run_sql too, and get_app_state / list_tabs show them
    assert [t['table'] for t in s.state()['tabs']] == ['data', 'other']
    assert [t['table'] for t in s.ok('list_tabs')['tabs']] == ['data', 'other']
    assert s.ok('run_sql', query='SELECT count(*) FROM Other')['rows'] == [[300]]

    # show_query: the SQL goes into the Query panel (t1 becomes the table name) and runs there
    shown = s.ok('show_query', query="SELECT city, count(*) AS n FROM t1 WHERE note <> 't1' GROUP BY 1 ORDER BY 1")
    assert shown['sql'] == "SELECT city, count(*) AS n FROM data WHERE note <> 't1' GROUP BY 1 ORDER BY 1", shown
    assert shown['tabId'] == 1 and shown['table'] == 'data' and not shown['error'] and shown['columns'] == ['city', 'n']
    assert {r[0]: r[1] for r in shown['rows']} == dict(Counter(r[2] for r in rows if r[3])), shown  # empty notes are NULL
    panel = window.queryPanel
    assert tools.gui(lambda: (panel.editSql.isChecked(), panel.sqlBox.toPlainText(), panel.fileBox.currentText(),
                        window.sideDock.isVisible(), len(panel.model.rows))) == (True, shown['sql'], 'data', True, 5)
    # The first table mentioned picks the panel; errors come back; run=false only fills the box
    other2 = s.ok('show_query', query='SELECT count(*) FROM T2_text')
    assert other2['tabId'] == 2 and other2['sql'] == 'SELECT count(*) FROM other_text' and other2['rows'] == [[300]]
    failed = s.ok('show_query', query='SELECT nope FROM t1')
    assert failed['tabId'] == 1 and 'nope' in failed['error'] and 'rows' not in failed
    assert s.ok('show_query', query='SELECT 1 AS x', tabId=2, run=False)['status'] == 'shown'
    assert tools.gui(lambda: panel.sqlBox.toPlainText()) == 'SELECT 1 AS x'
    assert 'read-only' in s.ok('show_query', query='DROP VIEW t1')['error']

    # go_to_row moves the current cell
    moved = s.ok('go_to_row', tabId=1, row=1234, column='city')
    cur = s.state()['currentCell']
    assert cur == {'tabId': 1, 'row': 1234, 'column': 'city', 'value': rows[1233][2]} == moved['current'], cur
    s.ok('go_to_row', tabId=2, row=300)
    assert s.state()['currentCell']['row'] == 300 and s.state()['currentTabId'] == 2
    s.bad('go_to_row', 'out of range', tabId=1, row=N + 1)
    s.bad('go_to_row', 'no column', tabId=1, row=1, column='zzz')

    # set_search equals the Search truth
    idx = CsvIndex(data)
    idx.build()
    truthSearch = Search(idx, 'Calgary', False, 2)
    truthSearch.run()
    res = s.ok('set_search', tabId=1, text='Calgary', column='city', onlyMatches=True)
    assert res['done'] and res['matches'] == len(truthSearch.hits) == len(truth), res
    assert res['firstRows'] == [h + 1 for h in truthSearch.hits[:20]] and res['onlyMatches'] and res['column'] == 'city'
    act = s.tabState(1)['activeSearch']
    assert act == {'text': 'Calgary', 'column': 'city', 'caseSensitive': False, 'onlyMatches': True,
                   'matches': len(truth), 'done': True}, act
    filtered = tools.gui(lambda: (window.tabs.widget(0).model.rowMap is not None, window.tabs.widget(0).model.virtualTotal()))
    assert filtered == (True, len(truth)), filtered
    whole = s.ok('set_search', tabId=1, text='name12', onlyMatches=False)
    hits = [i for i, r in enumerate(rows) if 'name12' in ','.join(r)]
    assert whole['matches'] == len(hits) and not whole['onlyMatches']
    assert s.ok('set_search', tabId=1, text='calgary', caseSensitive=True)['matches'] == sum('calgary' in r[2] for r in rows)
    s.ok('set_search', tabId=1, text='Calgary', column='city', onlyMatches=True)
    # go_to_row turns "only matches" off
    s.ok('go_to_row', tabId=1, row=2)
    assert s.tabState(1)['activeSearch']['onlyMatches'] is False and s.state()['currentCell']['row'] == 2
    s.bad('set_search', 'no column', tabId=1, text='x', column='zzz')
    s.bad('set_search', 'empty', tabId=1, text='')

    # export_matches
    out = os.path.join(TMP, 'out.csv')
    ex = s.ok('export_matches', tabId=1, path=out)
    assert ex['rowsWritten'] == len(truth)
    with open(out, newline='', encoding='utf-8') as f:
        written = list(csv.reader(f))
    assert written[0] == HEADER and written[1:] == [rows[i] for i in truth]
    s.bad('export_matches', 'source file', tabId=1, path=data)
    s.bad('export_matches', 'open in tab 2', tabId=1, path=otherPath)
    s.bad('export_matches', 'no search', tabId=2, path=out)
    s.bad('export_matches', 'folder does not exist', tabId=1, path=os.path.join(TMP, 'nope', 'x.csv'))

    # Freeze and hide show up in the state
    assert s.ok('set_frozen_columns', tabId=1, count=2)['frozenColumns'] == 2
    s.bad('set_frozen_columns', 'out of range', tabId=1, count=99)
    hid = s.ok('set_column_visibility', tabId=1, columns=['note', 'QTY'], visible=False)
    assert hid['hiddenColumns'] == ['note', 'qty']
    t = s.tabState(1)
    assert t['frozenColumns'] == 2 and t['hiddenColumns'] == ['note', 'qty']
    s.ok('set_column_visibility', tabId=1, columns=['qty'], visible=True)
    assert s.tabState(1)['hiddenColumns'] == ['note']
    s.bad('set_column_visibility', 'no column', tabId=1, columns=['zzz'], visible=False)
    s.ok('show_all_columns', tabId=1)
    assert s.tabState(1)['hiddenColumns'] == []
    s.ok('set_frozen_columns', tabId=1, count=0)

    # get_selection: current cell, then a 3 x 2 block
    s.ok('go_to_row', tabId=1, row=50, column='name')
    sel = s.ok('get_selection')
    assert sel['tabId'] == 1 and sel['rows'] == [[50, rows[49][1]]] and sel['columns'] == ['row', 'name'], sel

    def selectBlock():
        tab = window.tabs.widget(0)
        m = tab.model
        tab.table.selectionModel().select(QItemSelection(m.index(99, 0), m.index(101, 2)),
                                          QItemSelectionModel.SelectionFlag.ClearAndSelect)
    tools.gui(selectBlock)
    sel = s.ok('get_selection', tabId=1)
    assert sel['columns'] == ['row', 'id', 'name', 'city'] and sel['rows'] == [[r + 1] + rows[r][:3] for r in (99, 100, 101)], sel
    assert sel['selectedCells'] == 9 and not sel['truncated']
    tools.gui(lambda: window.tabs.widget(0).table.selectAll())
    big = s.ok('get_selection', tabId=1)
    assert big['truncated'] and sum(v is not None for r in big['rows'] for v in r[1:]) == 2000 and big['selectedCells'] == N * len(HEADER)
    s.bad('get_selection', 'no tab', tabId=5)

    # set_theme
    assert s.ok('set_theme', theme='dark')['theme'] == 'dark' and s.state()['theme'] == 'dark'
    s.ok('set_theme', theme='light')
    s.bad('set_theme', 'one of', theme='blue')

    # clear_search
    s.ok('clear_search', tabId=1)
    assert s.tabState(1)['activeSearch'] is None

    # reopen_tab with other options, then back
    re1 = s.ok('reopen_tab', tabId=1, hasHeader=False)
    assert re1['hasHeader'] is False and re1['delimiter'] == ','
    end = time.time() + 30
    while not s.tabState(1)['indexed'] and time.time() < end:
        time.sleep(0.05)
    t = s.tabState(1)
    assert t['rows'] == N + 1 and t['columns'] == len(HEADER), t
    assert s.ok('get_rows', tabId=1, startRow=1, count=1)['rows'][0][1:] == HEADER
    s.ok('reopen_tab', tabId=1, delimiter=';', hasHeader=True)
    assert s.tabState(1)['delimiter'] == ';'
    s.ok('reopen_tab', tabId=1, delimiter='auto')
    s.bad('reopen_tab', 'delimiter', tabId=1, delimiter='ab')
    while not s.tabState(1)['indexed']:
        time.sleep(0.05)
    assert s.tabState(1)['rows'] == N and s.tabState(1)['delimiter'] == ','

    # A call made on the GUI thread runs directly
    direct = tools.gui(lambda: tools.call('get_app_state', {}))
    assert not direct['isError'] and len(direct['structuredContent']['tabs']) == 2

    # close_tab
    closed = s.ok('close_tab', tabId=2)
    assert closed['remainingTabs'] == 1 and closed['name'] == 'other.csv'
    s.bad('close_tab', 'no tab 2', tabId=2)
    s.ok('close_tab', tabId=1)
    st = s.state()
    assert st['tabs'] == [] and st['currentCell'] is None
    s.bad('get_selection', 'no tab is open')
    return other


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    tools = McpTools(window)
    checkDefinitions(tools)
    steps = Steps(tools, window)
    result = {}

    def worker():
        try:
            run(tools, window, steps)
            result['ok'] = True
        except Exception:
            result['error'] = traceback.format_exc()
    thread = threading.Thread(target=worker, daemon=True)
    started = time.time()
    thread.start()
    # Qt runs here while the worker calls the tools
    while thread.is_alive() and time.time() - started < 300:
        app.processEvents()
        time.sleep(0.002)
    window.close()
    if 'ok' not in result:
        print(result.get('error', 'timed out'))
        sys.exit(1)
    print(f'ok: {len(tools.definitions())} tools, {steps.count} calls from a worker thread in {time.time() - started:.1f}s')


if __name__ == '__main__':
    main()
