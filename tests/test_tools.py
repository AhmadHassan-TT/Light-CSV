"""Exercises the assistant's CSV tools without Qt or the API. Run: .venv/bin/python tests/test_tools.py [--perf DIR]"""
import csv
import io
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from csvviewer.index import CsvIndex  # noqa: E402
from csvviewer.assistant.context import TabInfo  # noqa: E402
from csvviewer.assistant.tools import CsvTools, ToolError  # noqa: E402

CITIES = ['Toronto', 'Calgary, AB', 'Montréal', 'Winnipeg', 'Regina, SK']
HEADER = ['id', 'name', 'city', 'note', 'amount', 'qty', 'day']


def makeRows(n, seed):
    rng = random.Random(seed)
    for i in range(n):
        note = rng.choice(['plain', 'has, comma', 'multi\nline', 'quote "inside"', '', 'x' * rng.randint(0, 400)])
        yield [i, f'name{i}', rng.choice(CITIES), note, round(rng.uniform(0, 1000), 2), rng.randint(1, 9),
               f'2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}']


def writeCsv(path, rows):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)


def tabFor(path, tabId, current=False):
    idx = CsvIndex(path)
    idx.build()
    return TabInfo(tabId=tabId, name=os.path.basename(path), path=path, index=idx, columns=idx.header(),
                   indexed=idx.done, current=current)


def call(tools, name, **kw):
    text, action = tools.run(name, kw)
    return (json.loads(text) if text[:1] in '[{' else text), action


def expectError(tools, name, needle, **kw):
    try:
        tools.run(name, kw)
    except ToolError as e:
        assert needle in str(e), (name, kw, str(e))
        return str(e)
    raise AssertionError(f'{name} {kw} should fail')


