"""AssistantPanel: the chat UI of the Claude assistant, shown in the right dock."""
import math
import re

from PyQt6.QtCore import QEvent, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFontDatabase, QGuiApplication, QIcon, QPalette, QTextCharFormat, QTextCursor, QTextFormat, QTextFrameFormat, QTextOption
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

MODEL_LABEL = 'Haiku 5.5'
PROVIDERS = [('claude', 'Claude'), ('gemini', 'Google Gemini (free)'), ('groq', 'Groq (free)')]
SHORT_NAMES = {'gemini': 'Gemini', 'groq': 'Groq'}  # history marker per provider; none for Claude
PRIVACY_NOTE = ' Only small samples and query results are sent, never the whole file.'
RENDER_MS = 50  # streamed text is re-rendered at most this often
SUGGESTIONS = ['Summarize the current tab', 'What columns are there and what do they contain?',
               'Find anomalies or empty values']
MAX_HISTORY = 30  # chats listed in the History menu
TITLE_CHARS = 48  # history titles are elided past this
MAX_COLUMN = 900  # messages stop widening past this in a wide window
ROW_LINK = re.compile(r'^csvrow://([\d.]+)/(\d+)/?$', re.IGNORECASE)
Role = QPalette.ColorRole


def mix(a, b, t):
    # Blend color a toward b by t (0..1)
    return QColor.fromRgbF(a.redF() + (b.redF() - a.redF()) * t, a.greenF() + (b.greenF() - a.greenF()) * t,
                           a.blueF() + (b.blueF() - a.blueF()) * t)


def isDark(palette):
    return palette.color(Role.Base).lightness() < 128


def errorColor(palette):
    return QColor('#ff7b72' if isDark(palette) else '#c62828')


def smaller(widget, factor=0.88):
    font = widget.font()
    font.setPointSizeF(font.pointSizeF() * factor)
    widget.setFont(font)
    return widget


def repolish(widget, **props):
    # Restyle after a dynamic property used by the stylesheet changed
    for k, v in props.items():
        widget.setProperty(k, v)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def polishDoc(doc, palette):
    # Make Qt's markdown output fit a narrow panel: smaller headings, wrapping shaded code, bordered tables
    base, text = palette.color(Role.Base), palette.color(Role.Text)
    codeBg, border = mix(base, text, 0.08), mix(base, text, 0.3)
    cur = QTextCursor(doc)
    tables = {}
    block = doc.begin()
    while block.isValid():
        bf = block.blockFormat()
        cur.setPosition(block.position())
        if bf.hasProperty(QTextFormat.Property.BlockCodeFence) or bf.nonBreakableLines():
            bf.setNonBreakableLines(False)
            bf.setBackground(codeBg)
            cur.setBlockFormat(bf)
        elif bf.headingLevel():
            cur.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)
            cf = QTextCharFormat()
            cf.setProperty(QTextFormat.Property.FontSizeAdjustment, 1 if bf.headingLevel() <= 2 else 0)
            cur.mergeCharFormat(cf)
        table = cur.currentTable()
        if table is not None:
            tables[table.firstPosition()] = table
        block = block.next()
    for table in tables.values():
        tf = table.format()
        tf.setBorder(1)
        tf.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
        tf.setBorderBrush(border)
        tf.setBorderCollapse(True)
        tf.setCellSpacing(0)
        tf.setCellPadding(4)
        table.setFormat(tf)
        for c in range(table.columns()):  # shade the header row
            cell = table.cellAt(0, c)
            cf = cell.format().toTableCellFormat()
            cf.setBackground(codeBg)
            cell.setFormat(cf)


