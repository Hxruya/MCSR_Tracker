"""query_benchmarks: shows the database queries behind each page of a Flask app.

A self-contained Flask extension. It records every statement run while a request is handled and appends a
small "Behind this page" panel to HTML responses: engine, label, time, rows and the statement itself.

Wiring (all opt-in; nothing is monkeypatched):

    lens = QueryBenchmarks(app)                                   # or lens.init_app(app) in a factory
    pymysql.connect(..., cursorclass=query_benchmarks.sql.cursor_class(DictCursor))
    MongoClient(uri, event_listeners=[query_benchmarks.mongo.CommandListener()])

Any other driver can call query_benchmarks.record(...) itself.

Config:
    QUERY_BENCHMARKS_ENABLED     show the panel (default False)
    QUERY_BENCHMARKS_HIDE        table / collection names whose statements are hidden (still counted and timed)
    QUERY_BENCHMARKS_SQL_LABEL   engine name shown for SQL statements (default "SQL")
    QUERY_BENCHMARKS_MAX_ENTRIES entries kept per request (default 200; the totals still count every query)

Labels: start an SQL statement with a comment (/* Running score */ SELECT ...), or pass MongoDB's own
`comment=` option to find()/aggregate(). Parameter values are never recorded.
"""
import time

from flask import current_app, g, has_request_context

__all__ = ["QueryBenchmarks", "record", "Entry"]

EXT = "query_benchmarks"


class Entry:
    """One recorded statement."""
    __slots__ = ("engine", "kind", "statement", "label", "ms", "rows", "features", "summary", "hidden")

    def __init__(self, engine, kind, statement, ms, rows=None, label=None, features=(), summary="", hidden=False):
        self.engine, self.kind, self.statement, self.ms = engine, kind, statement, ms
        self.rows, self.label, self.features, self.summary, self.hidden = rows, label, tuple(features), summary, hidden


class _Log:
    """Per-request log: every query is counted, but only the first MAX entries are kept."""
    __slots__ = ("entries", "count", "ms", "by_engine", "started")

    def __init__(self):
        self.entries, self.count, self.ms, self.by_engine, self.started = [], 0, 0.0, {}, time.perf_counter()


def _state():
    """The extension's settings if it's installed and enabled and we're inside a request, else None."""
    if not has_request_context():
        return None
    ext = current_app.extensions.get(EXT)
    if ext is None or not current_app.config.get("QUERY_BENCHMARKS_ENABLED"):
        return None
    return ext


def _log():
    log = g.get("_query_benchmarks")
    if log is None:
        log = g._query_benchmarks = _Log()
    return log


def record(engine, kind, statement, ms, rows=None, label=None, features=(), summary="", touches=()):
    """Record one statement for the current request. A no-op outside a request or when disabled.

    `touches` are the table / collection names involved; any of them in QUERY_BENCHMARKS_HIDE hides the statement.
    Returns the Entry (or None) so a driver adapter can add time to it later (e.g. MongoDB getMore)."""
    ext = _state()
    if ext is None:
        return None
    hide = {h.lower() for h in current_app.config.get("QUERY_BENCHMARKS_HIDE", ())}
    hidden = any(t.lower() in hide for t in touches)
    entry = Entry(engine, kind, "" if hidden else statement, ms, rows, None if hidden else label,
                  () if hidden else features, "" if hidden else summary, hidden)
    log = _log()
    log.count += 1
    log.ms += ms
    log.by_engine[engine] = log.by_engine.get(engine, 0) + 1
    if len(log.entries) < current_app.config.get("QUERY_BENCHMARKS_MAX_ENTRIES", 200):
        log.entries.append(entry)
    return entry


def add_time(entry, ms, rows=0):
    """Add follow-up work (another batch of a cursor) to an entry already recorded."""
    if entry is None or not has_request_context():
        return
    entry.ms += ms
    if entry.rows is not None:
        entry.rows += rows
    log = g.get("_query_benchmarks")
    if log is not None:
        log.ms += ms


class QueryBenchmarks:
    def __init__(self, app=None):
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        app.config.setdefault("QUERY_BENCHMARKS_ENABLED", False)
        app.config.setdefault("QUERY_BENCHMARKS_HIDE", ())
        app.config.setdefault("QUERY_BENCHMARKS_SQL_LABEL", "SQL")
        app.config.setdefault("QUERY_BENCHMARKS_MAX_ENTRIES", 200)
        app.extensions[EXT] = self
        # first in line, so the page timer covers the app's own before_request work
        app.before_request_funcs.setdefault(None, []).insert(0, self._start)
        app.after_request(self._inject)

    @staticmethod
    def _start():
        if _state() is not None:
            g._query_benchmarks = _Log()

    def _inject(self, response):
        if _state() is None:
            return response
        if response.mimetype != "text/html" or response.direct_passthrough or response.is_streamed:
            return response
        body = response.get_data(as_text=True)
        at = body.rfind("</body>")
        if at == -1:
            return response
        from app.query_benchmarks.panel import render
        log = g.get("_query_benchmarks") or _Log()
        page_ms = (time.perf_counter() - log.started) * 1000
        response.set_data(body[:at] + render(log, page_ms) + body[at:])
        return response
