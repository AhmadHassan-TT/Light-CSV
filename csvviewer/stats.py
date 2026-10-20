"""Column statistics: counts for one column over the rows on screen, with its most common values."""
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .filters import BLANKS
from .index import MAX_STATS_DISTINCT, ColumnStats

TOP = 200
FIELDS = (('rows', 'Rows'), ('filled', 'With a value'), ('blanks', 'Blank'), ('distinct', 'Distinct values'),
          ('repeated', 'Values that repeat'), ('extra', 'Repeat rows (not the first of their value)'),
          ('numbers', 'Numbers'), ('sum', 'Sum'), ('avg', 'Average'), ('min', 'Minimum'), ('max', 'Maximum'))


def number(x):
    return f'{x:,.0f}' if x == int(x) and abs(x) < 1e15 else f'{x:,.6g}'


class ColumnStatsDialog(QDialog):
    """Opens over a tab; reads the column in the background and fills in as it goes."""

    def __init__(self, tab, col):
        super().__init__(tab)
        self.tab, self.col = tab, col
        self.name = tab.model.columns[col]
        self.encoding = tab.index.encoding
        filtered = {c: spec for c, spec in tab.filters.items() if spec}
        self.scan = ColumnStats(tab.index, filtered, col).start()
        self.setWindowTitle(f'Column statistics: {self.name}')
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(460, 600)
        scope = QLabel('Over the rows on screen (the column filters apply).' if filtered else 'Over every row of the file.')
        scope.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.labels = {}
        form = QFormLayout()
        for key, text in FIELDS:
            label = self.labels[key] = QLabel('…')
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(text, label)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        self.list = QTableWidget(0, 2)
        self.list.setHorizontalHeaderLabels(['Value', 'Rows'])
        self.list.verticalHeader().hide()
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.list.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.list.setToolTip('Double-click a value to show only its rows')
        self.list.cellDoubleClicked.connect(self.filterToRow)
        unique = QPushButton('Show unique rows')
        unique.setToolTip(f'Keep the first row of each {self.name} value')
        unique.clicked.connect(lambda: self.applyFilter(('first', None)))
        dups = QPushButton('Show duplicates')
        dups.setToolTip(f'Keep only rows whose {self.name} appears more than once')
        dups.clicked.connect(lambda: self.applyFilter(('dups', None)))
        copy = QPushButton('Copy')
        copy.setToolTip('Copy the numbers and the value list')
        copy.clicked.connect(self.copy)
        close = QPushButton('Close')
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        for b in (unique, dups):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(copy)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(scope)
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addWidget(QLabel(f'<b>Most common values</b> (top {TOP})'))
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)
        self.timer = QTimer(self, interval=400, timeout=self.refresh)
        self.timer.start()
        self.refresh()

    def text(self, value):
        return value.decode(self.encoding, 'replace').replace('\r\n', ' ⏎ ').replace('\n', ' ⏎ ') if value.strip() else BLANKS

    def values(self):
        s = self.scan
        at = '≥ ' if s.capped else ''
        counts = s.counter
        repeated = [n for n in counts.values() if n > 1]
        out = {'rows': f'{s.rows:,}', 'filled': f'{s.rows - s.blanks:,}', 'blanks': f'{s.blanks:,}',
               'distinct': f'{at}{len(counts):,}', 'repeated': f'{at}{len(repeated):,}',
               'extra': f'{at}{sum(repeated) - len(repeated):,}', 'numbers': f'{s.numbers:,}'}
        n = s.numbers
        out |= {'sum': number(s.total) if n else '–', 'avg': number(s.total / n) if n else '–',
                'min': number(s.low) if n else '–', 'max': number(s.high) if n else '–'}
        return out

    def refresh(self):
        s = self.scan
        if s.done:
            self.timer.stop()
        for key, text in self.values().items():
            self.labels[key].setText(text)
        if not s.done:
            self.status.setText(f'Reading… {s.progress:.0%}')
        elif s.capped:
            self.status.setText(f'More than {MAX_STATS_DISTINCT:,} different values: values first seen after that are '
                                'not told apart, so distinct and repeat counts are lower bounds.')
        else:
            self.status.setText('')
        top = s.top(TOP)
        self.list.setRowCount(len(top))
        for r, (value, count) in enumerate(top):
            item = QTableWidgetItem(self.text(value)[:300])
            item.setData(Qt.ItemDataRole.UserRole, value)
            num = QTableWidgetItem(f'{count:,}')
            num.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
            self.list.setItem(r, 0, item)
            self.list.setItem(r, 1, num)

    def filterToRow(self, row, _col):
        item = self.list.item(row, 0)
        if item is not None:
            self.applyFilter(('in', frozenset([item.data(Qt.ItemDataRole.UserRole)])))

    def applyFilter(self, spec):
        self.tab.setFilterFromCell(self.col, spec)
        self.close()

    def copy(self):
        lines = [f'Column\t{self.name}']
        values = self.values()
        lines += [f'{text}\t{values[key]}' for key, text in FIELDS]
        lines += ['', 'Value\tRows'] + [f'{self.text(v)}\t{n}' for v, n in self.scan.top(TOP)]
        QApplication.clipboard().setText('\n'.join(lines))

    def done(self, result):
        self.scan.cancel()
        self.timer.stop()
        super().done(result)

    def closeEvent(self, event):
        self.scan.cancel()
        self.timer.stop()
        super().closeEvent(event)
