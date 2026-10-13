"""Query builder: SQL generation, the DuckDB engine and the dialog inside a real MainWindow.
Run: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_query.py [screenshotDir]"""
import csv
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
TMP = tempfile.mkdtemp(prefix='querytest')
# Keep the user's real settings untouched
for var in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
    os.environ.setdefault(var, os.path.join(TMP, var.lower()))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import duckdb  # noqa: E402

from csvviewer.assistant.context import TabInfo  # noqa: E402
from csvviewer.index import CsvIndex  # noqa: E402
from csvviewer.query.builder import BuilderError, buildSql, kind, operators  # noqa: E402
from csvviewer.query.engine import QueryEngine, QueryError  # noqa: E402

HEADER = ['id', 'city', 'note', 'amount', 'qty', 'day', 'odd "col", x']
CITIES = ['Toronto', 'Calgary, AB', 'Montréal', 'calgary west', "O'Hare", '']
N = 3000


def expectError(fn, needle, cls=Exception):
    try:
        fn()
    except cls as e:
        assert needle.lower() in str(e).lower(), (needle, str(e))
        return
    raise AssertionError(f'expected an error with {needle!r}')


# ---------- builder ----------

WEIRD = 'my "odd" col'
TYPES = {'name': 'VARCHAR', WEIRD: 'VARCHAR', 'amount': 'DOUBLE', 'qty': 'BIGINT', 'day': 'DATE',
         'at': 'TIMESTAMP', 'flag': 'BOOLEAN', 'price': 'DECIMAL(10,2)'}


def st(**kw):
    return {'tabId': 1, 'types': TYPES, 'columns': [], 'filters': [], 'match': 'all', 'groupBy': [], 'aggregates': [],
            'orderBy': [], 'limit': 0, 'distinct': False} | kw


def where(*filters, match='all'):
    sql = buildSql(st(filters=[dict(f) for f in filters], match=match))
    return sql.split('WHERE ', 1)[1] if 'WHERE' in sql else None