class TextView(QTextBrowser):
    """Read-only rich text that grows to its content height and leaves scrolling to the message list."""

    def __init__(self, mono=False):
        super().__init__()
        self.setObjectName('md')
        self.setOpenLinks(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.viewport().setAutoFillBackground(False)
        self.document().setDocumentMargin(1)
        if mono:
            self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.parts = []  # streamed markdown pieces
        self.setFixedHeight(self.fontMetrics().height() + 4)

    def markdown(self):
        return ''.join(self.parts)

    def render(self):
        self.document().setMarkdown(self.markdown())
        polishDoc(self.document(), QApplication.palette())  # own palette's Base is transparent via the stylesheet
        self.fit()

    def fit(self):
        doc = self.document()
        doc.setTextWidth(self.viewport().width())
        h = math.ceil(doc.size().height()) + 2 * self.frameWidth()
        if h != self.height():
            self.setFixedHeight(h)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit()

    def wheelEvent(self, event):
        event.ignore()  # let the message list scroll

    def sizeHint(self):
        return QSize(120, self.height())

    def minimumSizeHint(self):
        return QSize(40, self.height())


class ToolChip(QFrame):
    """Compact tool-call line: click to show the detail (e.g. the SQL)."""

    def __init__(self, title, detail):
        super().__init__()
        self.setObjectName('chip')
        self.title = title
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 2, 8, 2)
        lay.setSpacing(0)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.toggle = smaller(QToolButton(autoRaise=True, checkable=True))
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.toggled.connect(self.onToggled)
        self.status = smaller(QLabel('running…', objectName='muted', wordWrap=True))
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.toggle)
        row.addWidget(self.status, 1)
        lay.addLayout(row)
        self.detail = TextView(mono=True)
        smaller(self.detail)
        self.detail.setPlainText(detail or '')
        self.detail.hide()
        self.toggle.setEnabled(bool(detail))
        lay.addWidget(self.detail)
        self.onToggled(False)

    def onToggled(self, on):
        self.toggle.setText(('▾ ' if on else '▸ ') + self.title)
        self.detail.setVisible(on)
        if on:
            self.detail.fit()

    def finish(self, summary, ok):
        self.status.setText(('' if ok else '✕ ') + summary)
        self.status.setToolTip(summary)
        repolish(self.status, tone='' if ok else 'bad')


class AssistantBubble(QFrame):
    """One assistant answer: markdown segments interleaved with tool chips, then a copy button."""

    def __init__(self, panel):
        super().__init__()
        self.setObjectName('asst')
        self.panel = panel
        self.segments = []
        self.last = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 4)
        lay.setSpacing(6)
        self.thinking = QLabel(objectName='muted')
        font = self.thinking.font()
        font.setItalic(True)
        self.thinking.setFont(font)
        self.thinking.hide()
        self.footer = QWidget()
        row = QHBoxLayout(self.footer)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch()
        self.copyButton = smaller(QToolButton(text='Copy', autoRaise=True))
        self.copyButton.setToolTip('Copy this answer as markdown')
        self.copyButton.clicked.connect(self.copy)
        row.addWidget(self.copyButton)
        self.footer.hide()
        lay.addWidget(self.thinking)
        lay.addWidget(self.footer)

    def insert(self, widget):
        lay = self.layout()
        lay.insertWidget(lay.count() - 2, widget)  # before the thinking line and footer
        self.last = widget

    def segment(self):
        # The text segment that receives streamed text: a new one after a chip
        if not isinstance(self.last, TextView):
            view = TextView()
            view.anchorClicked.connect(self.panel.onLink)
            self.segments.append(view)
            self.insert(view)
        return self.last

    def markdown(self):
        return '\n\n'.join(s.markdown().strip() for s in self.segments if s.parts).strip()

    def copy(self):
        QGuiApplication.clipboard().setText(self.markdown())
        self.copyButton.setText('Copied')
        QTimer.singleShot(1200, lambda: self.copyButton.setText('Copy'))


class InputBox(QPlainTextEdit):
    """Question box: Enter sends, Shift+Enter adds a line; grows from 2 to 6 lines."""
    submitted = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setPlaceholderText('Ask about the open tabs…')
        self.setTabChangesFocus(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().documentLayout().documentSizeChanged.connect(self.fit)
        self.fit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)

    def fit(self, *_):
        lines = min(6, max(2, int(self.document().documentLayout().documentSize().height())))
        m = self.contentsMargins()
        h = lines * self.fontMetrics().lineSpacing() + 2 * int(self.document().documentMargin()) + m.top() + m.bottom()
        if h != self.height():
            self.setFixedHeight(h)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit()


class Suggestion(QLabel):
    """Wrapping, clickable starter question."""
    clicked = pyqtSignal(str)

    def __init__(self, text):
        super().__init__(text, objectName='sugg', wordWrap=True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def mousePressEvent(self, event):
        if self.isEnabled() and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.text())