def main():
    tmp = tempfile.mkdtemp()
    n = 50000
    rows = list(makeRows(n, 7))
    path = os.path.join(tmp, 'data.csv')
    writeCsv(path, rows)
    with open(path, newline='', encoding='utf-8') as f:
        want = list(csv.reader(f))[1:]
    tab = tabFor(path, 1, current=True)
    tools = CsvTools([tab])

    # Definitions are deterministic and strict
    defs = tools.definitions()
    assert [d['name'] for d in defs] == ['list_tabs', 'describe_tab', 'get_rows', 'search_rows', 'run_sql', 'show_query',
                                         'show_rows']
    assert json.dumps(defs) == json.dumps(CsvTools([]).definitions())
    for d in defs:
        assert d['strict'] and d['input_schema']['additionalProperties'] is False and 'required' in d['input_schema']

    # list_tabs
    tabs, _ = call(tools, 'list_tabs')
    assert tabs[0]['rows'] == n and tabs[0]['columns'] == HEADER and tabs[0]['sqlView'] == 't1' and tabs[0]['current']

    # describe_tab: first 5, middle 5, last 5
    desc, _ = call(tools, 'describe_tab', tabId=1)
    nums = [s['row'] for s in desc['samples']]
    assert nums[:5] == [1, 2, 3, 4, 5] and nums[-5:] == list(range(n - 4, n + 1)) and len(nums) == 15
    assert desc['samples'][0]['values']['name'] == want[0][1]

    # get_rows with column filter, truncation of long cells, embedded newlines
    got, _ = call(tools, 'get_rows', tabId=1, startRow=101, count=500, columns=['id', 'note'])
    assert got['columns'] == ['row', 'id', 'note'] and len(got['rows']) == 100
    for r in got['rows']:
        note = want[r[0] - 1][3]
        assert r[1] == want[r[0] - 1][0]
        assert r[2] == (note[:300] + '…' if len(note) > 300 else note)
    got, _ = call(tools, 'get_rows', tabId=1, startRow=n - 1, count=10)
    assert len(got['rows']) == 2 and 'note' in got

    # Errors
    expectError(tools, 'get_rows', 'no tab 9', tabId=9, startRow=1, count=1)
    expectError(tools, 'get_rows', 'out of range', tabId=1, startRow=n + 1, count=1)
    expectError(tools, 'get_rows', 'out of range', tabId=1, startRow=0, count=1)
    expectError(tools, 'get_rows', 'no column', tabId=1, startRow=1, count=1, columns=['nope'])
    expectError(tools, 'show_rows', 'out of range', tabId=1, row=n + 5)
    expectError(tools, 'nope', 'unknown tool')
    expectError(tools, 'get_rows', 'bad input', tabId=1)

    # show_query hands the query to the window (its Query panel) for the tab it mentions
    two = CsvTools([tab, TabInfo(tabId=2, name='b.csv', path=path, index=tab.index, columns=HEADER, table='sales')])
    text, action = two.run('show_query', {'query': 'SELECT count(*) FROM T2'})
    assert action == {'type': 'showQuery', 'tabId': 2, 'query': 'SELECT count(*) FROM T2'} and 'Query panel' in text
    assert two.run('show_query', {'query': 'SELECT * FROM sales_text'})[1]['tabId'] == 2  # by table name
    assert two.run('show_query', {'query': 'SELECT 1'})[1]['tabId'] == 1  # the current tab
    assert two.run('show_query', {'query': 'SELECT 1', 'tabId': 2})[1]['tabId'] == 2
    expectError(two, 'show_query', 'no tab 5', query='SELECT 1', tabId=5)
    expectError(two, 'show_query', 'empty', query='  ')
    # A table name works in run_sql like its t<N> view, and list_tabs shows it
    res, _ = call(two, 'run_sql', query='SELECT count(*) FROM SALES')
    assert res['rows'] == [[n]], res
    assert [t.get('table') for t in call(two, 'list_tabs')[0]] == [None, 'sales']
    two.close()

    # search_rows against Python truth
    res, _ = call(tools, 'search_rows', tabId=1, text='multi\nline')
    truth = [i for i, r in enumerate(want) if 'multi\nline' in ','.join(r)]
    assert res['totalMatches'] == len(truth) and [r[0] for r in res['rows']] == [i + 1 for i in truth[:20]]
    res, _ = call(tools, 'search_rows', tabId=1, text='calgary', column='city', limit=500)
    truth = [i for i, r in enumerate(want) if 'calgary' in r[2].lower()]
    assert res['totalMatches'] == len(truth) and len(res['rows']) == 50

    # run_sql aggregates checked against Python
    res, _ = call(tools, 'run_sql', query='SELECT count(*) FROM t1')
    assert res['rows'] == [[n]], res
    res, _ = call(tools, 'run_sql', query='SELECT "city", count(*) AS c, sum("qty") AS q FROM t1 GROUP BY 1 ORDER BY 1')
    cnt, qty = Counter(r[2] for r in want), Counter()
    for r in want:
        qty[r[2]] += int(r[5])
    assert res['rows'] == [[c, cnt[c], qty[c]] for c in sorted(cnt)], res['rows']
    res, _ = call(tools, 'run_sql', query="SELECT round(sum(amount), 2), max(day) FROM t1 WHERE note LIKE '%comma%'")
    sel = [r for r in want if 'comma' in r[3]]
    assert abs(res['rows'][0][0] - sum(float(r[4]) for r in sel)) < 0.01 and res['rows'][0][1] == max(r[6] for r in sel)
    res, _ = call(tools, 'run_sql', query='SELECT count(*) FROM t1_text WHERE "note" LIKE \'multi%\'')
    assert res['rows'] == [[sum(1 for r in want if r[3].startswith('multi'))]]
    res, _ = call(tools, 'run_sql', query='SELECT * FROM t1')
    assert res['rowCount'] == 200 and res['truncated'] and res['columns'] == HEADER
    res, _ = call(tools, 'run_sql', query='DESCRIBE t1')
    assert [r[0] for r in res['rows']] == HEADER
    call(tools, 'run_sql', query='SUMMARIZE t1')
    call(tools, 'run_sql', query='EXPLAIN SELECT count(*) FROM t1')
    for bad in ['DROP VIEW t1', 'CREATE TABLE x AS SELECT 1', "COPY t1 TO 'x.csv'", 'SELECT 1; SELECT 2',
                'SET threads=1', "ATTACH 'x.db'", 'INSTALL httpfs', 'EXPLAIN ANALYZE CREATE TABLE y AS SELECT 1']:
        expectError(tools, 'run_sql', '', query=bad)
    err = expectError(tools, 'run_sql', 'Conversion', query="SELECT CAST(name AS INTEGER) FROM t1")
    assert 't1_text = read_csv(' in err and 'all_varchar=true' in err, err
    expectError(tools, 'run_sql', 'Catalog', query='SELECT * FROM t7')

    # show_rows
    text, action = tools.run('show_rows', {'tabId': 1, 'row': 42, 'column': 'CITY'})
    assert text == 'Shown row 42 in tab 1' and action == {'type': 'showRow', 'tabId': 1, 'row': 42, 'column': 'city'}
    tools.close()

    # cancel() interrupts a long query from another thread
    slow = CsvTools([tab])
    threading.Timer(1.5, slow.cancel).start()
    t0 = time.monotonic()
    expectError(slow, 'run_sql', 'stopped', query='SELECT count(*) FROM t1 a, t1 b WHERE a.id + b.id * 7 = -1')
    took = time.monotonic() - t0
    assert took < 10, took
    expectError(slow, 'list_tabs', 'stopped')
    slow.close()
    print(f'cancel interrupted the cross join after {took:.1f} s')

    # cancel() stops a running search
    slow = CsvTools([tab])
    threading.Timer(0.05, slow.cancel).start()
    try:
        slow.run('search_rows', {'tabId': 1, 'text': 'zzzz-not-there'})
        print('search finished before cancel (file too small to catch it)')
    except ToolError as e:
        assert 'stopped' in str(e)
    slow.close()

    # Still-indexing tab
    fresh = CsvIndex(path)
    part = TabInfo(tabId=2, name='part', path=path, index=fresh, columns=HEADER)
    t2 = CsvTools([part])
    expectError(t2, 'search_rows', 'still indexing', tabId=2, text='x')
    expectError(t2, 'get_rows', 'still indexing', tabId=2, startRow=10, count=1)
    fresh.close()

    tab.index.close()
    shutil.rmtree(tmp)
    print('all tools tests passed')


