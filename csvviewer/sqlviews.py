"""Shared DuckDB plumbing: one tuned connection, read_csv views over the open tabs (t1, t1_text) and the read-only check."""
import os
import re

READ_ONLY = re.compile(r'^\s*(select|with|from|describe|summarize|show|pragma\s+table_info|explain|values|\()', re.I)
VIEW_REF = re.compile(r'\bt(\d+)(_text)?\b', re.I)
READ_ONLY_HELP = 'only read-only queries are allowed (SELECT, WITH, DESCRIBE, SUMMARIZE, PRAGMA table_info, EXPLAIN)'


def sqlString(text):
    return "'" + text.replace("'", "''") + "'"


def connect():
    """New in-memory connection using all cores and 40% of RAM; raises ImportError without duckdb."""
    import duckdb
    con = duckdb.connect()
    con.execute(f'SET threads={os.cpu_count() or 4}')
    try:
        # 40% of RAM so the viewer and the OS keep room
        ram = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
        con.execute(f"SET memory_limit='{int(ram * 0.4) >> 20}MiB'")
    except (ValueError, OSError, AttributeError):
        pass
    return con


def readCsv(path, idx, text=False):
    """read_csv(...) call for a file with the CsvIndex's options; text=True gives the all-VARCHAR, keep-every-line form."""
    enc = {'utf-8': 'utf-8', 'cp1252': 'latin-1'}.get(idx.encoding.lower(), 'utf-8')
    args = [sqlString(path), f'delim={sqlString(idx.delimiter)}', f'header={str(idx.hasHeader).lower()}',
            "quote='\"'", "escape='\"'", f"encoding='{enc}'", 'strict_mode=false']
    # DuckDB pads short lines only single-threaded when fields hold newlines, so only the text view pays that
    args += ['all_varchar=true', 'null_padding=true', 'parallel=false'] if text else ['sample_size=100000', 'ignore_errors=true']
    return f"read_csv({', '.join(args)})"


def viewName(tabId, text=False):
    return f't{int(tabId)}' + ('_text' if text else '')


def createView(con, tabId, path, idx, text=False):
    con.execute(f'CREATE OR REPLACE VIEW {viewName(tabId, text)} AS SELECT * FROM {readCsv(path, idx, text)}')


def viewRefs(query):
    """(tabId, text) for every t<N> / t<N>_text the query mentions."""
    return [(int(m.group(1)), bool(m.group(2))) for m in VIEW_REF.finditer(query)]


def readOnlyError(con, query):
    """Why the query is not allowed (one read-only statement), or None when it is."""
    import duckdb
    try:
        stmts = con.extract_statements(query)
    except duckdb.Error as e:
        return str(e)
    if len(stmts) != 1:
        return 'send exactly one statement'
    st = duckdb.StatementType
    if stmts[0].type not in (st.SELECT, st.EXPLAIN) or not READ_ONLY.match(query):
        return READ_ONLY_HELP
    # EXPLAIN ANALYZE runs its statement, so that must be read-only too
    if stmts[0].type == st.EXPLAIN and not re.match(r'^\s*explain\s+(analyze\s+)?(select|with|from|\()', query, re.I):
        return 'EXPLAIN is allowed for SELECT queries only'
    return None


def shortError(msg):
    # DuckDB appends its reader settings and fix hints; keep the cause only
    return msg.split('\n  file =')[0].split('\nPossible fixes')[0].strip()
