"""Runs the MCP server inside the app and offers the Tools ▸ MCP server menu and setup dialog."""
import json
import os
import shlex
import sys

from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                             QTabWidget, QVBoxLayout, QWidget)

from . import config

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def bridgeCommand():
    """(command, args) that starts the stdio bridge on this install."""
    if getattr(sys, 'frozen', False):
        exe = os.path.join(os.path.dirname(sys.executable), 'csvview-mcp' + ('.exe' if sys.platform == 'win32' else ''))
        return exe, []
    python = os.path.join(REPO, '.venv', 'Scripts' if os.name == 'nt' else 'bin', 'python' + ('.exe' if os.name == 'nt' else ''))
    return (python if os.path.exists(python) else sys.executable), [os.path.join(REPO, 'csvview_mcp.py')]


class McpControl(QObject):
    """Owns the server's lifetime: on by default, toggled from the Tools menu, stopped on exit."""

    def __init__(self, window, forceOn=False):
        super().__init__(window)
        from .server import McpServer
        from .tools import McpTools
        self.window = window
        self.serverClass = McpServer
        self.tools = McpTools(window)
        self.server = None
        self.toggle = None
        if forceOn or window.settings.value('mcp/enabled', True, type=bool):
            self.start(quiet=True)

    def menuActions(self, menu):
        self.toggle = menu.addAction('MCP server (let other apps control this window)')
        self.toggle.setCheckable(True)
        self.toggle.setChecked(self.server is not None)
        self.toggle.toggled.connect(self.setEnabled)
        menu.addAction('Connect an app…', self.showSetup)

    def setEnabled(self, on):
        self.window.settings.setValue('mcp/enabled', on)
        if on and not self.server:
            self.start()
        elif not on and self.server:
            self.stop()

    def start(self, quiet=False):
        try:
            server = self.serverClass(self.tools, config.loadToken())
            server.start()
        except OSError as e:
            if not quiet:
                QMessageBox.warning(self.window, 'MCP server', f'Could not start the MCP server: {e}')
            self.window.statusBar().showMessage(f'MCP server could not start: {e}', 10000)
            if self.toggle:
                self.toggle.setChecked(False)
            return
        self.server = server
        config.writeState(server.url, server.port, os.getpid())
        try:
            config.saveToolsCache(self.tools.definitions())  # lets the bridge list tools without starting the app
        except OSError:
            pass
        self.window.statusBar().showMessage(f'MCP server on at {server.url}', 6000)

    def stop(self):
        if self.server:
            self.server.stop()
            self.server = None
            config.clearState()

    # ---------- setup dialog ----------

    def setupTexts(self):
        url = self.server.url if self.server else 'http://127.0.0.1:8765/mcp'
        token = config.loadToken()
        command, args = bridgeCommand()
        claudeCode = (f'claude mcp add --transport http light-csv {url} --header "Authorization: Bearer {token}"\n\n'
                      f'# or through the bridge (starts Light-CSV if it is closed):\n'
                      f'claude mcp add light-csv -- {shlex.join([command] + args)}')
        desktop = json.dumps({'mcpServers': {'light-csv': {'command': command, 'args': args}}}, indent=2)
        http = json.dumps({'mcpServers': {'light-csv': {'type': 'http', 'url': url,
                                                        'headers': {'Authorization': f'Bearer {token}'}}}}, indent=2)
        return [('Claude Code', claudeCode,
                 'Run this in a terminal. Claude Code can then use the light-csv tools.'),
                ('Claude Desktop / stdio apps', desktop,
                 'Add this to claude_desktop_config.json (Claude Desktop ▸ Settings ▸ Developer ▸ Edit Config), '
                 'or to any app that starts MCP servers as a command. The bridge opens Light-CSV if it is not running.'),
                ('HTTP apps (Cursor, VS Code, …)', http,
                 f'For apps that connect to an MCP URL. Server: {url}  ·  token stays on this PC.')]

    def showSetup(self):
        dialog = QDialog(self.window)
        dialog.setWindowTitle('Connect an app over MCP')
        dialog.resize(760, 460)
        layout = QVBoxLayout(dialog)
        state = f'MCP server is on at {self.server.url}' if self.server else 'MCP server is off - turn it on in Tools ▸ MCP server.'
        intro = QLabel(f'{state}\nOther apps get full control of this window (files, tabs, search, SQL, navigation) '
                       'but not the Assistant. Only apps on this PC with the token below can connect.')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        tabs = QTabWidget()
        for title, text, hint in self.setupTexts():
            page = QWidget()
            pageLayout = QVBoxLayout(page)
            label = QLabel(hint)
            label.setWordWrap(True)
            box = QPlainTextEdit(text)
            box.setReadOnly(True)
            copy = QPushButton('Copy')
            copy.clicked.connect(lambda _=False, t=text: QApplication.clipboard().setText(t))
            for w in (label, box, copy):
                pageLayout.addWidget(w)
            tabs.addTab(page, title)
        layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