class AssistantPanel(QWidget):
    askRequested = pyqtSignal(str)
    stopRequested = pyqtSignal()
    newChatRequested = pyqtSignal()
    signInRequested = pyqtSignal()
    apiKeyEntered = pyqtSignal(str)
    providerChanged = pyqtSignal(str)  # provider id, e.g. 'claude', 'gemini', 'groq'
    providerKeyEntered = pyqtSignal(str, str)  # providerId, key
    modelChosen = pyqtSignal(str)  # model id
    rowLinkClicked = pyqtSignal(int, int)  # tabId, row
    chatOpened = pyqtSignal(str)  # chat id the user picked
    chatDeleted = pyqtSignal(str)  # delete one chat
    historyCleared = pyqtSignal()  # delete all
    expandRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(320)
        self.current = None  # assistant bubble being streamed
        self.dirty = None  # text segment waiting for a re-render
        self.chips = {}
        self.busy = False
        self.stickBottom = True
        self.dots = 0
        self.provider = 'claude'
        self.providerCombos = []
        self.providers = list(PROVIDERS)
        self.keyProvider = {}  # info dict of the key provider shown signed out
        self.historyItems = []
        self.currentChat = None
        self.expanded = False
        self.renderTimer = QTimer(self, singleShot=True, interval=RENDER_MS, timeout=self.flush)
        self.thinkTimer = QTimer(self, interval=400, timeout=self.animateThinking)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.buildSignIn())
        self.stack.addWidget(self.buildChat())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.stack)
        self.styleKey = None
        self.restyle()
        self.setProvider('claude')
        self.setHistory([])
        self.setExpanded(False)

    # ---------- building ----------

    def providerPicker(self):
        # Compact provider combo; one sits on each page, kept in sync
        combo = smaller(QComboBox())
        for pid, label in self.providers:
            combo.addItem(label, pid)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        combo.setToolTip('Which AI answers your questions')
        combo.activated.connect(lambda i, c=combo: self.onProviderPicked(c.itemData(i)))
        self.providerCombos.append(combo)
        return combo

    def buildSignIn(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)
        self.signInTitle = QLabel('Claude assistant')
        font = self.signInTitle.font()
        font.setPointSizeF(font.pointSizeF() * 1.25)
        font.setBold(True)
        self.signInTitle.setFont(font)
        lay.addWidget(self.signInTitle)
        lay.addWidget(QLabel('Ask questions about your open CSV tabs.', wordWrap=True))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel('Provider', objectName='muted'))
        row.addWidget(self.providerPicker())
        row.addStretch()
        lay.addLayout(row)
        self.claudeBox = self.buildClaudeSignIn()
        self.freeKeyBox = self.buildKeySignIn()
        lay.addWidget(self.claudeBox)
        lay.addWidget(self.freeKeyBox)
        self.signInStatus = QLabel(objectName='muted', wordWrap=True)
        self.signInStatus.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.signInStatus.hide()
        lay.addWidget(self.signInStatus)
        lay.addStretch()
        return page

    def buildClaudeSignIn(self):
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(QLabel('Uses your own Claude API account; questions are billed to it. Only small samples '
                             'and query results are sent — never the whole file.', objectName='muted', wordWrap=True))
        lay.addSpacing(4)
        signIn = QPushButton('Sign in with Claude', objectName='primary')
        signIn.setCursor(Qt.CursorShape.PointingHandCursor)
        signIn.clicked.connect(self.signInRequested)
        lay.addWidget(signIn)
        self.keyToggle = QToolButton(text='▸ or use an API key', autoRaise=True, checkable=True, objectName='flat')
        self.keyToggle.toggled.connect(self.onKeyToggled)
        lay.addWidget(self.keyToggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.keyBox = QWidget()
        row = QHBoxLayout(self.keyBox)
        row.setContentsMargins(0, 0, 0, 0)
        self.keyEdit = QLineEdit(placeholderText='sk-ant-…', echoMode=QLineEdit.EchoMode.Password)
        self.keyEdit.returnPressed.connect(self.saveKey)
        save = QPushButton('Save')
        save.clicked.connect(self.saveKey)
        row.addWidget(self.keyEdit, 1)
        row.addWidget(save)
        self.keyBox.hide()
        lay.addWidget(self.keyBox)
        return box

    def buildKeySignIn(self):
        # Generic "free key" view; its texts come from setKeyProvider
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.keySignupLabel = QLabel(objectName='muted', wordWrap=True)
        lay.addWidget(self.keySignupLabel)
        lay.addSpacing(4)
        self.getKeyButton = QPushButton('Get a free key', objectName='primary')
        self.getKeyButton.setCursor(Qt.CursorShape.PointingHandCursor)
        self.getKeyButton.clicked.connect(self.openKeyUrl)
        lay.addWidget(self.getKeyButton)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.providerKeyEdit = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        self.providerKeyEdit.returnPressed.connect(self.saveProviderKey)
        save = QPushButton('Save')
        save.clicked.connect(self.saveProviderKey)
        row.addWidget(self.providerKeyEdit, 1)
        row.addWidget(save)
        lay.addLayout(row)
        return box

    def buildChat(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(6)
        # header: title + provider, expand, New chat; model + auth label + History below
        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel('Assistant')
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        newChat = QToolButton(text='New chat', autoRaise=True)
        newChat.setToolTip('Start a new conversation')
        newChat.clicked.connect(self.newChatRequested)
        self.expandButton = smaller(QToolButton(autoRaise=True), 1.2)
        self.expandButton.clicked.connect(self.expandRequested)
        head.addWidget(title)
        head.addWidget(self.providerPicker())
        head.addStretch()
        head.addWidget(self.expandButton)
        head.addWidget(newChat)
        lay.addLayout(head)
        sub = QHBoxLayout()
        sub.setSpacing(8)
        self.modelLabel = smaller(QLabel(MODEL_LABEL, objectName='muted'))
        self.modelCombo = smaller(QComboBox())
        self.modelCombo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.modelCombo.setToolTip('Model')
        self.modelCombo.activated.connect(lambda i: self.modelChosen.emit(self.modelCombo.itemData(i)))
        self.modelCombo.hide()
        self.authLabel = smaller(QLabel(objectName='muted'))
        self.authLabel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.historyMenu = QMenu(self)
        self.historyButton = smaller(QToolButton(text='History ▾', autoRaise=True, objectName='history'))
        icon = QIcon.fromTheme('document-open-recent')
        if icon.isNull():
            self.historyButton.setText('◷ History ▾')
        else:
            self.historyButton.setIcon(icon)
            self.historyButton.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.historyButton.setToolTip('Saved chats')
        self.historyButton.setMenu(self.historyMenu)
        self.historyButton.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        sub.addWidget(self.modelLabel)
        sub.addWidget(self.modelCombo)
        sub.addWidget(self.authLabel, 1)
        sub.addWidget(self.historyButton)
        lay.addLayout(sub)
        # message list
        self.scroll = QScrollArea(widgetResizable=True, objectName='list')
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setBackgroundRole(Role.Base)
        self.listWidget = QWidget(objectName='listInner')
        outer = QHBoxLayout(self.listWidget)  # centers a capped column in a wide window
        outer.setContentsMargins(0, 0, 0, 0)
        column = QWidget()
        column.setMaximumWidth(MAX_COLUMN)
        outer.addStretch(1)
        outer.addWidget(column, 1000)
        outer.addStretch(1)
        self.listLay = QVBoxLayout(column)
        self.listLay.setContentsMargins(6, 6, 6, 6)
        self.listLay.setSpacing(8)
        self.suggestions = QWidget()
        sl = QVBoxLayout(self.suggestions)
        sl.setContentsMargins(0, 4, 0, 0)
        sl.setSpacing(6)
        sl.addWidget(smaller(QLabel('Try asking', objectName='muted')))
        self.suggestionLabels = []
        for text in SUGGESTIONS:
            s = Suggestion(text)
            s.clicked.connect(self.askRequested)
            sl.addWidget(s)
            self.suggestionLabels.append(s)
        self.listLay.addWidget(self.suggestions)
        self.listLay.addStretch()
        self.scroll.setWidget(self.listWidget)
        bar = self.scroll.verticalScrollBar()
        bar.valueChanged.connect(lambda v: setattr(self, 'stickBottom', v >= bar.maximum() - 4))
        bar.rangeChanged.connect(lambda _lo, hi: self.stickBottom and bar.setValue(hi))
        lay.addWidget(self.scroll, 1)
        # context line + input
        self.contextLabel = smaller(QLabel(objectName='muted', wordWrap=True))
        self.contextLabel.hide()
        lay.addWidget(self.contextLabel)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.input = InputBox()
        self.input.submitted.connect(self.send)
        buttons = QVBoxLayout()
        buttons.setSpacing(4)
        self.sendButton = QPushButton('Send', objectName='primary')
        self.sendButton.clicked.connect(self.send)
        self.stopButton = QPushButton('Stop', enabled=False)
        self.stopButton.clicked.connect(self.stopRequested)
        buttons.addStretch()
        buttons.addWidget(self.sendButton)
        buttons.addWidget(self.stopButton)
        row.addWidget(self.input, 1)
        row.addLayout(buttons)
        lay.addLayout(row)
        return page

    def restyle(self):
        # Colors derive from the palette, so light, dark and system themes all fit; redone on theme change
        # Read the app palette: a widget with a stylesheet no longer follows app palette changes by itself
        p = QApplication.palette()
        base, text, hl = p.color(Role.Base), p.color(Role.Text), p.color(Role.Highlight)
        err = errorColor(p)
        key = (base.name(), text.name(), hl.name(), p.color(Role.Window).name(), p.color(Role.WindowText).name())
        if key == self.styleKey:
            return
        self.styleKey = key
        self.setPalette(p)  # labels, inputs and text views take their text colors from here
        c = lambda col: col.name()
        self.setStyleSheet(f"""
            QScrollArea#list {{ border: 1px solid {c(mix(base, text, 0.18))}; border-radius: 6px; }}
            QFrame#asst {{ background: {c(mix(base, text, 0.045))}; border-radius: 8px; }}
            QFrame#user {{ background: {c(mix(base, hl, 0.18))}; border-radius: 8px; }}
            QFrame#err {{ background: {c(mix(base, err, 0.12))}; border: 1px solid {c(mix(base, err, 0.55))}; border-radius: 8px; }}
            QFrame#chip {{ background: {c(mix(base, text, 0.09))}; border-radius: 6px; }}
            QToolButton#flat {{ border: none; background: transparent; color: {c(p.color(Role.Link))}; padding: 2px 0; }}
            QToolButton#history::menu-indicator {{ image: none; width: 0; }}
            QFrame#chip QToolButton {{ border: none; background: transparent; padding: 2px 0; }}
            QTextBrowser#md {{ background: transparent; border: none; }}
            QLabel#muted {{ color: {c(mix(base, text, 0.6))}; }}
            QLabel[tone="bad"] {{ color: {c(err)}; }}
            QLabel#errText {{ color: {c(err)}; }}
            QLabel#sugg {{ border: 1px solid {c(mix(base, text, 0.2))}; border-radius: 6px; padding: 6px 8px; }}
            QLabel#sugg:hover {{ background: {c(mix(base, hl, 0.15))}; border-color: {c(hl)}; }}
            QLabel#sugg:disabled {{ color: {c(mix(base, text, 0.4))}; }}
            QPushButton#primary {{ background: {c(hl)}; color: {c(p.color(Role.HighlightedText))}; border: none;
                                   border-radius: 5px; padding: 5px 12px; font-weight: bold; }}
            QPushButton#primary:hover {{ background: {c(mix(hl, base, 0.15))}; }}
            QPushButton#primary:disabled {{ background: {c(mix(base, text, 0.15))}; color: {c(mix(base, text, 0.45))}; }}
        """)
        for view in self.findChildren(TextView):  # re-shade code blocks and tables
            if view.parts:
                view.render()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            self.restyle()

    # ---------- signed-out view ----------

    def onKeyToggled(self, on):
        self.keyToggle.setText(('▾' if on else '▸') + ' or use an API key')
        self.keyBox.setVisible(on)
        if on:
            self.keyEdit.setFocus()

    def saveKey(self):
        key = self.keyEdit.text().strip()
        if key:
            self.keyEdit.clear()
            self.apiKeyEntered.emit(key)

    def saveProviderKey(self):
        key = self.providerKeyEdit.text().strip()
        if key:
            self.providerKeyEdit.clear()
            self.providerKeyEntered.emit(self.provider, key)

    def openKeyUrl(self):
        if self.keyProvider.get('keyUrl'):
            QDesktopServices.openUrl(QUrl(self.keyProvider['keyUrl']))

    def setProviders(self, items):
        # items: [(id, label)]; refills both pickers, keeping the selection
        self.providers = list(items)
        for combo in self.providerCombos:
            combo.clear()
            for pid, label in self.providers:
                combo.addItem(label, pid)
            combo.setCurrentIndex(max(0, combo.findData(self.provider)))

    def setKeyProvider(self, info):
        # info: {id, name, signupText, keyUrl, keyPlaceholder}; fills the free-key signed-out view
        self.keyProvider = dict(info or {})
        name = self.keyProvider.get('name', '')
        self.updateSignInTitle()
        self.keySignupLabel.setText((self.keyProvider.get('signupText') or '') + PRIVACY_NOTE)
        self.getKeyButton.setToolTip(self.keyProvider.get('keyUrl', ''))
        self.providerKeyEdit.setPlaceholderText(self.keyProvider.get('keyPlaceholder') or f'Paste your {name} API key')
        self.providerKeyEdit.clear()

    def onProviderPicked(self, provider):
        if provider != self.provider:
            self.setProvider(provider)
            self.providerChanged.emit(provider)

    def setProvider(self, provider):
        # Select the provider everywhere and switch the signed-out content; never emits providerChanged
        self.provider = provider
        for combo in self.providerCombos:
            combo.setCurrentIndex(max(0, combo.findData(provider)))
        claude = provider == 'claude'
        self.updateSignInTitle()
        self.claudeBox.setVisible(claude)
        self.freeKeyBox.setVisible(not claude)

    def updateSignInTitle(self):
        # The key provider's name once its info arrived, else the short name until it does
        p = self.provider
        name = self.keyProvider.get('name') if self.keyProvider.get('id') == p else SHORT_NAMES.get(p, p)
        self.signInTitle.setText('Claude assistant' if p == 'claude' else f'{name} assistant')

    def setModelChoices(self, choices, current=''):
        # choices: [(modelId, label)]; empty shows `current` as a plain label (a fixed model)
        self.modelCombo.clear()
        for modelId, label in choices:
            self.modelCombo.addItem(label, modelId)
        if choices:
            self.modelCombo.setCurrentIndex(max(0, self.modelCombo.findData(current)))
        else:
            self.modelLabel.setText(current or '')
        self.modelCombo.setVisible(bool(choices))
        self.modelLabel.setVisible(not choices and bool(current))

    def setAuthState(self, signedIn, label=''):
        self.stack.setCurrentIndex(1 if signedIn else 0)
        self.authLabel.setText(label or '')
        self.authLabel.setToolTip(label or '')
        self.authLabel.setVisible(bool(signedIn and label))
        self.signInStatus.setText(label or '')
        self.signInStatus.setVisible(bool(not signedIn and label))
        if signedIn:
            self.input.setFocus()

    # ---------- messages ----------

    def addBubble(self, widget):
        self.suggestions.hide()
        self.listLay.insertWidget(self.listLay.count() - 1, widget)  # before the trailing stretch

    def addUserMessage(self, text):
        self.stickBottom = True
        view = TextView()
        view.setPlainText(text)
        bubble = QFrame(objectName='user')
        lay = QVBoxLayout(bubble)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.addWidget(view)
        wrap = QWidget()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(36, 0, 0, 0)  # indented to read as the user's side
        row.addWidget(bubble)
        self.addBubble(wrap)

    def startAssistantMessage(self):
        if self.current:
            self.endAssistantMessage()
        self.current = AssistantBubble(self)
        self.addBubble(self.current)
        return self.current

    def bubble(self):
        return self.current or self.startAssistantMessage()

    def appendText(self, delta):
        if not delta:
            return
        seg = self.bubble().segment()
        seg.parts.append(delta)
        self.dirty = seg
        if not self.renderTimer.isActive():
            self.renderTimer.start()

    def flush(self):
        self.renderTimer.stop()
        if self.dirty is not None:
            seg, self.dirty = self.dirty, None
            seg.render()

    def setThinking(self, on):
        if on:
            self.bubble().thinking.show()
            self.dots = 0
            self.animateThinking()
            self.thinkTimer.start()
        else:
            self.thinkTimer.stop()
            if self.current:
                self.current.thinking.hide()

    def animateThinking(self):
        if self.current:
            self.current.thinking.setText('Thinking' + '.' * (self.dots % 3 + 1))
            self.dots += 1

    def addToolChip(self, chipId, title, detail=''):
        self.flush()
        chip = ToolChip(title, detail)
        self.chips[chipId] = chip
        self.bubble().insert(chip)

    def finishToolChip(self, chipId, summary, ok=True):
        chip = self.chips.get(chipId)
        if chip:
            chip.finish(summary, ok)

    def endAssistantMessage(self):
        self.flush()
        self.setThinking(False)
        if self.current:
            self.current.footer.setVisible(bool(self.current.markdown()))
        self.current = None

    def showError(self, text):
        self.flush()
        bubble = QFrame(objectName='err')
        lay = QVBoxLayout(bubble)
        lay.setContentsMargins(10, 6, 10, 6)
        label = QLabel(text, objectName='errText', wordWrap=True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(label)
        self.addBubble(bubble)

    def setBusy(self, busy):
        self.busy = busy
        self.sendButton.setEnabled(not busy)
        self.stopButton.setEnabled(busy)
        for s in self.suggestionLabels:
            s.setEnabled(not busy)

    def clear(self):
        self.renderTimer.stop()
        self.thinkTimer.stop()
        self.current = self.dirty = None
        self.chips.clear()
        for i in reversed(range(self.listLay.count())):
            w = self.listLay.itemAt(i).widget()
            if w is not None and w is not self.suggestions:
                self.listLay.takeAt(i)
                w.deleteLater()
        self.suggestions.show()
        self.stickBottom = True

    def setContextInfo(self, text):
        self.contextLabel.setText(text or '')
        self.contextLabel.setVisible(bool(text))

    # ---------- history + window ----------

    def setHistory(self, items):
        # items: [{id, title, when, provider}] newest first; rebuilds the History menu
        self.historyItems = list(items or [])
        self.rebuildHistory()

    def setCurrentChat(self, chatId):
        self.currentChat = chatId or None
        self.rebuildHistory()

    def rebuildHistory(self):
        menu = self.historyMenu
        menu.clear()
        if not self.historyItems:
            menu.addAction('No saved chats yet').setEnabled(False)
        for item in self.historyItems[:MAX_HISTORY]:
            chatId = str(item.get('id', ''))
            title = ' '.join(str(item.get('title') or 'Untitled chat').split())
            if len(title) > TITLE_CHARS:
                title = title[:TITLE_CHARS - 1].rstrip() + '…'
            text = title + (' — ' + item['when'] if item.get('when') else '')
            short = SHORT_NAMES.get(item.get('provider'))
            if short:
                text += ' · ' + short
            action = menu.addAction(text.replace('&', '&&'))
            action.setData(chatId)
            if chatId == self.currentChat:  # only the open chat carries a check, so the rest stay plain
                action.setCheckable(True)
                action.setChecked(True)
                font = action.font()
                font.setBold(True)
                action.setFont(font)
            action.triggered.connect(lambda _=False, a=action, c=chatId: self.onChatPicked(a, c))
        menu.addSeparator()
        delete = menu.addAction('Delete current chat')
        delete.setEnabled(bool(self.currentChat))
        delete.triggered.connect(lambda: self.currentChat and self.chatDeleted.emit(self.currentChat))
        clearAll = menu.addAction('Clear all history…')
        clearAll.setEnabled(bool(self.historyItems))
        clearAll.triggered.connect(self.confirmClearHistory)

    def onChatPicked(self, action, chatId):
        action.setChecked(action.isCheckable())  # the check moves only with setCurrentChat
        self.chatOpened.emit(chatId)

    def confirmClearHistory(self):
        answer = QMessageBox.question(self, 'Clear history', 'Delete all saved chats? This cannot be undone.')
        if answer == QMessageBox.StandardButton.Yes:
            self.historyCleared.emit()

    def setExpanded(self, on):
        self.expanded = bool(on)
        self.expandButton.setText('⤡' if on else '⤢')
        self.expandButton.setToolTip('Put back in the side panel' if on else 'Open in a larger window')

    # ---------- input + links ----------

    def send(self):
        text = self.input.toPlainText().strip()
        if not text or self.busy:
            return
        self.input.clear()
        self.askRequested.emit(text)

    def onLink(self, url):
        m = ROW_LINK.match(url.toString())
        if m:
            tabId = 0
            for part in m.group(1).split('.'):  # QUrl rewrites host 3 as IPv4 0.0.0.3
                tabId = tabId * 256 + int(part)
            self.rowLinkClicked.emit(tabId, int(m.group(2)))
        elif url.scheme().lower() in ('http', 'https'):
            QDesktopServices.openUrl(url)
