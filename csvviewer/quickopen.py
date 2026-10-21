"""Go to file (Ctrl+P), like VS Code's quick open: fuzzy-find files under the Explorer folder and open them."""
import os
import re
import threading
import time

from PyQt6.QtCore import QRect, QSize, Qt, QTimer
from PyQt6.QtGui import QPalette, QTextDocument
from PyQt6.QtWidgets import (
    QApplication, QFrame, QLabel, QLineEdit, QListWidget, QListWidgetItem, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QVBoxLayout,
)

from .explorer import CSV_EXTENSIONS

MAX_FILES = 200_000  # a folder scan stops collecting past this
MAX_SHOWN = 200
SCAN_TTL = 60  # seconds a finished scan of the same folder is reused before scanning again
SKIP_DIRS = {'node_modules', '__pycache__', 'venv', '.venv', 'site-packages', '$recycle.bin', 'system volume information'}
WORD_START = set('/\\_-. ')
PathRole = Qt.ItemDataRole.UserRole
MarksRole = Qt.ItemDataRole.UserRole + 1


class FileScan:
    """Walks a folder in the background; `files` (paths relative to root) grows while it runs."""

    def __init__(self, root, csvOnly):
        self.root, self.csvOnly = root, csvOnly
        self.files = []
        self.capped = False
        self.done = False
        self.cancelled = False
        self.finished = 0.0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def cancel(self):
        self.cancelled = True

    def run(self):
        exts = {'.' + e for e in CSV_EXTENSIONS}
        stack = ['']
        while stack and not self.cancelled:
            rel = stack.pop()
            try:
                entries = sorted(os.scandir(os.path.join(self.root, rel)), key=lambda e: e.name.lower())
            except OSError:
                continue
            dirs = []
            for e in entries:
                if e.name.startswith('.'):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name.lower() not in SKIP_DIRS:
                            dirs.append(os.path.join(rel, e.name))
                    elif not self.csvOnly or os.path.splitext(e.name)[1].lower() in exts:
                        self.files.append(os.path.join(rel, e.name))
                except OSError:
                    continue
            if len(self.files) >= MAX_FILES:
                self.capped = True
                break
            stack.extend(reversed(dirs))  # depth-first, in name order
        self.finished = time.monotonic()
        self.done = True


def subsequence(query):
    """Regex finding the query's characters in order (case-insensitive input expected)."""
    return re.compile('.*?'.join(re.escape(c) for c in query))


def fuzzyScore(query, text):
    """(score, positions) of query's characters found in order in text (both lower-case), or None.
    Like VS Code: runs of consecutive characters and starts of words count most, gaps cost a little."""
    if not query:
        return 0, []

    def walk(first, words):
        # Positions from a first one on; words: jump to a later word start for the next character when there is one
        positions, pos = [first], first + 1
        for c in query[1:]:
            nxt = text.find(c, pos)
            if nxt < 0:
                return None
            if words and nxt != pos:
                for j in range(nxt, len(text)):
                    if text[j] == c and text[j - 1] in WORD_START:
                        nxt = j
                        break
            positions.append(nxt)
            pos = nxt + 1
        return positions

    def score(positions):
        total, prev = 0, -2
        for p in positions:
            total += 16 if p == prev + 1 else (8 if p == 0 or text[p - 1] in WORD_START else 1)
            total -= min(5, max(0, p - prev - 1)) if prev >= 0 else min(5, p)
            prev = p
        return total

    best = None
    first = text.find(query[0])
    for _ in range(8):  # a few starting points, so "sq" prefers sales_q3 over s...q
        if first < 0:
            break
        for words in (True, False):  # the word-start walk can skip what later characters need; plain order cannot
            positions = walk(first, words)
            if positions and (best is None or score(positions) > best[0]):
                best = (score(positions), positions)
        first = text.find(query[0], first + 1)
    return best


def match(query, rel):
    """(score, name marks, folder marks) for a file's relative path, or None. Each space-separated part of the
    query must match; a part matching the file name ranks far above one that only matches the folders."""
    low = rel.lower()
    cut = max(low.rfind('/'), low.rfind('\\')) + 1
    name, folder = low[cut:], low[:cut]
    total, nameMarks, folderMarks = 0, set(), set()
    for part in query.split():
        hit = fuzzyScore(part, name)
        if hit:
            total += 100 + hit[0] - len(name) // 4
            nameMarks.update(hit[1])
            continue
        hit = fuzzyScore(part, low)
        if not hit:
            return None
        total += hit[0] - len(low) // 8
        for p in hit[1]:
            (nameMarks.add(p - cut) if p >= cut else folderMarks.add(p))
    return total, sorted(nameMarks), sorted(folderMarks)


