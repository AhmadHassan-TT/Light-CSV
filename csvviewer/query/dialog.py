"""QueryPanel: the query builder as a side-panel widget. Builder controls -> live SQL -> run / export on the engine's
worker pool; table column headers can be dropped onto it (mime COLUMN_MIME) to filter, group or show a column."""
import importlib.util
import json
import os
import re
import time

from PyQt6.QtCore import (QAbstractTableModel, QEvent, QModelIndex, QObject, QPoint, QStandardPaths, Qt, QTimer,
                          pyqtSignal, pyqtSlot)
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QKeySequence, QPalette, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QSplitter, QTableView, QToolButton, QVBoxLayout, QWidget,
)

from . import builder
from .builder import BuilderError
from .engine import tableOf

Role = QPalette.ColorRole
COLUMN_MIME = 'application/x-lightcsv-column'  # UTF-8 JSON {"path", "column"} from a table header drag
MAX_DISPLAY = 100_000  # rows the results table holds at most, whatever the limit
NO_LIMIT_DISPLAY = 10_000  # rows shown when "Limit rows" is off
MAX_CELL = 500
MAX_VALUE = 50_000_000  # characters a filter value may hold (a pasted list of ids)
WIDE_AT, NARROW_AT = 900, 820  # panel width that switches to side-by-side and back (hysteresis)
KIND_HINT = {'numeric': 'a number', 'date': 'YYYY-MM-DD', 'timestamp': 'YYYY-MM-DD HH:MM'}
KIND_NAME = {'numeric': 'number', 'date': 'date', 'timestamp': 'date & time', 'text': 'text'}
DROP_VERB = {'filter': 'filter by', 'group': 'group by', 'show': 'show', 'count': 'count rows per'}
BUTTON_TEXT = {'export': ('Export…', 'Export results…'), 'open': ('Open as tab', 'Open results as new tab')}
KEEP = object()  # showOutcome: leave the result as it is


def resultsDir():
    path = os.path.join(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation), 'query-results')
    os.makedirs(path, exist_ok=True)
    return path


def norm(path):
    return os.path.normcase(os.path.abspath(path)) if path else None


def mix(a, b, t):
    # Blend color a toward b by t (0..1)
    return QColor.fromRgbF(a.redF() + (b.redF() - a.redF()) * t, a.greenF() + (b.greenF() - a.greenF()) * t,
                           a.blueF() + (b.blueF() - a.blueF()) * t)


def errorColor(p):
    # A red as light or dark as the theme's text, so it reads on either base
    dark = p.color(Role.Base).lightness() < 128
    return QColor.fromHslF(0.0, 0.75, 0.70 if dark else 0.42)


def combo(items=(), minChars=6):
    box = QComboBox()
    box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    box.setMinimumContentsLength(minChars)
    for text, data in items:
        box.addItem(text, data)
    return box


def removeButton(tip):
    b = QToolButton(text='✕', toolTip=tip)
    b.setAutoRaise(True)
    return b


def addButton(text):
    b = QPushButton(text)
    b.setFlat(True)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    b.setForegroundRole(Role.Link)
    return b


def muted(text):
    label = QLabel(text)
    label.setForegroundRole(Role.PlaceholderText)
    label.setWordWrap(True)
    return label


def hbox(*widgets, margins=(0, 0, 0, 0), spacing=4):
    layout = QHBoxLayout()
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    for w in widgets:
        layout.addStretch(1) if w is None else layout.addWidget(w)
    return layout


def displayValue(v):
    if v is None:
        return ''
    if isinstance(v, float):
        return f'{v:.10g}'
    s = str(v)
    return s[:MAX_CELL] + '…' if len(s) > MAX_CELL else s


def dropInfo(mime):
    """{'path', 'column'} of a dragged column header, or None for anything else."""
    if mime is None or not mime.hasFormat(COLUMN_MIME):
        return None
    try:
        info = json.loads(bytes(mime.data(COLUMN_MIME)).decode('utf-8'))
    except ValueError:
        return None
    if not isinstance(info, dict) or not isinstance(info.get('column'), str):
        return None
    return {'path': info.get('path') if isinstance(info.get('path'), str) else None, 'column': info['column']}


class ResultModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.columns, self.numeric, self.rows = [], [], []

    def setResult(self, columns=(), types=(), rows=()):
        self.beginResetModel()
        self.columns, self.rows = list(columns), list(rows)
        self.numeric = [builder.kind(t) == 'numeric' for t in types]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def data(self, idx, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            v = self.rows[idx.row()][idx.column()]
            return '(empty)' if v is None else displayValue(v)
        if role == Qt.ItemDataRole.ForegroundRole and self.rows[idx.row()][idx.column()] is None:
            return QApplication.palette().color(Role.PlaceholderText)
        if role == Qt.ItemDataRole.ToolTipRole:
            v = self.rows[idx.row()][idx.column()]
            return str(v)[:4000] if v is not None and len(str(v)) > 60 else None
        if role == Qt.ItemDataRole.TextAlignmentRole and self.numeric[idx.column()]:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return self.columns[section] if orientation == Qt.Orientation.Horizontal else f'{section + 1:,}'


class ValueEdit(QLineEdit):
    """Filter value box without the 32,767 character cap; pasted lines (a column copied from a sheet) become
    a comma list, which the IN / NOT IN operators read."""

    def __init__(self):
        super().__init__()
        self.setMaxLength(MAX_VALUE)

    def pasteList(self):
        text = QApplication.clipboard().text()
        lines = [line.strip() for line in text.replace('\r\n', '\n').split('\n') if line.strip()]
        self.insert(', '.join(lines) if len(lines) > 1 else text.strip('\r\n'))

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Paste):
            self.pasteList()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu()
        for action in menu.actions():
            if action.objectName() == 'edit-paste':
                action.triggered.disconnect()
                action.triggered.connect(self.pasteList)
        menu.exec(event.globalPos())
        menu.deleteLater()


