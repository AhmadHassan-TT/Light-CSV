"""CSV Viewer: a tabbed, read-only viewer for very large CSV files (tested on 11 GB)."""
import json
import os
import sys
import threading
from bisect import bisect_left, bisect_right

import numpy as np
from PyQt6.QtCore import QAbstractTableModel, QEvent, QMimeData, QModelIndex, QRect, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QActionGroup, QColor, QDrag, QIcon, QKeySequence, QMouseEvent, QPalette, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDockWidget,
    QFileDialog, QFormLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QToolButton,
    QHBoxLayout, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QTabWidget, QTableView, QVBoxLayout, QWidget,
)

from .explorer import Explorer
from .filters import FilterPopup
from .index import ColumnScan, ColumnSort, CsvIndex, Export, FileCopy, Search

ROW_HEIGHT = 22
FILTER_BUTTON = 18  # width of the ▾ button on a filtered column's header
MAX_CELL_CHARS = 500
MAX_COPY_CELLS = 200_000
MAX_RECENT = 30  # recently opened files Go to file (Ctrl+P) offers
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


def isQueryResult(path):
    """True for a query result opened as a tab: a temporary file in the app's query-results folder."""
    from .query.dialog import resultsDir
    try:
        return os.path.normcase(os.path.dirname(os.path.abspath(path))) == os.path.normcase(os.path.abspath(resultsDir()))
    except OSError:
        return False


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
        self.rowMap = None  # rows on screen when filtered or "only matches" is on
        self.filterCols = {}  # col -> True when its dropdown filters rows, False when it shows every row
        self.sortedBy = None  # (col, desc) the rows on screen are sorted by

    def load(self, csv):
        self.beginResetModel()
        self.csv = csv
        self.rowMap = None
        self.filterCols = {}
        self.sortedBy = None
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
        return int(self.rowMap[self.base + viewRow]) if self.rowMap is not None else self.base + viewRow

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
            if section >= len(self.columns):
                return None
            arrow = ' ▼' if self.sortedBy == (section, True) else ' ▲' if self.sortedBy == (section, False) else ''
            return self.columns[section] + arrow
        return f'{self.dataRow(section) + 1:,}'


COLUMN_MIME = 'application/x-lightcsv-column'


class DragHeader(QHeaderView):
    """Column header whose sections can be dragged out (e.g. into the Query panel's filters), like Studio 3T.

    Columns with a filter dropdown get a ▾ button on the right, like Excel; clicking it emits filterClicked.
    """

    filterClicked = pyqtSignal(object, int)

    def __init__(self, parent):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        self.pressPos = None

    def onEdge(self, x):
        # Near a section border the press resizes the column, so it never starts a drag
        section = self.logicalIndexAt(x)
        if section < 0:
            return True
        left = self.sectionViewportPosition(section)
        return x - left < 5 or left + self.sectionSize(section) - x < 5

    def filterState(self, section):
        return getattr(self.model(), 'filterCols', {}).get(section)

    def onFilterButton(self, x):
        section = self.logicalIndexAt(x)
        if section < 0 or self.filterState(section) is None:
            return -1
        right = self.sectionViewportPosition(section) + self.sectionSize(section)
        return section if right - FILTER_BUTTON - 2 <= x < right - 2 else -1

    def paintSection(self, painter, rect, section):
        super().paintSection(painter, rect, section)
        state = self.filterState(section)
        if state is None or rect.width() < FILTER_BUTTON + 8:
            return
        box = QRect(rect.right() - FILTER_BUTTON - 1, rect.top() + 3, FILTER_BUTTON, rect.height() - 6)
        pal = self.palette()
        painter.save()
        painter.setPen(pal.color(QPalette.ColorRole.Mid))
        painter.setBrush(pal.color(QPalette.ColorRole.Highlight if state else QPalette.ColorRole.Button))
        painter.drawRoundedRect(box, 3, 3)
        painter.setPen(pal.color(QPalette.ColorRole.HighlightedText if state else QPalette.ColorRole.ButtonText))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, '⏷' if state else '▾')
        painter.restore()

    def mousePressEvent(self, event):
        x = int(event.position().x())
        if event.button() == Qt.MouseButton.LeftButton:
            section = self.onFilterButton(x)
            if section >= 0:
                self.pressPos = None
                self.filterClicked.emit(self, section)
                return
        self.pressPos = event.position().toPoint() if event.button() == Qt.MouseButton.LeftButton and not self.onEdge(x) else None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.pressPos is not None and (event.position().toPoint() - self.pressPos).manhattanLength() >= QApplication.startDragDistance():
            section = self.logicalIndexAt(self.pressPos.x())
            self.pressPos = None
            if self.startColumnDrag(section):
                # The drag consumed the release; end the header's own press so it stops selecting
                release = QMouseEvent(QEvent.Type.MouseButtonRelease, event.position(), event.globalPosition(),
                                      Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, event.modifiers())
                super().mouseReleaseEvent(release)
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.pressPos = None
        super().mouseReleaseEvent(event)

    def startColumnDrag(self, section):
        model = self.model()
        csvIndex = getattr(model, 'csv', None)
        if section < 0 or not csvIndex or section >= len(model.columns):
            return False
        name = model.columns[section]
        mime = QMimeData()
        mime.setData(COLUMN_MIME, json.dumps({'path': csvIndex.path, 'column': name}).encode())
        mime.setText(name)
        # A small chip with the column name follows the cursor
        label = QLabel(f'  {name}  ')
        label.setStyleSheet('background: palette(highlight); color: palette(highlighted-text); border-radius: 4px; padding: 3px;')
        label.adjustSize()
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(label.grab())
        drag.exec(Qt.DropAction.CopyAction)
        return True


