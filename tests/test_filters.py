"""Excel-style column filters in a real MainWindow: dropdown, values list, cell menu, search together with filters.
Run: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_filters.py [screenshotDir]"""
import csv
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
TMP = tempfile.mkdtemp(prefix='filtertest')
# Keep the user's real settings untouched
for var in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
    os.environ.setdefault(var, os.path.join(TMP, var.lower()))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QPoint, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402
from PyQt6.QtCore import QEvent, QPointF  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from csvviewer.app import FILTER_BUTTON, MainWindow  # noqa: E402
from csvviewer.filters import FilterPopup  # noqa: E402

CITIES = ['Toronto', 'Calgary, AB', 'Montréal', 'calgary west', '', 'multi\nline']
N = 5000


def waitFor(app, cond, timeout=30):
    end = time.monotonic() + timeout
    while not cond():
        app.processEvents()
        if time.monotonic() > end:
            raise AssertionError('timed out')
        time.sleep(0.01)


def writeCsv(path):
    rng = random.Random(4)
    rows = [[str(i), rng.choice(CITIES), rng.choice(['a', 'b', 'c']), str(rng.randint(1, 9))] for i in range(N)]
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['id', 'city', 'kind', 'qty'])
        w.writerows(rows)
    return rows


def visibleIds(tab):
    return [tab.model.dataRow(r) for r in range(tab.model.rowCount())]


def popups():
    return [w for w in QApplication.topLevelWidgets() if isinstance(w, FilterPopup) and w.isVisible()]


def click(header, x):
    pos = QPointF(x, header.height() / 2)
    for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        QApplication.sendEvent(header.viewport(), QMouseEvent(kind, pos, header.viewport().mapToGlobal(pos),
                                                              Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                                              Qt.KeyboardModifier.NoModifier))