class FilterRow(QWidget):
    changed = pyqtSignal()
    removed = pyqtSignal(object)

    def __init__(self, names, types):
        super().__init__()
        self.types = types
        self.compact = False
        self.column = combo([(n, n) for n in names], 8)
        self.op = combo(minChars=8)
        self.value = ValueEdit()
        self.andLabel = QLabel('and')
        self.value2 = ValueEdit()
        # Wide rows line up whether or not the value box shows
        policy = self.value.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.value.setSizePolicy(policy)
        self.values = QWidget()
        self.values.setLayout(hbox(self.value, self.andLabel, self.value2))
        remove = removeButton('Remove this filter')
        self.line1 = hbox(self.column, self.op, self.values, remove)
        self.line1.setStretch(0, 3)
        self.line1.setStretch(1, 3)
        self.line1.setStretch(2, 4)
        self.line2 = hbox()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addLayout(self.line1)
        layout.addLayout(self.line2)
        self.column.currentIndexChanged.connect(self.onColumn)
        self.op.currentIndexChanged.connect(self.onOp)
        self.value.textChanged.connect(self.changed)
        self.value2.textChanged.connect(self.changed)
        remove.clicked.connect(lambda: self.removed.emit(self))
        self.onColumn()

    def resizeEvent(self, event):
        # Narrow: the value boxes go on a second line under column + operator
        w = event.size().width()
        if (w < 400) != self.compact and (w < 400 or w > 440):
            self.setCompact(w < 400)
        super().resizeEvent(event)

    def setCompact(self, on):
        self.compact = on
        (self.line1 if on else self.line2).removeWidget(self.values)
        if on:
            self.line2.addWidget(self.values)
        else:
            self.line1.insertWidget(2, self.values, 4)
        self.onOp(emit=False)

    def onColumn(self):
        # Operators follow the column's type; keep the chosen one when it still applies
        keep = self.op.currentData()
        duckType = self.types.get(self.column.currentData(), 'VARCHAR')
        k = builder.kind(duckType)
        self.op.blockSignals(True)
        self.op.clear()
        for op, label in builder.operators(duckType):
            self.op.addItem(label, op)
        self.op.setCurrentIndex(max(0, self.op.findData(keep)))
        self.op.blockSignals(False)
        self.column.setToolTip(f'{self.column.currentText()} ({KIND_NAME[k]})')
        self.value.setPlaceholderText(KIND_HINT.get(k, 'text'))
        self.value2.setPlaceholderText(KIND_HINT.get(k, 'text'))
        self.onOp()

    def onOp(self, *_, emit=True):
        op = self.op.currentData()
        self.value.setVisible(op not in builder.NO_VALUE)
        self.values.setVisible(op not in builder.NO_VALUE or not self.compact)
        self.andLabel.setVisible(op == 'between')
        self.value2.setVisible(op == 'between')
        listOp = op in builder.LIST_OPS
        self.value.setToolTip('Values separated by commas; a pasted column (one value per line) works too' if listOp else '')
        if listOp:
            self.value.setPlaceholderText('a, b, c  (or paste a list)')
        elif op in ('equals', 'not_equals', 'contains', 'not_contains', 'starts_with', 'ends_with'):
            self.value.setPlaceholderText('text (any case)')
        else:
            self.value.setPlaceholderText(KIND_HINT.get(builder.kind(self.types.get(self.column.currentData())), 'text'))
        if emit:
            self.changed.emit()

    def set(self, column, op=None, value='', value2=''):
        # No op: the column type's default (first) operator
        self.column.setCurrentIndex(self.column.findData(column))
        self.op.setCurrentIndex(max(0, self.op.findData(op)))
        self.value.setText(value)
        self.value2.setText(value2)

    def state(self):
        return {'column': self.column.currentData(), 'op': self.op.currentData(), 'value': self.value.text(),
                'value2': self.value2.text()}


class GroupRow(QWidget):
    changed = pyqtSignal()
    removed = pyqtSignal(object)

    def __init__(self, names):
        super().__init__()
        self.column = combo([(n, n) for n in names], 8)
        remove = removeButton('Stop grouping by this column')
        self.setLayout(hbox(QLabel('Group by'), self.column, remove))
        self.layout().setStretch(1, 1)
        self.column.currentIndexChanged.connect(self.changed)
        remove.clicked.connect(lambda: self.removed.emit(self))

    def set(self, column):
        self.column.setCurrentIndex(self.column.findData(column))

    def state(self):
        return self.column.currentData()


class SummaryRow(QWidget):
    changed = pyqtSignal()
    removed = pyqtSignal(object)

    def __init__(self, names):
        super().__init__()
        self.func = combo([(label, f) for f, label in builder.AGGREGATES], 8)
        self.column = combo([('(all rows)', None)] + [(n, n) for n in names], 8)
        remove = removeButton('Remove this summary')
        self.setLayout(hbox(self.func, QLabel('of'), self.column, remove))
        self.layout().setStretch(0, 2)
        self.layout().setStretch(2, 3)
        self.func.currentIndexChanged.connect(self.onFunc)
        self.column.currentIndexChanged.connect(self.changed)
        remove.clicked.connect(lambda: self.removed.emit(self))
        self.onFunc()

    def onFunc(self):
        # "(all rows)" only makes sense for a plain count
        model = self.column.model()
        model.item(0).setEnabled(self.func.currentData() == 'count')
        if self.func.currentData() != 'count' and self.column.currentIndex() == 0 and self.column.count() > 1:
            self.column.setCurrentIndex(1)
        self.changed.emit()

    def set(self, func, column=None):
        self.func.setCurrentIndex(self.func.findData(func))
        self.column.setCurrentIndex(max(0, self.column.findData(column)))

    def state(self):
        return {'func': self.func.currentData(), 'column': self.column.currentData()}


class DropZone(QFrame):
    """A builder section that takes column drops; the panel decides what a drop does."""

    def __init__(self, panel, kind):
        super().__init__()
        self.panel, self.kind = panel, kind
        self.setObjectName('zone')
        self.setProperty('dropHover', False)
        self.setAcceptDrops(True)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(6, 2, 6, 6)
        self.box.setSpacing(4)

    def dragEnterEvent(self, event):
        self.panel.onDrag(self.kind, event, self)

    def dragMoveEvent(self, event):
        self.panel.onDrag(self.kind, event, self)

    def dragLeaveEvent(self, event):
        self.panel.onDrag(self.kind, event, self)

    def dropEvent(self, event):
        self.panel.onDrag(self.kind, event, self)


class Bridge(QObject):
    # Carries a finished Future from a worker thread back to the GUI thread
    done = pyqtSignal(object, object)

    def __init__(self, parent):
        super().__init__(parent)
        self.done.connect(self.deliver)

    @pyqtSlot(object, object)
    def deliver(self, callback, future):
        callback(future)


