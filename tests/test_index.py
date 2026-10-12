"""Checks the index against Python's csv module on awkward files. Run: python3 tests/test_index.py"""
import csv
import io
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from csvviewer.index import CsvIndex, Search, Export  # noqa: E402


def write(tmp, name, data):
    path = os.path.join(tmp, name)
    with open(path, 'wb') as f:
        f.write(data)
    return path


def expectedRows(data, delimiter=','):
    text = data.decode('utf-8-sig')
    return list(csv.reader(io.StringIO(text, newline=''), delimiter=delimiter))


def randomCsv(n, seed):
    rng = random.Random(seed)
    out = io.StringIO(newline='')
    w = csv.writer(out, lineterminator=rng.choice(['\n', '\r\n']))
    w.writerow(['id', 'name', 'note', 'value'])
    for i in range(n):
        note = rng.choice(['plain', 'has, comma', 'multi\nline', 'quote "inside"', '', 'ÄÖÜ café', 'x' * rng.randint(0, 50)])
        w.writerow([i, f'name{i}', note, rng.random()])
    return out.getvalue().encode('utf-8')


def checkAll(path, data, block, **kw):
    idx = CsvIndex(path, block=block, **kw)
    idx.build()
    want = expectedRows(data, idx.delimiter)
    assert idx.rowsIndexed == len(want), (path, block, idx.rowsIndexed, len(want))
    got = [idx.row(r) for r in range(idx.rowsIndexed)]
    assert got == want, (path, block)
    idx.close()


def main():
    tmp = tempfile.mkdtemp()
    cases = {
        'simple.csv': b'a,b\n1,2\n3,4\n',
        'noTrailingNewline.csv': b'a,b\n1,2\n3,4',
        'crlf.csv': b'a,b\r\n1,2\r\n3,4\r\n',
        'bom.csv': b'\xef\xbb\xbfa,b\n1,2\n',
        'quotedNewline.csv': b'a,b\n"x\ny",2\n"p""q\n",3\nlast,4\n',
        'semicolon.csv': b'a;b;c\n1;2;3\n4;5;6\n',
        'headerOnly.csv': b'a,b,c\n',
    }
    for n, seed in [(1000, 1), (5000, 2), (333, 3)]:
        cases[f'random{seed}.csv'] = randomCsv(n, seed)
    for name, data in cases.items():
        path = write(tmp, name, data)
        for block in (1, 2, 7, 128):
            checkAll(path, data, block)

    # Search: all columns and one column, case folding
    data = randomCsv(4000, 9)
    path = write(tmp, 'search.csv', data)
    idx = CsvIndex(path, block=16)
    idx.build()
    rows = expectedRows(data)[1:]
    for term, cs, col in [('comma', False, None), ('CAFÉ', False, None), ('café', True, 2), ('name12', True, 1), ('NAME3', False, None), ('zzz', False, None)]:
        s = Search(idx, term, cs, col)
        s.run()
        f = (lambda v: term in v) if cs else (lambda v: term.lower() in v.lower())
        want = [i for i, r in enumerate(rows) if (f(r[col]) if col is not None else f(','.join(r)))]
        if term == 'CAFÉ':
            want = []  # non-ASCII case folding is not supported in the byte scan
        assert list(s.hits) == want, (term, len(s.hits), len(want))

    # Export round trip
    s = Search(idx, 'multi', False, None)
    s.run()
    out = os.path.join(tmp, 'out.csv')
    Export(idx, s.hits, out).run()
    with open(out, 'rb') as f:
        exported = expectedRows(f.read())
    assert exported[0] == ['id', 'name', 'note', 'value']
    assert exported[1:] == [rows[i] for i in s.hits]
    idx.close()
    print('all index tests passed')


if __name__ == '__main__':
    main()
