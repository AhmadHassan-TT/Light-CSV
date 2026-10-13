"""Visual query builder over the open tabs (DuckDB).

  builder.py  builder state dict -> SQL (pure, no Qt)
  engine.py   QueryEngine: DuckDB connection, tab views, run / export / cancel off the GUI thread
  dialog.py   QueryDialog: the "Query builder" window
"""


def openQueryBuilder(window):
    """Show the query builder of a MainWindow (one per window, reused), preselecting its current tab."""
    from .dialog import QueryDialog
    dialog = getattr(window, '_queryBuilder', None)
    if dialog is None:
        dialog = window._queryBuilder = QueryDialog(window)
    tab = window.tabs.currentWidget()
    dialog.showFor(tab.index.path if tab is not None and tab.index else None)
    return dialog