class QueryPanel(QWidget):
    def __init__(self, window):
        super().__init__()
        self.main = window  # not self.window: QWidget.window() must keep working
        self.engine = None  # made on first need, closed in shutdown()
        self.tables = {}  # path -> the tab's table name
        self.saved = {}  # norm(path) -> snapshot(): each file keeps its own builder, SQL and result
        self.restoring = None  # snapshot waiting for the columns of the file just picked
        self.follow = True  # next refresh picks the window's current tab
        self.busyPath = None  # file of the running query / export
        self.pendingSql = None  # (sql, run, ticket) from showQuery, waiting for the tab's columns
        self.ticket = 0
        self.finished = {}  # ticket -> outcome of a showQuery run: {path, result, status, error}
        self.path = None
        self.tabKey = None
        self.names, self.types = [], {}
        self.columnsGen = 0
        self.loading = False
        self.pending = []  # (kind, column) waiting for the columns of a file being loaded
        self.busy = None  # 'run' | 'export' | None
        self.future = None
        self.started = 0.0
        self.result = None
        self.styleKey = None
        self.builderError = None
        self.stale = True  # tab list needs a refresh on next show
        self.closed = False
        self.wide = False
        self.sqlChoice = {False: None, True: None}  # the user's SQL expand / collapse per layout, else its default
        self.hoverZone = None
        self.dragSeq = 0
        self.bridge = Bridge(self)
        self.ticker = QTimer(self, interval=200, timeout=self.tickBusy)
        self.setAcceptDrops(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        self.available = importlib.util.find_spec('duckdb') is not None
        if not self.available:
            note = QLabel('<h3>The query builder needs DuckDB</h3><p>Install it with <code>pip install duckdb</code> '
                          'and restart CSV Viewer.</p>')
            note.setWordWrap(True)
            outer.addWidget(note, 0, Qt.AlignmentFlag.AlignCenter)
            return
        self.build(outer)
        QApplication.instance().aboutToQuit.connect(self.shutdown)
        tabs = window.tabs
        tabs.currentChanged.connect(lambda _: self.tabsChanged(follow=True))
        tabs.tabCloseRequested.connect(lambda _: QTimer.singleShot(0, self.tabsChanged))
        tabs.tabBar().tabMoved.connect(lambda *_: self.tabsChanged())

    # ---------- layout ----------

    def build(self, outer):
        self.fileBox = combo(minChars=12)
        self.fileBox.setToolTip('The table (open tab) this query is for; each tab keeps its own query')
        self.fileBox.activated.connect(lambda _: self.onFilePicked())
        self.renameButton = QToolButton(text='✎', toolTip='Rename this table (also: double-click its tab)')
        self.renameButton.setAutoRaise(True)
        self.renameButton.clicked.connect(self.renameTable)
        self.columnsNote = muted('')
        top = hbox(QLabel('Table'), self.fileBox, self.renameButton, self.columnsNote, spacing=6)
        top.setStretch(1, 3)
        top.setStretch(3, 2)
        outer.addLayout(top)

        self.builderPane = QWidget()
        # Always as wide as the panel: platform styles (Windows) report larger minimum widths that would otherwise clip it
        self.builderPane.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        pane = QVBoxLayout(self.builderPane)
        pane.setContentsMargins(0, 0, 4, 0)
        pane.setSpacing(4)

        self.columnsZone = DropZone(self, 'show')
        box = self.columnsZone.box
        box.addWidget(self.section('Show columns'))
        self.columnFilter = QLineEdit(placeholderText='Find a column…', clearButtonEnabled=True)
        self.columnFilter.textChanged.connect(self.filterColumnList)
        allButton, noneButton = QPushButton('All'), QPushButton('None')
        allButton.clicked.connect(lambda: self.checkColumns(True))
        noneButton.clicked.connect(lambda: self.checkColumns(False))
        row = hbox(self.columnFilter, allButton, noneButton)
        row.setStretch(0, 1)
        box.addLayout(row)
        self.columnList = QListWidget()
        self.columnList.itemChanged.connect(lambda _: self.changed())
        box.addWidget(self.columnList)
        pane.addWidget(self.columnsZone)

        self.filterZone = DropZone(self, 'filter')
        box = self.filterZone.box
        box.addWidget(self.section('Filter rows'))
        self.filterBox = QVBoxLayout()
        self.filterBox.setSpacing(8)
        box.addLayout(self.filterBox)
        addFilter = addButton('+ Add filter')
        addFilter.clicked.connect(lambda: self.addFilter())
        self.matchBox = combo([('all', 'all'), ('any', 'any')], 3)
        self.matchBox.currentIndexChanged.connect(self.changed)
        self.matchRow = QWidget()
        self.matchRow.setLayout(hbox(QLabel('Match'), self.matchBox, QLabel('of the filters')))
        box.addLayout(hbox(addFilter, None, self.matchRow))
        pane.addWidget(self.filterZone)

        self.groupZone = DropZone(self, 'group')
        box = self.groupZone.box
        box.addWidget(self.section('Group & summarize', 'optional: one row per group, e.g. a count per city'))
        self.groupBox = QVBoxLayout()
        self.groupBox.setSpacing(4)
        box.addLayout(self.groupBox)
        self.summaryBox = QVBoxLayout()
        self.summaryBox.setSpacing(4)
        box.addLayout(self.summaryBox)
        addGroup, addSummary = addButton('+ Group by column'), addButton('+ Add summary')
        addGroup.clicked.connect(lambda: self.addGroup())
        addSummary.clicked.connect(lambda: self.addSummary())
        box.addLayout(hbox(addGroup, addSummary, None))
        pane.addWidget(self.groupZone)

        rest = QVBoxLayout()
        rest.setContentsMargins(7, 0, 7, 0)
        rest.setSpacing(4)
        rest.addWidget(self.section('Sort by'))
        self.sortBox = combo([('(no sorting)', None)], 10)
        self.sortBox.currentIndexChanged.connect(self.changed)
        self.sortDir = combo([('Ascending', False), ('Descending', True)], 8)
        self.sortDir.currentIndexChanged.connect(self.changed)
        row = hbox(self.sortBox, self.sortDir)
        row.setStretch(0, 1)
        rest.addLayout(row)
        rest.addWidget(self.section('Limit rows'))
        self.limitSpin = QSpinBox(minimum=0, maximum=100_000_000, value=1000, singleStep=1000)
        self.limitSpin.setSpecialValueText('No limit')
        self.limitSpin.setGroupSeparatorShown(True)
        self.limitSpin.valueChanged.connect(self.changed)
        self.distinct = QCheckBox('Only distinct rows')
        self.distinct.toggled.connect(self.changed)
        rest.addLayout(hbox(self.limitSpin, self.distinct, None, spacing=10))
        pane.addLayout(rest)
        pane.addStretch(1)

        self.scroll = QScrollArea()
        self.scroll.setWidget(self.builderPane)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setMinimumHeight(120)

        self.resultsPane = QWidget()
        side = QVBoxLayout(self.resultsPane)
        side.setContentsMargins(0, 4, 0, 0)
        side.setSpacing(4)
        self.sqlToggle = QToolButton(checkable=True, toolTip='Show or hide the SQL the builder writes')
        self.sqlToggle.setAutoRaise(True)
        self.sqlToggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        font = self.sqlToggle.font()
        font.setWeight(QFont.Weight.DemiBold)
        self.sqlToggle.setFont(font)
        self.sqlToggle.clicked.connect(lambda on: self.setSqlOpen(on, True))
        self.sqlPeek = muted('')
        self.sqlPeek.setWordWrap(False)
        self.sqlPeek.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.editSql = QCheckBox('Edit SQL')
        self.editSql.setToolTip('Write the query yourself; the builder is paused. Each tab is a table by the name on '
                                'its tab (join them freely); <name>_text is an all-text view of it')
        self.editSql.toggled.connect(self.onEditSql)
        row = hbox(self.sqlToggle, self.sqlPeek, self.editSql, spacing=6)
        row.setStretch(1, 1)
        row.insertStretch(2, 0)  # keeps Edit SQL on the right while the peek is hidden
        side.addLayout(row)
        self.sqlBox = QPlainTextEdit(readOnly=True)
        self.sqlBox.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.sqlBox.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.sqlBox.setTabChangesFocus(True)
        self.sqlBox.setMinimumHeight(60)
        # SQL above, buttons + results below; the handle between them sizes the SQL box (long queries)
        self.sqlSplit = QSplitter(Qt.Orientation.Vertical)
        self.sqlSplit.setChildrenCollapsible(False)
        self.sqlSplit.addWidget(self.sqlBox)
        below = QWidget()
        side.addWidget(self.sqlSplit, 1)
        side = QVBoxLayout(below)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(4)
        self.sqlSplit.addWidget(below)
        self.sqlSplit.setStretchFactor(0, 0)
        self.sqlSplit.setStretchFactor(1, 1)

        self.runButton = QPushButton('▶ Run', toolTip='Run the query (Ctrl+Enter)', default=True)
        self.runButton.setObjectName('primary')
        self.stopButton = QPushButton('Stop', toolTip='Stop the running query or export', enabled=False)
        self.clearButton = QPushButton('Clear', toolTip='Start over with an empty query')
        self.exportButton = QPushButton(toolTip='Save the whole result (not just the rows shown) as CSV')
        self.openButton = QPushButton(toolTip='Save the whole result and open it in the viewer')
        self.runButton.clicked.connect(self.runQuery)
        self.stopButton.clicked.connect(self.stop)
        self.clearButton.clicked.connect(self.clear)
        self.exportButton.clicked.connect(self.exportDialog)
        self.openButton.clicked.connect(self.openAsTab)
        self.buttonRow = hbox(self.runButton, self.stopButton, self.clearButton, None, self.exportButton, self.openButton)
        self.buttonRow2 = hbox(None)
        self.buttonsWrapped = False
        side.addLayout(self.buttonRow)
        side.addLayout(self.buttonRow2)

        self.statusLabel = QLabel('')
        self.statusLabel.setWordWrap(True)
        self.statusLabel.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.busyBar = QProgressBar(minimum=0, maximum=0, maximumWidth=120, maximumHeight=12, textVisible=False)
        self.busyBar.hide()
        row = hbox(self.statusLabel, self.busyBar)
        row.setStretch(0, 1)
        side.addLayout(row)
        self.errorLabel = QLabel('')
        self.errorLabel.setObjectName('error')
        self.errorLabel.setWordWrap(True)
        self.errorLabel.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.errorLabel.hide()
        side.addWidget(self.errorLabel)

        self.model = ResultModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ContiguousSelection)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setDefaultSectionSize(120)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.setMinimumHeight(80)
        side.addWidget(self.table, 1)

        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.addWidget(self.scroll)
        self.split.addWidget(self.resultsPane)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 1)
        self.split.setChildrenCollapsible(False)
        self.split.splitterMoved.connect(lambda *_: self.fitButtons())
        outer.addWidget(self.split, 1)

        # Floats over the panel, so showing it never moves the zone under the cursor
        self.dropHint = QLabel(self)
        self.dropHint.setObjectName('dropHint')
        self.dropHint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.dropHint.hide()
        for keys in ('Ctrl+Return', 'Ctrl+Enter'):
            QShortcut(QKeySequence(keys), self, self.runQuery, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.filters, self.groups, self.summaries = [], [], []
        self.watchDrops(self)
        self.applyMode(False)
        self.restyle()
        self.updateButtons()

    def section(self, title, hint=None):
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 4, 0, 0)
        label = QLabel(title)
        font = label.font()
        font.setWeight(QFont.Weight.DemiBold)
        label.setFont(font)
        layout.addWidget(label)
        if hint:
            layout.addWidget(muted(hint), 1)
        layout.addStretch(0 if hint else 1)
        return w

    def applyMode(self, wide):
        # Narrow (side panel): builder above results; wide (floated / expanded dock): side by side like the old dialog
        self.saveLayout()
        self.wide = wide
        orientation = Qt.Orientation.Horizontal if wide else Qt.Orientation.Vertical
        state = self.main.settings.value('query/split' + ('Wide' if wide else 'Narrow'))
        restored = state is not None and self.split.restoreState(state)
        self.split.setOrientation(orientation)
        if not restored:
            size = max(self.width(), 900) if wide else max(self.height(), 600)
            self.split.setSizes([440, size - 440] if wide else [int(size * 0.55), int(size * 0.45)])
        self.builderPane.layout().setContentsMargins(0, 0, 8 if wide else 4, 0)
        self.resultsPane.layout().setContentsMargins(6 if wide else 0, 4 if not wide else 0, 0, 0)
        self.columnList.setMaximumHeight(150 if wide else 96)
        # Same SQL box height as before by default; drag the handle under it for long queries
        self.sqlSplit.setSizes([200 if wide else 130, max(self.height(), 600)])
        self.exportButton.setText(BUTTON_TEXT['export'][wide])
        self.openButton.setText(BUTTON_TEXT['open'][wide])
        self.setSqlOpen(wide if self.sqlChoice[wide] is None else self.sqlChoice[wide])
        self.fitButtons()

    def setSqlOpen(self, on, byUser=False):
        if byUser:
            self.sqlChoice[self.wide] = on
        on = on or self.editSql.isChecked()
        self.sqlToggle.setChecked(on)
        self.sqlToggle.setText(('▾' if on else '▸') + ' SQL')
        self.sqlBox.setVisible(on)
        self.sqlPeek.setVisible(not on)

    def fitButtons(self):
        # Export / Open drop to a second line when one line is too narrow for all five buttons
        buttons = (self.runButton, self.stopButton, self.clearButton, self.exportButton, self.openButton)
        need = sum(b.sizeHint().width() for b in buttons) + 4 * 6 + 12
        wrap = need > self.resultsPane.width()
        if wrap == self.buttonsWrapped:
            return
        self.buttonsWrapped = wrap
        src, dst = (self.buttonRow, self.buttonRow2) if wrap else (self.buttonRow2, self.buttonRow)
        for b in (self.exportButton, self.openButton):
            src.removeWidget(b)
            dst.addWidget(b)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.available:
            return
        w = event.size().width()
        if not self.wide and w >= WIDE_AT:
            self.applyMode(True)
        elif self.wide and w < NARROW_AT:
            self.applyMode(False)
        QTimer.singleShot(0, self.fitButtons)

    def restyle(self):
        # Colors derive from the palette, so light, dark and system themes all fit; redone on theme change
        p = QApplication.palette()
        base, text, hl, err = p.color(Role.Base), p.color(Role.Text), p.color(Role.Highlight), errorColor(p)
        key = (base.name(), text.name(), hl.name(), p.color(Role.Window).name(), p.color(Role.WindowText).name())
        if key == self.styleKey or not self.available:
            return
        self.styleKey = key
        self.setPalette(p)  # a widget with a stylesheet no longer follows app palette changes by itself
        window = p.color(Role.Window)
        c = lambda col: col.name()
        self.setStyleSheet(f"""
            QFrame#zone {{ border: 1px solid transparent; border-radius: 6px; }}
            QFrame#zone[dropHover="true"] {{ border: 1px solid {c(hl)}; background: {c(mix(window, hl, 0.14))}; }}
            QLabel#dropHint {{ background: {c(hl)}; color: {c(p.color(Role.HighlightedText))}; border-radius: 4px;
                               padding: 3px 8px; font-weight: bold; }}
            QLabel#error {{ color: {c(err)}; background: {c(mix(base, err, 0.10))}; border: 1px solid {c(mix(base, err, 0.45))};
                            border-radius: 5px; padding: 6px 8px; }}
            QPushButton#primary {{ background: {c(hl)}; color: {c(p.color(Role.HighlightedText))}; border: none;
                                   border-radius: 4px; padding: 4px 12px; font-weight: bold; }}
            QPushButton#primary:hover {{ background: {c(mix(hl, base, 0.15))}; }}
            QPushButton#primary:disabled {{ background: {c(mix(base, text, 0.15))}; color: {c(mix(base, text, 0.45))}; }}
        """)

    def event(self, event):
        # Our own palette is explicit, so a theme switch arrives as ApplicationPaletteChange (not via changeEvent)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange, QEvent.Type.StyleChange):
            QTimer.singleShot(0, self.restyle)
        return super().event(event)

    # ---------- tabs / columns ----------

    def ensureEngine(self):
        if self.engine is None and self.available and not self.closed:
            from .engine import QueryEngine
            self.engine = QueryEngine()
        return self.engine

    def tabsChanged(self, follow=False):
        # Hidden (other side tab, dock closed): catch up on next show
        if self.closed or not self.available:
            return
        self.follow = self.follow or follow
        if self.isVisible():
            self.refreshTabs()
        else:
            self.stale = True

    def showEvent(self, event):
        super().showEvent(event)
        if self.stale and self.available and not self.closed:
            self.refreshTabs()

    def refreshTabs(self, prefer=None):
        """Reload the file list from the window's tabs; keeps the chosen file while it is open."""
        if not self.ensureEngine():
            return
        from csvviewer.assistant.context import snapshotTabs
        self.stale = False
        tabs = snapshotTabs(self.main)
        self.engine.setTabs(tabs)
        self.tables = {t.path: tableOf(t) for t in tabs}
        byNorm = {norm(p): p for p in self.tables}
        prefer = byNorm.get(norm(prefer))
        current = next((t.path for t in tabs if t.current), tabs[0].path if tabs else None)
        keep = prefer or (current if self.follow or self.path not in self.tables else self.path)
        self.follow = False
        self.fileBox.blockSignals(True)
        self.fileBox.clear()
        from csvviewer.sqlviews import defaultTableName
        for t in tabs:
            table = tableOf(t)
            self.fileBox.addItem(table if table == defaultTableName(t.name) else f'{table} · {t.name}', t.path)
            self.fileBox.setItemData(self.fileBox.count() - 1, f'{t.path}\nTable name in SQL: {tableOf(t)}',
                                     Qt.ItemDataRole.ToolTipRole)
        self.fileBox.setCurrentIndex(self.fileBox.findData(keep))
        self.fileBox.blockSignals(False)
        self.onFile()

    def selectTabPath(self, path):
        """Pick the open tab of this file (the builder starts over for a new file); False when it is not open."""
        self.refreshTabs(prefer=path)
        return self.path is not None and norm(self.path) == norm(path)

    def onFilePicked(self):
        # Picking a table here shows its tab too, so the panel and the window agree
        path = self.fileBox.currentData()
        for i in range(self.main.tabs.count()):
            tab = self.main.tabs.widget(i)
            if tab.index and norm(tab.index.path) == norm(path):
                self.main.tabs.setCurrentIndex(i)
        self.onFile()

    def onFile(self):
        path = self.fileBox.currentData()
        tab = next((t for t in self.engine.tabs.values() if t.path == path), None)
        key = self.engine.tabKey(tab) if tab else None
        self.renameButton.setEnabled(tab is not None)
        if path != self.path:
            if self.path:
                self.saved[norm(self.path)] = self.restoring if self.restoring is not None else self.snapshot()
            if self.pendingSql:  # meant for the table being left
                self.finished[self.pendingSql[2]] = {'path': self.path, 'result': None, 'status': '',
                                                     'error': 'another tab was picked before the query could be shown'}
                self.pendingSql = None
            self.path = path
            self.pending.clear()
            self.resetBuilder()
            self.setColumns([], {})
            self.setEditSql(False)
            self.setSql('')
            self.restoring = self.saved.get(norm(path)) if path else None
            self.showOutcome(path, **{k: (self.restoring or {}).get(k) for k in ('result', 'status', 'error')})
        if key != self.tabKey:
            self.tabKey = key
            self.loadColumns()
        self.changed()

    def loadColumns(self):
        self.columnsGen += 1
        gen = self.columnsGen
        if not self.path:
            self.loading = False
            self.columnsNote.setText('Open a CSV file in the viewer first.')
            self.updateButtons()
            return
        self.loading = True
        self.columnsNote.setText('Reading columns…')
        self.builderPane.setEnabled(False)
        table = self.tables[self.path]
        self.job(lambda: self.engine.columns(table), lambda f: self.onColumns(gen, f))

    def onColumns(self, gen, future):
        if gen != self.columnsGen or self.closed:
            return
        self.loading = False
        try:
            cols = future.result()
        except Exception as e:
            self.pending.clear()
            self.restoring = None
            self.columnsNote.setText('')
            self.showError(f'Could not read the columns: {e}')
            if self.pendingSql:
                self.finished[self.pendingSql[2]] = {'path': self.path, 'result': None, 'status': '',
                                                     'error': self.errorLabel.text()}
                self.pendingSql = None
            return
        self.setColumns([c for c, _ in cols], dict(cols))
        table = self.tables.get(self.path)
        textOnly = table is not None and self.engine.viewName(table).endswith('_text')
        self.columnsNote.setText(f'{os.path.basename(self.path)} · {len(cols):,} columns' + (' (as text)' if textOnly else ''))
        self.columnsNote.setToolTip(self.path)
        self.builderPane.setEnabled(not self.editSql.isChecked())
        snap, self.restoring = self.restoring, None
        if snap:
            self.restore(snap)
        self.changed()
        pending, self.pending = self.pending, []
        for kind, column in pending:
            self.applyColumn(kind, column)
        self.applyPendingSql()

    # ---------- SQL from outside (the assistant, MCP) ----------

    def showQuery(self, sql, path=None, run=True):
        """Put SQL into the panel (Edit SQL) of a tab's table (default: the shown one) and run it, as if the user
        typed it. Returns a ticket; finished[ticket] holds the outcome once it ran, or None when nothing can run."""
        if not self.available or self.closed or self.busy:
            return None
        self.main.showSidePanel('query')
        if path:
            for i in range(self.main.tabs.count()):
                tab = self.main.tabs.widget(i)
                if tab.index and norm(tab.index.path) == norm(path):
                    self.main.tabs.setCurrentIndex(i)
            if not self.selectTabPath(path):
                return None
        elif self.stale or self.engine is None:
            self.refreshTabs()
        if not self.path:
            return None
        self.ticket += 1
        self.pendingSql = (sql, run, self.ticket)
        if not self.loading and self.names:
            self.applyPendingSql()
        return self.ticket

    def applyPendingSql(self):
        if not self.pendingSql:
            return
        sql, run, ticket = self.pendingSql
        self.pendingSql = None
        self.setEditSql(True, sql)
        self.hideError()
        if not run:
            self.finished[ticket] = {'path': self.path, 'result': None, 'status': 'not run', 'error': ''}
        elif not self.runQuery(ticket):
            self.finished[ticket] = {'path': self.path, 'result': None, 'status': '',
                                     'error': self.errorLabel.text() or 'the query did not start'}

    def setColumns(self, names, types):
        # New names for every picker; rows keep their choice when the column still exists
        self.names, self.types = names, types
        checked = {self.columnList.item(i).text() for i in range(self.columnList.count())
                   if self.columnList.item(i).checkState() == Qt.CheckState.Checked}
        fresh = not checked or not checked & set(names)
        self.columnList.blockSignals(True)
        self.columnList.clear()
        for n in names:
            item = QListWidgetItem(n)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if fresh or n in checked else Qt.CheckState.Unchecked)
            item.setToolTip(f'{n} ({KIND_NAME[builder.kind(types.get(n))]})')
            self.columnList.addItem(item)
        self.columnList.blockSignals(False)
        self.filterColumnList(self.columnFilter.text())
        for r in self.filters + self.groups + self.summaries:
            self.refillCombo(r.column, names, isinstance(r, SummaryRow))
        for r in self.filters:
            r.types = types
            r.onColumn()

    def refillCombo(self, box, names, allRows):
        keep = box.currentData()
        box.blockSignals(True)
        box.clear()
        if allRows:
            box.addItem('(all rows)', None)
        for n in names:
            box.addItem(n, n)
        box.setCurrentIndex(max(0, box.findData(keep)))
        box.blockSignals(False)

    def filterColumnList(self, text):
        text = text.strip().lower()
        for i in range(self.columnList.count()):
            item = self.columnList.item(i)
            item.setHidden(bool(text) and text not in item.text().lower())

    def checkColumns(self, on):
        # All / None act on the columns the find box shows
        self.columnList.blockSignals(True)
        for i in range(self.columnList.count()):
            item = self.columnList.item(i)
            if not item.isHidden():
                item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.columnList.blockSignals(False)
        self.changed()

    # ---------- column actions (header menu and drops) ----------

    def addFilterForColumn(self, path, column):
        """Filter row on a column of a tab (switching to that file first); the value box gets the focus."""
        return self.columnAction('filter', path, column)

    def addGroupForColumn(self, path, column):
        return self.columnAction('group', path, column)

    def countValuesForColumn(self, path, column):
        """Group by the column with a row count, most rows first, and run it."""
        return self.columnAction('count', path, column)

    def showColumn(self, path, column):
        """Make sure a column is ticked in "Show columns" and scrolled into view."""
        return self.columnAction('show', path, column)

    def columnAction(self, kind, path, column):
        # Returns the row / list item, or None when it waits for the file's columns (applied once they load)
        if not self.available or self.closed:
            return None
        if self.stale or self.engine is None:
            self.refreshTabs()
        if path and norm(path) != norm(self.path) and not self.selectTabPath(path):
            self.showError(f'{os.path.basename(path)} is not open in a tab.')
            return None
        if self.editSql.isChecked():
            self.editSql.setChecked(False)  # asked for the builder, so it takes over again
        if self.loading or not self.names:
            self.pending.append((kind, column))
            return None
        return self.applyColumn(kind, column)

    def applyColumn(self, kind, column):
        name = column if column in self.types else next((n for n in self.names if n.lower() == column.lower()), None)
        if name is None:
            self.showError(f'There is no column "{column}" in {os.path.basename(self.path or "")}.')
            return None
        self.hideError()
        if kind == 'filter':
            row = self.addFilter(name)
            QTimer.singleShot(0, lambda: self.reveal(row, row.value))
            return row
        if kind in ('group', 'count'):
            row = next((r for r in self.groups if r.state() == name), None) or self.addGroup(name)
            QTimer.singleShot(0, lambda: self.reveal(row, row.column))
            if kind == 'count':
                if not any(r.state() == {'func': 'count', 'column': None} for r in self.summaries):
                    self.addSummary('count')
                self.setSort('Count', True)
                self.runQuery()
            return row
        item = next(self.columnList.item(i) for i in range(self.columnList.count()) if self.columnList.item(i).text() == name)
        if item.isHidden():
            self.columnFilter.clear()
        item.setCheckState(Qt.CheckState.Checked)
        self.columnList.setCurrentItem(item)
        self.columnList.scrollToItem(item)
        QTimer.singleShot(0, lambda: self.reveal(self.columnsZone, None))
        return item

    def reveal(self, widget, focus):
        try:
            self.scroll.ensureWidgetVisible(widget, 0, 8)
            if focus is not None:
                focus.setFocus()
        except RuntimeError:
            pass  # row removed meanwhile

    def focusBuilder(self):
        """Keyboard focus into the builder: the newest filter's value, else the file picker."""
        if not self.available:
            return
        if self.stale:
            self.refreshTabs()
        if self.filters and self.filters[-1].value.isVisible():
            self.reveal(self.filters[-1], self.filters[-1].value)
        else:
            self.fileBox.setFocus()

    # ---------- drag & drop ----------

    def dragEnterEvent(self, event):
        self.onDrag('filter', event, None)

    def dragMoveEvent(self, event):
        self.onDrag('filter', event, None)

    def dragLeaveEvent(self, event):
        self.onDrag('filter', event, None)

    def dropEvent(self, event):
        self.onDrag('filter', event, None)

    def watchDrops(self, root):
        # Text boxes take the drag's plain text themselves; route column drags to their zone instead
        for w in root.findChildren(QLineEdit) + [self.sqlBox.viewport()]:
            if w.acceptDrops() and not w.property('dropWatched'):
                w.setProperty('dropWatched', True)
                w.installEventFilter(self)

    def eventFilter(self, obj, event):
        t = event.type()
        if t in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop) and dropInfo(event.mimeData()):
            if self.editSql.isChecked():
                return False  # Edit SQL: the column name drops into the text as usual
            zone = obj
            while zone is not None and zone is not self and not isinstance(zone, DropZone):
                zone = zone.parentWidget()
            zone = zone if isinstance(zone, DropZone) else None
            self.onDrag(zone.kind if zone else 'filter', event, zone, obj)
            return True
        if t == QEvent.Type.DragLeave and obj.property('dropWatched'):
            self.onDrag('filter', event, None)
        return False

    def onDrag(self, kind, event, zone, source=None):
        # One handler for the three zones and the panel itself (anywhere else = filter)
        if event.type() == QEvent.Type.DragLeave:
            seq = self.dragSeq
            QTimer.singleShot(60, lambda: seq == self.dragSeq and self.endHover())  # unless the drag moved on to another zone
            return
        info = dropInfo(event.mimeData())
        if info is None or not self.available or self.closed or self.editSql.isChecked():
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.dragSeq += 1
        if event.type() == QEvent.Type.Drop:
            self.endHover()
            self.columnAction(kind, info['path'], info['column'])
            return
        pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
        self.hover(self.filterZone if zone is None else zone, kind, info['column'], (source or zone or self).mapTo(self, pos))

    def hover(self, zone, kind, column, pos):
        if zone is not self.hoverZone:
            self.endHover(hint=False)
            self.hoverZone = zone
            self.setZoneHover(zone, True)
        self.dropHint.setText(f'Drop to {DROP_VERB[kind]} {column}')
        self.dropHint.adjustSize()
        # Just below-right of the cursor, kept inside the panel
        x = min(max(4, pos.x() + 14), max(4, self.width() - self.dropHint.width() - 4))
        y = pos.y() + 18 if pos.y() + 18 + self.dropHint.height() < self.height() else pos.y() - self.dropHint.height() - 8
        self.dropHint.move(QPoint(x, max(4, y)))
        self.dropHint.show()
        self.dropHint.raise_()

    def endHover(self, hint=True):
        if self.hoverZone is not None:
            self.setZoneHover(self.hoverZone, False)
            self.hoverZone = None
        if hint:
            self.dropHint.hide()

    def setZoneHover(self, zone, on):
        zone.setProperty('dropHover', on)
        zone.style().unpolish(zone)
        zone.style().polish(zone)
        zone.update()

    # ---------- builder rows ----------

    def addRow(self, rows, box, row):
        rows.append(row)
        box.addWidget(row)
        row.changed.connect(self.changed)
        row.removed.connect(lambda r: self.removeRow(rows, r))
        self.changed()
        return row

    def removeRow(self, rows, row):
        rows.remove(row)
        self.dropRow(row)
        self.changed()

    def dropRow(self, row):
        # Hidden in place, not unparented: an unparented row holding the focus would become a stray window.
        # Its layout lets go of it when it is deleted.
        row.hide()
        row.deleteLater()

    def addFilter(self, column=None, op=None, value='', value2=''):
        row = self.addRow(self.filters, self.filterBox, FilterRow(self.names, self.types))
        self.watchDrops(row)
        if column is not None:
            row.set(column, op, value, value2)
        return row

    def addGroup(self, column=None):
        row = self.addRow(self.groups, self.groupBox, GroupRow(self.names))
        if column is not None:
            row.set(column)
        return row

    def addSummary(self, func='count', column=None):
        row = self.addRow(self.summaries, self.summaryBox, SummaryRow(self.names))
        row.set(func, column)
        return row

    def setSort(self, column=None, desc=False):
        self.refreshSort()
        self.sortBox.setCurrentIndex(max(0, self.sortBox.findData(column)))
        self.sortDir.setCurrentIndex(1 if desc else 0)

    def resetBuilder(self):
        for rows in (self.filters, self.groups, self.summaries):
            for r in rows:
                self.dropRow(r)
            rows.clear()
        for w in (self.matchBox, self.sortBox, self.sortDir, self.limitSpin, self.distinct, self.columnFilter):
            w.blockSignals(True)
        self.matchBox.setCurrentIndex(0)
        self.sortBox.setCurrentIndex(0)
        self.sortDir.setCurrentIndex(0)
        self.limitSpin.setValue(1000)
        self.distinct.setChecked(False)
        self.columnFilter.clear()
        for w in (self.matchBox, self.sortBox, self.sortDir, self.limitSpin, self.distinct, self.columnFilter):
            w.blockSignals(False)
        self.filterColumnList('')
        self.checkColumns(True)

    def checkedColumns(self):
        return [self.columnList.item(i).text() for i in range(self.columnList.count())
                if self.columnList.item(i).checkState() == Qt.CheckState.Checked]

    def state(self):
        table = self.tables.get(self.path)
        checked = self.checkedColumns()
        sort = self.sortBox.currentData()
        return {'tabId': 0, 'view': self.engine.viewName(table) if table else None, 'types': self.types,
                'columns': [] if len(checked) == len(self.names) else checked,
                'filters': [r.state() for r in self.filters], 'match': self.matchBox.currentData(),
                'groupBy': [r.state() for r in self.groups], 'aggregates': [r.state() for r in self.summaries],
                'orderBy': [{'column': sort, 'desc': self.sortDir.currentData()}] if sort else [],
                'limit': self.limitSpin.value(), 'distinct': self.distinct.isChecked()}

    def refreshSort(self):
        # Sorting can only pick what the query returns
        options = builder.outputColumns(self.state() | {'orderBy': []}, self.names)
        keep = self.sortBox.currentData()
        self.sortBox.blockSignals(True)
        self.sortBox.clear()
        self.sortBox.addItem('(no sorting)', None)
        for n in options:
            self.sortBox.addItem(n, n)
        self.sortBox.setCurrentIndex(max(0, self.sortBox.findData(keep)))
        self.sortBox.blockSignals(False)

    def changed(self, *_):
        if not self.available or self.engine is None:
            return
        self.matchRow.setVisible(len(self.filters) > 1)
        self.refreshSort()
        if self.editSql.isChecked():
            return
        if not self.path:
            self.setSql('')
            return
        if not self.names:
            return
        try:
            self.setSql(builder.buildSql(self.state()))
            self.builderError = None
        except BuilderError as e:
            self.builderError = str(e)
            self.setSql(f'-- {e}')
        self.updateButtons()

    def setSql(self, sql):
        self.sqlBox.setPlainText(sql)
        self.sqlPeek.setText(' '.join(sql.split()))

    def setEditSql(self, on, sql=None):
        """Edit SQL on or off without taking the keyboard focus (switching tables); sql replaces the text."""
        if self.editSql.isChecked() != on:
            self.editSql.blockSignals(True)
            self.editSql.setChecked(on)
            self.editSql.blockSignals(False)
            self.onEditSql(on, focus=False)
        if sql is not None:
            self.setSql(sql)

    def onEditSql(self, on, focus=True):
        self.sqlBox.setReadOnly(not on)
        self.builderPane.setEnabled(not on and bool(self.names))
        self.setSqlOpen(self.sqlToggle.isChecked() or on)
        if on and focus:
            self.sqlBox.setFocus()
        else:
            self.setSqlOpen(self.wide if self.sqlChoice[self.wide] is None else self.sqlChoice[self.wide])
            self.changed()

    def snapshot(self):
        """This file's query: builder rows, options, typed SQL and the last result."""
        return {'checked': self.checkedColumns(), 'filters': [r.state() for r in self.filters],
                'match': self.matchBox.currentData(), 'groups': [r.state() for r in self.groups],
                'summaries': [r.state() for r in self.summaries], 'sort': self.sortBox.currentData(),
                'desc': self.sortDir.currentData(), 'limit': self.limitSpin.value(), 'distinct': self.distinct.isChecked(),
                'editSql': self.editSql.isChecked(), 'sql': self.sqlBox.toPlainText(), 'result': self.result,
                'status': self.statusLabel.text(), 'error': self.errorLabel.text()}

    def restore(self, snap):
        """Rebuild a snapshot() once the file's columns are loaded; columns that are gone are left out."""
        names = set(self.names)
        for f in snap['filters']:
            if f['column'] in names:
                self.addFilter(f['column'], f['op'], f['value'], f['value2'])
        for g in snap['groups']:
            if g in names:
                self.addGroup(g)
        for a in snap['summaries']:
            if a['column'] is None or a['column'] in names:
                self.addSummary(a['func'], a['column'])
        self.matchBox.setCurrentIndex(max(0, self.matchBox.findData(snap['match'])))
        if set(snap['checked']) & names:
            self.columnList.blockSignals(True)
            for i in range(self.columnList.count()):
                item = self.columnList.item(i)
                item.setCheckState(Qt.CheckState.Checked if item.text() in snap['checked'] else Qt.CheckState.Unchecked)
            self.columnList.blockSignals(False)
        self.limitSpin.setValue(snap['limit'])
        self.distinct.setChecked(snap['distinct'])
        self.setSort(snap['sort'], snap['desc'])
        if snap['editSql']:
            self.setEditSql(True, snap['sql'])

    def showOutcome(self, path, result=KEEP, status=None, error=None):
        """Show a result / status / error for a file, or keep it with that file's query when another one is shown."""
        if not path:
            return
        if norm(path) != norm(self.path):
            snap = self.saved.get(norm(path))
            if snap is not None:
                snap.update({k: v for k, v in (('result', result), ('status', status), ('error', error)) if v is not KEEP})
            return
        if result is not KEEP:
            self.result = result
            self.model.setResult(*((result['columns'], result['types'], result['rows']) if result else ()))
            if result:
                self.table.resizeColumnsToContents()
                for c in range(self.model.columnCount()):
                    self.table.setColumnWidth(c, min(self.table.columnWidth(c), 360))
        self.statusLabel.setText(status or '')
        if error:
            self.showError(error)
        else:
            self.hideError()

    def renameTable(self):
        for i in range(self.main.tabs.count()):
            tab = self.main.tabs.widget(i)
            if tab.index and norm(tab.index.path) == norm(self.path):
                self.main.renameTable(i)
                return

    def tableRenamed(self, old, new):
        """A tab got a new table name: typed SQL follows it, the picker shows it."""
        if not self.available or self.closed:
            return
        pattern = re.compile(rf'\b{re.escape(old)}(_text)?\b', re.I)
        rename = lambda sql: pattern.sub(lambda m: new + (m.group(1) or ''), sql or '')
        for snap in list(self.saved.values()) + ([self.restoring] if self.restoring else []):
            if snap.get('editSql'):
                snap['sql'] = rename(snap['sql'])
        if self.editSql.isChecked():
            self.setSql(rename(self.sqlBox.toPlainText()))
        if self.engine is not None:
            self.engine.renamed(old, new)
        self.tabsChanged()

    def fileMoved(self, old, new, oldName, newName):
        """A tab now shows another file (a query result saved under a name): its query goes along."""
        if not self.available or self.closed:
            return
        if norm(old) in self.saved:
            self.saved[norm(new)] = self.saved.pop(norm(old))
        if norm(self.path) == norm(old):
            self.path = new
        if oldName != newName:
            self.tableRenamed(oldName, newName)
        else:
            self.tabsChanged()

    def clear(self):
        self.restoring = None  # a query still waiting for its columns is cleared too
        if self.editSql.isChecked():
            self.editSql.setChecked(False)
        self.resetBuilder()
        self.model.setResult()
        self.result = None
        self.statusLabel.setText('')
        self.hideError()
        self.changed()

    # ---------- running ----------

    def job(self, fn, callback):
        future = self.engine.submit(fn)
        future.add_done_callback(lambda f: self.bridge.done.emit(callback, f))
        return future

    def currentSql(self):
        if self.editSql.isChecked():
            return self.sqlBox.toPlainText()
        return builder.buildSql(self.state())

    def startBusy(self, kind, fn, callback):
        self.refreshTabs()
        try:
            sql = self.currentSql()
        except BuilderError as e:
            self.showError(str(e))
            return None
        self.hideError()
        self.busy = kind
        self.busyPath = self.path
        self.started = time.monotonic()
        self.busyBar.show()
        self.ticker.start()
        self.tickBusy()
        self.updateButtons()
        self.future = self.job(lambda: fn(sql), callback)
        return self.future

    def tickBusy(self):
        verb = {'run': 'Running', 'export': 'Saving the whole result'}.get(self.busy)
        if verb:
            self.statusLabel.setText(f'{verb}… {time.monotonic() - self.started:.1f} s')

    def endBusy(self):
        path, self.busyPath = self.busyPath, None
        self.busy = None
        self.future = None
        self.ticker.stop()
        self.busyBar.hide()
        self.updateButtons()
        return path

    def runQuery(self, ticket=None):
        """Run the query; True when it started."""
        if not self.available or self.busy or not self.path or self.closed:
            return False
        limit = self.limitSpin.value()
        shown = min(limit or NO_LIMIT_DISPLAY, MAX_DISPLAY)
        typed = self.editSql.isChecked()
        return self.startBusy('run', lambda sql: self.engine.run(sql, shown),
                              lambda f: self.onRun(f, limit, shown, typed, ticket)) is not None

    def onRun(self, future, limit, shown, typed, ticket=None):
        path = self.endBusy()
        try:
            res = future.result()
        except Exception as e:
            self.showOutcome(path, status='', error=str(e) or 'Stopped.')
            if ticket:
                self.finished[ticket] = {'path': path, 'result': None, 'status': '', 'error': str(e) or 'Stopped.'}
            return
        n = len(res['rows'])
        if res['more']:
            text = f'First {n:,} rows shown (there are more; export to get them all)'
        elif limit and n == limit and not typed:
            text = f'{n:,} rows (stopped at the limit of {limit:,})'
        else:
            text = f'{n:,} row' + ('' if n == 1 else 's')
        self.showOutcome(path, res, f'{text} · {res["seconds"]:.2f} s', '')
        if ticket:
            self.finished[ticket] = {'path': path, 'result': res, 'status': f'{text} · {res["seconds"]:.2f} s', 'error': ''}
        self.updateButtons()

    def stop(self):
        if self.future is not None:
            self.future.cancel()
        if self.engine is not None:
            self.engine.cancel()

    def exportDialog(self):
        settings = self.main.settings
        start = settings.value('query/exportDir') or os.path.dirname(self.path or '') or os.path.expanduser('~')
        stem = os.path.splitext(os.path.basename(self.path or 'query'))[0]
        path, _ = QFileDialog.getSaveFileName(self, 'Export results', os.path.join(start, f'{stem}-query.csv'),
                                              'CSV files (*.csv);;All files (*)')
        if path:
            settings.setValue('query/exportDir', os.path.dirname(path))
            self.exportTo(path)

    def exportTo(self, path, openAfter=False):
        if self.busy or not self.path or self.closed:
            return None
        return self.startBusy('export', lambda sql: self.engine.export(sql, path), lambda f: self.onExport(f, openAfter))

    def openAsTab(self):
        stem = os.path.splitext(os.path.basename(self.path or 'query'))[0]
        return self.exportTo(os.path.join(resultsDir(), f'{stem}-{time.strftime("%Y%m%d-%H%M%S")}.csv'), True)

    def onExport(self, future, openAfter):
        path = self.endBusy()
        try:
            res = future.result()
        except Exception as e:
            self.showOutcome(path, status='', error=str(e) or 'Stopped.')
            return
        rows = f'{res["rows"]:,} rows' if res['rows'] is not None else 'Result'
        self.showOutcome(path, status=f'{rows} saved to {res["path"]} · {res["seconds"]:.2f} s', error='')
        self.main.statusBar().showMessage(f'Query result: {rows} saved to {res["path"]}', 10000)
        if openAfter and not self.closed:
            self.main.openPath(res['path'])

    def updateButtons(self):
        busy, ready = bool(self.busy), bool(self.path and self.names)
        self.runButton.setEnabled(not busy and ready)
        self.stopButton.setEnabled(busy)
        self.clearButton.setEnabled(not busy)
        self.exportButton.setEnabled(not busy and ready)
        self.openButton.setEnabled(not busy and ready)
        self.fileBox.setEnabled(not busy)

    def showError(self, text):
        self.errorLabel.setText(text)
        self.errorLabel.show()

    def hideError(self):
        self.errorLabel.hide()
        self.errorLabel.setText('')

    # ---------- lifetime ----------

    def saveLayout(self):
        if self.available and not self.closed and self.isVisible():
            self.main.settings.setValue('query/split' + ('Wide' if self.wide else 'Narrow'), self.split.saveState())

    def hideEvent(self, event):
        self.saveLayout()
        super().hideEvent(event)

    def shutdown(self):
        """Stop a running query or export and close the engine (MainWindow.closeEvent); safe to call twice."""
        if self.closed or not self.available:
            return
        self.saveLayout()
        self.closed = True
        self.pending.clear()
        self.ticker.stop()
        if self.engine is not None:
            self.stop()
            self.engine.close()
            self.engine = None
