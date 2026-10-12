"""Snapshots of the open tabs, taken on the GUI thread so tools never touch Qt widgets from the worker."""
from dataclasses import dataclass, field


@dataclass
class TabInfo:
    tabId: int  # 1-based tab number shown to Claude and the user
    name: str  # file name
    path: str  # absolute path
    index: object  # csvviewer.index.CsvIndex (thread-safe reads)
    columns: list = field(default_factory=list)
    indexed: bool = False  # True once the whole file is indexed
    current: bool = False  # the tab the user is looking at
    searchTerm: str = ''  # the tab's active search, if any
    searchHits: int = 0


def snapshotTabs(window):
    """TabInfo for every open tab of a MainWindow (call on the GUI thread)."""
    tabs = []
    current = window.tabs.currentWidget()
    for i in range(window.tabs.count()):
        tab = window.tabs.widget(i)
        if not tab.index:
            continue
        search = tab.search
        tabs.append(TabInfo(
            tabId=i + 1, name=window.tabs.tabText(i).split(' (')[0], path=tab.index.path, index=tab.index,
            columns=list(tab.model.columns), indexed=tab.index.done, current=tab is current,
            searchTerm=tab.lastQuery[0] if tab.lastQuery else '', searchHits=len(search.hits) if search else 0))
    return tabs
