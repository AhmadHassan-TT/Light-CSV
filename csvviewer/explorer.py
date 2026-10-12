"""File explorer side panel: browse folders and open CSV files."""
import os

from PyQt6.QtCore import QDir, QEvent, QSettings, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFileSystemModel, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFileDialog, QHBoxLayout, QHeaderView, QLineEdit,
    QMenu, QStyle, QToolButton, QTreeView, QVBoxLayout, QWidget,
)

CSV_EXTENSIONS = ['csv', 'tsv', 'txt', 'psv', 'dat']
SETTINGS_KEY = 'explorer/root'


def anyCase(text):
    """Wildcard pattern matching text in any letter case, e.g. 'csv' -> '[cC][sS][vV]'."""
    return ''.join(f'[{c.lower()}{c.upper()}]' if c.lower() != c.upper() else c for c in text)


class Explorer(QWidget):
    openRequested = pyqtSignal(str)

    def __init__(self, rootPath=None, parent=None):
        super().__init__(parent)
        self.settings = QSettings('csv-viewer', 'csv-viewer')
        self.pendingReveal = None

        style = self.style()
        self.pathEdit = QLineEdit()
        self.pathEdit.setToolTip('Current folder (Enter to go)')
        self.pathEdit.returnPressed.connect(self.onPathEntered)
        self.upButton = self.toolButton(QIcon.fromTheme('go-up', style.standardIcon(QStyle.StandardPixmap.SP_FileDialogToParent)), 'Up one folder', self.goUp)
        self.homeButton = self.toolButton(QIcon.fromTheme('go-home', style.standardIcon(QStyle.StandardPixmap.SP_DirHomeIcon)), 'Home folder', lambda: self.setRoot(QDir.homePath()))
        self.chooseButton = self.toolButton(QIcon.fromTheme('folder-open', style.standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)), 'Choose folder…', self.chooseFolder)
        pathRow = QHBoxLayout()
        pathRow.setSpacing(2)
        pathRow.addWidget(self.pathEdit, 1)
        for b in (self.upButton, self.homeButton, self.chooseButton):
            pathRow.addWidget(b)

        self.filterEdit = QLineEdit()
        self.filterEdit.setPlaceholderText('Filter files…')
        self.filterEdit.setClearButtonEnabled(True)
        self.filterEdit.textChanged.connect(self.applyFilters)
        self.csvOnly = QCheckBox('CSV files only')
        self.csvOnly.setChecked(True)
        self.csvOnly.toggled.connect(self.applyFilters)
        filterRow = QHBoxLayout()
        filterRow.addWidget(self.filterEdit, 1)
        filterRow.addWidget(self.csvOnly)

        self.model = QFileSystemModel(self)
        self.model.setFilter(QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot)
        self.model.setNameFilterDisables(False)
        self.model.directoryLoaded.connect(self.onDirectoryLoaded)

        self.view = QTreeView()
        self.view.setModel(self.model)
        self.view.setColumnHidden(2, True)  # Type
        self.view.setColumnHidden(3, True)  # Date Modified, to leave room for names
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.setUniformRowHeights(True)
        self.view.setAnimated(False)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.showMenu)
        self.view.doubleClicked.connect(self.onDoubleClicked)
        self.view.installEventFilter(self)
        header = self.view.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.view.setColumnWidth(1, 72)
        self.view.setColumnWidth(3, 130)
        header.setMinimumSectionSize(60)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addLayout(pathRow)
        layout.addLayout(filterRow)
        layout.addWidget(self.view, 1)
        self.setMinimumWidth(240)

        self.applyFilters()
        if not rootPath or not os.path.isdir(rootPath):
            saved = self.settings.value(SETTINGS_KEY, '', str)
            rootPath = saved if saved and os.path.isdir(saved) else QDir.homePath()
        self.setRoot(rootPath)

    def toolButton(self, icon, tip, slot):
        b = QToolButton()
        b.setIcon(icon)
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.clicked.connect(slot)
        return b

    # --- root ---

    def currentRoot(self):
        return self.model.rootPath()

    def setRoot(self, path):
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            return
        self.view.setRootIndex(self.model.setRootPath(path))  # only this folder is watched
        self.pathEdit.setText(path)
        self.upButton.setEnabled(os.path.dirname(path) != path)
        self.settings.setValue(SETTINGS_KEY, path)

    def goUp(self):
        self.setRoot(os.path.dirname(self.currentRoot()))

    def chooseFolder(self):
        path = QFileDialog.getExistingDirectory(self, 'Choose folder', self.currentRoot())
        if path:
            self.setRoot(path)

    def onPathEntered(self):
        path = os.path.expanduser(self.pathEdit.text().strip())
        if os.path.isdir(path):
            self.setRoot(path)
        else:
            self.pathEdit.setText(self.currentRoot())

    # --- filtering ---

    def applyFilters(self):
        text = self.filterEdit.text().strip()
        name = '' if not text else anyCase(text if any(c in text for c in '*?[') else f'*{text}*')
        if self.csvOnly.isChecked() and not (name and '.' in text and any(c in text for c in '*?[')):  # a wildcard with its own extension wins
            exts = [f'.{anyCase(e)}' for e in CSV_EXTENSIONS]
            patterns = [(name or '*') + e for e in exts]
        else:
            patterns = [name] if name else []
        self.model.setNameFilters(patterns)

    # --- reveal ---

    def revealFile(self, path):
        path = os.path.abspath(path)
        folder = os.path.dirname(path)
        root = self.currentRoot()
        if os.path.commonpath([root, folder]) != root:
            self.setRoot(folder)
        self.pendingReveal = path
        self.tryReveal()

    def tryReveal(self):
        index = self.model.index(self.pendingReveal) if self.pendingReveal else None
        if not index or not index.isValid():
            return
        parent = index.parent()
        while parent.isValid() and parent != self.view.rootIndex():
            self.view.expand(parent)
            parent = parent.parent()
        self.view.setCurrentIndex(index)
        self.view.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        self.pendingReveal = None

    def onDirectoryLoaded(self, path):
        if self.pendingReveal:
            self.tryReveal()

    # --- activation ---

    def onDoubleClicked(self, index):
        if not self.model.isDir(index):  # folders expand via the tree's own double-click
            self.openRequested.emit(self.model.filePath(index))

    def eventFilter(self, obj, event):
        if obj is self.view and event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            index = self.view.currentIndex()
            if index.isValid():
                if self.model.isDir(index):
                    self.view.setExpanded(index, not self.view.isExpanded(index))
                else:
                    self.openRequested.emit(self.model.filePath(index))
                return True
        return super().eventFilter(obj, event)

    def showMenu(self, pos):
        index = self.view.indexAt(pos)
        if not index.isValid():
            return
        path = self.model.filePath(index)
        isDir = self.model.isDir(index)
        menu = QMenu(self)
        if not isDir:
            menu.addAction('Open', lambda: self.openRequested.emit(path))
            menu.addAction('Open in new tab', lambda: self.openRequested.emit(path))
        menu.addSeparator()
        menu.addAction('Copy path', lambda: QApplication.clipboard().setText(path))
        menu.addAction('Show in file manager', lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path))))
        if isDir:
            menu.addAction('Set as root', lambda: self.setRoot(path))
        menu.exec(self.view.viewport().mapToGlobal(pos))