def main():
    shots = sys.argv[1] if len(sys.argv) > 1 else None
    app = QApplication.instance() or QApplication(sys.argv)
    path = os.path.join(TMP, 'data.csv')
    rows = writeCsv(path)
    window = MainWindow()
    window.resize(1300, 800)
    window.show()
    window.openPath(path)
    tab = window.current()
    waitFor(app, lambda: tab.index.done and tab.columnsReady)
    header = tab.table.horizontalHeader()

    # Adding a dropdown opens the popup with every value of the column and its row count
    tab.addColumnFilter(1)
    assert header.filterState(1) is False
    popup = popups()[0]
    waitFor(app, lambda: popup.scan.done)
    popup.rebuild()
    want = {c: sum(1 for r in rows if r[1] == c) for c in CITIES}
    got = {popup.list.item(i).data(Qt.ItemDataRole.UserRole).decode(): popup.list.item(i).text() for i in range(popup.list.count())}
    assert set(got) == set(CITIES), got
    assert all(got[c].endswith(f'({want[c]:,})') for c in CITIES), got
    assert got[''].startswith('(Blanks)') and popup.list.item(popup.list.count() - 1).data(Qt.ItemDataRole.UserRole) == b''
    if shots:
        popup.grab().save(os.path.join(shots, 'filter-popup.png'))

    # Untick two values: the rows with them disappear
    for i in range(popup.list.count()):
        item = popup.list.item(i)
        if item.data(Qt.ItemDataRole.UserRole) in (b'Toronto', b''):
            item.setCheckState(Qt.CheckState.Unchecked)
    assert popup.allBox.checkState() == Qt.CheckState.PartiallyChecked
    popup.accept()
    assert tab.filters[1] == ('out', frozenset({b'Toronto', b''}))
    waitFor(app, lambda: tab.filterScan.done)
    tab.tick()
    expect = [i for i, r in enumerate(rows) if r[1] not in ('Toronto', '')]
    assert visibleIds(tab) == expect
    assert header.filterState(1) is True and tab.clearFiltersBtn.isVisible()
    assert f'{len(expect):,} of {N:,}' in tab.filterLabel.text(), tab.filterLabel.text()

    # A second column narrows further, and its values list only counts rows the first filter keeps
    tab.setColumnFilter(2, ('in', frozenset({b'a'})))
    waitFor(app, lambda: tab.filterScan.done)
    tab.tick()
    expect = [i for i, r in enumerate(rows) if r[1] not in ('Toronto', '') and r[2] == 'a']
    assert visibleIds(tab) == expect
    scan = tab.valueScan(2)
    waitFor(app, lambda: scan.done)
    assert sum(scan.values.values()) == sum(1 for r in rows if r[1] not in ('Toronto', ''))

    # Clicking the ▾ button on the header opens the popup; the search box narrows the list, OK keeps those values
    right = header.sectionViewportPosition(2) + header.sectionSize(2)
    click(header, right - FILTER_BUTTON // 2 - 2)
    popup = popups()[0]
    assert popup.col == 2
    waitFor(app, lambda: popup.scan.done)
    popup.rebuild()
    popup.searchBox.setText('b')
    assert [i.data(Qt.ItemDataRole.UserRole) for i in popup.visibleItems()] == [b'b']
    popup.accept()
    assert tab.filters[2] == ('in', frozenset({b'b'}))
    waitFor(app, lambda: tab.filterScan.done)
    tab.tick()
    assert visibleIds(tab) == [i for i, r in enumerate(rows) if r[1] not in ('Toronto', '') and r[2] == 'b']

    # Search inside the filtered view: F3 only visits hits the filters keep
    tab.searchBox.setText('calgary')
    tab.columnBox.setCurrentIndex(2)
    tab.startSearch()
    waitFor(app, lambda: tab.search.done)
    kept = set(visibleIds(tab))
    hits = [h for h in tab.search.hits if h in kept]
    assert hits and len(hits) < len(tab.search.hits)
    tab.jumpTo(0)
    seen = []
    for _ in range(3):
        tab.nextMatch()
        seen.append(tab.currentDataRow())
    assert seen == [h for h in hits if h > visibleIds(tab)[0]][:3], (seen, hits[:4])
    tab.prevMatch()
    assert tab.currentDataRow() == seen[1]

    # Only matches with filters shows the intersection
    tab.onlyMatches.setChecked(True)
    tab.tick()
    assert visibleIds(tab) == hits
    tab.onlyMatches.setChecked(False)

    # Cell menu: "Filter to" and "Hide rows with" this value
    tab.setFilterFromCell(3, ('in', frozenset({b'7'})))
    waitFor(app, lambda: tab.filterScan.done)
    tab.tick()
    assert all(rows[i][3] == '7' for i in visibleIds(tab))

    # Clear filters keeps the dropdowns but shows every row; removing a dropdown drops its button
    tab.clearFilters()
    assert tab.filterScan is None and tab.model.rowMap is None and tab.model.rowCount() == N
    assert header.filterState(1) is False and not tab.clearFiltersBtn.isVisible()
    tab.removeColumnFilter(1)
    assert header.filterState(1) is None

    # Go to row lands on the nearest visible row when filtered
    tab.setColumnFilter(2, ('in', frozenset({b'c'})))
    waitFor(app, lambda: tab.filterScan.done)
    tab.tick()
    target = 100
    tab.jumpToDataRow(target)
    assert tab.currentDataRow() == min(i for i, r in enumerate(rows) if r[2] == 'c' and i >= target)

    # Export writes the filtered rows
    from csvviewer.index import Export
    out = os.path.join(TMP, 'out.csv')
    Export(tab.index, tab.model.rowMap, out).run()
    with open(out, newline='', encoding='utf-8') as f:
        exported = list(csv.reader(f))
    assert exported[1:] == [r for r in rows if r[2] == 'c']
    if shots:
        window.grab().save(os.path.join(shots, 'filtered.png'))

    # Reopening the file drops the filters
    tab.openFile(path)
    waitFor(app, lambda: tab.index.done and tab.columnsReady)
    assert tab.filters == {} and tab.model.filterCols == {} and not tab.filterLabel.isVisible()
    window.close()
    print('all filter tests passed')


if __name__ == '__main__':
    main()
