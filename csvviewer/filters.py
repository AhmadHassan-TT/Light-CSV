"""Excel-style filter dropdown for one column: a searchable checklist of the column's values with row counts."""
from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout,
)

from .index import MAX_DISTINCT

MAX_LABEL = 200
BLANKS = '(Blanks)'


def sortKey(byCount, counts):
    def key(v):
        if not v:
            return (2, 0, '')  # blanks last, like Excel
        if byCount:
            return (0, -counts[v], v.lower())
        try:
            return (0, float(v), b'')
        except ValueError:
            return (1, 0, v.lower())
    return key


class FilterPopup(QFrame):
    """Opens under a column header; OK hands the tab a filter spec (see ColumnScan) or None for no filter."""

    def __init__(self, tab, col):
        super().__init__(tab, Qt.WindowType.Popup)
        self.tab, self.col = tab, col
        self.encoding = tab.index.encoding
        self.spec = tab.filters.get(col)
        self.scan = tab.valueScan(col)
        self.state = {}  # value -> checked, for the values the user ticked or unticked
        self.shownKeys = None
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        title = QLabel(f'<b>Filter "{tab.model.columns[col]}"</b>')
        self.searchBox = QLineEdit(placeholderText='Search values…')
        self.searchBox.setClearButtonEnabled(True)
        self.searchBox.textChanged.connect(self.applySearch)
        self.sortBox = QComboBox()
        self.sortBox.addItems(['A → Z', 'Most rows first'])
        self.sortBox.setToolTip('Order of the values')
        self.sortBox.activated.connect(lambda _: self.rebuild(force=True))
        top = QHBoxLayout()
        top.addWidget(self.searchBox, 1)
        top.addWidget(self.sortBox)
        self.allBox = QCheckBox('(Select all)')
        self.allBox.setTristate(True)
        self.allBox.clicked.connect(self.selectAll)
        self.list = QListWidget()
        self.list.setUniformItemSizes(True)
        self.list.itemChanged.connect(self.onItemChanged)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        clearBtn = QPushButton('Clear filter')
        clearBtn.setToolTip('Show every row again (keeps the dropdown)')
        clearBtn.clicked.connect(lambda: self.finish(None))
        removeBtn = QPushButton('Remove')
        removeBtn.setToolTip('Clear the filter and remove the dropdown from this column')
        removeBtn.clicked.connect(self.remove)
        self.okBtn = QPushButton('OK')
        self.okBtn.setDefault(True)
        self.okBtn.clicked.connect(self.accept)
        cancelBtn = QPushButton('Cancel')
        cancelBtn.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(clearBtn)
        buttons.addWidget(removeBtn)
        buttons.addStretch(1)
        buttons.addWidget(self.okBtn)
        buttons.addWidget(cancelBtn)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addLayout(top)
        layout.addWidget(self.allBox)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        self.resize(340, 440)
        if self.spec and self.spec[0] == 'contains':
            self.searchBox.setText(self.spec[1].decode(self.encoding, 'replace'))
        self.timer = QTimer(self, interval=700, timeout=self.rebuild)
        self.rebuild()
        if not self.scan.done:
            self.timer.start()
        self.searchBox.setFocus()

    def showUnder(self, header, section):
        x = header.sectionViewportPosition(section)
        pos = header.mapToGlobal(QPoint(max(0, x), header.height()))
        screen = header.screen().availableGeometry()
        pos.setX(max(screen.left(), min(pos.x(), screen.right() - self.width())))
        if pos.y() + self.height() > screen.bottom():
            pos.setY(max(screen.top(), pos.y() - self.height() - header.height()))
        self.move(pos)
        self.show()

    # ---------- the list ----------

    def initiallyChecked(self, value):
        spec = self.spec
        if not spec or spec[0] == 'contains':
            return True
        return value in spec[1] if spec[0] == 'in' else value not in spec[1]

    def isChecked(self, value):
        return self.state.get(value, self.initiallyChecked(value))

    def label(self, value, count):
        text = value.decode(self.encoding, 'replace').replace('\r\n', ' ⏎ ').replace('\n', ' ⏎ ') if value else BLANKS
        if len(text) > MAX_LABEL:
            text = text[:MAX_LABEL] + '…'
        return f'{text}   ({count:,})'

    def rebuild(self, force=False):
        scan = self.scan
        counts = scan.values
        if scan.done:
            self.timer.stop()
        if force or self.shownKeys is None or len(counts) != len(self.shownKeys) or scan.done:
            keys = sorted(counts, key=sortKey(self.sortBox.currentIndex() == 1, counts))
            self.list.blockSignals(True)
            self.list.setUpdatesEnabled(False)
            self.list.clear()
            for v in keys:
                item = QListWidgetItem(self.label(v, counts[v]))
                item.setData(Qt.ItemDataRole.UserRole, v)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if self.isChecked(v) else Qt.CheckState.Unchecked)
                self.list.addItem(item)
            self.list.setUpdatesEnabled(True)
            self.list.blockSignals(False)
            self.shownKeys = keys
            self.applySearch()
        rows = sum(counts.values())
        if scan.capped:
            self.status.setText(f'More than {MAX_DISTINCT:,} different values, so only those found first are listed. '
                                'Type in the search box and press OK to keep every row containing that text.')
        elif not scan.done:
            self.status.setText(f'Reading values… {scan.progress:.0%}  ·  {len(counts):,} so far')
        else:
            self.status.setText(f'{len(counts):,} values in {rows:,} rows')

    def visibleItems(self):
        return [self.list.item(i) for i in range(self.list.count()) if not self.list.item(i).isHidden()]

    def applySearch(self):
        term = self.searchBox.text().strip().lower()
        self.list.setUpdatesEnabled(False)
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            item = self.list.item(i)
            value = item.data(Qt.ItemDataRole.UserRole)
            hidden = bool(term) and term not in value.decode(self.encoding, 'replace').lower()
            item.setHidden(hidden)
            # Like Excel, every search result starts ticked; clearing the search brings the earlier ticks back
            checked = True if term else self.isChecked(value)
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.list.blockSignals(False)
        self.list.setUpdatesEnabled(True)
        self.updateAllBox()

    def onItemChanged(self, item):
        # Ticks made while searching only pick among the results
        if not self.searchBox.text().strip():
            self.state[item.data(Qt.ItemDataRole.UserRole)] = item.checkState() == Qt.CheckState.Checked
        self.updateAllBox()

    def selectAll(self):
        on = self.allBox.checkState() != Qt.CheckState.Unchecked
        self.list.blockSignals(True)
        searching = bool(self.searchBox.text().strip())
        for item in self.visibleItems():
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            if not searching:
                self.state[item.data(Qt.ItemDataRole.UserRole)] = on
        self.list.blockSignals(False)
        self.updateAllBox()

    def updateAllBox(self):
        items = self.visibleItems()
        checked = sum(1 for i in items if i.checkState() == Qt.CheckState.Checked)
        self.allBox.setCheckState(Qt.CheckState.Checked if items and checked == len(items) else
                                  Qt.CheckState.PartiallyChecked if checked else Qt.CheckState.Unchecked)
        self.okBtn.setEnabled(checked > 0 or (self.scan.capped and bool(self.searchBox.text().strip())))

    # ---------- result ----------

    def chosenSpec(self):
        term = self.searchBox.text().strip()
        if term and self.scan.capped:
            return ('contains', term.encode(self.encoding, 'replace').lower())
        if term:
            # Like Excel, a search keeps only the ticked values it shows
            return ('in', frozenset(i.data(Qt.ItemDataRole.UserRole) for i in self.visibleItems() if i.checkState() == Qt.CheckState.Checked))
        values = self.scan.values
        checked = frozenset(v for v in values if self.isChecked(v))
        unchecked = frozenset(values) - checked
        if not unchecked:
            return None
        # Values the scan has not reached (still reading, or past the cap) stay visible
        if self.scan.capped or not self.scan.done or len(unchecked) <= len(checked):
            return ('out', unchecked)
        return ('in', checked)

    def accept(self):
        self.finish(self.chosenSpec())

    def finish(self, spec):
        self.close()
        self.tab.setColumnFilter(self.col, spec)

    def remove(self):
        self.close()
        self.tab.removeColumnFilter(self.col)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.okBtn.isEnabled():
            return self.accept()
        super().keyPressEvent(event)
