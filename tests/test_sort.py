"""Quick sort from the column menu in a real MainWindow: numbers and text, blanks last, with filters and search.
Run: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_sort.py [screenshotDir]"""
import csv
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
TMP = tempfile.mkdtemp(prefix='sorttest')
# Keep the user's real settings untouched
for var in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
    os.environ.setdefault(var, os.path.join(TMP, var.lower()))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenu  # noqa: E402

from csvviewer.app import MainWindow  # noqa: E402

N = 3000
NAMES = ['banana', 'Apple', 'cherry', '', 'apple', 'Ünïcode', 'multi\nline', 'a,b']


def waitFor(app, cond, timeout=30):
    end = time.monotonic() + timeout
    while not cond():
        app.processEvents()
        if time.monotonic() > end:
            raise AssertionError('timed out')
        time.sleep(0.01)


def writeCsv(path):
    rng = random.Random(7)
    rows = [[str(i), rng.choice(NAMES), rng.choice(['', str(rng.randint(-50, 1000)), f'{rng.random() * 10:.2f}']),
             rng.choice(['x', 'y'])] for i in range(N)]
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['id', 'name', 'amount', 'kind'])
        w.writerows(rows)
    return rows


def shown(tab):
    return [tab.model.dataRow(r) for r in range(tab.model.rowCount())]


def expected(rows, col, desc, numeric, keep=None):
    ids = [i for i in range(len(rows)) if keep is None or keep(rows[i])]
    filled = [i for i in ids if rows[i][col].strip()]
    key = (lambda i: float(rows[i][col])) if numeric else (lambda i: rows[i][col].strip().encode().lower())
    return sorted(filled, key=key, reverse=desc) + [i for i in ids if not rows[i][col].strip()]


def sortAndWait(app, tab, col, desc):
    tab.sortBy(col, desc)
    waitFor(app, lambda: tab.model.sortedBy == (col, desc))


def main():
    shots = sys.argv[1] if len(sys.argv) > 1 else None
    app = QApplication.instance() or QApplication(sys.argv)
    path = os.path.join(TMP, 'data.csv')
    rows = writeCsv(path)
    window = MainWindow()
    window.resize(1200, 760)
    window.show()
    window.openPath(path)
    tab = window.current()
    waitFor(app, lambda: tab.index.done and tab.columnsReady)

    # The column menus offer both directions
    menu = QMenu()
    tab.sortActions(menu, 2)
    texts = [a.text() for a in menu.actions()]
    assert texts[0].startswith('Sort A → Z') and texts[1].startswith('Sort Z → A') and 'Clear sort' not in texts, texts

    # Numbers by value (not as text), blanks last either way; ties keep file order
    sortAndWait(app, tab, 2, False)
    assert shown(tab) == expected(rows, 2, False, True)
    assert tab.sortLabel.isVisibleTo(tab) and tab.sortLabel.text() == 'Sorted by amount ▲', tab.sortLabel.text()
    assert 'smallest first' in tab.sortLabel.toolTip()
    assert tab.model.headerData(2, Qt.Orientation.Horizontal) == 'amount ▲'
    if shots:
        window.grab().save(os.path.join(shots, 'sort.png'))
    sortAndWait(app, tab, 2, True)
    assert shown(tab) == expected(rows, 2, True, True)
    # Text ignoring case
    sortAndWait(app, tab, 1, False)
    assert shown(tab) == expected(rows, 1, False, False)
    assert tab.model.cell(0, 1) == 'a,b' and tab.model.cell(tab.model.rowCount() - 1, 1) == ''
    sortAndWait(app, tab, 1, True)
    assert shown(tab) == expected(rows, 1, True, False)
    # The row header keeps each row's number in the file
    assert tab.model.headerData(0, Qt.Orientation.Vertical) == f'{shown(tab)[0] + 1:,}'

    # A filter keeps the sort; clearing the filter brings every row back, still sorted
    tab.setColumnFilter(3, ('in', frozenset([b'x'])))
    waitFor(app, lambda: tab.filterScan.done and not tab.sortPending)
    assert shown(tab) == expected(rows, 1, True, False, lambda r: r[3] == 'x')
    tab.clearFilters()
    assert shown(tab) == expected(rows, 1, True, False)

    # "Only matches" with a search, sorted too
    tab.searchBox.setText('apple')
    tab.startSearch()
    tab.onlyMatches.setChecked(True)
    waitFor(app, lambda: tab.search.done and not tab.sortPending)
    assert shown(tab) == expected(rows, 1, True, False, lambda r: 'apple' in ','.join(r).lower())
    tab.clearSearch()

    # Go to row finds a data row wherever the sort put it
    tab.jumpToDataRow(1234)
    assert tab.model.dataRow(tab.table.currentIndex().row()) == 1234

    # Clear sort: file order again, label and arrow gone
    menu = QMenu()
    tab.sortActions(menu, 1)
    assert [a for a in menu.actions() if a.text() == 'Clear sort'] and menu.actions()[1].isChecked()
    tab.clearSort()
    assert shown(tab) == list(range(N)) and tab.model.rowMap is None
    assert not tab.sortLabel.isVisibleTo(tab) and tab.model.headerData(1, Qt.Orientation.Horizontal) == 'name'

    # A second sort started while one shows replaces it when done
    sortAndWait(app, tab, 0, True)
    assert shown(tab) == list(range(N - 1, -1, -1))
    sortAndWait(app, tab, 2, False)
    assert shown(tab) == expected(rows, 2, False, True)
    window.close()
    print('all sort tests passed')


if __name__ == '__main__':
    main()
