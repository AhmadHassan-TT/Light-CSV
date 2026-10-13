"""Visual query builder over the open tabs (DuckDB).

  builder.py  builder state dict -> SQL (pure, no Qt)
  engine.py   QueryEngine: DuckDB connection, tab views, run / export / cancel off the GUI thread
  dialog.py   QueryPanel: the builder widget in the window's side panel ("⊞ Query" tab); takes column header drops
"""


def openQueryBuilder(window):
    """Show the Query tab of a MainWindow's side panel and return its QueryPanel."""
    window.showSidePanel('query')
    return window.queryPanel