def testBuilderSql():
    assert kind('BIGINT') == kind('DOUBLE') == kind('DECIMAL(10,2)') == kind('UTINYINT') == 'numeric'
    assert kind('DATE') == 'date' and kind('TIMESTAMP WITH TIME ZONE') == 'timestamp'
    assert kind('VARCHAR') == kind('BOOLEAN') == kind(None) == 'text'
    assert [o for o, _ in operators('VARCHAR')] == ['equals', 'not_equals', 'contains', 'not_contains', 'starts_with',
                                                     'ends_with', 'empty', 'not_empty', 'one_of']
    assert [l for _, l in operators('DATE')] == ['=', '≠', '>', '≥', '<', '≤', 'between', 'is empty', 'is not empty']
    assert buildSql(st()) == 'SELECT *\nFROM t1'
    assert buildSql(st(columns=['name', WEIRD], distinct=True, limit=50)) == \
        'SELECT DISTINCT "name", "my ""odd"" col"\nFROM t1\nLIMIT 50'
    assert buildSql(st(view='t3_text')).endswith('FROM t3_text')
    expectError(lambda: buildSql(st(view='t1; DROP TABLE x')), 'bad view', BuilderError)
    f = lambda op, v='', v2='', c='name': {'column': c, 'op': op, 'value': v, 'value2': v2}
    # Text operators, case-insensitive
    assert where(f('equals', 'Bob')) == "lower(\"name\") = lower('Bob')"
    assert where(f('not_equals', 'Bob')) == "(\"name\" IS NULL OR lower(\"name\") <> lower('Bob'))"
    assert where(f('contains', '50%_off')) == "\"name\" ILIKE '%50\\%\\_off%' ESCAPE '\\'"
    assert where(f('not_contains', 'x')) == "(\"name\" IS NULL OR \"name\" NOT ILIKE '%x%' ESCAPE '\\')"
    assert where(f('starts_with', 'a')) == "\"name\" ILIKE 'a%' ESCAPE '\\'"
    assert where(f('ends_with', 'a')) == "\"name\" ILIKE '%a' ESCAPE '\\'"
    assert where(f('empty')) == "(\"name\" IS NULL OR \"name\" = '')"
    assert where(f('not_empty')) == "(\"name\" IS NOT NULL AND \"name\" <> '')"
    assert where(f('one_of', " a, O'Brien ,,b ")) == "lower(\"name\") IN (lower('a'), lower('O''Brien'), lower('b'))"
    assert where(f('equals', 'true', c='flag')) == "lower(CAST(\"flag\" AS VARCHAR)) = lower('true')"
    # Blank values leave the filter out until filled
    assert where(f('equals'), f('one_of', ' , '), f('>', ' ', c='qty'), f('between', '1', '', c='qty')) is None
    # Numeric / date / timestamp operators
    for op in ('=', '>', '>=', '<', '<='):
        assert where(f(op, '-1.5e3', c='amount')) == f'"amount" {op} -1.5e3'
    assert where(f('!=', '7', c='qty')) == '("qty" IS NULL OR "qty" <> 7)'
    assert where(f('between', '1', '9', c='price')) == '"price" BETWEEN 1 AND 9'
    assert where(f('empty', c='qty')) == '"qty" IS NULL' and where(f('not_empty', c='day')) == '"day" IS NOT NULL'
    assert where(f('>=', ' 2026-03-01 ', c='day')) == "\"day\" >= DATE '2026-03-01'"
    assert where(f('between', '2026-01-01', '2026-01-02T10:30', c='at')) == \
        "\"at\" BETWEEN TIMESTAMP '2026-01-01 00:00:00' AND TIMESTAMP '2026-01-02 10:30:00'"
    expectError(lambda: where(f('>', '1; DROP TABLE t1', c='qty')), 'not a number', BuilderError)
    expectError(lambda: where(f('=', "1' OR '1'='1", c='amount')), 'not a number', BuilderError)
    expectError(lambda: where(f('=', "2026-01-01' OR 1=1 --", c='day')), 'not a date', BuilderError)
    expectError(lambda: where(f('contains', 'x', c='qty')), 'does not apply', BuilderError)
    # all / any
    assert where(f('equals', 'a'), f('>', '1', c='qty')) == "(lower(\"name\") = lower('a'))\n  AND (\"qty\" > 1)"
    assert where(f('equals', 'a'), f('>', '1', c='qty'), match='any') == "(lower(\"name\") = lower('a'))\n   OR (\"qty\" > 1)"
    # Group, summaries, order, limit
    sql = buildSql(st(groupBy=['name', WEIRD], columns=['ignored'], limit=10,
                      aggregates=[{'func': 'count'}, {'func': 'count_distinct', 'column': 'qty'},
                                  {'func': 'sum', 'column': 'amount'}, {'func': 'avg', 'column': 'name'},
                                  {'func': 'min', 'column': 'day'}, {'func': 'max', 'column': 'day'},
                                  {'func': 'count', 'column': WEIRD}, {'func': 'count'}],
                      orderBy=[{'column': 'Count', 'desc': True}, {'column': WEIRD, 'desc': False}]))
    assert sql == ('SELECT "name", "my ""odd"" col", count(*) AS "Count", count(DISTINCT "qty") AS "Distinct qty", '
                   'sum("amount") AS "Sum of amount", avg(TRY_CAST("name" AS DOUBLE)) AS "Average of name", '
                   'min("day") AS "Min of day", max("day") AS "Max of day", count("my ""odd"" col") AS '
                   '"Count of my ""odd"" col", count(*) AS "Count 2"\nFROM t1\nGROUP BY "name", "my ""odd"" col"\n'
                   'ORDER BY "Count" DESC, "my ""odd"" col"\nLIMIT 10'), sql
    assert buildSql(st(aggregates=[{'func': 'count'}])) == 'SELECT count(*) AS "Count"\nFROM t1'
    expectError(lambda: buildSql(st(aggregates=[{'func': 'sum'}])), 'pick a column', BuilderError)
    expectError(lambda: buildSql(st(aggregates=[{'func': 'drop'}])), 'unknown', BuilderError)
    expectError(lambda: buildSql(st(limit='5; DROP')), 'whole number', BuilderError)
    print('builder sql ok')


