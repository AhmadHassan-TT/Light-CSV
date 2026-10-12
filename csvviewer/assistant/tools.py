"""Client-side tools Claude calls to read the open CSV tabs; runs on the assistant's worker thread, never touches Qt."""
import json
import os
import re
import threading
import time

from csvviewer.index import Search

MAX_CELL = 300
MAX_GET = 100
MAX_SEARCH = 50
MAX_SQL_ROWS = 200
SQL_TIMEOUT = 180
SAMPLE_ROWS = 15
NAME_LIMIT = 100

SQL_DESCRIPTION = (
    "Run one read-only DuckDB SQL statement over the open tabs. Each tab is a view named t{tabId} "
    "(t1, t2, ... as listed by list_tabs), read straight from its file with column names from the header "
    "(headerless files: run DESCRIBE t1 to see the generated names). Quote column names with double quotes, "
    "e.g. SELECT \"City\", count(*) FROM t1 GROUP BY 1 ORDER BY 2 DESC. Column types are auto-detected from "
    "the first 100,000 rows; lines that cannot be parsed or converted to the detected types are skipped "
    "(ignore_errors), as are lines with too few fields; extra fields are dropped. For exact counts of every line, "
    "or when types look wrong, query the text-only view t{tabId}_text instead (all columns VARCHAR, keeps every "
    "line, pads short lines with NULL, cast yourself with TRY_CAST; it reads single-threaded so it is several "
    "times slower). "
    "Every query scans the whole file: a multi-GB file takes about 10-60 s, so prefer one well-formed aggregate "
    "query over many small ones and never SELECT * without LIMIT. Allowed: SELECT, WITH, FROM, DESCRIBE, "
    "SUMMARIZE, SHOW, PRAGMA table_info, EXPLAIN. At most 200 result rows are returned (truncated=true when "
    "there were more); long cells are cut at 300 characters. Times out after 180 s.")

READ_ONLY = re.compile(r'^\s*(select|with|from|describe|summarize|show|pragma\s+table_info|explain|values|\()', re.I)
VIEW_REF = re.compile(r'\bt(\d+)(_text)?\b', re.I)


class ToolError(Exception):
    pass


def cell(value):
    # Long cells cost tokens, keep the start
    if isinstance(value, str) and len(value) > MAX_CELL:
        return value[:MAX_CELL] + '…'
    return value


def humanSize(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


def dump(obj):
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(',', ':'))


def sqlString(text):
    return "'" + text.replace("'", "''") + "'"


def tool(name, description, props, required):
    return {'name': name, 'description': description, 'strict': True,
            'input_schema': {'type': 'object', 'properties': props, 'required': required, 'additionalProperties': False}}


TAB_ID = {'type': 'integer', 'description': 'Tab number from list_tabs.'}
COLUMN = {'type': 'string', 'description': 'Exact column name.'}