def perf(folder):
    """Times a GROUP BY over a ~1 GB file through run_sql."""
    path = os.path.join(folder, 'perf.csv')
    out = io.StringIO(newline='')
    w = csv.writer(out)
    w.writerows(makeRows(100000, 3))
    chunk = out.getvalue()
    t0 = time.monotonic()
    with open(path, 'w', newline='', encoding='utf-8') as f:
        f.write(','.join(HEADER) + '\r\n')  # csv.writer ends rows with CRLF
        while f.tell() < 1 << 30:
            f.write(chunk)
    print(f'wrote {os.path.getsize(path) / 2**30:.2f} GB in {time.monotonic() - t0:.1f} s')
    t0 = time.monotonic()
    tab = tabFor(path, 1)
    print(f'indexed {tab.index.dataRows:,} rows in {time.monotonic() - t0:.1f} s')
    tools = CsvTools([tab])
    try:
        for q in ['SELECT city, count(*), sum(qty), avg(amount) FROM t1 GROUP BY 1 ORDER BY 2 DESC',
                  'SELECT city, count(*) FROM t1_text GROUP BY 1 ORDER BY 2 DESC']:
            t0 = time.monotonic()
            res, _ = call(tools, 'run_sql', query=q)
            print(f'{q}\n  -> {sum(r[1] for r in res["rows"]):,} rows counted, {res["seconds"]} s in query, '
                  f'{time.monotonic() - t0:.1f} s total (first includes view setup)')
    finally:
        tools.close()
        tab.index.close()
        os.remove(path)


if __name__ == '__main__':
    if '--perf' in sys.argv:
        perf(sys.argv[sys.argv.index('--perf') + 1])
    else:
        main()