def testBuilderExecutes():
    # Weird names and hostile values run as data, never as SQL
    con = duckdb.connect()
    con.execute(f'CREATE TABLE t1 (name VARCHAR, "{WEIRD.replace(chr(34), chr(34) * 2)}" VARCHAR, qty BIGINT)')
    rows = [('Bob', 'x', 1), ("O'Brien", "'; DROP TABLE t1; --", 2), ('50%_off', None, None), ('', '', 4), ('bob', 'X', 5)]
    con.executemany('INSERT INTO t1 VALUES (?, ?, ?)', rows)
    q = lambda *fs, **kw: con.execute(buildSql(st(filters=list(fs), **kw))).fetchall()
    f = lambda op, v='', v2='', c='name': {'column': c, 'op': op, 'value': v, 'value2': v2}
    assert len(q(f('equals', 'BOB'))) == 2
    assert q(f('equals', "O'Brien")) == [rows[1]]
    assert q(f('contains', "'; DROP TABLE t1; --", c=WEIRD)) == [rows[1]]
    assert q(f('contains', '%_')) == [rows[2]] and len(q(f('contains', '%'))) == 1
    assert len(q(f('not_contains', 'x', c=WEIRD))) == 3  # NULL and '' count as "does not contain"
    assert len(q(f('empty', c=WEIRD))) == 2 and len(q(f('not_empty', c=WEIRD))) == 3
    assert len(q(f('empty', c='qty'))) == 1 and len(q(f('!=', '1', c='qty'))) == 4
    assert len(q(f('one_of', "bob, o'brien"))) == 3
    assert len(q(f('equals', 'bob'), f('between', '2', '9', c='qty'), match='any')) == 4
    assert con.execute(buildSql(st(groupBy=[WEIRD], aggregates=[{'func': 'count'}], orderBy=[{'column': 'Count', 'desc': True}],
                                   types={'qty': 'BIGINT'}))).fetchall()[0][1] == 1
    assert con.execute('SELECT count(*) FROM t1').fetchone()[0] == 5
    print('builder executes ok')


# ---------- engine ----------

def writeCsv(path):
    rng = random.Random(7)
    rows = []
    for i in range(N):
        rows.append([str(i), rng.choice(CITIES), rng.choice(['plain', 'has, comma', 'multi\nline', 'quote "in"', '']),
                     f'{rng.uniform(0, 1000):.2f}', str(rng.randint(1, 9)), f'2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}',
                     rng.choice(['a', 'b'])])
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    return rows


def tabFor(path, tabId=1):
    idx = CsvIndex(path)
    idx.build()
    return TabInfo(tabId=tabId, name=os.path.basename(path), path=path, index=idx, columns=idx.header(), indexed=True)


