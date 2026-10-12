"""Claude assistant side panel: ask questions about the open tabs.

Pieces (the contracts below are what the modules agree on):
  context.py  TabInfo snapshots of the open tabs, taken on the GUI thread per question
  tools.py    CsvTools: tool schemas + local execution (rows, search, DuckDB SQL), runs on the worker thread
  agent.py    Claude API loop (streaming, tool use) on a worker QThread, signals for the panel
  panel.py    AssistantPanel: the chat UI in the right dock
"""
