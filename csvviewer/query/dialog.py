"""QueryDialog: the "Query builder" window. Builder controls -> live SQL -> run / export on the engine's worker pool."""
import importlib.util
import os
import time

from PyQt6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QObject, QStandardPaths, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QKeySequence, QPalette, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QSpinBox, QSplitter, QTableView, QToolButton, QVBoxLayout, QWidget,
)

from . import builder
from .builder import BuilderError

Role = QPalette.ColorRole
MAX_DISPLAY = 100_000  # rows the results table holds at most, whatever the limit
NO_LIMIT_DISPLAY = 10_000  # rows shown when "Limit rows" is off
MAX_CELL = 500
KIND_HINT = {'numeric': 'a number', 'date': 'YYYY-MM-DD', 'timestamp': 'YYYY-MM-DD HH:MM'}
KIND_NAME = {'numeric': 'number', 'date': 'date', 'timestamp': 'date & time', 'text': 'text'}


def resultsDir():
    path = os.path.join(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation), 'query-results')
    os.makedirs(path, exist_ok=True)
    return path


def mix(a, b, t):
    # Blend color a toward b by t (0..1)
    return QColor.fromRgbF(a.redF() + (b.redF() - a.redF()) * t, a.greenF() + (b.greenF() - a.greenF()) * t,
                           a.blueF() + (b.blueF() - a.blueF()) * t)


def errorColor(p):
    # A red as light or dark as the theme's text, so it reads on either base
    dark = p.color(Role.Base).lightness() < 128
    return QColor.fromHslF(0.0, 0.75, 0.70 if dark else 0.42)


def combo(items=(), minChars=10):
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


def displayValue(v):
    if v is None:
        return ''
    if isinstance(v, float):
        return f'{v:.10g}'
    s = str(v)
    return s[:MAX_CELL] + '…' if len(s) > MAX_CELL else s


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


class FilterRow(QWidget):
    changed = pyqtSignal()
    removed = pyqtSignal(object)

    def __init__(self, names, types):
        super().__init__()
        self.types = types
        self.column = combo([(n, n) for n in names])
        self.op = combo(minChars=8)
        self.value = QLineEdit()
        self.andLabel = QLabel('and')
        self.value2 = QLineEdit()
        # Rows line up whether or not the value box shows
        policy = self.value.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.value.setSizePolicy(policy)
        remove = removeButton('Remove this filter')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.column, 3)
        layout.addWidget(self.op, 2)
        layout.addWidget(self.value, 3)
        layout.addWidget(self.andLabel)
        layout.addWidget(self.value2, 2)
        layout.addWidget(remove)
        self.column.currentIndexChanged.connect(self.onColumn)
        self.op.currentIndexChanged.connect(self.onOp)
        self.value.textChanged.connect(self.changed)
        self.value2.textChanged.connect(self.changed)
        remove.clicked.connect(lambda: self.removed.emit(self))
        self.onColumn()

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

    def onOp(self):
        op = self.op.currentData()
        self.value.setVisible(op not in builder.NO_VALUE)
        self.andLabel.setVisible(op == 'between')
        self.value2.setVisible(op == 'between')
        if op == 'one_of':
            self.value.setPlaceholderText('a, b, c')
        elif op in ('equals', 'not_equals', 'contains', 'not_contains', 'starts_with', 'ends_with'):
            self.value.setPlaceholderText('text (any case)')
        self.changed.emit()

    def set(self, column, op, value='', value2=''):
        self.column.setCurrentIndex(self.column.findData(column))
        self.op.setCurrentIndex(self.op.findData(op))
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
        self.column = combo([(n, n) for n in names])
        remove = removeButton('Stop grouping by this column')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.column, 1)
        layout.addWidget(remove)
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
        self.column = combo([('(all rows)', None)] + [(n, n) for n in names])
        remove = removeButton('Remove this summary')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.func, 2)
        layout.addWidget(QLabel('of'))
        layout.addWidget(self.column, 3)
        layout.addWidget(remove)
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


class Bridge(QObject):
    # Carries a finished Future from a worker thread back to the GUI thread
    done = pyqtSignal(object, object)

    def __init__(self, parent):
        super().__init__(parent)
        self.done.connect(self.deliver)

    @pyqtSlot(object, object)
    def deliver(self, callback, future):
        callback(future)


class QueryDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle('Query builder')
        self.setWindowFlags(Qt.WindowType.Window)
        self.engine = None
        self.tabIds = {}  # path -> tabId
        self.path = None
        self.tabKey = None
        self.names, self.types = [], {}
        self.columnsGen = 0
        self.busy = None  # 'run' | 'export' | None
        self.future = None
        self.started = 0.0
        self.result = None
        self.styleKey = None
        self.builderError = None
        self.bridge = Bridge(self)
        self.ticker = QTimer(self, interval=200, timeout=self.tickBusy)
        outer = QVBoxLayout(self)
        if importlib.util.find_spec('duckdb') is None:
            outer.addWidget(QLabel('<h3>The query builder needs DuckDB</h3><p>Install it with <code>pip install duckdb</code> '
                                   'and restart CSV Viewer.</p>'), 0, Qt.AlignmentFlag.AlignCenter)
            self.available = False
        else:
            from .engine import QueryEngine
            self.engine = QueryEngine()
            self.available = True
            self.build(outer)
            QApplication.instance().aboutToQuit.connect(self.engine.close)
            # While hidden nothing refreshes; showFor() catches up
            window.tabs.currentChanged.connect(lambda _: self.isVisible() and self.refreshTabs())
            window.tabs.tabCloseRequested.connect(lambda _: QTimer.singleShot(0, lambda: self.isVisible() and self.refreshTabs()))
            window.tabs.tabBar().tabMoved.connect(lambda *_: self.isVisible() and self.refreshTabs())
        window.installEventFilter(self)
        geometry = window.settings.value('query/geometry')
        if geometry is None or not self.restoreGeometry(geometry):
            self.resize(1000, 700)

    # ---------- layout ----------

    def build(self, outer):
        top = QHBoxLayout()
        top.addWidget(QLabel('File'))
        self.fileBox = combo(minChars=30)
        self.fileBox.activated.connect(lambda _: self.onFile())
        top.addWidget(self.fileBox, 1)
        self.columnsNote = muted('')
        top.addWidget(self.columnsNote, 1)
        outer.addLayout(top)

        self.builderPane = QWidget()
        pane = QVBoxLayout(self.builderPane)
        pane.setContentsMargins(0, 0, 8, 0)
        pane.setSpacing(6)

        pane.addWidget(self.section('Show columns'))
        row = QHBoxLayout()
        self.columnFilter = QLineEdit(placeholderText='Find a column…', clearButtonEnabled=True)
        self.columnFilter.textChanged.connect(self.filterColumnList)
        allButton, noneButton = QPushButton('All'), QPushButton('None')
        allButton.clicked.connect(lambda: self.checkColumns(True))
        noneButton.clicked.connect(lambda: self.checkColumns(False))
        row.addWidget(self.columnFilter, 1)
        row.addWidget(allButton)
        row.addWidget(noneButton)
        pane.addLayout(row)
        self.columnList = QListWidget()
        self.columnList.setMaximumHeight(150)
        self.columnList.itemChanged.connect(lambda _: self.changed())
        pane.addWidget(self.columnList)

        pane.addWidget(self.section('Filter rows'))
        self.filterBox = QVBoxLayout()
        self.filterBox.setSpacing(4)
        pane.addLayout(self.filterBox)
        row = QHBoxLayout()
        addFilter = addButton('+ Add filter')
        addFilter.clicked.connect(lambda: self.addFilter())
        self.matchBox = combo([('all', 'all'), ('any', 'any')], 3)
        self.matchBox.currentIndexChanged.connect(self.changed)
        self.matchRow = QWidget()
        matchLayout = QHBoxLayout(self.matchRow)
        matchLayout.setContentsMargins(0, 0, 0, 0)
        matchLayout.addWidget(QLabel('Match'))
        matchLayout.addWidget(self.matchBox)
        matchLayout.addWidget(QLabel('of the filters'))
        row.addWidget(addFilter)
        row.addStretch(1)
        row.addWidget(self.matchRow)
        pane.addLayout(row)

        pane.addWidget(self.section('Group & summarize', 'optional: one row per group, e.g. a count per city'))
        self.groupBox = QVBoxLayout()
        self.groupBox.setSpacing(4)
        pane.addLayout(self.groupBox)
        self.summaryBox = QVBoxLayout()
        self.summaryBox.setSpacing(4)
        pane.addLayout(self.summaryBox)
        row = QHBoxLayout()
        addGroup, addSummary = addButton('+ Group by column'), addButton('+ Add summary')
        addGroup.clicked.connect(lambda: self.addGroup())
        addSummary.clicked.connect(lambda: self.addSummary())
        row.addWidget(addGroup)
        row.addWidget(addSummary)
        row.addStretch(1)
        pane.addLayout(row)

        pane.addWidget(self.section('Sort by'))
        row = QHBoxLayout()
        self.sortBox = combo([('(no sorting)', None)], 14)
        self.sortBox.currentIndexChanged.connect(self.changed)
        self.sortDir = combo([('Ascending', False), ('Descending', True)], 8)
        self.sortDir.currentIndexChanged.connect(self.changed)
        row.addWidget(self.sortBox, 1)
        row.addWidget(self.sortDir)
        pane.addLayout(row)

        pane.addWidget(self.section('Limit rows'))
        row = QHBoxLayout()
        self.limitSpin = QSpinBox(minimum=0, maximum=100_000_000, value=1000, singleStep=1000)
        self.limitSpin.setSpecialValueText('No limit')
        self.limitSpin.setGroupSeparatorShown(True)
        self.limitSpin.valueChanged.connect(self.changed)
        self.distinct = QCheckBox('Only distinct rows')
        self.distinct.toggled.connect(self.changed)
        row.addWidget(self.limitSpin)
        row.addWidget(self.distinct)
        row.addStretch(1)
        pane.addLayout(row)
        pane.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(self.builderPane)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumWidth(430)

        right = QWidget()
        side = QVBoxLayout(right)
        side.setContentsMargins(8, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self.section('SQL'))
        row.addStretch(1)
        self.editSql = QCheckBox('Edit SQL')
        self.editSql.setToolTip('Write the query yourself; the builder on the left is paused. Views: t1, t2… per tab, '
                                't1_text for an all-text view')
        self.editSql.toggled.connect(self.onEditSql)
        row.addWidget(self.editSql)
        side.addLayout(row)
        self.sqlBox = QPlainTextEdit(readOnly=True)
        self.sqlBox.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.sqlBox.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.sqlBox.setTabChangesFocus(True)
        side.addWidget(self.sqlBox)

        row = QHBoxLayout()
        self.runButton = QPushButton('▶ Run', toolTip='Run the query (Ctrl+Enter)', default=True)
        self.stopButton = QPushButton('Stop', toolTip='Stop the running query or export', enabled=False)
        self.clearButton = QPushButton('Clear', toolTip='Start over with an empty query')
        self.exportButton = QPushButton('Export results…', toolTip='Save the whole result (not just the rows shown) as CSV')
        self.openButton = QPushButton('Open results as new tab', toolTip='Save the whole result and open it in the viewer')
        self.runButton.clicked.connect(self.runQuery)
        self.stopButton.clicked.connect(self.stop)
        self.clearButton.clicked.connect(self.clear)
        self.exportButton.clicked.connect(self.exportDialog)
        self.openButton.clicked.connect(self.openAsTab)
        for b in (self.runButton, self.stopButton, self.clearButton):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.exportButton)
        row.addWidget(self.openButton)
        side.addLayout(row)

        row = QHBoxLayout()
        self.statusLabel = QLabel('')
        self.statusLabel.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.busyBar = QProgressBar(minimum=0, maximum=0, maximumWidth=140, maximumHeight=14, textVisible=False)
        self.busyBar.hide()
        row.addWidget(self.statusLabel, 1)
        row.addWidget(self.busyBar)
        side.addLayout(row)
        self.errorLabel = QLabel('')
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

        results = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top.setLayout(side)
        results.addWidget(top)
        results.addWidget(self.table)
        results.setStretchFactor(1, 1)
        results.setSizes([230, 470])
        results.setChildrenCollapsible(False)
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.addWidget(scroll)
        self.split.addWidget(results)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([440, 560])
        self.split.setChildrenCollapsible(False)
        outer.addWidget(self.split, 1)
        state = self.window.settings.value('query/split')
        if state is not None:
            self.split.restoreState(state)
        for keys in ('Ctrl+Return', 'Ctrl+Enter'):
            QShortcut(QKeySequence(keys), self, self.runQuery)
        self.filters, self.groups, self.summaries = [], [], []
        self.restyle()
        self.updateButtons()

    def section(self, title, hint=None):
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 6, 0, 0)
        label = QLabel(title)
        font = label.font()
        font.setWeight(QFont.Weight.DemiBold)
        label.setFont(font)
        layout.addWidget(label)
        if hint:
            layout.addWidget(muted(hint), 1)
        layout.addStretch(0 if hint else 1)
        return w

    def restyle(self):
        # Only the error box has its own colors, derived from the palette so both themes fit
        p = QApplication.palette()
        base, err = p.color(Role.Base), errorColor(p)
        key = (base.name(), p.color(Role.Text).name())
        if key == self.styleKey or not self.available:
            return
        self.styleKey = key
        self.errorLabel.setStyleSheet(f'QLabel {{ color: {err.name()}; background: {mix(base, err, 0.10).name()}; '
                                      f'border: 1px solid {mix(base, err, 0.45).name()}; border-radius: 5px; padding: 6px 8px; }}')

    def changeEvent(self, event):
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange, QEvent.Type.StyleChange):
            self.restyle()
        super().changeEvent(event)

    # ---------- tabs / columns ----------

    def showFor(self, path=None):
        if self.available:
            self.refreshTabs(path)
        self.show()
        self.raise_()
        self.activateWindow()

    def refreshTabs(self, prefer=None):
        if not self.available:
            return
        from csvviewer.assistant.context import snapshotTabs
        tabs = snapshotTabs(self.window)
        self.engine.setTabs(tabs)
        self.tabIds = {t.path: t.tabId for t in tabs}
        keep = prefer if prefer in self.tabIds else self.path if self.path in self.tabIds else (tabs[0].path if tabs else None)
        self.fileBox.blockSignals(True)
        self.fileBox.clear()
        for t in tabs:
            self.fileBox.addItem(t.name, t.path)
            self.fileBox.setItemData(self.fileBox.count() - 1, t.path, Qt.ItemDataRole.ToolTipRole)
        self.fileBox.setCurrentIndex(self.fileBox.findData(keep))
        self.fileBox.blockSignals(False)
        self.onFile()

    def onFile(self):
        path = self.fileBox.currentData()
        tab = next((t for t in self.engine.tabs.values() if t.path == path), None)
        key = self.engine.tabKey(tab) if tab else None
        if path != self.path:
            self.path = path
            self.resetBuilder()
            self.setColumns([], {})
        if key != self.tabKey:
            self.tabKey = key
            self.loadColumns()
        self.changed()

    def loadColumns(self):
        self.columnsGen += 1
        gen = self.columnsGen
        if not self.path:
            self.columnsNote.setText('Open a CSV file in the viewer first.')
            self.updateButtons()
            return
        self.columnsNote.setText('Reading columns…')
        self.builderPane.setEnabled(False)
        self.job(lambda: self.engine.columns(self.tabIds[self.path]), lambda f: self.onColumns(gen, f))

    def onColumns(self, gen, future):
        if gen != self.columnsGen:
            return
        try:
            cols = future.result()
        except Exception as e:
            self.columnsNote.setText('')
            self.showError(f'Could not read the columns: {e}')
            return
        self.setColumns([c for c, _ in cols], dict(cols))
        tabId = self.tabIds.get(self.path)
        textOnly = tabId is not None and self.engine.viewName(tabId).endswith('_text')
        self.columnsNote.setText(f'{len(cols):,} columns' + (' (read as text)' if textOnly else ''))
        self.builderPane.setEnabled(not self.editSql.isChecked())
        self.changed()

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
        row.setParent(None)
        row.deleteLater()
        self.changed()

    def addFilter(self, column=None, op=None, value='', value2=''):
        row = self.addRow(self.filters, self.filterBox, FilterRow(self.names, self.types))
        if column is not None:
            row.set(column, op or row.op.currentData(), value, value2)
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
                r.setParent(None)
                r.deleteLater()
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

    def state(self):
        tabId = self.tabIds.get(self.path, 0)
        checked = [self.columnList.item(i).text() for i in range(self.columnList.count())
                   if self.columnList.item(i).checkState() == Qt.CheckState.Checked]
        sort = self.sortBox.currentData()
        return {'tabId': tabId, 'view': self.engine.viewName(tabId) if tabId else None, 'types': self.types,
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
        if not self.available:
            return
        self.matchRow.setVisible(len(self.filters) > 1)
        self.refreshSort()
        if self.editSql.isChecked():
            return
        if not self.path:
            self.sqlBox.setPlainText('')
            return
        if not self.names:
            return
        try:
            self.sqlBox.setPlainText(builder.buildSql(self.state()))
            self.builderError = None
        except BuilderError as e:
            self.builderError = str(e)
            self.sqlBox.setPlainText(f'-- {e}')
        self.updateButtons()

    def onEditSql(self, on):
        self.sqlBox.setReadOnly(not on)
        self.builderPane.setEnabled(not on and bool(self.names))
        if on:
            self.sqlBox.setFocus()
        else:
            self.changed()

    def clear(self):
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
        self.busy = None
        self.future = None
        self.ticker.stop()
        self.busyBar.hide()
        self.updateButtons()

    def runQuery(self):
        if not self.available or self.busy or not self.path:
            return
        limit = self.limitSpin.value()
        shown = min(limit or NO_LIMIT_DISPLAY, MAX_DISPLAY)
        self.startBusy('run', lambda sql: self.engine.run(sql, shown), lambda f: self.onRun(f, limit, shown))

    def onRun(self, future, limit, shown):
        self.endBusy()
        try:
            res = future.result()
        except Exception as e:
            self.statusLabel.setText('')
            self.showError(str(e) or 'Stopped.')
            return
        self.result = res
        self.model.setResult(res['columns'], res['types'], res['rows'])
        self.table.resizeColumnsToContents()
        for c in range(self.model.columnCount()):
            self.table.setColumnWidth(c, min(self.table.columnWidth(c), 360))
        n = len(res['rows'])
        if res['more']:
            text = f'Showing the first {n:,} rows (there are more; export to get them all)'
        elif limit and n == limit and not self.editSql.isChecked():
            text = f'{n:,} rows (stopped at the limit of {limit:,})'
        else:
            text = f'{n:,} row' + ('' if n == 1 else 's')
        self.statusLabel.setText(f'{text} · {res["seconds"]:.2f} s')
        self.updateButtons()

    def stop(self):
        if self.future is not None:
            self.future.cancel()
        self.engine.cancel()

    def exportDialog(self):
        settings = self.window.settings
        start = settings.value('query/exportDir') or os.path.dirname(self.path or '') or os.path.expanduser('~')
        stem = os.path.splitext(os.path.basename(self.path or 'query'))[0]
        path, _ = QFileDialog.getSaveFileName(self, 'Export results', os.path.join(start, f'{stem}-query.csv'),
                                              'CSV files (*.csv);;All files (*)')
        if path:
            settings.setValue('query/exportDir', os.path.dirname(path))
            self.exportTo(path)

    def exportTo(self, path, openAfter=False):
        if self.busy or not self.path:
            return None
        return self.startBusy('export', lambda sql: self.engine.export(sql, path), lambda f: self.onExport(f, openAfter))

    def openAsTab(self):
        stem = os.path.splitext(os.path.basename(self.path or 'query'))[0]
        return self.exportTo(os.path.join(resultsDir(), f'{stem}-{time.strftime("%Y%m%d-%H%M%S")}.csv'), True)

    def onExport(self, future, openAfter):
        self.endBusy()
        try:
            res = future.result()
        except Exception as e:
            self.statusLabel.setText('')
            self.showError(str(e) or 'Stopped.')
            return
        rows = f'{res["rows"]:,} rows' if res['rows'] is not None else 'Result'
        self.statusLabel.setText(f'{rows} saved to {res["path"]} · {res["seconds"]:.2f} s')
        self.window.statusBar().showMessage(f'Query result: {rows} saved to {res["path"]}', 10000)
        if openAfter:
            self.window.openPath(res['path'])

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

    # ---------- window ----------

    def saveLayout(self):
        self.window.settings.setValue('query/geometry', self.saveGeometry())
        if self.available:
            self.window.settings.setValue('query/split', self.split.saveState())

    def hideEvent(self, event):
        self.saveLayout()
        super().hideEvent(event)

    def reject(self):
        # Esc / close hides the window and stops a running query; an export keeps going
        if self.busy == 'run':
            self.stop()
        self.saveLayout()
        super().reject()

    def eventFilter(self, obj, event):
        if obj is self.window and event.type() == QEvent.Type.Close:
            if self.available:
                self.stop()
            self.close()
        return False