def testEngine(path, rows):
    eng = QueryEngine()
    eng.setTabs([tabFor(path)])
    cols = eng.columns(1)
    types = dict(cols)
    assert [c for c, _ in cols] == HEADER, cols
    assert kind(types['amount']) == 'numeric' and types['day'] == 'DATE' and types['city'] == 'VARCHAR', types
    state = st(types=types, view=eng.viewName(1), limit=0)
    # Filter: city contains "calgary" (any case) and qty >= 5
    sql = buildSql(state | {'filters': [{'column': 'city', 'op': 'contains', 'value': 'CALGARY'},
                                        {'column': 'qty', 'op': '>=', 'value': '5'}], 'columns': ['id', 'note']})
    res = eng.run(sql, limit=10_000)
    truth = [[r[0], r[2]] for r in rows if 'calgary' in r[1].lower() and int(r[4]) >= 5]
    assert res['columns'] == ['id', 'note'] and not res['more'] and res['totalRows'] == len(truth)
    assert sorted([str(a), b or ''] for a, b in res['rows']) == sorted(truth), 'filter rows differ'
    # Notes with newlines and quotes survive
    assert any('\n' in (b or '') for _, b in res['rows']) and any('"' in (b or '') for _, b in res['rows'])
    # Group + summaries against Python
    sql = buildSql(state | {'groupBy': ['city'], 'aggregates': [{'func': 'count'}, {'func': 'sum', 'column': 'qty'}],
                            'orderBy': [{'column': 'Count', 'desc': True}]})
    res = eng.run(sql)
    got = {(c or ''): (n, s) for c, n, s in res['rows']}
    want = {}
    for r in rows:
        n, s = want.get(r[1], (0, 0))
        want[r[1]] = (n + 1, s + int(r[4]))
    assert got == want, (got, want)
    assert [r[1] for r in res['rows']] == sorted([n for n, _ in want.values()], reverse=True)
    # Empty city (NULL in the typed view) and a date range
    empty = eng.run(buildSql(state | {'filters': [{'column': 'city', 'op': 'empty'}], 'aggregates': [{'func': 'count'}]}))
    assert empty['rows'][0][0] == sum(1 for r in rows if not r[1])
    march = eng.run(buildSql(state | {'filters': [{'column': 'day', 'op': 'between', 'value': '2026-03-01', 'value2': '2026-03-31'}],
                                      'aggregates': [{'func': 'count'}]}))
    assert march['rows'][0][0] == sum(1 for r in rows if r[5].startswith('2026-03'))
    # Weird column name and quote in a value
    hare = eng.run(buildSql(state | {'filters': [{'column': 'city', 'op': 'equals', 'value': "o'hare"},
                                                 {'column': HEADER[-1], 'op': 'equals', 'value': 'A'}], 'columns': ['id']}), 10_000)
    assert sorted(int(r[0]) for r in hare['rows']) == [int(r[0]) for r in rows if r[1] == "O'Hare" and r[6] == 'a']
    # Limit + "more"
    res = eng.run('SELECT * FROM t1', limit=100)
    assert len(res['rows']) == 100 and res['more'] and res['totalRows'] is None and res['seconds'] >= 0
    res = eng.run('select * from t1_text where "note" like \'%\n%\'', limit=10_000)
    assert res['totalRows'] == sum(1 for r in rows if '\n' in r[2]) and set(res['types']) == {'VARCHAR'}
    # Export writes the full result, not the shown rows
    out = os.path.join(TMP, 'export.csv')
    info = eng.export('SELECT id, city, note FROM t1 ORDER BY id;', out)
    assert info['rows'] == N and not os.path.exists(out + '.part')
    with open(out, newline='', encoding='utf-8') as f:
        back = list(csv.reader(f))
    assert back[0] == ['id', 'city', 'note'] and [[a, b, c] for a, b, c in back[1:]] == [[r[0], r[1], r[2]] for r in rows]
    # Read-only rules, also for export
    for bad, needle in (('DROP VIEW t1', 'read-only'), ("COPY t1 TO '/tmp/x.csv'", 'read-only'),
                        ('SELECT 1; SELECT 2', 'one statement'), ('CREATE TABLE x AS SELECT 1', 'read-only'),
                        ('EXPLAIN ANALYZE INSERT INTO x VALUES (1)', 'SELECT queries only'), ("ATTACH 'x.db'", 'read-only'),
                        ('', 'no query')):
        expectError(lambda: eng.run(bad), needle, QueryError)
    expectError(lambda: eng.export('DELETE FROM t1', out), 'read-only', QueryError)
    expectError(lambda: eng.run('SELECT nope FROM t1'), 'nope', QueryError)
    # Cancel a long query running on the pool
    future = eng.submit(eng.run, 'SELECT count(*) FROM t1 a, t1 b, t1 c WHERE a.note || b.note <> c.city')
    time.sleep(0.8)
    started = time.monotonic()
    eng.cancel()
    expectError(future.result, 'stopped', QueryError)
    assert time.monotonic() - started < 5
    assert eng.run('SELECT count(*) FROM t1')['rows'][0][0] == N  # usable after a cancel
    # Tabs moved: tab 2 is now this file, views follow the file
    other = os.path.join(TMP, 'other.csv')
    with open(other, 'w') as f:
        f.write('x;y\n1;2\n3;4\n')
    eng.setTabs([tabFor(other, 1), tabFor(path, 2)])
    assert eng.columns(1) == [('x', 'BIGINT'), ('y', 'BIGINT')]
    assert eng.run('SELECT count(*) FROM t2')['rows'][0][0] == N
    eng.close()
    print(f'engine ok ({N} rows, export + cancel + read-only)')


# ---------- dialog ----------

def waitFor(app, cond, timeout=30):
    end = time.monotonic() + timeout
    while not cond():
        app.processEvents()
        if time.monotonic() > end:
            raise AssertionError('timed out')
        time.sleep(0.01)


