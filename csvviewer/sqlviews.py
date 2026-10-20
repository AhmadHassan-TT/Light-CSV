"""Shared DuckDB plumbing: one tuned connection, read_csv views over the open tabs (t1, t1_text) and the read-only check."""
import os
import re

READ_ONLY = re.compile(r'^\s*(select|with|from|describe|summarize|show|pragma\s+table_info|explain|values|\()', re.I)
VIEW_REF = re.compile(r'\bt(\d+)(_text)?\b', re.I)
READ_ONLY_HELP = 'only read-only queries are allowed (SELECT, WITH, DESCRIBE, SUMMARIZE, PRAGMA table_info, EXPLAIN)'
TABLE_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')
WORD = re.compile(r'"((?:[^"]|"")+)"|\b([A-Za-z_][A-Za-z0-9_]*)\b')
# Words a bare table name cannot be in DuckDB (its reserved keywords, plus the t<N> views of the assistant / MCP)
RESERVED = set('''all analyse analyze and any array as asc asymmetric both case cast check collate column constraint
create default deferrable desc describe distinct do else end except false fetch for foreign from grant group having in
initially intersect into lambda lateral leading limit not null offset on only or order pivot pivot_longer pivot_wider
placing primary qualify references returning select show summarize symmetric table then to trailing true union unique
unpivot using variadic when where window with'''.split())


def sqlString(text):
    return "'" + text.replace("'", "''") + "'"


def tableNameError(name, taken=()):
    """Why a tab's SQL table name is not allowed, or None; taken = names other tabs use (any case)."""
    if not TABLE_NAME.match(name or ''):
        return 'use letters, digits and _ only, starting with a letter or _ (at most 63 characters)'
    low = name.lower()
    if low in RESERVED or re.match(r'^t\d+$', low):
        return f'"{name}" is a reserved SQL word'
    if low.endswith('_text'):
        return 'names ending in _text are kept for the all-text views'
    if low in {t.lower() for t in taken}:
        return f'another tab is already called "{name}"'
    return None


def defaultTableName(path, taken=()):
    """A valid, unused table name made from a file name: "Sales 2024 (1).csv" -> sales_2024_1."""
    stem = os.path.splitext(os.path.basename(path or ''))[0]
    base = re.sub(r'[^a-z0-9_]+', '_', stem.lower()).strip('_')[:50] or 'data'
    if base[0].isdigit():
        base = 't_' + base
    if tableNameError(base) and not tableNameError(base + '_data'):
        base += '_data'
    if tableNameError(base):
        base = 'data'
    name, n = base, 1
    while tableNameError(name, taken):
        n += 1
        name = f'{base}_{n}'
    return name


def renameRefs(query, names):
    """The query with table references renamed, e.g. {'t1': 'people'} turns t1 / T1_text into people / people_text.
    Only bare words change; text in quotes ('...' and "...") is left alone."""
    low = {k.lower(): v for k, v in names.items()}

    def sub(m):
        word = m.group(0)
        if word[0] in '\'"':
            return word
        text = word.lower().endswith('_text')
        new = low.get((word[:-5] if text else word).lower())
        return new + ('_text' if text else '') if new else word
    return re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\b[A-Za-z_][A-Za-z0-9_]*\b", sub, query)


def tableRefs(query, names):
    """(name, text) for every table name (or name_text) of `names` the query mentions, matched in any case."""
    byLow = {n.lower(): n for n in names}
    refs = []
    for m in WORD.finditer(query):
        word = (m.group(1).replace('""', '"') if m.group(1) else m.group(2)).lower()
        text = word.endswith('_text')
        name = byLow.get(word[:-5] if text else word)
        if name and (name, text) not in refs:
            refs.append((name, text))
    return refs


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
    """t<N> / t<N>_text for a tab number, or <name> / <name>_text for a table name."""
    base = tabId if isinstance(tabId, str) else f't{int(tabId)}'
    return base + ('_text' if text else '')


def createView(con, tabId, path, idx, text=False):
    con.execute(f'CREATE OR REPLACE VIEW "{viewName(tabId, text)}" AS SELECT * FROM {readCsv(path, idx, text)}')


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
