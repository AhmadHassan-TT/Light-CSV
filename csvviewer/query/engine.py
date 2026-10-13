"""QueryEngine: one DuckDB connection with views t<N> / t<N>_text over the open tabs; no Qt.

Blocking calls (columns, run, export) are safe from any thread, each on its own cursor; submit() runs one on the
engine's worker pool and returns a concurrent Future, which is how the dialog keeps the GUI thread free.
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from csvviewer import sqlviews

DISPLAY_LIMIT = 1000


class QueryError(Exception):
    pass


class QueryEngine:
    def __init__(self):
        self.con = None
        self.tabs = {}  # tabId -> tab (tabId, name, path, index)
        self.viewKeys = {}  # view name -> file + options it was made from
        self.textOnly = set()  # tabIds whose typed view failed, so the text view stands in
        self.active = set()  # cursors of running run / export calls
        self.stopped = False
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='query')

    # ---------- setup ----------

    def connect(self):
        with self.lock:
            if self.con is None:
                try:
                    self.con = sqlviews.connect()
                except ImportError:
                    raise QueryError('The query builder needs the duckdb package (pip install duckdb).')
            return self.con

    def setTabs(self, tabs):
        """Tabs as TabInfo-like objects (tabId, name, path, index); views follow on next use."""
        with self.lock:
            self.tabs = {t.tabId: t for t in tabs}

    def tabKey(self, tab):
        idx = tab.index
        return (tab.path, idx.delimiter, idx.encoding, idx.hasHeader)

    def ensureView(self, tabId, text=False):
        con = self.connect()
        name = sqlviews.viewName(tabId, text)
        with self.lock:
            tab = self.tabs.get(tabId)
            if tab is None:
                raise QueryError(f'There is no open tab {tabId}.')
            key = self.tabKey(tab)
            if self.viewKeys.get(name) != key:
                try:
                    sqlviews.createView(con, tabId, tab.path, tab.index, text)
                except Exception as e:
                    raise QueryError(f'Could not read {tab.name}: {sqlviews.shortError(str(e))}')
                self.viewKeys[name] = key
                if not text:
                    self.textOnly.discard(tabId)
        return name

    def viewName(self, tabId):
        """The view the builder should query for a tab: typed, or the text view when typing failed."""
        return sqlviews.viewName(tabId, tabId in self.textOnly)

    def columns(self, tabId):
        """[(name, duckdbType)] of a tab via DESCRIBE; falls back to the all-text view."""
        cur = self.connect().cursor()
        try:
            try:
                return [(r[0], r[1]) for r in cur.execute(f'DESCRIBE {self.ensureView(tabId)}').fetchall()]
            except Exception:
                cols = [(r[0], r[1]) for r in cur.execute(f'DESCRIBE {self.ensureView(tabId, True)}').fetchall()]
                self.textOnly.add(tabId)
                return cols
        except QueryError:
            raise
        except Exception as e:
            raise QueryError(sqlviews.shortError(str(e)))
        finally:
            cur.close()

    # ---------- queries ----------

    def prepare(self, sql):
        con = self.connect()
        sql = sql.strip().rstrip(';').strip()
        if not sql:
            raise QueryError('There is no query to run.')
        error = sqlviews.readOnlyError(con, sql)
        if error:
            raise QueryError(error[0].upper() + error[1:])
        for tabId, text in sqlviews.viewRefs(sql):
            if tabId in self.tabs:
                self.ensureView(tabId, text)
        return sql

    def execute(self, sql, fetch):
        import duckdb
        cur = self.connect().cursor()
        with self.lock:
            if self.stopped:
                cur.close()
                raise QueryError('Stopped.')
            self.active.add(cur)
        start = time.monotonic()
        try:
            return fetch(cur.execute(sql)), time.monotonic() - start
        except duckdb.InterruptException:
            raise QueryError('Stopped.')
        except duckdb.Error as e:
            raise QueryError(friendly(str(e)))
        finally:
            with self.lock:
                self.active.discard(cur)
            cur.close()

    def run(self, sql, limit=DISPLAY_LIMIT):
        """{columns, types, rows (at most limit), totalRows (None when there are more), more, seconds}."""
        sql = self.prepare(sql)
        self.stopped = False

        def fetch(res):
            desc = res.description or []
            return [d[0] for d in desc], [str(d[1]) for d in desc], res.fetchmany(limit + 1)
        (columns, types, rows), seconds = self.execute(sql, fetch)
        more = len(rows) > limit
        rows = rows[:limit]
        return {'columns': columns, 'types': types, 'rows': rows, 'totalRows': None if more else len(rows),
                'more': more, 'seconds': round(seconds, 3)}

    def export(self, sql, path):
        """Whole result of sql to a CSV file with a header; {rows, seconds, path}."""
        sql = self.prepare(sql)
        self.stopped = False
        tmp = path + '.part'
        try:
            (count,), seconds = self.execute(f'COPY ({sql}) TO {sqlviews.sqlString(tmp)} (FORMAT CSV, HEADER)',
                                             lambda res: res.fetchone() or (None,))
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return {'rows': count, 'seconds': round(seconds, 3), 'path': path}

    def submit(self, fn, *args):
        """Run fn(*args) on the worker pool; returns a Future."""
        return self.pool.submit(fn, *args)

    def cancel(self):
        """Interrupt every running run / export."""
        with self.lock:
            self.stopped = True
            for cur in list(self.active):
                cur.interrupt()

    def close(self):
        self.cancel()
        self.pool.shutdown(wait=True, cancel_futures=True)
        with self.lock:
            if self.con:
                self.con.close()
                self.con = None
            self.viewKeys.clear()


def friendly(msg):
    # Short DuckDB cause, plus a hint for the type-detection case
    msg = sqlviews.shortError(msg)
    if 'Conversion Error' in msg or 'Could not convert' in msg:
        msg += '\nA value did not match the type detected for its column; query the text view (t<N>_text) instead.'
    return msg