DEFINITIONS = [
    tool('list_tabs', "List the open CSV tabs: tabId, name, data rows, columns, whether indexing is finished, "
         "which tab the user is looking at, delimiter, file size, the tab's active search and its SQL view name.", {}, []),
    tool('describe_tab', "Columns of a tab plus 15 sample rows (first 5, 5 from the middle, last 5; the first 15 "
         "while the tab is still indexing). Row numbers are the 1-based data row numbers the viewer shows.",
         {'tabId': TAB_ID}, ['tabId']),
    tool('get_rows', "Read up to 100 consecutive data rows starting at a 1-based row number, optionally only some columns.",
         {'tabId': TAB_ID, 'startRow': {'type': 'integer', 'description': '1-based data row number.'},
          'count': {'type': 'integer', 'description': 'Rows to read, 1-100.'},
          'columns': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Column names to return; omit for all.'}},
         ['tabId', 'startRow', 'count']),
    tool('search_rows', "Find rows containing a text (substring match, the same as the viewer's search), in any column "
         "or one column. Returns the total number of matching rows and the first `limit` of them. Scans the whole "
         "file (a few seconds per GB). For counts, filters or aggregates prefer run_sql.",
         {'tabId': TAB_ID, 'text': {'type': 'string', 'description': 'Text to find.'},
          'column': COLUMN, 'caseSensitive': {'type': 'boolean', 'description': 'Default false.'},
          'limit': {'type': 'integer', 'description': 'Rows to return, 1-50, default 20.'}},
         ['tabId', 'text']),
    tool('run_sql', SQL_DESCRIPTION, {'query': {'type': 'string', 'description': 'One DuckDB SQL statement.'}}, ['query']),
    tool('show_rows', "Scroll the user's viewer to a row (and optionally a column) and select it, so they can see it.",
         {'tabId': TAB_ID, 'row': {'type': 'integer', 'description': '1-based data row number.'}, 'column': COLUMN},
         ['tabId', 'row']),
]


class CsvTools:
    """Tools over a snapshot of the open tabs; one instance per question, cancel() stops it for good."""

    def __init__(self, tabs):
        self.tabs = {t.tabId: t for t in tabs}
        self.con = None
        self.views = set()
        self.search = None
        self.cancelled = False
        self.timedOut = False
        self.lock = threading.Lock()

    def definitions(self):
        return DEFINITIONS

    def run(self, name, toolInput):
        if self.cancelled:
            raise ToolError('stopped by the user')
        handler = getattr(self, 'tool_' + name, None)
        if handler is None:
            raise ToolError(f'unknown tool {name}')
        toolInput = toolInput or {}
        schema = next(d['input_schema'] for d in DEFINITIONS if d['name'] == name)
        bad = set(toolInput) - set(schema['properties'])
        missing = set(schema['required']) - set(toolInput)
        if bad or missing:
            raise ToolError(f'bad input: unknown {sorted(bad)}, missing {sorted(missing)}')
        return handler(**toolInput)

    def cancel(self):
        self.cancelled = True
        with self.lock:
            if self.search:
                self.search.cancel()
            if self.con:
                self.con.interrupt()

    def close(self):
        with self.lock:
            if self.con:
                self.con.close()
                self.con = None

    # ---------- helpers ----------

    def tab(self, tabId):
        tab = self.tabs.get(tabId)
        if tab is None:
            raise ToolError(f'no tab {tabId}; open tabs: {sorted(self.tabs)}')
        return tab

    def columns(self, tab):
        return list(tab.columns) or tab.index.header()

    def columnIndex(self, tab, name):
        cols = self.columns(tab)
        if name in cols:
            return cols.index(name)
        # Forgive case and surrounding spaces
        low = [c.strip().lower() for c in cols]
        if name.strip().lower() in low:
            return low.index(name.strip().lower())
        raise ToolError(f'tab {tab.tabId} has no column {name!r}; call describe_tab for the names')

    def readRow(self, tab, row):
        idx = tab.index
        return idx.row(row - 1 + (1 if idx.hasHeader else 0))

    def checkRow(self, tab, row):
        rows = tab.index.dataRows
        if 1 <= row <= rows:
            return
        if not tab.index.done:
            raise ToolError(f'row {row} is not available yet: tab {tab.tabId} is still indexing ({rows} rows so far), try again in a moment')
        raise ToolError(f'row {row} is out of range; tab {tab.tabId} has rows 1-{rows}')

    def names(self, tab, width):
        cols = self.columns(tab)
        return cols + [f'column{i + 1}' for i in range(len(cols), width)]

    # ---------- tools ----------

    def tool_list_tabs(self):
        out = []
        for t in self.tabs.values():
            cols = self.columns(t)
            idx = t.index
            info = {'tabId': t.tabId, 'name': t.name, 'rows': idx.dataRows, 'columnCount': len(cols),
                    'columns': cols[:NAME_LIMIT], 'indexed': idx.done, 'current': t.current,
                    'delimiter': idx.delimiter, 'size': humanSize(idx.size), 'sqlView': f't{t.tabId}'}
            if not idx.done:
                info['note'] = 'still indexing, row count is growing'
            if t.searchTerm:
                info['search'] = {'term': t.searchTerm, 'hits': t.searchHits}
            out.append(info)
        return dump(out), None

    def tool_describe_tab(self, tabId):
        tab = self.tab(tabId)
        n = tab.index.dataRows
        if tab.index.done and n > SAMPLE_ROWS:
            mid = n // 2 - 2
            picks = list(range(1, 6)) + list(range(mid, mid + 5)) + list(range(n - 4, n + 1))
        else:
            picks = list(range(1, min(n, SAMPLE_ROWS) + 1))
        cols = self.columns(tab)
        samples = []
        for r in picks:
            vals = self.readRow(tab, r)
            samples.append({'row': r, 'values': {k: cell(v) for k, v in zip(self.names(tab, len(vals)), vals)}})
        return dump({'tabId': tabId, 'rows': n, 'indexed': tab.index.done, 'columns': cols, 'samples': samples}), None

    def tool_get_rows(self, tabId, startRow, count, columns=None):
        tab = self.tab(tabId)
        count = max(1, min(count, MAX_GET))
        self.checkRow(tab, startRow)
        end = min(startRow + count - 1, tab.index.dataRows)
        pick = [self.columnIndex(tab, c) for c in columns] if columns else None
        names = [self.columns(tab)[i] for i in pick] if pick else self.columns(tab)
        rows = []
        for r in range(startRow, end + 1):
            vals = self.readRow(tab, r)
            if pick:
                vals = [vals[i] if i < len(vals) else '' for i in pick]
            rows.append([r] + [cell(v) for v in vals])
        out = {'columns': ['row'] + names, 'rows': rows}
        if end < startRow + count - 1:
            out['note'] = f'tab has {tab.index.dataRows} rows' + ('' if tab.index.done else ' so far (still indexing)')
        return dump(out), None

    def tool_search_rows(self, tabId, text, column=None, caseSensitive=False, limit=20):
        tab = self.tab(tabId)
        if not tab.index.done:
            raise ToolError(f'tab {tabId} is still indexing, try again in a moment')
        if not text:
            raise ToolError('text is empty')
        limit = max(1, min(limit, MAX_SEARCH))
        col = self.columnIndex(tab, column) if column else None
        search = Search(tab.index, text, caseSensitive, col)
        with self.lock:
            self.search = search
        if self.cancelled:
            search.cancel()
        try:
            search.run()
        finally:
            with self.lock:
                self.search = None
        if search.cancelled:
            raise ToolError('search stopped by the user')
        names = self.columns(tab)
        rows = [[h + 1] + [cell(v) for v in self.readRow(tab, h + 1)] for h in search.hits[:limit]]
        return dump({'totalMatches': len(search.hits), 'columns': ['row'] + names, 'rows': rows}), None

    def tool_show_rows(self, tabId, row, column=None):
        tab = self.tab(tabId)
        self.checkRow(tab, row)
        if column:
            column = self.columns(tab)[self.columnIndex(tab, column)]
        return f'Shown row {row} in tab {tabId}', {'type': 'showRow', 'tabId': tabId, 'row': row, 'column': column or None}

    # ---------- SQL ----------

    def connect(self):
        try:
            import duckdb
        except ImportError:
            raise ToolError('SQL is unavailable: duckdb not installed')
        with self.lock:
            if self.con is None:
                con = duckdb.connect()
                con.execute(f'SET threads={os.cpu_count() or 4}')
                try:
                    # 40% of RAM so the viewer and the OS keep room
                    ram = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
                    con.execute(f"SET memory_limit='{int(ram * 0.4) >> 20}MiB'")
                except (ValueError, OSError, AttributeError):
                    pass
                self.con = con
            return self.con

    def readCsv(self, tab, text=False):
        idx = tab.index
        enc = {'utf-8': 'utf-8', 'cp1252': 'latin-1'}.get(idx.encoding.lower(), 'utf-8')
        args = [sqlString(tab.path), f'delim={sqlString(idx.delimiter)}', f'header={str(idx.hasHeader).lower()}',
                "quote='\"'", "escape='\"'", f"encoding='{enc}'", 'strict_mode=false']
        # DuckDB pads short lines only single-threaded when fields hold newlines, so only the text view pays that
        args += ['all_varchar=true', 'null_padding=true', 'parallel=false'] if text else ['sample_size=100000', 'ignore_errors=true']
        return f"read_csv({', '.join(args)})"

    def ensureViews(self, con, query):
        for m in VIEW_REF.finditer(query):
            tabId, text = int(m.group(1)), bool(m.group(2))
            name = f't{tabId}' + ('_text' if text else '')
            if name in self.views or tabId not in self.tabs:
                continue
            try:
                con.execute(f'CREATE OR REPLACE VIEW {name} AS SELECT * FROM {self.readCsv(self.tabs[tabId], text)}')
            except Exception as e:
                raise ToolError(f'could not open tab {tabId} for SQL: {e}')
            self.views.add(name)

    def checkReadOnly(self, con, query):
        import duckdb
        try:
            stmts = con.extract_statements(query)
        except duckdb.Error as e:
            raise ToolError(str(e))
        if len(stmts) != 1:
            raise ToolError('send exactly one statement')
        st = duckdb.StatementType
        if stmts[0].type not in (st.SELECT, st.EXPLAIN) or not READ_ONLY.match(query):
            raise ToolError('only read-only queries are allowed (SELECT, WITH, DESCRIBE, SUMMARIZE, PRAGMA table_info, EXPLAIN)')
        # EXPLAIN ANALYZE runs its statement, so that must be read-only too
        if stmts[0].type == st.EXPLAIN and not re.match(r'^\s*explain\s+(analyze\s+)?(select|with|from|\()', query, re.I):
            raise ToolError('EXPLAIN is allowed for SELECT queries only')

    def tool_run_sql(self, query):
        con = self.connect()
        import duckdb
        query = query.strip().rstrip(';')
        self.checkReadOnly(con, query)
        self.ensureViews(con, query)
        self.timedOut = False

        def timeout():
            self.timedOut = True
            con.interrupt()
        timer = threading.Timer(SQL_TIMEOUT, timeout)
        start = time.monotonic()
        timer.start()
        try:
            if self.cancelled:
                raise ToolError('stopped by the user')
            cur = con.execute(query)
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(MAX_SQL_ROWS + 1)
        except duckdb.InterruptException:
            if self.cancelled:
                raise ToolError('query stopped by the user')
            raise ToolError(f'query timed out after {SQL_TIMEOUT} s; narrow it or use one aggregate over the file')
        except duckdb.Error as e:
            raise ToolError(self.sqlHint(str(e), query))
        finally:
            timer.cancel()
        truncated = len(rows) > MAX_SQL_ROWS
        rows = [[cell(v) for v in r] for r in rows[:MAX_SQL_ROWS]]
        return dump({'columns': columns, 'rows': rows, 'rowCount': len(rows), 'truncated': truncated,
                     'seconds': round(time.monotonic() - start, 2)}), None

    def sqlHint(self, msg, query):
        if self.timedOut:
            return f'query timed out after {SQL_TIMEOUT} s'
        # DuckDB appends its reader settings and fix hints; keep the cause only
        msg = msg.split('\n  file =')[0].split('\nPossible fixes')[0].strip()
        if 'Conversion Error' not in msg and 'Could not convert' not in msg:
            return msg
        ids = sorted({int(m.group(1)) for m in VIEW_REF.finditer(query) if int(m.group(1)) in self.tabs})
        calls = '; '.join(f't{i}_text = {self.readCsv(self.tabs[i], True)}' for i in ids)
        return (f'{msg}\nA column did not match its detected type. Query the text-only view instead '
                f'(all columns VARCHAR, use TRY_CAST), e.g. t{ids[0] if ids else 1}_text. {calls}')
