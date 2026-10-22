"""MongoDB support: a PyMongo CommandListener, passed to MongoClient(event_listeners=[...]).

Reads (find, aggregate, count, distinct) show their full structure with every value replaced by ?, as SQL shows
%s placeholders; follow-up getMore batches are added to the original command's time and row count. Writes show
only which fields they touch, never the values.
"""
import json
import threading

from pymongo import monitoring

from app.query_benchmarks import add_time, record

READS = {"find", "aggregate", "count", "distinct"}
WRITES = {"insert", "update", "delete", "findAndModify"}
CURSOR_FOLLOW_UPS = {"getMore", "killCursors"}


class CommandListener(monitoring.CommandListener):
    def __init__(self):
        self._pending = {}                       # request_id -> started command (between started/succeeded)
        self._cursors = {}                       # cursor id -> Entry, so getMore batches add to it
        self._lock = threading.Lock()

    def started(self, event):
        if event.command_name in READS | WRITES | CURSOR_FOLLOW_UPS:
            with self._lock:
                self._pending[(event.connection_id, event.request_id)] = event.command

    def succeeded(self, event):
        with self._lock:
            command = self._pending.pop((event.connection_id, event.request_id), None)
        if command is None:
            return
        try:
            self._record(event, command)
        except Exception:   # the panel must never break a query
            pass

    def failed(self, event):
        with self._lock:
            self._pending.pop((event.connection_id, event.request_id), None)

    def _record(self, event, command):
        name, ms, reply = event.command_name, event.duration_micros / 1000, event.reply or {}
        cursor = reply.get("cursor") or {}
        if name == "killCursors":   # a cursor closed before its last batch: forget it, so the map can't grow
            with self._lock:
                for cursor_id in command.get("cursors") or []:
                    self._cursors.pop(cursor_id, None)
            return
        if name == "getMore":
            with self._lock:
                entry = self._cursors.get(command.get("getMore"))
                if cursor.get("id", 0) == 0:
                    self._cursors.pop(command.get("getMore"), None)
            add_time(entry, ms, len(cursor.get("nextBatch") or []))
            return
        coll = str(command.get(name))
        label = command.get("comment") if isinstance(command.get("comment"), str) else None
        statement, summary, features, rows = describe(name, coll, command, reply)
        entry = record("MongoDB", name, statement, ms, rows=rows, label=label, features=features,
                       summary=summary, touches=[coll])
        if entry is not None and cursor.get("id"):
            with self._lock:
                self._cursors[cursor["id"]] = entry


def describe(name, coll, command, reply):
    """(statement text, summary, features, rows) for one command."""
    cursor = reply.get("cursor") or {}
    rows = len(cursor.get("firstBatch") or []) if cursor else reply.get("n")
    if name == "aggregate":
        pipeline = command.get("pipeline") or []
        stages = [next(iter(s), "?") for s in pipeline if isinstance(s, dict)]
        text = "db.{}.aggregate([\n{}\n])".format(coll, ",\n".join("  " + _json(_redact(s)) for s in pipeline))
        if command.get("hint"):
            text += f"\nhint: {_json(command['hint'])}"
        return text, f"aggregate on {coll}: {', '.join(stages)}", list(dict.fromkeys(stages)), rows
    if name == "find":
        parts = [f"db.{coll}.find({_json(_redact(command.get('filter', {}), in_filter=True))}"
                 + (f", {_json(command['projection'])})" if command.get("projection") else ")")]
        for key in ("sort", "skip", "limit"):
            if command.get(key) not in (None, {}, 0):
                parts.append(f".{key}({_json(command[key])})")
        return "".join(parts), f"find on {coll}", [], rows
    if name == "count":
        return (f"db.{coll}.countDocuments({_json(_redact(command.get('query', {}), in_filter=True))})",
                f"count on {coll}", [], reply.get("n"))
    if name == "distinct":
        values = reply.get("values") or []
        query = _redact(command.get("query", {}), in_filter=True)
        return (f"db.{coll}.distinct({_json(command.get('key'))}, {_json(query)})",
                f"distinct on {coll}", [], len(values))
    if name == "insert":
        docs = command.get("documents") or []
        fields = sorted({k for d in docs for k in d})
        return (f"db.{coll}.insert({len(docs)} document(s) with fields {', '.join(fields)})",
                f"insert into {coll}", ["write"], reply.get("n"))
    if name == "update":
        ups = command.get("updates") or []
        lines = [f"db.{coll}.update({_json(_shape(u.get('q', {})))}, {_json(_shape(u.get('u', {})))}"
                 + (", upsert" if u.get("upsert") else "") + ")" for u in ups[:20]]
        return "\n".join(lines), f"update on {coll}", ["write"], reply.get("nModified", reply.get("n"))
    if name == "delete":
        dels = command.get("deletes") or []
        return ("\n".join(f"db.{coll}.delete({_json(_shape(d.get('q', {})))})" for d in dels[:20]),
                f"delete from {coll}", ["write"], reply.get("n"))
    return f"{name} on {coll}", f"{name} on {coll}", [], rows


# String values that are syntax, not data: kept as they are
SYNTAX_KEYS = {"as", "method"}            # $filter / $map variable names, $median / $percentile method
KEYWORDS = {"unbounded", "current"}       # $setWindowFields window bounds


def _redact(value, key=None, in_filter=False):
    """A read's structure with its values replaced by "?". Kept: field paths and variables ("$season", "$$e"),
    null and booleans, syntax words, and numbers outside filters (sort directions, projections, limits, window
    sizes). Inside a filter or a $match stage every number is a value too (a user_id, a season), so it goes, and
    so do syntax words (a notes search for "current" is the user's text, not a window bound)."""
    if isinstance(value, dict):
        return {k: _redact(v, k, in_filter or k == "$match") for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key, in_filter) for v in value]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        syntax = not in_filter and (key in SYNTAX_KEYS or value in KEYWORDS)
        return value if value.startswith("$") or syntax else "?"
    if isinstance(value, (int, float)) and not in_filter:
        return value
    return "?"


def _shape(doc):
    """The document's structure with every value replaced, so a write never shows the data written."""
    if isinstance(doc, dict):
        return {k: _shape(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [_shape(v) for v in doc[:3]]
    return "…"


def _json(value):
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)
