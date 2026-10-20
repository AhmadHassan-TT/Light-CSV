"""Builder state -> DuckDB SQL; pure functions, no Qt. Every identifier and literal is quoted, numbers and dates are validated.

state = {tabId, view?, types?: {name: duckdbType}, columns: [names] ([] = all), filters: [{column, op, value, value2}],
         match: 'all'|'any', groupBy: [names], aggregates: [{func, column}], orderBy: [{column, desc}], limit, distinct}
"""
import datetime
import re

from csvviewer.sqlviews import sqlString

TEXT_OPS = [('equals', 'equals'), ('not_equals', 'not equals'), ('contains', 'contains'),
            ('not_contains', 'does not contain'), ('starts_with', 'starts with'), ('ends_with', 'ends with'),
            ('empty', 'is empty'), ('not_empty', 'is not empty'), ('one_of', 'is one of (IN)'),
            ('not_one_of', 'is not one of (NOT IN)')]
VALUE_OPS = [('=', '='), ('!=', '≠'), ('>', '>'), ('>=', '≥'), ('<', '<'), ('<=', '≤'), ('between', 'between'),
             ('empty', 'is empty'), ('not_empty', 'is not empty'), ('one_of', 'is one of (IN)'),
             ('not_one_of', 'is not one of (NOT IN)')]
LIST_OPS = {'one_of', 'not_one_of'}
LIST_SPLIT = re.compile(r'[,\n\r\t]+')  # a list value: commas, or one per line / cell as pasted from a column
NO_VALUE = {'empty', 'not_empty'}
AGGREGATES = [('count', 'Count rows'), ('count_distinct', 'Count distinct'), ('sum', 'Sum'), ('avg', 'Average'),
              ('min', 'Minimum'), ('max', 'Maximum')]
NUMBER = re.compile(r'^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$')
NUMERIC_TYPES = re.compile(r'^(U?(TINY|SMALL|BIG|HUGE)?INT(EGER)?\d*|FLOAT\d*|DOUBLE|REAL|DECIMAL.*|NUMERIC.*)$')
VIEW = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')  # a tab's table name (sqlviews.tableNameError) or t<N>, maybe _text


class BuilderError(ValueError):
    pass


def ident(name):
    return '"' + str(name).replace('"', '""') + '"'


def kind(duckType):
    """'numeric', 'date', 'timestamp' or 'text' for a DuckDB type name."""
    t = (duckType or 'VARCHAR').upper()
    if NUMERIC_TYPES.match(t):
        return 'numeric'
    if t == 'DATE':
        return 'date'
    if t.startswith('TIMESTAMP'):
        return 'timestamp'
    return 'text'


def operators(duckType):
    """[(op, label)] the builder offers for a column type."""
    return TEXT_OPS if kind(duckType) == 'text' else VALUE_OPS


def literal(value, k, column):
    # Typed literal for a numeric / date / timestamp column, validated so nothing but a value reaches the SQL
    v = str(value).strip()
    if k == 'numeric':
        if not NUMBER.match(v):
            raise BuilderError(f'"{v}" is not a number (filter on {column}); use digits like 42 or 3.5')
        return v
    try:
        if k == 'date':
            return 'DATE ' + sqlString(datetime.date.fromisoformat(v).isoformat())
        return 'TIMESTAMP ' + sqlString(datetime.datetime.fromisoformat(v).isoformat(sep=' '))
    except ValueError:
        shape = 'YYYY-MM-DD' if k == 'date' else 'YYYY-MM-DD HH:MM:SS'
        raise BuilderError(f'"{v}" is not a {k} (filter on {column}); use {shape}')


def listItems(value):
    return [v.strip() for v in LIST_SPLIT.split(value) if v.strip()]


def likePattern(value, before, after):
    # ILIKE pattern with % and _ in the value taken literally (ESCAPE '\')
    v = value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return sqlString(('%' if before else '') + v + ('%' if after else '')) + " ESCAPE '\\'"


