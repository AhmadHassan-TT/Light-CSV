"""CSV Viewer: a tabbed, read-only viewer for very large CSV files (tested on 11 GB)."""
import os
import sys
import threading
from bisect import bisect_left, bisect_right

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QActionGroup, QColor, QIcon, QKeySequence, QPalette, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDockWidget,
    QFileDialog, QFormLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QToolButton,
    QHBoxLayout, QSpinBox, QSplitter, QStackedWidget, QTabWidget, QTableView, QVBoxLayout, QWidget,
)

from .explorer import Explorer
from .index import CsvIndex, Export, Search

ROW_HEIGHT = 22
MAX_CELL_CHARS = 500
MAX_COPY_CELLS = 200_000
# Qt measures the table in 32-bit pixels, so one view holds at most ~97M rows; bigger files are shown in segments
SEGMENT = 50_000_000
DELIMITERS = {'Auto': None, 'Comma ,': ',', 'Semicolon ;': ';', 'Tab': '\t', 'Pipe |': '|'}
ENCODINGS = ['Auto', 'utf-8', 'cp1252', 'latin-1']
THEMES = {'system': '&System', 'light': '&Light', 'dark': '&Dark'}


def assetPath(name):
    # Assets sit next to the package in a checkout, or in the PyInstaller bundle dir
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, 'assets', name)


# Window, WindowText, Base, AlternateBase, Text, Button, ButtonText, Mid, Highlight, HighlightedText, disabled text
PALETTES = {
    'light': ('#f0f0f0', '#1a1a1a', '#ffffff', '#f5f5f5', '#1a1a1a', '#e6e6e6', '#1a1a1a', '#b4b4b4',
              '#3070d8', '#ffffff', '#a0a0a0'),
    'dark': ('#2b2b2b', '#e0e0e0', '#1e1e1e', '#262626', '#e0e0e0', '#333333', '#e0e0e0', '#555555',
             '#2f65ca', '#ffffff', '#7a7a7a'),
}


def themePalette(theme):
    window, windowText, base, altBase, text, button, buttonText, mid, highlight, highlightText, disabled =         PALETTES[theme]
    Role = QPalette.ColorRole
    palette = QPalette(QColor(button), QColor(window))  # derives Light / Midlight / Dark / Shadow from these
    for role, color in ((Role.WindowText, windowText), (Role.Base, base), (Role.AlternateBase, altBase),
                        (Role.Text, text), (Role.ButtonText, buttonText), (Role.Mid, mid),
                        (Role.Highlight, highlight), (Role.HighlightedText, highlightText),
                        (Role.ToolTipBase, base), (Role.ToolTipText, text), (Role.PlaceholderText, disabled),
                        (Role.Link, highlight)):
        palette.setColor(role, QColor(color))
    for role in (Role.WindowText, Role.Text, Role.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(disabled))
    return palette


def applyTheme(theme, systemStyle):
    # Light and dark use Fusion with an explicit palette: the native Windows 10 style cannot draw dark
    app = QApplication.instance()
    hints = app.styleHints()
    if hasattr(hints, 'setColorScheme'):  # Qt 6.8+: also themes native parts such as the title bar
        hints.setColorScheme({'light': Qt.ColorScheme.Light, 'dark': Qt.ColorScheme.Dark}.get(theme, Qt.ColorScheme.Unknown))
    if theme == 'system':
        app.setStyle(systemStyle)
        app.setPalette(QPalette())
    else:
        app.setStyle('Fusion')
        app.setPalette(themePalette(theme))