def testDialog(path, rows, shots):
    from PyQt6.QtWidgets import QApplication
    from csvviewer.app import MainWindow
    from csvviewer.query import openQueryBuilder
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    other = os.path.join(TMP, 'other.csv')
    window.openPath(other)
    window.openPath(path)
    waitFor(app, lambda: all(window.tabs.widget(i).index.done for i in range(window.tabs.count())))
    dlg = openQueryBuilder(window)
    assert openQueryBuilder(window) is dlg and dlg.isVisible()
    assert dlg.fileBox.currentData() == os.path.abspath(path) and dlg.fileBox.count() == 2
    waitFor(app, lambda: dlg.names == HEADER)
    assert dlg.runButton.isEnabled()
    dlg.addFilter('qty', '>=', '3')
    dlg.addFilter('note', 'not_empty')
    dlg.addGroup('city')
    dlg.addSummary('count')
    dlg.addSummary('avg', 'amount')
    dlg.setSort('Count', True)
    sql = dlg.sqlBox.toPlainText()
    assert 'GROUP BY "city"' in sql and 'ORDER BY "Count" DESC' in sql and '"qty" >= 3' in sql, sql
    dlg.runQuery()
    assert dlg.busy == 'run'
    waitFor(app, lambda: dlg.busy is None)
    assert not dlg.errorLabel.isVisible(), dlg.errorLabel.text()
    want = {}
    for r in rows:
        if int(r[4]) >= 3 and r[2]:
            want[r[1]] = want.get(r[1], 0) + 1
    got = {(row[0] or ''): row[1] for row in dlg.model.rows}
    assert got == want, (got, want)
    assert dlg.model.columns == ['city', 'Count', 'Average of amount'] and 'rows' in dlg.statusLabel.text()
    # Bad number shows a friendly error, nothing runs
    bad = dlg.addFilter('amount', '>', 'abc')
    assert dlg.sqlBox.toPlainText().startswith('-- ')
    dlg.runQuery()
    assert dlg.errorLabel.isVisible() and 'not a number' in dlg.errorLabel.text() and dlg.busy is None
    dlg.removeRow(dlg.filters, bad)
    app.processEvents()
    for theme in ('light', 'dark'):
        window.setTheme(theme)
        dlg.runQuery()
        waitFor(app, lambda: dlg.busy is None)
        if theme == 'dark':
            dlg.showError('Binder Error: Referenced column "amout" not found in FROM clause!\nCandidate bindings: "amount"')
        app.processEvents()
        if shots:
            dlg.grab().save(os.path.join(shots, f'query-builder-{theme}.png'))
    dlg.hideError()
    # Edit SQL mode runs the typed SQL; read-only check applies
    dlg.editSql.setChecked(True)
    assert not dlg.builderPane.isEnabled() and not dlg.sqlBox.isReadOnly()
    dlg.sqlBox.setPlainText('DROP VIEW t2')
    dlg.runQuery()
    waitFor(app, lambda: dlg.busy is None)
    assert 'read-only' in dlg.errorLabel.text()
    dlg.sqlBox.setPlainText('SELECT * FROM t2 ORDER BY id')
    dlg.limitSpin.setValue(0)
    dlg.runQuery()
    waitFor(app, lambda: dlg.busy is None)
    assert len(dlg.model.rows) == N and not dlg.errorLabel.isVisible()
    # Stop a long query from the dialog
    dlg.sqlBox.setPlainText('SELECT count(*) FROM t2 a, t2 b, t2 c WHERE a.note || b.note <> c.city')
    dlg.runQuery()
    time.sleep(0.5)
    dlg.stop()
    waitFor(app, lambda: dlg.busy is None, 10)
    assert 'Stopped' in dlg.errorLabel.text()
    dlg.editSql.setChecked(False)
    assert dlg.builderPane.isEnabled()
    # Open results as new tab: COPY into the data dir, then a new viewer tab
    tabsBefore = window.tabs.count()
    dlg.openAsTab()
    waitFor(app, lambda: dlg.busy is None)
    assert window.tabs.count() == tabsBefore + 1, dlg.errorLabel.text()
    newPath = window.tabs.currentWidget().index.path
    # The app data dir (XDG_DATA_HOME on Linux, AppData on Windows)
    from PyQt6.QtCore import QStandardPaths
    dataDir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    assert os.path.normcase(newPath).startswith(os.path.normcase(dataDir)) and 'query-results' in newPath, (newPath, dataDir)
    with open(newPath, newline='', encoding='utf-8') as f:
        back = list(csv.reader(f))
    assert back[0] == ['city', 'Count', 'Average of amount'] and len(back) - 1 == len(want)
    # The new tab shows in the file list; closing a tab refreshes it
    waitFor(app, lambda: dlg.fileBox.count() == 3)
    window.closeTab(window.tabs.count() - 1)
    waitFor(app, lambda: dlg.fileBox.count() == 2)
    assert dlg.fileBox.currentData() == os.path.abspath(path)
    # Clear resets the builder; switching file reloads columns
    dlg.clear()
    assert not dlg.filters and not dlg.groups and dlg.sqlBox.toPlainText() == 'SELECT *\nFROM t2\nLIMIT 1000'
    dlg.fileBox.setCurrentIndex(dlg.fileBox.findData(os.path.abspath(other)))
    dlg.onFile()
    waitFor(app, lambda: dlg.names == ['x', 'y'])
    assert 'FROM t1' in dlg.sqlBox.toPlainText()
    dlg.close()
    assert window.settings.value('query/geometry') is not None
    window.close()
    print('dialog ok')


def main():
    shots = sys.argv[1] if len(sys.argv) > 1 else None
    testBuilderSql()
    testBuilderExecutes()
    path = os.path.join(TMP, 'people.csv')
    rows = writeCsv(path)
    testEngine(path, rows)
    testDialog(path, rows, shots)
    print('all query tests passed')


if __name__ == '__main__':
    main()