def marked(text, marks):
    out = []
    for i, ch in enumerate(text):
        ch = ch.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        out.append(f'<b><u>{ch}</u></b>' if i in marks else ch)
    return ''.join(out)


class ResultDelegate(QStyledItemDelegate):
    """Two lines per file: the name with the matched letters marked, then its folder, muted."""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ''
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        pal = opt.palette
        color = pal.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text).name()
        muted = pal.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.PlaceholderText).name()
        name, folder, tag = text.split('\0')
        nameMarks, folderMarks = index.data(MarksRole) or ((), ())
        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        doc.setDocumentMargin(2)
        doc.setHtml(f'<div style="color:{color}">{marked(name, set(nameMarks))}'
                    f'<span style="color:{muted}">{"  —  " + tag if tag else ""}</span></div>'
                    f'<div style="color:{muted}; font-size:small">{marked(folder, set(folderMarks))}</div>')
        painter.save()
        painter.translate(opt.rect.left() + 6, opt.rect.top())
        painter.setClipRect(QRect(0, 0, opt.rect.width() - 8, opt.rect.height()))
        doc.drawContents(painter)
        painter.restore()

    def sizeHint(self, option, index):
        h = option.fontMetrics.height()
        return QSize(200, h * 2 + 8)


class QuickOpen(QFrame):
    """The Ctrl+P popup over the main window."""

    def __init__(self, window):
        super().__init__(window, Qt.WindowType.Popup)
        self.main = window
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.scan = None
        self.shownCount = 0
        self.candidateCount = 0
        self.narrowing = (None, None)  # (query, (candidate count, candidates it matched))
        self.root = ''
        self.box = QLineEdit(placeholderText='Search files by name (fuzzy, like sls q3)…')
        self.box.setClearButtonEnabled(True)
        self.box.textChanged.connect(lambda _: self.timer.start())
        self.box.installEventFilter(self)
        self.list = QListWidget()
        self.list.setItemDelegate(ResultDelegate(self.list))
        self.list.setUniformItemSizes(True)
        self.list.itemActivated.connect(self.openItem)
        self.list.itemClicked.connect(self.openItem)
        self.status = QLabel('')
        self.status.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addWidget(self.box)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.status)
        self.timer = QTimer(self, singleShot=True, interval=60, timeout=self.refresh)
        self.poll = QTimer(self, interval=300, timeout=self.onPoll)

    # ---------- showing ----------

    def popup(self):
        explorer = self.main.explorer
        root = explorer.currentRoot()
        csvOnly = explorer.csvOnly.isChecked()
        s = self.scan
        fresh = s and s.root == root and s.csvOnly == csvOnly and (not s.done or time.monotonic() - s.finished < SCAN_TTL)
        if not fresh and root and os.path.isdir(root):
            if s:
                s.cancel()
            self.scan = FileScan(root, csvOnly).start()
        self.root = root
        w = self.main
        width = min(680, max(420, w.width() // 2))
        height = min(520, max(300, w.height() * 2 // 3))
        top = w.mapToGlobal(w.rect().topLeft())
        self.setGeometry(top.x() + (w.width() - width) // 2, top.y() + 60, width, height)
        self.box.clear()
        self.refresh()
        self.show()
        self.box.setFocus()
        self.poll.start()

    def hideEvent(self, event):
        self.poll.stop()
        super().hideEvent(event)

    def onPoll(self):
        # New files keep arriving while the folder is scanned
        if self.scan and (not self.scan.done or self.shownCount != len(self.scan.files)):
            self.refresh()
        if not self.scan or self.scan.done:
            self.poll.stop()

    # ---------- results ----------

    def openTabs(self):
        out = []
        for i in range(self.main.tabs.count()):
            tab = self.main.tabs.widget(i)
            if tab.index:
                out.append(os.path.abspath(tab.index.path))
        return out

    def candidates(self):
        """[(display path, absolute path, tag)]: open tabs, recent files, then the folder scan."""
        seen, out = set(), []

        def add(rel, path, tag):
            key = os.path.normcase(path)
            if key not in seen:
                seen.add(key)
                out.append((rel, path, tag))
        for path in self.openTabs():
            add(self.display(path), path, 'open')
        for path in self.main.recentFiles():
            if os.path.isfile(path):
                add(self.display(path), path, 'recent')
        if self.scan:
            files = self.scan.files
            self.shownCount = len(files)
            for rel in files[:]:  # a snapshot: the scan thread keeps appending
                add(rel, os.path.join(self.scan.root, rel), '')
        else:
            self.shownCount = 0
        self.candidateCount = len(out)
        return out

    def display(self, path):
        # Inside the Explorer folder: relative, like the scan; elsewhere the full path
        root = self.root
        try:
            if root and os.path.normcase(os.path.commonpath([root, path])) == os.path.normcase(root):
                return os.path.relpath(path, root)
        except ValueError:
            pass
        return path

    def refresh(self):
        self.timer.stop()  # this is the refresh a keystroke was waiting for
        query = self.box.text().strip().lower()
        items = self.candidates()
        # Typing on: only what the shorter query matched can match the longer one (same file list)
        last, survivors = self.narrowing
        if last and query.startswith(last) and ' ' not in query[len(last) - 1:] and survivors[0] == len(items):
            items = survivors[1]
        if query:
            # A quick subsequence test drops most files before scoring
            parts = query.split()
            tests = [subsequence(p) for p in parts]
            scored = []
            for rel, path, tag in items:
                low = rel.lower()
                if all(t.search(low) for t in tests):
                    m = match(query, rel)
                    if m:
                        scored.append((-m[0] - (50 if tag else 0), len(rel), rel, path, tag, m[1], m[2]))
            self.narrowing = (query, (self.candidateCount, [(rel, path, tag) for _, _, rel, path, tag, _, _ in scored]))
            scored.sort()
            rows = [(rel, path, tag, nm, fm) for _, _, rel, path, tag, nm, fm in scored[:MAX_SHOWN]]
            found = len(scored)
        else:
            rows = [(rel, path, tag, (), ()) for rel, path, tag in items if tag][:MAX_SHOWN]
            found = None
            self.narrowing = (None, None)
        expanded = os.path.expanduser(self.box.text().strip())
        if os.path.isabs(expanded) and os.path.isfile(expanded) and not any(r[1] == expanded for r in rows):
            rows.insert(0, (expanded, expanded, 'path', (), ()))
        self.list.setUpdatesEnabled(False)
        self.list.clear()
        for rel, path, tag, nameMarks, folderMarks in rows:
            cut = max(rel.rfind('/'), rel.rfind('\\')) + 1
            item = QListWidgetItem(f"{rel[cut:]}\0{rel[:cut].rstrip('/' + os.sep)}\0{tag}")
            item.setData(PathRole, path)
            item.setData(MarksRole, (list(nameMarks), list(folderMarks)))
            item.setToolTip(path)
            self.list.addItem(item)
        self.list.setUpdatesEnabled(True)
        if self.list.count():
            self.list.setCurrentRow(0)
        self.updateStatus(found)

    def updateStatus(self, found):
        s = self.scan
        where = f'in {self.root}' if self.root else ''
        if s is None:
            total = 'Pick a folder in the Explorer panel to search its files'
        else:
            total = f'{len(s.files):,} {"CSV " if s.csvOnly else ""}files {where}'
            if not s.done:
                total = f'Searching… {total}'
            elif s.capped:
                total += f' (stopped at {MAX_FILES:,})'
        if found is None:
            text = f'Open and recent files · type to search {len(s.files) if s else 0:,} files {where}'.strip()
        else:
            text = f'{found:,} match{"es" if found != 1 else ""}' + (f' (first {MAX_SHOWN})' if found > MAX_SHOWN else '') + \
                   f' · {total}'
        self.status.setText(text)

    # ---------- keys ----------

    def eventFilter(self, obj, event):
        if obj is self.box and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                step = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1, Qt.Key.Key_PageDown: 10, Qt.Key.Key_PageUp: -10}[key]
                if self.list.count():
                    self.list.setCurrentRow(max(0, min(self.list.count() - 1, self.list.currentRow() + step)))
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self.timer.isActive():  # typed just now: match what is in the box, not the previous list
                    self.timer.stop()
                    self.refresh()
                item = self.list.currentItem()
                if item:
                    self.openItem(item)
                return True
            if key == Qt.Key.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, event)

    def openItem(self, item):
        path = item.data(PathRole)
        self.close()
        if path:
            self.main.openPath(path)