def samePath(a, b):
    # Windows paths are case-insensitive
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def humanSize(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f'{n:.1f} {unit}' if unit != 'B' else f'{n} B'
        n /= 1024
    return f'{n:.1f} PB'


class CsvModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.csv = None
        self.columns = []
        self.rows = 0
        self.base = 0  # first virtual row of the segment on screen
        self.rowMap = None  # search hits when "only matches" is on

    def load(self, csv):
        self.beginResetModel()
        self.csv = csv
        self.rowMap = None
        self.rows = 0
        self.base = 0
        self.columns = []
        self.endResetModel()

    def setColumns(self, columns):
        self.beginResetModel()
        self.columns = columns
        self.endResetModel()

    def setRowMap(self, rowMap):
        self.beginResetModel()
        self.rowMap = rowMap
        self.base = 0
        self.rows = self.segmentRows()
        self.endResetModel()

    def setBase(self, base):
        self.beginResetModel()
        self.base = base
        self.rows = self.segmentRows()
        self.endResetModel()

    def virtualTotal(self):
        if not self.csv:
            return 0
        return len(self.rowMap) if self.rowMap is not None else self.csv.dataRows

    def segmentRows(self):
        return max(0, min(SEGMENT, self.virtualTotal() - self.base))

    def sync(self):
        """Grow the row count as indexing or searching finds more rows."""
        if not self.csv:
            return
        total = self.segmentRows()
        if total > self.rows:
            self.beginInsertRows(QModelIndex(), self.rows, total - 1)
            self.rows = total
            self.endInsertRows()

    def virtualRow(self, viewRow):
        return self.base + viewRow

    def dataRow(self, viewRow):
        return self.rowMap[self.base + viewRow] if self.rowMap is not None else self.base + viewRow

    def fileRow(self, viewRow):
        return self.dataRow(viewRow) + (1 if self.csv.hasHeader else 0)

    def cell(self, viewRow, col):
        row = self.csv.row(self.fileRow(viewRow))
        return row[col] if col < len(row) else ''

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.rows

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def data(self, idx, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            value = self.cell(idx.row(), idx.column())
            if len(value) > MAX_CELL_CHARS:
                value = value[:MAX_CELL_CHARS] + '…'
            return value.replace('\r\n', ' ⏎ ').replace('\n', ' ⏎ ')
        if role == Qt.ItemDataRole.ToolTipRole:
            value = self.cell(idx.row(), idx.column())
            return value[:2000] if len(value) > 40 or '\n' in value else None
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.columns[section] if section < len(self.columns) else None
        return f'{self.dataRow(section) + 1:,}'


class FrozenTable(QTableView):
    """Table whose first N columns stay pinned on the left while scrolling sideways."""

    def __init__(self, model):
        super().__init__()
        self.frozenCount = 0
        self.setModel(model)
        self.frozen = QTableView(self)
        self.frozen.setModel(model)
        self.frozen.setSelectionModel(self.selectionModel())
        self.frozen.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.frozen.verticalHeader().hide()
        self.frozen.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.frozen.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.frozen.setStyleSheet('QTableView { border: none; border-right: 2px solid palette(mid); }')
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.frozen.horizontalHeader().sectionResized.connect(lambda col, _old, size: self.setColumnWidth(col, size))
        for view in (self, self.frozen):
            view.setWordWrap(False)
            view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            view.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
            view.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
            view.horizontalHeader().setHighlightSections(False)
        self.verticalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.viewport().stackUnder(self.frozen)
        self.frozen.verticalScrollBar().valueChanged.connect(self.verticalScrollBar().setValue)
        self.verticalScrollBar().valueChanged.connect(self.frozen.verticalScrollBar().setValue)
        self.horizontalHeader().sectionResized.connect(self.onSectionResized)
        self.frozen.doubleClicked.connect(self.doubleClicked.emit)
        model.modelReset.connect(self.refreshFrozen)
        self.frozen.hide()

    def setFrozenCount(self, n):
        self.frozenCount = max(0, min(n, self.model().columnCount()))
        self.refreshFrozen()

    def refreshFrozen(self):
        n = self.frozenCount
        for col in range(self.model().columnCount()):
            self.frozen.setColumnHidden(col, col >= n or self.isColumnHidden(col))
            if col < n:
                self.frozen.setColumnWidth(col, self.columnWidth(col))
        self.frozen.setVisible(n > 0)
        self.updateFrozenGeometry()

    def onSectionResized(self, col, _old, size):
        if col < self.frozenCount:
            if self.frozen.columnWidth(col) != size:
                self.frozen.setColumnWidth(col, size)
            self.updateFrozenGeometry()

    def updateFrozenGeometry(self):
        width = sum(self.columnWidth(c) for c in range(self.frozenCount) if not self.isColumnHidden(c))
        self.frozen.setGeometry(self.verticalHeader().width() + self.frameWidth(), self.frameWidth(),
                                width, self.viewport().height() + self.horizontalHeader().height())

    def setColumnHidden(self, col, hide):
        super().setColumnHidden(col, hide)
        if col < self.frozenCount:
            self.refreshFrozen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.updateFrozenGeometry()

    def updateGeometries(self):
        super().updateGeometries()
        if hasattr(self, 'frozen'):
            self.updateFrozenGeometry()

    def scrollTo(self, idx, hint=QAbstractItemView.ScrollHint.EnsureVisible):
        # Pinned columns never scroll sideways, so only the row position matters for them
        if idx.column() < self.frozenCount:
            scrollX = self.horizontalScrollBar().value()
            super().scrollTo(idx, hint)
            self.horizontalScrollBar().setValue(scrollX)
        else:
            super().scrollTo(idx, hint)


class OpenOptions(QDialog):
    def __init__(self, parent, current=None):
        super().__init__(parent)
        self.setWindowTitle('Open options')
        form = QFormLayout(self)
        self.delimiter = QComboBox()
        self.delimiter.addItems(DELIMITERS)
        self.encoding = QComboBox()
        self.encoding.addItems(ENCODINGS)
        self.header = QCheckBox('First row is the header')
        self.header.setChecked(True)
        self.quotes = QCheckBox('Line breaks inside "quoted" values stay in the cell')
        self.quotes.setChecked(True)
        self.quotes.setToolTip('Turn off for files with stray quote characters, where rows look merged.')
        if current:
            name = next((k for k, v in DELIMITERS.items() if v == current.delimiter), 'Auto')
            self.delimiter.setCurrentText(name)
            self.encoding.setCurrentText(current.encoding)
            self.header.setChecked(current.hasHeader)
            self.quotes.setChecked(current.quoteAware)
        form.addRow('Delimiter', self.delimiter)
        form.addRow('Encoding', self.encoding)
        form.addRow(self.header)
        form.addRow(self.quotes)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        enc = self.encoding.currentText()
        return dict(delimiter=DELIMITERS[self.delimiter.currentText()], encoding=None if enc == 'Auto' else enc,
                    hasHeader=self.header.isChecked(), quoteAware=self.quotes.isChecked())


class ColumnsDialog(QDialog):
    """Show/hide columns, with a filter box for wide files."""

    def __init__(self, parent, table, columns):
        super().__init__(parent)
        self.setWindowTitle('Columns')
        self.resize(360, 480)
        layout = QVBoxLayout(self)
        self.filter = QLineEdit(placeholderText='Filter columns…')
        self.list = QListWidget()
        for c, name in enumerate(columns):
            item = QListWidgetItem(f'{c + 1}. {name}')
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if table.isColumnHidden(c) else Qt.CheckState.Checked)
            self.list.addItem(item)
        self.filter.textChanged.connect(self.applyFilter)
        allBtn, noneBtn = QPushButton('Check all shown'), QPushButton('Uncheck all shown')
        allBtn.clicked.connect(lambda: self.setAll(Qt.CheckState.Checked))
        noneBtn.clicked.connect(lambda: self.setAll(Qt.CheckState.Unchecked))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        for w in (self.filter, self.list, allBtn, noneBtn, buttons):
            layout.addWidget(w)

    def applyFilter(self, text):
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(text.lower() not in item.text().lower())

    def setAll(self, state):
        for i in range(self.list.count()):
            if not self.list.item(i).isHidden():
                self.list.item(i).setCheckState(state)

    def visibleColumns(self):
        return [self.list.item(i).checkState() == Qt.CheckState.Checked for i in range(self.list.count())]


class CsvTab(QWidget):
    """One open file: its own index, table, search and detail pane."""

    titleChanged = pyqtSignal()

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.index = None
        self.search = None
        self.export = None
        self.indexThread = None
        self.lastQuery = None
        self.columnsReady = False
        self.firstJumpPending = False
        self.model = CsvModel()
        self.table = FrozenTable(self.model)
        self.buildSearchBar()
        self.detail = QPlainTextEdit(readOnly=True)
        self.detailTitle = QLabel('Cell detail')
        detailBox = QWidget()
        detailLayout = QVBoxLayout(detailBox)
        detailLayout.setContentsMargins(0, 4, 0, 0)
        detailLayout.addWidget(self.detailTitle)
        detailLayout.addWidget(self.detail)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(detailBox)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setSizes([700, 160])
        detailBox.hide()
        self.detailBox = detailBox
        self.infoLabel = QLabel('')
        self.infoLabel.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.cellLabel = QLabel('')
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setFormat('Indexing %p%')
        self.progress.hide()
        info = QHBoxLayout()
        info.setContentsMargins(6, 0, 6, 2)
        info.addWidget(self.infoLabel, 1)
        self.segmentBox = QComboBox()
        self.segmentBox.setToolTip('Files over 50 million rows are shown 50 million rows at a time')
        self.segmentBox.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.segmentBox.activated.connect(lambda i: self.showSegment(i * SEGMENT))
        self.segmentBox.hide()
        info.addWidget(self.segmentBox)
        info.addWidget(self.cellLabel)
        info.addWidget(self.progress)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(self.searchBar)
        layout.addWidget(self.splitter, 1)
        layout.addLayout(info)
        self.table.selectionModel().currentChanged.connect(self.onCurrentChanged)
        self.table.doubleClicked.connect(lambda _: self.setDetailVisible(True))
        hdr = self.table.horizontalHeader()
        hdr.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hdr.customContextMenuRequested.connect(self.headerMenu)

    def buildSearchBar(self):
        bar = self.searchBar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 0)
        self.searchBox = QLineEdit(placeholderText='Search the whole file…  (Enter = find / next, Shift+Enter = previous)')
        self.searchBox.setClearButtonEnabled(True)
        self.searchBox.setMinimumWidth(300)
        self.searchBox.returnPressed.connect(self.onSearchEnter)
        prev = QShortcut(QKeySequence('Shift+Return'), self.searchBox)
        prev.setContext(Qt.ShortcutContext.WidgetShortcut)
        prev.activated.connect(self.prevMatch)
        bar.addWidget(self.searchBox, 1)
        self.columnBox = QComboBox()
        self.columnBox.setMinimumWidth(150)
        self.columnBox.setMaxVisibleItems(25)
        self.columnBox.setToolTip('Search in one column only')
        bar.addWidget(self.columnBox)
        self.caseBox = QCheckBox('Match case')
        bar.addWidget(self.caseBox)
        for text, slot, tip in (('Find all', self.startSearch, 'Scan the whole file'), ('◀', self.prevMatch, 'Previous match (Shift+F3)'),
                                ('▶', self.nextMatch, 'Next match (F3)')):
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            if len(text) == 1:
                btn.setFixedWidth(34)
            bar.addWidget(btn)
        self.stopBtn = QPushButton('Stop')
        self.stopBtn.clicked.connect(self.stopSearch)
        self.stopBtn.setEnabled(False)
        bar.addWidget(self.stopBtn)
        self.onlyMatches = QCheckBox('Only matches')
        self.onlyMatches.setToolTip('Show only the rows that match the search')
        self.onlyMatches.toggled.connect(self.applyFilter)
        bar.addWidget(self.onlyMatches)
        clearBtn = QPushButton('Clear')
        clearBtn.clicked.connect(self.clearSearch)
        bar.addWidget(clearBtn)
        self.matchLabel = QLabel('')
        self.matchLabel.setMinimumWidth(190)
        bar.addWidget(self.matchLabel)
        bar.addWidget(QLabel('Freeze'))
        self.freezeSpin = QSpinBox()
        self.freezeSpin.setRange(0, 0)
        self.freezeSpin.setSuffix(' col')
        self.freezeSpin.setToolTip('Pin the first N columns on the left')
        self.freezeSpin.valueChanged.connect(self.table.setFrozenCount)
        bar.addWidget(self.freezeSpin)

    def title(self):
        if not self.index:
            return 'Empty'
        name = os.path.basename(self.index.path)
        return name if self.index.done else f'{name} ({self.index.progress:.0%})'

    def setDetailVisible(self, on):
        self.detailBox.setVisible(on)
        self.window.detailAct.setChecked(on)

    # ---------- opening ----------

    def openFile(self, path, **options):
        self.closeFile()
        try:
            index = CsvIndex(path, **options)
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, 'Cannot open file', f'{path}\n\n{e}')
            return False
        self.index = index
        self.columnsReady = False
        self.model.load(index)
        self.progress.setValue(0)
        self.progress.show()
        self.indexThread = threading.Thread(target=index.build, daemon=True)
        self.indexThread.start()
        self.tick()
        return True

    def reopenWithOptions(self):
        if not self.index:
            return
        dialog = OpenOptions(self, self.index)
        if dialog.exec():
            self.openFile(self.index.path, **dialog.values())

    def closeFile(self):
        self.stopSearch()
        if self.export:
            self.export.cancelled = True
            self.export.thread.join()
            self.export = None
        self.search = None
        self.lastQuery = None
        self.onlyMatches.setChecked(False)
        self.matchLabel.setText('')
        if self.index:
            self.index.cancelled = True
            if self.indexThread:
                self.indexThread.join()
            old = self.index
            self.index = None
            self.model.load(None)
            old.close()

    def setupColumns(self):
        idx = self.index
        sample = [idx.row(r) for r in range(min(idx.rowsIndexed, 2000))]
        width = max((len(r) for r in sample), default=0)
        header = idx.header() if idx.hasHeader else []
        width = max(width, len(header))
        names = [header[c] if c < len(header) and header[c].strip() else f'Column {c + 1}' for c in range(width)]
        self.model.setColumns(names)
        self.model.sync()
        self.columnBox.clear()
        self.columnBox.addItem('All columns')
        self.columnBox.addItems(names)
        self.freezeSpin.setRange(0, width)
        self.freezeSpin.setValue(0)
        self.fitColumns()
        self.columnsReady = True

    # ---------- periodic updates ----------

    def tick(self):
        idx = self.index
        if not idx:
            return
        wasDone = not self.progress.isVisible()
        if not self.columnsReady and (idx.rowsIndexed >= 2000 or idx.done):
            self.setupColumns()
        if self.columnsReady:
            self.model.sync()
            self.updateSegments()
            self.updateRowHeaderWidth()
        if idx.done:
            self.progress.hide()
        else:
            self.progress.setValue(int(idx.progress * 100))
        if not (wasDone and idx.done):
            self.titleChanged.emit()
        state = '' if idx.done else '  ·  indexing…'
        delim = {'\t': 'tab', ',': 'comma', ';': 'semicolon', '|': 'pipe'}.get(idx.delimiter, repr(idx.delimiter))
        self.infoLabel.setText(f'{idx.path}  ·  {humanSize(idx.size)}  ·  {idx.dataRows:,} rows  ·  '
                               f'{self.model.columnCount()} columns  ·  {delim}  ·  {idx.encoding}{state}')
        s = self.search
        if s:
            if s.done:
                self.stopBtn.setEnabled(False)
            scanning = '' if s.done else f'  (scanning {s.progress:.0%})'
            self.matchLabel.setText(f'{len(s.hits):,} matching rows{scanning}')
        e = self.export
        if e:
            if e.done:
                self.export = None
                if e.error:
                    QMessageBox.critical(self, 'Export failed', e.error)
                else:
                    self.window.statusBar().showMessage(f'Exported {len(e.rows):,} rows to {e.path}', 10000)
            elif self.window.current() is self:
                self.window.statusBar().showMessage(f'Exporting… {e.progress:.0%}')

    def updateSegments(self):
        total = self.model.virtualTotal()
        count = (total + SEGMENT - 1) // SEGMENT
        if count > 1 and self.segmentBox.count() != count:
            current = self.model.base // SEGMENT
            self.segmentBox.clear()
            for s in range(count):
                self.segmentBox.addItem(f'Rows {s * SEGMENT + 1:,} – {min(total, (s + 1) * SEGMENT):,}')
            self.segmentBox.setCurrentIndex(current)
        elif count > 1 and self.segmentBox.count() == count:
            # The last segment's end grows while indexing
            self.segmentBox.setItemText(count - 1, f'Rows {(count - 1) * SEGMENT + 1:,} – {total:,}')
        self.segmentBox.setVisible(count > 1)

    def showSegment(self, base):
        if base != self.model.base:
            self.model.setBase(base)
            self.table.refreshFrozen()
        self.segmentBox.setCurrentIndex(base // SEGMENT)

    def updateRowHeaderWidth(self):
        digits = len(f'{max(1, self.model.virtualTotal()):,}')
        width = self.table.fontMetrics().horizontalAdvance('9' * digits) + 18
        if self.table.verticalHeader().width() != width:
            self.table.verticalHeader().setFixedWidth(width)

    # ---------- navigation ----------

    def jumpTo(self, row, col=None):
        """Select a virtual row (data row, or match number when filtered), switching segment if needed."""
        if row < 0 or row >= self.model.virtualTotal():
            return
        if not self.model.base <= row < self.model.base + SEGMENT:
            self.showSegment(row - row % SEGMENT)
        col = self.table.currentIndex().column() if col is None else col
        target = self.model.index(row - self.model.base, max(0, col))
        self.table.setCurrentIndex(target)
        self.table.scrollTo(target, QAbstractItemView.ScrollHint.PositionAtCenter)
        self.table.setFocus()

    def gotoRow(self):
        if not self.index:
            return
        if self.model.rowMap is not None:
            self.onlyMatches.setChecked(False)
        total = self.model.virtualTotal()
        row, ok = QInputDialog.getInt(self, 'Go to row', f'Row number (1 – {total:,})', 1, 1, max(1, total))
        if ok:
            self.jumpTo(row - 1)

    def onCurrentChanged(self, current, _previous):
        if not current.isValid() or not self.index:
            self.cellLabel.setText('')
            return
        name = self.model.columns[current.column()] if current.column() < len(self.model.columns) else ''
        value = self.model.cell(current.row(), current.column())
        rowNo = self.model.dataRow(current.row()) + 1
        self.cellLabel.setText(f'Row {rowNo:,}  ·  {name}')
        self.detailTitle.setText(f'Row {rowNo:,}, {name}  ({len(value):,} chars)')
        self.detail.setPlainText(value)

    # ---------- search ----------

    def focusSearch(self):
        self.searchBox.setFocus()
        self.searchBox.selectAll()

    def query(self):
        col = self.columnBox.currentIndex() - 1
        return (self.searchBox.text(), self.caseBox.isChecked(), col if col >= 0 else None)

    def onSearchEnter(self):
        if self.query() != self.lastQuery:
            self.startSearch()
        else:
            self.nextMatch()

    def startSearch(self):
        if not self.index:
            return
        term, cs, col = self.query()
        if not term:
            return self.clearSearch()
        if not self.index.done:
            self.matchLabel.setText('Wait — still indexing')
            return
        self.stopSearch()
        self.lastQuery = (term, cs, col)
        self.search = Search(self.index, term, cs, col).start()
        self.stopBtn.setEnabled(True)
        self.firstJumpPending = True
        QTimer.singleShot(300, self.jumpToFirst)
        if self.onlyMatches.isChecked():
            self.applyFilter(True)

    def jumpToFirst(self):
        s = self.search
        if not s or not self.firstJumpPending:
            return
        if len(s.hits):
            self.firstJumpPending = False
            if self.model.rowMap is None:
                cur = self.table.currentIndex()
                start = self.model.dataRow(cur.row()) if cur.isValid() else -1
                i = bisect_right(s.hits, start)
                self.jumpTo(s.hits[i] if i < len(s.hits) else s.hits[0], self.matchColumn())
            else:
                self.jumpTo(0, self.matchColumn())
        elif not s.done:
            QTimer.singleShot(300, self.jumpToFirst)

    def matchColumn(self):
        return self.lastQuery[2] if self.lastQuery and self.lastQuery[2] is not None else None

    def stopSearch(self):
        if self.search and not self.search.done:
            self.search.cancel()
            self.search.thread.join()
        self.stopBtn.setEnabled(False)

    def clearSearch(self):
        self.stopSearch()
        self.search = None
        self.lastQuery = None
        self.matchLabel.setText('')
        self.onlyMatches.setChecked(False)

    def applyFilter(self, on):
        if not self.index:
            return
        cur = self.table.currentIndex()
        keep = self.model.dataRow(cur.row()) if cur.isValid() else None
        if on and self.search:
            self.model.setRowMap(self.search.hits)
            if keep is not None:
                i = bisect_left(self.search.hits, keep)
                if i < len(self.search.hits):
                    self.jumpTo(i, cur.column())
        else:
            self.model.setRowMap(None)
            if keep is not None:
                self.jumpTo(keep, cur.column())
        self.table.refreshFrozen()

    def nextMatch(self):
        self.stepMatch(1)

    def prevMatch(self):
        self.stepMatch(-1)

    def stepMatch(self, step):
        s = self.search
        if not s:
            return self.startSearch()
        if not len(s.hits):
            return
        cur = self.table.currentIndex()
        col = self.matchColumn()
        if self.model.rowMap is not None:
            row = (self.model.virtualRow(cur.row()) + step) if cur.isValid() else 0
            return self.jumpTo(row % len(s.hits), col)
        here = self.model.dataRow(cur.row()) if cur.isValid() else -1
        if step > 0:
            i = bisect_right(s.hits, here)
            self.jumpTo(s.hits[i] if i < len(s.hits) else s.hits[0], col)
        else:
            i = bisect_left(s.hits, here) - 1
            self.jumpTo(s.hits[i] if i >= 0 else s.hits[-1], col)

    def exportMatches(self):
        s = self.search
        if not s or not len(s.hits):
            QMessageBox.information(self, 'Export', 'Run a search first; its matching rows are what gets exported.')
            return
        if not s.done:
            QMessageBox.information(self, 'Export', 'Wait for the search to finish.')
            return
        base = os.path.splitext(self.index.path)[0]
        path, _ = QFileDialog.getSaveFileName(self, 'Export matching rows', f'{base}_matches.csv', 'CSV (*.csv)')
        if path:
            if samePath(path, self.index.path):
                QMessageBox.warning(self, 'Export', 'Choose a different file than the one being viewed.')
                return
            self.export = Export(self.index, s.hits, path).start()

    # ---------- columns ----------

    def headerMenu(self, pos):
        if not self.index:
            return
        col = self.table.horizontalHeader().logicalIndexAt(pos)
        menu = QMenu(self)
        if col >= 0:
            menu.addAction(f'Hide "{self.model.columns[col]}"', lambda: self.table.setColumnHidden(col, True))
            menu.addAction('Freeze columns up to here', lambda: self.freezeSpin.setValue(col + 1))
            menu.addAction('Search in this column', lambda: (self.columnBox.setCurrentIndex(col + 1), self.focusSearch()))
            menu.addAction('Fit width to content', lambda: self.fitColumns([col]))
        menu.addAction('Unfreeze all', lambda: self.freezeSpin.setValue(0))
        menu.addSeparator()
        menu.addAction('Columns…', self.columnsDialog)
        menu.addAction('Show all columns', self.showAllColumns)
        menu.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def columnsDialog(self):
        if not self.index:
            return
        dialog = ColumnsDialog(self, self.table, self.model.columns)
        if dialog.exec():
            for c, visible in enumerate(dialog.visibleColumns()):
                if self.table.isColumnHidden(c) == visible:
                    self.table.setColumnHidden(c, not visible)
            self.table.refreshFrozen()

    def showAllColumns(self):
        for c in range(self.model.columnCount()):
            self.table.setColumnHidden(c, False)
        self.table.refreshFrozen()

    def fitColumns(self, cols=None):
        """Size columns to the header and the rows on screen (never scans the whole file)."""
        fm = self.table.fontMetrics()
        first = max(0, self.table.rowAt(0))
        last = self.table.rowAt(self.table.viewport().height() - 1)
        last = min(self.model.rowCount() - 1, first + 60) if last < 0 else last
        for c in cols if cols is not None else range(self.model.columnCount()):
            texts = [self.model.columns[c]] + [self.model.data(self.model.index(r, c)) or '' for r in range(first, last + 1)]
            width = max(fm.horizontalAdvance(t) for t in texts) + 24
            self.table.setColumnWidth(c, max(50, min(width, 420)))
        self.table.refreshFrozen()

    # ---------- clipboard ----------

    def copySelection(self, withHeader=False):
        sel = self.table.selectionModel()
        ranges = sel.selection()
        if ranges.isEmpty():
            return
        cells = sum(r.width() * r.height() for r in ranges)
        if cells > MAX_COPY_CELLS:
            QMessageBox.warning(self, 'Copy', f'Selection has {cells:,} cells; copy is limited to {MAX_COPY_CELLS:,}.\n'
                                              'Use search + File ▸ Export matching rows for large extracts.')
            return
        picked = sel.selectedIndexes()
        rows = sorted({i.row() for i in picked})
        cols = sorted({i.column() for i in picked if not self.table.isColumnHidden(i.column())})
        chosen = {(i.row(), i.column()) for i in picked}
        clean = lambda v: v.replace('\t', ' ').replace('\r\n', ' ').replace('\n', ' ')
        lines = ['\t'.join(clean(self.model.columns[c]) for c in cols)] if withHeader else []
        for r in rows:
            lines.append('\t'.join(clean(self.model.cell(r, c)) if (r, c) in chosen else '' for c in cols))
        QApplication.clipboard().setText('\n'.join(lines))
        self.window.statusBar().showMessage(f'Copied {len(rows):,} rows × {len(cols)} columns', 4000)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('CSV Viewer')
        self.systemStyle = QApplication.style().name()
        self.resize(1500, 900)
        self.setAcceptDrops(True)
        self.settings = QSettings('csv-viewer', 'csv-viewer')
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.tabs.tabCloseRequested.connect(self.closeTab)
        self.tabs.currentChanged.connect(self.onTabChanged)
        newTab = QToolButton(text='+', toolTip='Open a file in a new tab (Ctrl+T)')
        newTab.setAutoRaise(True)
        newTab.clicked.connect(self.openDialog)
        self.tabs.setCornerWidget(newTab, Qt.Corner.TopRightCorner)
        welcome = QLabel('<h2>CSV Viewer</h2><p>Open a CSV file: double-click one in the Explorer on the left,<br>'
                         'use File ▸ Open (Ctrl+O), or drag files onto this window.</p>'
                         '<p>Each file opens in its own tab. Ask Claude about them with ✦ Assistant (Ctrl+Shift+A).</p>')
        welcome.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack = QStackedWidget()
        self.stack.addWidget(welcome)
        self.stack.addWidget(self.tabs)
        self.setCentralWidget(self.stack)
        self.explorer = Explorer()
        self.explorer.openRequested.connect(self.openPath)
        self.explorerDock = QDockWidget('Explorer', self)
        self.explorerDock.setObjectName('explorer')
        self.explorerDock.setWidget(self.explorer)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.explorerDock)
        from .assistant.agent import Assistant
        self.assistant = Assistant(self)
        askButton = QToolButton(text='✦ Assistant')
        askButton.clicked.connect(self.assistant.toggleAction.trigger)
        askButton.setToolTip('Ask Claude about the open tabs (Ctrl+Shift+A)')
        askButton.setAutoRaise(True)
        corner = QWidget()
        cornerLayout = QHBoxLayout(corner)
        cornerLayout.setContentsMargins(0, 0, 4, 0)
        cornerLayout.setSpacing(2)
        cornerLayout.addWidget(askButton)
        cornerLayout.addWidget(self.tabs.cornerWidget(Qt.Corner.TopRightCorner))
        self.tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        self.buildMenus()
        self.setTheme(self.settings.value('view/theme', 'system'))
        geometry = self.settings.value('window/geometry')
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = self.settings.value('window/state')
        if state is not None:
            self.restoreState(state)
        else:
            self.resizeDocks([self.explorerDock], [300], Qt.Orientation.Horizontal)
        self.timer = QTimer(self, interval=200, timeout=self.tick)
        self.timer.start()

    # ---------- menus ----------

    def buildMenus(self):
        file = self.menuBar().addMenu('&File')
        self.act(file, '&Open in new tab…', self.openDialog, [QKeySequence.StandardKey.Open, QKeySequence('Ctrl+T')])
        self.act(file, 'Reopen with &options…', lambda: self.onTab('reopenWithOptions'), 'Ctrl+Shift+O')
        file.addSeparator()
        self.act(file, '&Export matching rows…', lambda: self.onTab('exportMatches'), 'Ctrl+E')
        file.addSeparator()
        self.act(file, '&Close tab', lambda: self.closeTab(self.tabs.currentIndex()), QKeySequence('Ctrl+W'))
        self.act(file, '&Quit', self.close, QKeySequence.StandardKey.Quit)
        edit = self.menuBar().addMenu('&Edit')
        self.act(edit, '&Copy', lambda: self.onTab('copySelection'), QKeySequence.StandardKey.Copy)
        self.act(edit, 'Copy with &header', lambda: self.onTab('copySelection', withHeader=True), 'Ctrl+Shift+C')
        edit.addSeparator()
        self.act(edit, '&Find', lambda: self.onTab('focusSearch'), QKeySequence.StandardKey.Find)
        self.act(edit, 'Find &next', lambda: self.onTab('nextMatch'), 'F3')
        self.act(edit, 'Find &previous', lambda: self.onTab('prevMatch'), 'Shift+F3')
        self.act(edit, '&Go to row…', lambda: self.onTab('gotoRow'), 'Ctrl+G')
        view = self.menuBar().addMenu('&View')
        explorerAct = self.explorerDock.toggleViewAction()
        explorerAct.setText('&Explorer panel')
        explorerAct.setShortcut('F4')
        view.addAction(explorerAct)
        self.assistant.menuActions(view)
        self.detailAct = view.addAction('Cell &detail panel')
        self.detailAct.setShortcut('Ctrl+D')
        self.detailAct.setCheckable(True)
        self.detailAct.triggered.connect(lambda on: self.onTab('setDetailVisible', on))
        view.addSeparator()
        self.act(view, '&Columns…', lambda: self.onTab('columnsDialog'), 'Ctrl+Shift+H')
        self.act(view, '&Show all columns', lambda: self.onTab('showAllColumns'))
        self.act(view, '&Fit columns to content (visible rows)', lambda: self.onTab('fitColumns'), 'Ctrl+R')
        view.addSeparator()
        self.act(view, 'Go to &top', lambda: self.onTab('jumpTo', 0), 'Ctrl+Home')
        self.act(view, 'Go to &bottom', lambda: self.current() and self.current().jumpTo(self.current().model.virtualTotal() - 1), 'Ctrl+End')
        view.addSeparator()
        themeMenu = view.addMenu('&Theme')
        self.themeActions = QActionGroup(self)
        for key, text in THEMES.items():
            action = themeMenu.addAction(text)
            action.setCheckable(True)
            action.setData(key)
            action.triggered.connect(lambda checked=False, k=key: self.setTheme(k))
            self.themeActions.addAction(action)
        self.act(themeMenu, '&Toggle light / dark',
                 lambda: self.setTheme('light' if self.settings.value('view/theme') == 'dark' else 'dark'), 'Ctrl+Shift+L')
        view.addSeparator()
        self.act(view, 'Ne&xt tab', lambda: self.cycleTab(1), [QKeySequence('Ctrl+Tab'), QKeySequence('Ctrl+PgDown')])
        self.act(view, 'Pre&vious tab', lambda: self.cycleTab(-1), [QKeySequence('Ctrl+Shift+Tab'), QKeySequence('Ctrl+PgUp')])

    def setTheme(self, theme):
        if theme not in THEMES:
            theme = 'system'
        applyTheme(theme, self.systemStyle)
        self.settings.setValue('view/theme', theme)
        for action in self.themeActions.actions():
            action.setChecked(action.data() == theme)

    def act(self, menu, text, slot, shortcut=None):
        action = menu.addAction(text)
        action.triggered.connect(lambda checked=False: slot())
        if isinstance(shortcut, list):
            action.setShortcuts(shortcut)
        elif shortcut:
            action.setShortcut(shortcut)
        return action

    def current(self):
        return self.tabs.currentWidget()

    def onTab(self, method, *args, **kwargs):
        tab = self.current()
        if tab:
            getattr(tab, method)(*args, **kwargs)

    # ---------- tabs ----------

    def openDialog(self):
        tab = self.current()
        start = os.path.dirname(tab.index.path) if tab and tab.index else self.explorer.currentRoot()
        paths, _ = QFileDialog.getOpenFileNames(self, 'Open CSV', start,
                                                'CSV / text files (*.csv *.tsv *.txt *.psv *.dat);;All files (*)')
        for path in paths:
            self.openPath(path)

    def openPath(self, path, **options):
        path = os.path.abspath(path)
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if tab.index and samePath(tab.index.path, path):
                self.tabs.setCurrentIndex(i)
                return
        if os.path.isdir(path):
            self.explorer.setRoot(path)
            return
        tab = CsvTab(self)
        if not tab.openFile(path, **options):
            tab.deleteLater()
            return
        tab.titleChanged.connect(lambda t=tab: self.updateTabTitle(t))
        i = self.tabs.addTab(tab, tab.title())
        self.tabs.setTabToolTip(i, path)
        self.tabs.setCurrentIndex(i)
        self.stack.setCurrentIndex(1)

    def updateTabTitle(self, tab):
        i = self.tabs.indexOf(tab)
        if i >= 0:
            self.tabs.setTabText(i, tab.title())
            if tab is self.current():
                self.setWindowTitle(f'{tab.title()} — CSV Viewer')

    def closeTab(self, i):
        tab = self.tabs.widget(i)
        if not tab:
            return
        tab.closeFile()
        self.tabs.removeTab(i)
        tab.deleteLater()
        if not self.tabs.count():
            self.stack.setCurrentIndex(0)
            self.setWindowTitle('CSV Viewer')

    def cycleTab(self, step):
        if self.tabs.count():
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % self.tabs.count())

    def onTabChanged(self, i):
        tab = self.tabs.widget(i)
        if tab:
            self.detailAct.setChecked(tab.detailBox.isVisible())
            self.setWindowTitle(f'{tab.title()} — CSV Viewer')

    def tick(self):
        # Background tabs keep indexing / searching, so all of them refresh
        for i in range(self.tabs.count()):
            self.tabs.widget(i).tick()

    # ---------- drag & drop / close ----------

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.openPath(url.toLocalFile())

    def closeEvent(self, event):
        self.assistant.shutdown()
        self.settings.setValue('window/geometry', self.saveGeometry())
        self.settings.setValue('window/state', self.saveState())
        while self.tabs.count():
            self.closeTab(0)
        super().closeEvent(event)


def main():
    if sys.platform == 'win32':
        # Own taskbar group and icon instead of python.exe's
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('CSVViewer.App')
    app = QApplication(sys.argv)
    app.setApplicationName('CSV Viewer')
    app.setDesktopFileName('csv-viewer')
    app.setWindowIcon(QIcon(assetPath('csv-viewer.ico' if sys.platform == 'win32' else 'csv-viewer.svg')))
    window = MainWindow()
    window.show()
    for path in sys.argv[1:]:
        window.openPath(path)
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
