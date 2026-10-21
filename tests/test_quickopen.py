"""Go to file (Ctrl+P): folder scan, fuzzy ranking and the popup in a real MainWindow.
Run: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_quickopen.py [screenshotDir]"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
TMP = tempfile.mkdtemp(prefix='quickopen')
# Keep the user's real settings untouched
for var in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
    os.environ.setdefault(var, os.path.join(TMP, var.lower()))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QKeyEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from csvviewer.quickopen import FileScan, PathRole, fuzzyScore, match  # noqa: E402

FILES = ['sales_q3_2024.csv', 'sales/q3/summary.csv', 'sales/q4/summary.csv', 'assets/asset_ids.csv',
         'assets/archive/old_assets.tsv', 'notes.txt', 'readme.md', 'big data/SalesQuarter3.csv',
         '.hidden/secret.csv', 'node_modules/pkg/data.csv', 'sq.csv', 'misc/s_x_q.csv']


def waitFor(app, cond, timeout=20):
    end = time.monotonic() + timeout
    while not cond():
        app.processEvents()
        if time.monotonic() > end:
            raise AssertionError('timed out')
        time.sleep(0.01)


def same(a, b):
    # Temp folders can be reached through symlinks (macOS /var -> /private/var)
    return a is not None and b is not None and \
        os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def key(widget, k, text=''):
    for kind in (QKeyEvent.Type.KeyPress, QKeyEvent.Type.KeyRelease):
        QApplication.sendEvent(widget, QKeyEvent(kind, k, Qt.KeyboardModifier.NoModifier, text))


def ranked(query, files):
    hits = [(match(query, f), f) for f in files]
    return [f for m, f in sorted(((m, f) for m, f in hits if m), key=lambda x: (-x[0][0], len(x[1])))]


def testMatching():
    assert fuzzyScore('sq', 'sales_q3') and fuzzyScore('xyz', 'sales') is None
    # Consecutive letters and word starts beat scattered ones
    assert fuzzyScore('sq', 'sq.csv')[0] > fuzzyScore('sq', 'sales_q3_2024.csv')[0] > fuzzyScore('sq', 'misc_s_x_q.csv')[0] - 99
    assert fuzzyScore('sq', 'sales_q3_2024.csv')[1] == [0, 6]  # the q of "_q3", not a later one
    # Found exactly when the letters appear in order, even where a word-start jump would skip needed letters
    import random
    from csvviewer.quickopen import subsequence
    rng = random.Random(3)
    for _ in range(20000):
        text = ''.join(rng.choice('ab_c.') for _ in range(rng.randint(1, 12)))
        query = ''.join(rng.choice('abc') for _ in range(rng.randint(1, 4)))
        assert (fuzzyScore(query, text) is not None) == bool(subsequence(query).search(text)), (query, text)
    names = [f.replace('/', os.sep) for f in FILES]
    order = ranked('salq3', names)
    assert order[0] == 'sales_q3_2024.csv', order
    # Space-separated parts: each must match; a folder part narrows by path
    assert ranked('q4 summ', names) == [os.path.join('sales', 'q4', 'summary.csv')]
    assert ranked('assets ids', names)[0] == os.path.join('assets', 'asset_ids.csv')
    m = match('ids', os.path.join('assets', 'asset_ids.csv'))
    assert m[1] == [6, 7, 8] and m[2] == []  # marks in the name
    m = match('arch', os.path.join('assets', 'archive', 'old_assets.tsv'))
    assert m and m[2]  # only the folder matched
    print('matching ok')


def testScan():
    for f in FILES:
        path = os.path.join(TMP, 'root', f)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as out:
            out.write('a,b\n1,2\n')
    s = FileScan(os.path.join(TMP, 'root'), csvOnly=True).start()
    s.thread.join()
    got = sorted(s.files)
    want = sorted(f.replace('/', os.sep) for f in FILES
                  if not f.startswith(('.', 'node_modules')) and f.endswith(('.csv', '.tsv', '.txt')))  # the Explorer's CSV types
    assert got == want, got
    s = FileScan(os.path.join(TMP, 'root'), csvOnly=False).start()
    s.thread.join()
    assert 'readme.md' in s.files and 'notes.txt' in s.files and not s.capped
    print('scan ok')


def testPopup(shots):
    from csvviewer.app import MainWindow
    app = QApplication.instance() or QApplication(sys.argv)
    root = os.path.join(TMP, 'root')
    window = MainWindow()
    window.resize(1200, 760)
    window.show()
    window.explorer.setRoot(root)
    window.openPath(os.path.join(root, 'notes.txt'))  # an open tab and a recent file
    window.quickOpen()
    pop = window.quickOpenPopup
    assert pop.isVisible() and pop.box.hasFocus() or QApplication.focusWidget() is pop.box
    # Empty: open tabs (and recent files) only
    waitFor(app, lambda: pop.scan.done)
    pop.refresh()
    items = [pop.list.item(i).data(PathRole) for i in range(pop.list.count())]
    assert len(items) == 1 and same(items[0], os.path.join(root, 'notes.txt')), items
    assert 'type to search' in pop.status.text()
    # Typing ranks fuzzy matches; arrows move, Enter opens
    pop.box.setText('salq3')
    pop.refresh()
    first = pop.list.item(0).data(PathRole)
    assert same(first, os.path.join(root, 'sales_q3_2024.csv')), first
    assert 'match' in pop.status.text() and 'CSV files' in pop.status.text(), pop.status.text()
    if shots:
        pop.grab().save(os.path.join(shots, 'quick-open.png'))
    key(pop.box, Qt.Key.Key_Down)
    second = pop.list.currentItem().data(PathRole)
    key(pop.box, Qt.Key.Key_Return)
    assert not pop.isVisible() and same(window.current().index.path, second)
    assert same(window.recentFiles()[0], second)
    # Again: the opened file is tagged open and ranks first for a matching query
    window.quickOpen()
    pop.box.setText('q3')
    pop.refresh()
    assert same(pop.list.item(0).data(PathRole), second), [pop.list.item(i).data(PathRole) for i in range(3)]
    # Escape closes; a full path to a file outside the folder works too
    key(pop.box, Qt.Key.Key_Escape)
    assert not pop.isVisible()
    outside = os.path.join(TMP, 'elsewhere.csv')
    with open(outside, 'w') as f:
        f.write('x\n1\n')
    window.quickOpen()
    pop.box.setText(outside)
    pop.refresh()
    assert same(pop.list.item(0).data(PathRole), outside)
    key(pop.box, Qt.Key.Key_Return)
    assert same(window.current().index.path, outside)
    # Nothing matches: Enter does nothing
    window.quickOpen()
    pop.box.setText('zzzqqq')
    key(pop.box, Qt.Key.Key_Return)
    assert pop.list.count() == 0 and pop.isVisible() and '0 matches' in pop.status.text()
    pop.close()
    window.close()
    print('popup ok')


def main():
    shots = sys.argv[1] if len(sys.argv) > 1 else None
    testMatching()
    testScan()
    testPopup(shots)
    print('all quick open tests passed')


if __name__ == '__main__':
    main()