def condition(f, types):
    """SQL for one filter, or None while its value is still blank."""
    column, op = f['column'], f['op']
    duckType = types.get(column, 'VARCHAR')
    k = kind(duckType)
    c = ident(column)
    value, value2 = str(f.get('value') or ''), str(f.get('value2') or '')
    text = k == 'text'
    if text and duckType.upper() != 'VARCHAR':
        c = f'CAST({c} AS VARCHAR)'
    if op == 'empty':
        return f"({c} IS NULL OR {c} = '')" if text else f'{c} IS NULL'
    if op == 'not_empty':
        return f"({c} IS NOT NULL AND {c} <> '')" if text else f'{c} IS NOT NULL'
    valid = dict(TEXT_OPS if text else VALUE_OPS)
    if op not in valid:
        raise BuilderError(f'"{op}" does not apply to {column}')
    if op == 'between':
        if not value.strip() or not value2.strip():
            return None
        return f'{c} BETWEEN {literal(value, k, column)} AND {literal(value2, k, column)}'
    if op in LIST_OPS:
        items = listItems(value)
        if not items:
            return None
        if text:
            items = [f'lower({sqlString(s)})' for s in items]
        else:
            items = [literal(s, k, column) for s in items]
        test = f"{f'lower({c})' if text else c} {'NOT IN' if op == 'not_one_of' else 'IN'} ({', '.join(items)})"
        # "not one of" keeps empty cells, like "not equals"
        return f'({c} IS NULL OR {test})' if op == 'not_one_of' else test
    if not text:
        if not value.strip():
            return None
        v = literal(value, k, column)
        # "not equal" keeps empty cells, as a person would expect
        return f'({c} IS NULL OR {c} <> {v})' if op == '!=' else f'{c} {op} {v}'
    if not value:
        return None
    if op == 'equals':
        return f'lower({c}) = lower({sqlString(value)})'
    if op == 'not_equals':
        return f'({c} IS NULL OR lower({c}) <> lower({sqlString(value)}))'
    if op == 'contains':
        return f'{c} ILIKE {likePattern(value, True, True)}'
    if op == 'not_contains':
        return f'({c} IS NULL OR {c} NOT ILIKE {likePattern(value, True, True)})'
    if op == 'starts_with':
        return f'{c} ILIKE {likePattern(value, False, True)}'
    return f'{c} ILIKE {likePattern(value, True, False)}'  # ends_with


def aggregateAlias(agg):
    func, column = agg['func'], agg.get('column')
    if func not in dict(AGGREGATES):
        raise BuilderError(f'unknown summary "{func}"')
    if func == 'count':
        return f'Count of {column}' if column else 'Count'
    names = {'count_distinct': 'Distinct', 'sum': 'Sum of', 'avg': 'Average of', 'min': 'Min of', 'max': 'Max of'}
    return f'{names[func]} {column}'


def aggregateAliases(aggregates):
    # Aliases are column headers, so repeats get a number
    seen, out = {}, []
    for a in aggregates:
        alias = aggregateAlias(a)
        seen[alias] = seen.get(alias, 0) + 1
        out.append(alias if seen[alias] == 1 else f'{alias} {seen[alias]}')
    return out


def aggregateSql(agg, types):
    func, column = agg['func'], agg.get('column')
    if func == 'count':
        return f'count({ident(column)})' if column else 'count(*)'
    if not column:
        raise BuilderError(f'pick a column to {dict(AGGREGATES)[func].lower()}')
    c = ident(column)
    if func == 'count_distinct':
        return f'count(DISTINCT {c})'
    # Sum / average of a text column: numbers only, other cells are skipped
    if func in ('sum', 'avg') and kind(types.get(column)) != 'numeric':
        c = f'TRY_CAST({c} AS DOUBLE)'
    return f'{func}({c})'


def buildSql(state):
    """One SELECT for the builder state; raises BuilderError for bad values."""
    types = state.get('types') or {}
    view = state.get('view') or f"t{int(state['tabId'])}"
    if not VIEW.match(view):
        raise BuilderError(f'bad view name {view!r}')
    groupBy, aggregates = state.get('groupBy') or [], state.get('aggregates') or []
    if groupBy or aggregates:
        aliases = aggregateAliases(aggregates)
        cols = [ident(g) for g in groupBy] + [f'{aggregateSql(a, types)} AS {ident(n)}' for a, n in zip(aggregates, aliases)]
    else:
        cols = [ident(c) for c in state.get('columns') or []] or ['*']
    lines = ['SELECT ' + ('DISTINCT ' if state.get('distinct') else '') + ', '.join(cols), f'FROM {view}']
    conds = [c for c in (condition(f, types) for f in state.get('filters') or []) if c]
    if conds:
        joiner = '\n   OR ' if state.get('match') == 'any' else '\n  AND '
        lines.append('WHERE ' + joiner.join(conds if len(conds) == 1 else [f'({c})' for c in conds]))
    if groupBy:
        lines.append('GROUP BY ' + ', '.join(ident(g) for g in groupBy))
    order = state.get('orderBy') or []
    if order:
        lines.append('ORDER BY ' + ', '.join(ident(o['column']) + (' DESC' if o.get('desc') else '') for o in order))
    limit = state.get('limit')
    if limit:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise BuilderError('limit must be a whole number')
        lines.append(f'LIMIT {limit}')
    return '\n'.join(lines)


def outputColumns(state, allColumns):
    """Column names the query returns (what "Sort by" can pick)."""
    if state.get('groupBy') or state.get('aggregates'):
        return list(state.get('groupBy') or []) + aggregateAliases(state.get('aggregates') or [])
    return list(state.get('columns') or allColumns)