class FrozenTable(QTableView):
    """Table whose first N columns stay pinned on the left while scrolling sideways."""

    def __init__(self, model):
        super().__init__()
        self.frozenCount = 0
        self.setHorizontalHeader(DragHeader(self))
        self.setModel(model)
        self.frozen = QTableView(self)
        self.frozen.setHorizontalHeader(DragHeader(self.frozen))
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
        self.filters = {}  # col -> filter spec (see ColumnScan), or None for a dropdown that shows every row
        self.filterScan = None  # finds the rows the filters keep
        self.filterRows = None  # its hits as a numpy array once it is done
        self.valueScans = {}  # (col, other filters) -> ColumnScan counting that column's values
        self.mapPending = False  # filters plus "only matches" wait for both scans to intersect them
        self.columnsReady = False
        self.firstJumpPending = False
        self.tableName = ''  # SQL table name in the Query panel; set by MainWindow, the user can rename it
        self.namedByUser = False  # True once renamed (now or remembered from an earlier rename)
        self.countScans = []  # (scan, message) for "Count rows with …", reported when done
        self.sortScan = None  # ColumnSort for the sort being made or shown
        self.sortShown = None  # the finished ColumnSort the rows on screen follow (the last one while a new one runs)
        self.sortRank = None  # data row -> its place in sortShown's order
        self.sortPending = False  # sorted rows on screen wait for the filter / search to finish
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
        # A long path must not stop the window from giving width to the side panels; it clips instead
        self.infoLabel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.cellLabel = QLabel('')
        self.saveBtn = QToolButton(text='Save result as…')
        self.saveBtn.setToolTip('This query result is a temporary file; save it under a name and folder you choose')
        self.saveBtn.clicked.connect(lambda: self.window.saveTabAs(self))
        self.saveBtn.hide()
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
        self.filterLabel = QLabel('')
        self.filterLabel.hide()
        info.addWidget(self.filterLabel)
        self.clearFiltersBtn = QToolButton(text='Clear filters')
        self.clearFiltersBtn.setToolTip('Show every row again (the dropdowns stay)')
        self.clearFiltersBtn.clicked.connect(self.clearFilters)
        self.clearFiltersBtn.hide()
        info.addWidget(self.clearFiltersBtn)
        self.sortLabel = QLabel('')
        self.sortLabel.hide()
        info.addWidget(self.sortLabel)
        self.clearSortBtn = QToolButton(text='Clear sort')
        self.clearSortBtn.setToolTip('Show the rows in file order again')
        self.clearSortBtn.clicked.connect(self.clearSort)
        self.clearSortBtn.hide()
        info.addWidget(self.clearSortBtn)
        info.addWidget(self.cellLabel)
        info.addWidget(self.saveBtn)
        info.addWidget(self.progress)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(self.searchBar)
        layout.addWidget(self.splitter, 1)
        layout.addLayout(info)
        self.table.selectionModel().currentChanged.connect(self.onCurrentChanged)
        self.table.doubleClicked.connect(lambda _: self.setDetailVisible(True))
        for view in (self.table, self.table.frozen):
            hdr = view.horizontalHeader()
            hdr.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            hdr.customContextMenuRequested.connect(lambda pos, h=hdr: self.headerMenu(pos, h))
            hdr.filterClicked.connect(self.openFilter)
            view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda pos, v=view: self.cellMenu(v, pos))

    def buildSearchBar(self):
        bar = self.searchBar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 0)
        self.searchBox = QLineEdit(placeholderText='Search the whole file…  (Enter = find / next, Shift+Enter = previous)')
        self.searchBox.setClearButtonEnabled(True)
        self.searchBox.setMinimumWidth(140)  # small minimums let the side panels take more of the window
        self.searchBox.returnPressed.connect(self.onSearchEnter)
        prev = QShortcut(QKeySequence('Shift+Return'), self.searchBox)
        prev.setContext(Qt.ShortcutContext.WidgetShortcut)
        prev.activated.connect(self.prevMatch)
        bar.addWidget(self.searchBox, 1)
        self.columnBox = QComboBox()
        self.columnBox.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.columnBox.setMinimumContentsLength(10)
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
            else:
                btn.setMinimumWidth(56)
            bar.addWidget(btn)
        self.stopBtn = QPushButton('Stop')
        self.stopBtn.clicked.connect(self.stopSearch)
        self.stopBtn.setMinimumWidth(56)
        self.stopBtn.setEnabled(False)
        bar.addWidget(self.stopBtn)
        self.onlyMatches = QCheckBox('Only matches')
        self.onlyMatches.setToolTip('Show only the rows that match the search')
        self.onlyMatches.toggled.connect(self.applyFilter)
        bar.addWidget(self.onlyMatches)
        clearBtn = QPushButton('Clear')
        clearBtn.clicked.connect(self.clearSearch)
        clearBtn.setMinimumWidth(56)
        bar.addWidget(clearBtn)
        self.matchLabel = QLabel('')
        self.matchLabel.setMinimumWidth(0)
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
        from .sqlviews import defaultTableName
        file = os.path.basename(self.index.path)
        name = self.tableName or file
        if self.tableName and self.tableName != defaultTableName(file):
            name = f'{self.tableName} · {file}'  # renamed: the file stays recognizable
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
        self.saveBtn.setVisible(isQueryResult(path))
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
        for scan, _ in self.countScans:
            scan.cancel()
        self.countScans = []
        if self.export:
            self.export.cancelled = True
            self.export.thread.join()
            self.export = None
        self.search = None
        self.lastQuery = None
        self.onlyMatches.setChecked(False)
        self.matchLabel.setText('')
        self.resetFilters()
        self.resetSort()
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
        wasDone = self.progress.isHidden()  # isVisible() is False for every background tab
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
        table = f'Table {self.tableName}  ·  ' if self.tableName else ''
        self.infoLabel.setText(f'{table}{idx.path}  ·  {humanSize(idx.size)}  ·  {idx.dataRows:,} rows  ·  '
                               f'{self.model.columnCount()} columns  ·  {delim}  ·  {idx.encoding}{state}')
        self.updateFilterStatus()
        self.updateSort()
        self.reportCounts()
        s = self.search
        if s:
            if s.done:
                self.stopBtn.setEnabled(False)
            scanning = '' if s.done else f' · {s.progress:.0%}'
            self.matchLabel.setText(f'{len(s.hits):,} matches{scanning}')
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

    def virtualOf(self, dataRow):
        """Position of a data row on screen; when filtered, the first visible row at or after it."""
        rows = self.model.rowMap
        if rows is not None and self.sortRank is not None:
            at = np.flatnonzero(np.asarray(rows) == dataRow)
            return int(at[0]) if len(at) else 0
        return dataRow if rows is None else bisect_left(rows, dataRow)

    def jumpToDataRow(self, dataRow, col=None):
        self.firstJumpPending = False  # an explicit jump wins over a search's pending jump to its first match
        total = self.model.virtualTotal()
        if total:
            self.jumpTo(min(self.virtualOf(dataRow), total - 1), col)

    def gotoRow(self):
        if not self.index:
            return
        total = self.index.dataRows
        row, ok = QInputDialog.getInt(self, 'Go to row', f'Row number (1 – {total:,})', 1, 1, max(1, total))
        if ok:
            self.jumpToDataRow(row - 1)

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
        hit = self.nextHit(-1 if self.onlyMatches.isChecked() else self.currentDataRow(), 1)
        if hit is not None:
            self.firstJumpPending = False
            self.jumpTo(self.virtualOf(hit), self.matchColumn())
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

    def applyFilter(self, _on=None):
        """Show the rows the column filters keep, intersected with the search hits when "Only matches" is on."""
        if not self.index:
            return
        cur = self.table.currentIndex()
        keep = self.model.dataRow(cur.row()) if cur.isValid() else None
        s = self.search if self.onlyMatches.isChecked() else None
        rows = self.filterScan.hits if self.filterScan else None
        self.mapPending = False
        if s and rows is not None:
            if s.done and self.filterRows is not None:
                rows = np.intersect1d(np.frombuffer(s.hits, np.int64), self.filterRows, assume_unique=True)
            else:
                self.mapPending = True  # the filter rows show until both scans are done
        elif s:
            rows = s.hits
        if self.sortRank is not None:
            rows = self.sortedRows(rows)
        self.model.setRowMap(rows)
        if keep is not None:
            self.jumpToDataRow(keep, cur.column())
        self.table.refreshFrozen()

    def currentDataRow(self):
        cur = self.table.currentIndex()
        return self.model.dataRow(cur.row()) if cur.isValid() else -1

    def nextHit(self, here, step):
        """The next search hit after (step 1) or before (step -1) a data row that the filters keep, wrapping around."""
        hits = self.search.hits if self.search else ()
        n = len(hits)
        if not n:
            return None
        keep = self.filterRows if self.filterScan else None
        if keep is not None and not len(keep):
            return None
        i = bisect_right(hits, here) if step > 0 else bisect_left(hits, here)
        spans = ((i, n), (0, i)) if step > 0 else ((0, i), (i, n))
        for lo, hi in spans:
            edges = range(lo, hi, 65536) if step > 0 else range(hi, lo, -65536)
            for edge in edges:
                a, b = (edge, min(edge + 65536, hi)) if step > 0 else (max(lo, edge - 65536), edge)
                cand = np.frombuffer(hits[a:b], np.int64)
                if keep is not None:
                    j = np.minimum(np.searchsorted(keep, cand), len(keep) - 1)
                    cand = cand[keep[j] == cand]
                if len(cand):
                    return int(cand[0] if step > 0 else cand[-1])
        return None

    def nextMatch(self):
        self.stepMatch(1)

    def prevMatch(self):
        self.stepMatch(-1)

    def stepMatch(self, step):
        s = self.search
        if not s:
            return self.startSearch()
        hit = self.nextHit(self.currentDataRow(), step)
        if hit is not None:
            self.jumpTo(self.virtualOf(hit), self.matchColumn())

    def exportMatches(self):
        s = self.search
        if self.filterScan:
            # With column filters, the rows on screen are what gets exported
            if not self.filterScan.done or self.mapPending:
                QMessageBox.information(self, 'Export', 'Wait for the filter to finish.')
                return
            rows, title, suffix = self.model.rowMap, 'Export filtered rows', 'filtered'
        else:
            if not s or not len(s.hits):
                QMessageBox.information(self, 'Export', 'Run a search or set a column filter first; '
                                                        'its rows are what gets exported.')
                return
            if not s.done:
                QMessageBox.information(self, 'Export', 'Wait for the search to finish.')
                return
            rows, title, suffix = s.hits, 'Export matching rows', 'matches'
        if not len(rows):
            QMessageBox.information(self, 'Export', 'No rows to export.')
            return
        base = os.path.splitext(self.index.path)[0]
        path, _ = QFileDialog.getSaveFileName(self, title, f'{base}_{suffix}.csv', 'CSV (*.csv)')
        if path:
            if samePath(path, self.index.path):
                QMessageBox.warning(self, 'Export', 'Choose a different file than the one being viewed.')
                return
            self.export = Export(self.index, rows, path).start()

    # ---------- columns ----------

    def headerMenu(self, pos, header):
        if not self.index:
            return
        col = header.logicalIndexAt(pos)
        menu = QMenu(self)
        if col >= 0:
            if col in self.filters:
                menu.addAction('Open filter…', lambda: self.openFilter(header, col))
                menu.addAction('Remove filter dropdown', lambda: self.removeColumnFilter(col))
            else:
                menu.addAction('Add filter dropdown', lambda: self.addColumnFilter(col, header))
            if any(self.filters.values()):
                menu.addAction('Clear all filters', self.clearFilters)
            menu.addSeparator()
            self.sortActions(menu, col)
            menu.addSeparator()
            self.columnFunctions(menu, col)
            menu.addSeparator()
            menu.addAction(f'Hide "{self.model.columns[col]}"', lambda: self.table.setColumnHidden(col, True))
            menu.addAction('Freeze columns up to here', lambda: self.freezeSpin.setValue(col + 1))
            menu.addAction('Search in this column', lambda: (self.columnBox.setCurrentIndex(col + 1), self.focusSearch()))
            menu.addAction('Fit width to content', lambda: self.fitColumns([col]))
            menu.addSeparator()
            name, path = self.model.columns[col], self.index.path
            menu.addAction('Filter by this column in Query', lambda: self.toQuery('addFilterForColumn', path, name))
            menu.addAction('Group by this column in Query', lambda: self.toQuery('addGroupForColumn', path, name))
            menu.addAction('Count rows per value in Query', lambda: self.toQuery('countValuesForColumn', path, name))
            menu.addSeparator()
        menu.addAction('Unfreeze all', lambda: self.freezeSpin.setValue(0))
        menu.addSeparator()
        menu.addAction('Columns…', self.columnsDialog)
        menu.addAction('Show all columns', self.showAllColumns)
        menu.exec(header.mapToGlobal(pos))

    def columnFunctions(self, menu, col):
        """Unique / duplicate rows, hide blanks and statistics for a column (header and cell menus)."""
        name = self.model.columns[col]
        mode = (self.filters.get(col) or (None,))[0]
        for key, text, tip in (('first', f'Unique rows by "{name}"', 'Keep only the first row of each value (distinct)'),
                               ('dups', f'Only duplicated "{name}" values', 'Keep only rows whose value appears more than once')):
            action = menu.addAction(text, lambda k=key: self.setFilterFromCell(col, None if mode == k else (k, None)))
            action.setCheckable(True)
            action.setChecked(mode == key)
            action.setToolTip(tip)
        menu.addAction(f'Hide rows with blank "{name}"', lambda: self.setFilterFromCell(col, self.excluding(col, b'')))
        menu.addAction('Column statistics (count, distinct, sum)…', lambda: self.columnStats(col))
        menu.setToolTipsVisible(True)

    # ---------- sorting ----------

    def sortActions(self, menu, col):
        name = self.model.columns[col]
        for desc, text in ((False, f'Sort A → Z (smallest first) by "{name}"'), (True, f'Sort Z → A (largest first) by "{name}"')):
            action = menu.addAction(text, lambda d=desc: self.sortBy(col, d))
            action.setCheckable(True)
            action.setChecked(self.model.sortedBy == (col, desc))
        if self.sortScan:
            menu.addAction('Clear sort', self.clearSort)

    def sortBy(self, col, desc=False):
        """Show the rows ordered by a column (numbers by value, text ignoring case, blanks last); made in the background."""
        if not self.index or not self.index.done:
            self.window.statusBar().showMessage('Sorting works once indexing has finished', 5000)
            return
        if self.sortScan and self.sortScan is not self.sortShown:
            self.sortScan.cancel()
        if self.model.sortedBy == (col, desc):
            self.sortScan = self.sortShown
            self.updateSort()
            return
        self.sortScan = ColumnSort(self.index, col, desc).start()
        self.updateSort()

    def clearSort(self):
        self.resetSort()
        self.applyFilter()

    def resetSort(self):
        if self.sortScan:
            self.sortScan.cancel()
        self.sortScan = None
        self.sortShown = None
        self.sortRank = None
        self.sortPending = False
        self.model.sortedBy = None
        self.sortLabel.hide()
        self.clearSortBtn.hide()
        self.table.refreshFrozen()

    def sortedRows(self, rows):
        """The rows to show (None = all) in sort order; a filter or search still running is sorted as far as it got."""
        if rows is None:
            self.sortPending = False
            return self.sortShown.order
        scans = [x for x in (self.filterScan, self.search if self.onlyMatches.isChecked() else None) if x]
        self.sortPending = not all(x.done for x in scans) or self.mapPending
        rows = np.asarray(rows if isinstance(rows, np.ndarray) else np.frombuffer(rows[:].tobytes(), np.int64))
        return rows[np.argsort(self.sortRank[rows], kind='stable')]

    def updateSort(self):
        """Show a finished sort, re-sort rows a filter or search found since, and keep the label current."""
        scan = self.sortScan
        if not scan:
            return
        name = self.model.columns[scan.col] if scan.col < len(self.model.columns) else f'column {scan.col + 1}'
        how = ('Z → A' if scan.desc else 'A → Z') if not scan.done or not scan.numeric else ('largest first' if scan.desc else 'smallest first')
        if not scan.done:
            self.sortLabel.setText(f'Sorting by {name} ({how})… {scan.progress:.0%}')
        elif scan.error:
            self.window.statusBar().showMessage(scan.error, 10000)
            self.clearSort()
            return
        elif self.sortShown is not scan:
            rank = np.empty(len(scan.order), np.int64)
            rank[scan.order] = np.arange(len(scan.order))
            self.sortShown, self.sortRank = scan, rank
            self.model.sortedBy = (scan.col, scan.desc)
            self.applyFilter()
            self.jumpTo(0, scan.col)
            self.sortLabel.setText(f'Sorted by {name} {"▼" if scan.desc else "▲"}')
            self.sortLabel.setToolTip(f'{"Numbers" if scan.numeric else "Text, ignoring case"}, {how}; blank cells last')
        elif self.sortPending and not self.mapPending:
            scans = [x for x in (self.filterScan, self.search if self.onlyMatches.isChecked() else None) if x]
            if all(x.done for x in scans):
                self.applyFilter()
        self.sortLabel.show()
        self.clearSortBtn.show()

    def columnStats(self, col):
        if not self.index.done:
            self.window.statusBar().showMessage('Statistics work once indexing has finished', 5000)
            return
        from .stats import ColumnStatsDialog
        ColumnStatsDialog(self, col).show()

    def excluding(self, col, raw):
        """The column's filter with one more value hidden."""
        spec = self.filters.get(col)
        if spec and spec[0] == 'in':
            return ('in', spec[1] - {raw})
        return ('out', (spec[1] if spec and spec[0] == 'out' else frozenset()) | {raw})

    def countValue(self, col, raw, shown):
        """Count the rows with this value in the column (among rows the other filters keep); shown when done."""
        if not self.index.done:
            self.window.statusBar().showMessage('Counting works once indexing has finished', 5000)
            return
        others = {c: spec for c, spec in self.filters.items() if spec and c != col}
        scan = ColumnScan(self.index, others | {col: ('in', frozenset([raw]))}).start()
        where = f'"{shown}" in {self.model.columns[col]}' + (' (with the other filters)' if others else '')
        self.countScans.append((scan, where))
        self.window.statusBar().showMessage(f'Counting rows with {where}…')

    def reportCounts(self):
        for scan, where in [c for c in self.countScans if c[0].done]:
            self.countScans.remove((scan, where))
            n = len(scan.hits)
            self.window.statusBar().showMessage(f'{n:,} row{"" if n == 1 else "s"} with {where}', 15000)

    def toQuery(self, method, path, column):
        # Same as dragging the header into the Query panel
        self.window.showSidePanel('query')
        getattr(self.window.queryPanel, method)(path, column)

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
            width = max(fm.horizontalAdvance(t) for t in texts) + 24 + (FILTER_BUTTON if c in self.filters else 0)
            self.table.setColumnWidth(c, max(50, min(width, 420)))
        self.table.refreshFrozen()

    # ---------- column filters ----------

    def filterKey(self, filters):
        return tuple(sorted((c, spec) for c, spec in filters.items() if spec))

    def valueScan(self, col):
        """Counts of a column's values among the rows the other filters keep; cached per filter combination."""
        others = {c: spec for c, spec in self.filters.items() if spec and c != col}
        key = (col, self.filterKey(others))
        scan = self.valueScans.get(key)
        if scan is None or (scan.cancelled and not scan.done):
            scan = self.valueScans[key] = ColumnScan(self.index, others, count=col).start()
        return scan

    def addColumnFilter(self, col, header=None):
        if col not in self.filters:
            self.makeRoomForButton(col)
            self.filters[col] = None
            self.syncFilterHeaders()
        self.openFilter(header or self.headerFor(col), col)

    def headerFor(self, col):
        frozen = col < self.table.frozenCount
        return (self.table.frozen if frozen else self.table).horizontalHeader()

    def openFilter(self, header, col):
        if not self.index:
            return
        if not self.index.done:
            self.window.statusBar().showMessage('Filters work once indexing has finished', 5000)
            return
        if col not in self.filters:
            return self.addColumnFilter(col, header)
        if header is self.table.horizontalHeader() and header.sectionViewportPosition(col) < 0:
            self.table.scrollTo(self.model.index(max(0, self.table.rowAt(0)), col))
        FilterPopup(self, col).showUnder(header, col)

    def filterCurrentColumn(self):
        cur = self.table.currentIndex()
        if self.index and self.model.columnCount():
            self.addColumnFilter(cur.column() if cur.isValid() else 0)

    def makeRoomForButton(self, col):
        # Room for the ▾ button next to the column name
        need = self.table.fontMetrics().horizontalAdvance(self.model.columns[col]) + 24 + FILTER_BUTTON
        if self.table.columnWidth(col) < need:
            self.table.setColumnWidth(col, min(need, 420))
            self.table.refreshFrozen()

    def setColumnFilter(self, col, spec):
        if col not in self.filters:
            self.makeRoomForButton(col)
        self.filters[col] = spec
        self.restartFilter()

    def removeColumnFilter(self, col):
        self.filters.pop(col, None)
        self.restartFilter()

    def clearFilters(self):
        self.filters = dict.fromkeys(self.filters)
        self.restartFilter()

    def syncFilterHeaders(self):
        self.model.filterCols = {c: bool(spec) for c, spec in self.filters.items()}
        for view in (self.table, self.table.frozen):
            view.horizontalHeader().viewport().update()

    def stopFilterScans(self):
        if self.filterScan:
            self.filterScan.cancel()
        for scan in self.valueScans.values():
            scan.cancel()
        # Finished counts stay valid for their filter combination; unfinished ones restart when needed
        self.valueScans = {k: v for k, v in self.valueScans.items() if v.done}

    def restartFilter(self):
        self.stopFilterScans()
        self.syncFilterHeaders()
        active = {c: spec for c, spec in self.filters.items() if spec}
        self.filterScan = ColumnScan(self.index, active).start() if active and self.index else None
        self.filterRows = None
        self.applyFilter()
        self.updateFilterStatus()

    def resetFilters(self):
        self.stopFilterScans()
        self.valueScans = {}
        self.filters = {}
        self.filterScan = None
        self.filterRows = None
        self.mapPending = False
        self.syncFilterHeaders()
        self.updateFilterStatus()

    def updateFilterStatus(self):
        fs = self.filterScan
        if fs and fs.done and self.filterRows is None:
            self.filterRows = np.frombuffer(fs.hits, np.int64) if len(fs.hits) else np.zeros(0, np.int64)
        if self.mapPending and self.filterRows is not None and self.search and self.search.done:
            self.applyFilter()
        self.filterLabel.setVisible(fs is not None)
        self.clearFiltersBtn.setVisible(fs is not None)
        if fs and self.index:
            n = sum(1 for spec in self.filters.values() if spec)
            busy = '' if fs.done else f'  ·  filtering {fs.progress:.0%}'
            modes = {'first': 'unique', 'dups': 'duplicated'}
            what = ', '.join(f'{modes[spec[0]]} {self.model.columns[c]}' for c, spec in sorted(self.filters.items())
                             if spec and spec[0] in modes and c < len(self.model.columns))
            self.filterLabel.setText(f'Filtered ({n} column{"s" if n != 1 else ""}{": " + what if what else ""}): '
                                     f'{len(fs.hits):,} of {self.index.dataRows:,} rows{busy}')

    def cellMenu(self, view, pos):
        idx = view.indexAt(pos)
        if not self.index or not idx.isValid():
            return
        col = idx.column()
        value = self.model.cell(idx.row(), col)
        raw = value.encode(self.index.encoding, 'replace')
        shown = (value[:40] + '…' if len(value) > 40 else value).replace('\n', ' ') or '(Blanks)'
        spec = self.filters.get(col)
        menu = QMenu(self)
        menu.addAction('Copy', self.copySelection)
        menu.addAction('Copy with header', lambda: self.copySelection(withHeader=True))
        menu.addSeparator()
        menu.addAction(f'Filter to "{shown}"', lambda: self.setFilterFromCell(col, ('in', frozenset([raw]))))
        menu.addAction(f'Hide rows with "{shown}"', lambda: self.setFilterFromCell(col, self.excluding(col, raw)))
        menu.addAction(f'Count rows with "{shown}"', lambda: self.countValue(col, raw, shown))
        if spec:
            menu.addAction(f'Clear filter on "{self.model.columns[col]}"', lambda: self.setColumnFilter(col, None))
        menu.addSeparator()
        self.sortActions(menu, col)
        menu.addSeparator()
        self.columnFunctions(menu, col)
        menu.exec(view.viewport().mapToGlobal(pos))

    def setFilterFromCell(self, col, spec):
        if not self.index.done:
            self.window.statusBar().showMessage('Filters work once indexing has finished', 5000)
            return
        self.setColumnFilter(col, spec)

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
        self.tabs.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self.tabMenu)
        self.tabs.currentChanged.connect(self.onTabChanged)
        self.tabs.tabBarDoubleClicked.connect(lambda i: i >= 0 and self.renameTable(i))
        newTab = QToolButton(text='+', toolTip='Open a file in a new tab (Ctrl+T)')
        newTab.setAutoRaise(True)
        newTab.clicked.connect(self.openDialog)
        self.tabs.setCornerWidget(newTab, Qt.Corner.TopRightCorner)
        welcome = QLabel('<h2>CSV Viewer</h2><p>Open a CSV file: double-click one in the Explorer on the left,<br>'
                         'use File ▸ Open (Ctrl+O), Go to file (Ctrl+P), or drag files onto this window.</p>'
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
        self.explorerDock.visibilityChanged.connect(lambda on: on and QTimer.singleShot(0, self.revealInExplorer))
        self.copies = {}  # tab -> FileCopy of a running "Save as"
        self.quickOpenPopup = None
        # One right-hand side panel with two tabs: the Assistant and the Query builder
        self.sideDock = QDockWidget('Assistant', self)
        self.sideDock.setObjectName('sidepanel')
        self.sideTabs = QTabWidget()
        self.sideTabs.setDocumentMode(True)
        from .assistant.agent import Assistant
        from .query.dialog import QueryPanel
        self.assistant = Assistant(self, self.sideDock)
        self.queryPanel = QueryPanel(self)
        self.sidePages = {'assistant': self.assistant.panel, 'query': self.queryPanel}
        self.sideTabs.addTab(self.assistant.panel, '✦ Assistant')
        self.sideTabs.addTab(self.queryPanel, '⊞ Query')
        self.sideTabs.currentChanged.connect(lambda i: self.sideDock.setWindowTitle(self.sideTabs.tabText(i).split(' ', 1)[-1]))
        self.sideDock.setWidget(self.sideTabs)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.sideDock)
        self.sideDock.hide()
        askButton = QToolButton(text='✦ Assistant')
        askButton.clicked.connect(lambda: self.showSidePanel('assistant', toggle=True))
        askButton.setToolTip('Ask the AI assistant about the open tabs (Ctrl+Shift+A)')
        askButton.setAutoRaise(True)
        corner = QWidget()
        cornerLayout = QHBoxLayout(corner)
        cornerLayout.setContentsMargins(0, 0, 4, 0)
        cornerLayout.setSpacing(2)
        queryButton = QToolButton(text='⊞ Query')
        queryButton.setToolTip('Filter, group and summarize a whole file without writing SQL (Ctrl+Shift+Q)')
        queryButton.setAutoRaise(True)
        queryButton.clicked.connect(lambda: self.showSidePanel('query', toggle=True))
        cornerLayout.addWidget(queryButton)
        cornerLayout.addWidget(askButton)
        cornerLayout.addWidget(self.tabs.cornerWidget(Qt.Corner.TopRightCorner))
        self.tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        from .mcpserver.control import McpControl
        self.mcp = McpControl(self, forceOn='--mcp' in sys.argv)
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
        self.act(file, '&Go to file…', self.quickOpen, 'Ctrl+P')
        self.act(file, 'Reopen with &options…', lambda: self.onTab('reopenWithOptions'), 'Ctrl+Shift+O')
        self.act(file, '&Save as…', lambda: self.saveTabAs(self.current()), 'Ctrl+Shift+S')
        file.addSeparator()
        self.act(file, '&Export matching / filtered rows…', lambda: self.onTab('exportMatches'), 'Ctrl+E')
        file.addSeparator()
        self.act(file, '&Close tab', lambda: self.closeTab(self.tabs.currentIndex()), QKeySequence('Ctrl+W'))
        self.act(file, 'Close &other tabs', lambda: self.closeOthers(self.tabs.currentIndex()))
        self.act(file, 'Close &all tabs', self.closeAll, QKeySequence('Ctrl+Shift+W'))
        self.act(file, '&Quit', self.close, QKeySequence.StandardKey.Quit)
        edit = self.menuBar().addMenu('&Edit')
        self.act(edit, '&Copy', lambda: self.onTab('copySelection'), QKeySequence.StandardKey.Copy)
        self.act(edit, 'Copy with &header', lambda: self.onTab('copySelection', withHeader=True), 'Ctrl+Shift+C')
        edit.addSeparator()
        self.act(edit, '&Find', lambda: self.onTab('focusSearch'), QKeySequence.StandardKey.Find)
        self.act(edit, 'Find &next', lambda: self.onTab('nextMatch'), 'F3')
        self.act(edit, 'Find &previous', lambda: self.onTab('prevMatch'), 'Shift+F3')
        self.act(edit, '&Go to row…', lambda: self.onTab('gotoRow'), 'Ctrl+G')
        edit.addSeparator()
        self.act(edit, 'F&ilter this column…', lambda: self.onTab('filterCurrentColumn'), 'Ctrl+Shift+F')
        self.act(edit, 'C&lear all filters', lambda: self.onTab('clearFilters'))
        view = self.menuBar().addMenu('&View')
        explorerAct = self.explorerDock.toggleViewAction()
        explorerAct.setText('&Explorer panel')
        explorerAct.setShortcut('F4')
        view.addAction(explorerAct)
        self.act(view, '&Assistant panel', lambda: self.showSidePanel('assistant', toggle=True), 'Ctrl+Shift+A')
        self.act(view, '&Query builder panel', lambda: self.showSidePanel('query', toggle=True), 'Ctrl+Shift+Q')
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
        tools = self.menuBar().addMenu('&Tools')
        self.act(tools, '&Query builder', lambda: self.showSidePanel('query'))
        tools.addSeparator()
        self.mcp.menuActions(tools)

    def setTheme(self, theme):
        if theme not in THEMES:
            theme = 'system'
        applyTheme(theme, self.systemStyle)
        if hasattr(self, 'assistant'):
            self.assistant.panel.restyle()
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
        tab.tableName, tab.namedByUser = self.tableNameFor(path)
        if not isQueryResult(path):
            self.addRecentFile(path)
        i = self.tabs.addTab(tab, tab.title())
        self.tabs.setCurrentIndex(i)
        self.updateTabTitle(tab)
        self.stack.setCurrentIndex(1)

    def quickOpen(self):
        """Go to file (Ctrl+P): fuzzy-find a file under the Explorer folder, or an open / recent one."""
        if self.quickOpenPopup is None:
            from .quickopen import QuickOpen
            self.quickOpenPopup = QuickOpen(self)
        self.quickOpenPopup.popup()

    def recentFiles(self):
        try:
            files = json.loads(self.settings.value('files/recent', '[]'))
        except (TypeError, ValueError):
            files = []
        return [f for f in files if isinstance(f, str)] if isinstance(files, list) else []

    def addRecentFile(self, path):
        files = [path] + [f for f in self.recentFiles() if not samePath(f, path)]
        self.settings.setValue('files/recent', json.dumps(files[:MAX_RECENT]))

    def tableNames(self, skip=None):
        return [t.tableName for t in (self.tabs.widget(i) for i in range(self.tabs.count())) if t is not skip and t.tableName]

    def savedTableNames(self):
        try:
            names = json.loads(self.settings.value('query/tableNames', '{}'))
        except (TypeError, ValueError):
            names = {}
        return names if isinstance(names, dict) else {}

    def tableNameFor(self, path):
        """(name, renamed): the name a file had last time it was renamed, if still free; else one made from the file name."""
        from .sqlviews import defaultTableName, tableNameError
        taken = self.tableNames()
        saved = self.savedTableNames().get(os.path.normcase(os.path.abspath(path)))
        if isinstance(saved, str) and not tableNameError(saved, taken):
            return saved, True
        return defaultTableName(path, taken), False

    def renameTable(self, i, name=None):
        """Ask for (or set) a tab's SQL table name; returns an error message or None."""
        from .sqlviews import tableNameError
        tab = self.tabs.widget(i)
        if not tab or not tab.index:
            return 'no file open in that tab'
        ask = name is None
        name = tab.tableName if ask else name
        while True:
            if ask:
                name, ok = QInputDialog.getText(self, 'Rename table', f'Name of {os.path.basename(tab.index.path)} in '
                                                'queries (letters, digits and _):', text=name)
                if not ok:
                    return None
            name = name.strip()
            error = tableNameError(name, self.tableNames(skip=tab)) if name != tab.tableName else None
            if not error:
                break
            if not ask:
                return error
            QMessageBox.warning(self, 'Rename table', f'Cannot use "{name}": {error}.')
        if name != tab.tableName:
            old, tab.tableName = tab.tableName, name
            tab.namedByUser = True
            names = self.savedTableNames()
            names[os.path.normcase(os.path.abspath(tab.index.path))] = name
            self.settings.setValue('query/tableNames', json.dumps(names))
            self.updateTabTitle(tab)
            tab.tick()
            self.queryPanel.tableRenamed(old, name)
        return None

    def updateTabTitle(self, tab):
        i = self.tabs.indexOf(tab)
        if i >= 0:
            self.tabs.setTabText(i, tab.title())
            if tab.index:
                self.tabs.setTabToolTip(i, f'{tab.index.path}\nTable name in queries: {tab.tableName}\n'
                                           'Double-click to rename')
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

    def showSidePanel(self, page, toggle=False):
        """Show the side panel on the Assistant or Query tab; toggle hides it when that tab is already showing."""
        widget = self.sidePages[page]
        if toggle and self.sideDock.isVisible() and self.sideTabs.currentWidget() is widget:
            self.sideDock.hide()
            return
        self.sideTabs.setCurrentWidget(widget)
        self.sideDock.show()
        self.sideDock.raise_()

    def openQueryBuilder(self):
        self.showSidePanel('query')

    def closeOthers(self, keep):
        tab = self.tabs.widget(keep)
        for i in reversed(range(self.tabs.count())):
            if self.tabs.widget(i) is not tab:
                self.closeTab(i)

    def closeToRight(self, i):
        for j in reversed(range(i + 1, self.tabs.count())):
            self.closeTab(j)

    def closeAll(self):
        for i in reversed(range(self.tabs.count())):
            self.closeTab(i)

    def tabMenu(self, pos):
        i = self.tabs.tabBar().tabAt(pos)
        if i < 0:
            return
        count = self.tabs.count()
        menu = QMenu(self)
        tab = self.tabs.widget(i)
        if tab.index:
            menu.addAction(f'Rename table "{tab.tableName}"…', lambda: self.renameTable(i))
            menu.addAction('Save result as…' if isQueryResult(tab.index.path) else 'Save a copy as…',
                           lambda: self.saveTabAs(tab))
            menu.addSeparator()
        menu.addAction('Close', lambda: self.closeTab(i))
        menu.addAction('Close other tabs', lambda: self.closeOthers(i)).setEnabled(count > 1)
        menu.addAction('Close tabs to the right', lambda: self.closeToRight(i)).setEnabled(i < count - 1)
        menu.addSeparator()
        menu.addAction('Close all tabs', self.closeAll)
        if tab.index:
            menu.addSeparator()
            menu.addAction('Copy file path', lambda: QApplication.clipboard().setText(tab.index.path))
            menu.addAction('Show in Explorer panel', lambda: (self.explorerDock.show(), self.explorer.revealFile(tab.index.path)))
        menu.exec(self.tabs.tabBar().mapToGlobal(pos))

    def cycleTab(self, step):
        if self.tabs.count():
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % self.tabs.count())

    def onTabChanged(self, i):
        tab = self.tabs.widget(i)
        if tab:
            self.detailAct.setChecked(tab.detailBox.isVisible())
            self.setWindowTitle(f'{tab.title()} — CSV Viewer')
            self.revealInExplorer(tab)

    def revealInExplorer(self, tab=None):
        """Select the file of a tab (default: the current one) in the Explorer panel while it is shown."""
        tab = tab or self.current()
        if tab and tab.index and self.explorerDock.isVisible() and not isQueryResult(tab.index.path):
            self.explorer.revealFile(tab.index.path)

    def tick(self):
        # Background tabs keep indexing / searching, so all of them refresh
        for i in range(self.tabs.count()):
            self.tabs.widget(i).tick()
        for tab, copy in list(self.copies.items()):
            if copy.done:
                self.finishSave(tab, copy)
            else:
                self.statusBar().showMessage(f'Saving {os.path.basename(copy.path)}… {copy.progress:.0%}')

    # ---------- save as ----------

    def saveTabAs(self, tab, path=None):
        """Save a tab's file under a new name. A query result tab then shows the saved file (its temporary
        file goes); any other tab is copied and stays on its own file. Returns an error message or None."""
        if not tab or not tab.index:
            return 'no file open'
        source = tab.index.path
        result = isQueryResult(source)
        if path is None:
            start = self.settings.value('query/exportDir') if result else os.path.dirname(source)
            stem = tab.tableName or os.path.splitext(os.path.basename(source))[0]
            ext = os.path.splitext(source)[1] or '.csv'
            path, _ = QFileDialog.getSaveFileName(self, 'Save result as' if result else 'Save a copy as',
                                                  os.path.join(start or os.path.expanduser('~'), stem + ext),
                                                  'CSV files (*.csv);;All files (*)')
            if not path:
                return None
            ask = True
        else:
            ask = False
        path = os.path.abspath(path)
        error = None
        if tab in self.copies:
            error = 'this tab is already being saved'
        elif samePath(path, source):
            error = 'choose a different file than the one open in this tab'
        elif any(t.index and samePath(t.index.path, path) for t in (self.tabs.widget(i) for i in range(self.tabs.count()))):
            error = 'that file is open in another tab; close it first'
        if error:
            if ask:
                QMessageBox.warning(self, 'Save as', f'Cannot save: {error}.')
            return error
        self.settings.setValue('query/exportDir', os.path.dirname(path))
        self.copies[tab] = FileCopy(source, path).start()
        self.statusBar().showMessage(f'Saving {os.path.basename(path)}…')
        return None

    def finishSave(self, tab, copy):
        del self.copies[tab]
        if copy.error:
            QMessageBox.critical(self, 'Save as', f'Could not save {copy.path}\n\n{copy.error}')
            return
        # The tab may have been closed meanwhile (its widget is gone, so compare without touching it)
        idx = tab.index if any(self.tabs.widget(i) is tab for i in range(self.tabs.count())) else None
        if idx and samePath(idx.path, copy.source) and isQueryResult(copy.source):
            from .sqlviews import defaultTableName
            options = dict(delimiter=idx.delimiter, encoding=idx.encoding, hasHeader=idx.hasHeader, quoteAware=idx.quoteAware)
            oldName = tab.tableName
            if tab.openFile(copy.path, **options):
                if not tab.namedByUser:  # a name made from the temporary file follows the new file name
                    tab.tableName = defaultTableName(copy.path, self.tableNames(skip=tab))
                try:
                    os.remove(copy.source)
                except OSError:
                    pass
                self.updateTabTitle(tab)
                self.queryPanel.fileMoved(copy.source, copy.path, oldName, tab.tableName)
                if tab is self.current():
                    self.revealInExplorer(tab)
        self.statusBar().showMessage(f'Saved {copy.path}', 10000)

    # ---------- drag & drop / close ----------

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.openPath(url.toLocalFile())

    def closeEvent(self, event):
        for copy in self.copies.values():
            copy.cancelled = True  # removes its .part file
            copy.thread.join()
        self.assistant.shutdown()
        self.queryPanel.shutdown()
        self.mcp.stop()
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
        if not path.startswith('--'):  # --mcp: started by the MCP bridge
            window.openPath(path)
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
