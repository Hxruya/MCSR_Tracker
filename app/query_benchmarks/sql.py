"""SQL support: a DB-API cursor mixin that times statements, and statement analysis (label, kind, tables,
features). Works with any cursor class whose execute/executemany follow DB-API 2.0 (PyMySQL,
mysqlclient-style); parameters are never recorded, only the statement text."""
import re
import time

from flask import current_app

from app.query_benchmarks import record

_LABEL = re.compile(r"^\s*/\*\s*(.+?)\s*\*/\s*", re.S)
_STRINGS = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_COMMENTS = re.compile(r"/\*.*?\*/|--[^\n]*", re.S)
_TABLES = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?`?([A-Za-z_][\w$]*)`?",
                     re.I)
_CTE_NAMES = re.compile(r"(?:\bWITH|,)\s*(?:RECURSIVE\s+)?`?([A-Za-z_]\w*)`?\s*(?:\([^)]*\)\s*)?AS\s*\(", re.I)
_CLAUSES = re.compile(r"\s+(?=(?:FROM|WHERE|(?:LEFT |RIGHT |INNER )?JOIN|GROUP BY|ORDER BY|HAVING|LIMIT|WINDOW|"
                      r"UNION|ON DUPLICATE KEY UPDATE|VALUES)\b)")
_FEATURES = [  # (name, pattern) checked against the statement with strings and comments removed
    ("CTE", re.compile(r"^\s*WITH\b", re.I)),
    ("window function", re.compile(r"\bOVER\b\s*(\(|[A-Za-z_])", re.I)),   # OVER (...) or OVER w; not "overworld"
    ("percentile", re.compile(r"\bPERCENTILE_(CONT|DISC)\b", re.I)),
    ("aggregate", re.compile(r"\b(COUNT|SUM|AVG|MIN|MAX|GROUP_CONCAT)\s*\(|\bGROUP\s+BY\b", re.I)),
    ("join", re.compile(r"\bJOIN\b", re.I)),
    ("upsert", re.compile(r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b", re.I)),
    ("temporary table", re.compile(r"\bTEMPORARY\s+TABLE\b", re.I)),
    ("index hint", re.compile(r"\b(FORCE|USE|IGNORE)\s+INDEX\b", re.I)),
]


def analyse(statement):
    """(label, display text, kind, tables, features) for one SQL statement."""
    label = None
    m = _LABEL.match(statement)
    if m:
        label, statement = m.group(1), statement[m.end():]
    text = _dedent(statement)
    bare = _COMMENTS.sub(" ", _STRINGS.sub("''", text))
    if "\n" not in text:   # a one-line statement (built from Python strings): break it before its main clauses
        text = _CLAUSES.sub(lambda m: "\n" + m.group(0).lstrip(), text).strip()
    kind = (bare.split(None, 1) or ["?"])[0].upper()
    ctes = {n.lower() for n in _CTE_NAMES.findall(bare)} if kind == "WITH" else set()
    tables = list(dict.fromkeys(t for t in _TABLES.findall(bare)
                                if t.upper() not in {"SELECT", "LATERAL", "DUAL"} and t.lower() not in ctes))
    features = [name for name, rx in _FEATURES if rx.search(bare)]
    if len(re.findall(r"\bSELECT\b", bare, re.I)) > 1 + len(ctes):   # each CTE body is one SELECT of its own
        features.append("subquery")
    if kind == "WITH":
        kind = "SELECT"
    if kind == "CALL":
        features.append("stored procedure")
    return label, text, kind, tables, features


def _dedent(text):
    lines = [ln.rstrip() for ln in text.strip("\n").splitlines()]
    indents = [len(ln) - len(ln.lstrip()) for ln in lines[1:] if ln.strip()]
    cut = min(indents) if indents else 0
    return "\n".join([lines[0].strip()] + [ln[cut:] for ln in lines[1:]]) if lines else ""


def _summary(kind, tables):
    where = {"SELECT": "from", "WITH": "from", "DELETE": "from", "INSERT": "into", "REPLACE": "into"}.get(kind, "on")
    return f"{kind} {where} {', '.join(tables[:3])}" if tables else kind


def _record(statement, ms, rows, prefix=""):
    label, text, kind, tables, features = analyse(statement)
    engine = current_app.config.get("QUERY_BENCHMARKS_SQL_LABEL", "SQL")
    return record(engine, kind, text, ms, rows=rows, label=label, features=features,
                  summary=prefix + _summary(kind, tables), touches=tables)


class TimedCursorMixin:
    """Mix in before a DB-API cursor class to time its statements."""
    _bulk = False

    def execute(self, query, args=None):
        if self._bulk:                       # one statement of an executemany: recorded as a whole below
            return super().execute(query, args)
        t = time.perf_counter()
        try:
            return super().execute(query, args)
        finally:
            _safe(_record, query, (time.perf_counter() - t) * 1000, _rows(self))

    def executemany(self, query, args):
        args = list(args)
        t = time.perf_counter()
        self._bulk = True
        try:
            return super().executemany(query, args)
        finally:
            self._bulk = False
            _safe(_record, query, (time.perf_counter() - t) * 1000, _rows(self), f"{len(args)} rows: ")


def _rows(cursor):
    n = getattr(cursor, "rowcount", -1)
    return n if isinstance(n, int) and n >= 0 else None


def _safe(fn, *args):
    try:
        fn(*args)
    except Exception:   # the panel must never break a query
        pass


_classes = {}


def cursor_class(base):
    """A subclass of `base` (e.g. pymysql.cursors.DictCursor) that records its statements."""
    if base not in _classes:
        _classes[base] = type(f"Timed{base.__name__}", (TimedCursorMixin, base), {})
    return _classes[base]
