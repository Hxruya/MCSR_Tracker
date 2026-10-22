"""query_benchmarks, the "Behind this page" panel. Most tests use a bare Flask app with no SiftStats code, to keep
the package independent; the last one checks it inside SiftStats.
"""
import pytest
from flask import Flask

from app import query_benchmarks
from app.query_benchmarks.mongo import describe
from app.query_benchmarks.sql import analyse, cursor_class


class FakeCursor:
    """Just enough of a DB-API cursor: records nothing itself, reports a row count."""
    def __init__(self):
        self.rowcount = -1

    def execute(self, query, args=None):
        self.rowcount = 3
        return 3

    def executemany(self, query, args):
        for a in args:
            self.execute(query, a)
        self.rowcount = len(args)
        return len(args)


Cursor = cursor_class(FakeCursor)


@pytest.fixture
def bare_app():
    app = Flask(__name__)
    app.config.update(QUERY_BENCHMARKS_ENABLED=True, QUERY_BENCHMARKS_HIDE=("secrets",), QUERY_BENCHMARKS_SQL_LABEL="TestDB")
    query_benchmarks.QueryBenchmarks(app)

    @app.route("/")
    def page():
        cur = Cursor()
        cur.execute("/* Totals per season */ SELECT season, COUNT(*) FROM games WHERE player = %s GROUP BY season",
                    ("alice-the-param",))
        cur.execute("SELECT password FROM secrets WHERE id = %s", (1,))
        cur.executemany("INSERT INTO log (a) VALUES (%s)", [(1,), (2,), (3,)])
        return "<html><body><p>page</p></body></html>"

    @app.route("/json")
    def json_page():
        Cursor().execute("SELECT 1")
        return {"ok": True}

    @app.route("/label")
    def nasty():
        Cursor().execute("/* <script>alert(1)</script> */ SELECT 1")
        return "<html><body></body></html>"
    return app


def test_panel_lists_queries_safely(bare_app):
    """Each statement with its label, but never a parameter value, a hidden table's statement, or unescaped text."""
    body = bare_app.test_client().get("/").get_data(as_text=True)
    assert body.index('id="query_benchmarks"') < body.index("</body>")
    assert "3 queries" in body and "TestDB: 3" in body
    assert "Totals per season" in body and "SELECT from games" in body and "aggregate" in body
    assert "alice-the-param" not in body                    # parameter values are never recorded
    assert "Hidden by configuration" in body and "password" not in body
    assert "3 rows: INSERT into log" in body                 # executemany is one entry, not three
    body = bare_app.test_client().get("/label").get_data(as_text=True)
    assert "<script>alert(1)" not in body and "&lt;script&gt;alert(1)" in body


def test_panel_only_where_it_belongs(bare_app):
    """Only on HTML pages and only when enabled, and recording outside a request (an import thread, a script)
    does nothing."""
    client = bare_app.test_client()
    assert client.get("/json").get_json() == {"ok": True}
    bare_app.config["QUERY_BENCHMARKS_ENABLED"] = False
    assert 'id="query_benchmarks"' not in client.get("/").get_data(as_text=True)

    assert query_benchmarks.record("SQL", "SELECT", "SELECT 1", 1.0) is None
    Cursor().execute("SELECT 1")   # runs normally, records nothing


def test_sql_analysis():
    label, text, kind, tables, features = analyse(
        "WITH ranked AS (SELECT RANK() OVER (ORDER BY elo DESC) AS pos, name FROM players) "
        "SELECT * FROM ranked JOIN teams t ON t.id = ranked.team_id")
    assert (label, kind, tables) == (None, "SELECT", ["players", "teams"])     # the CTE name isn't a table
    assert {"CTE", "window function", "join"} <= set(features) and "subquery" not in features
    assert "window function" not in analyse("SELECT overworld FROM seeds")[4]   # "overworld" isn't OVER
    assert analyse("CREATE TEMPORARY TABLE IF NOT EXISTS tmp (a INT)")[3] == ["tmp"]


def test_mongo_listener(bare_app):
    """Writes show field names but not values, aggregations are summarised by stage, and a cursor closed early
    (killCursors) is dropped from the listener's map, so it can't grow without bound."""
    from types import SimpleNamespace

    from app.query_benchmarks.mongo import CommandListener
    text, summary, features, rows = describe(
        "insert", "notes", {"documents": [{"text": "my private note", "tags": ["x"]}]}, {"n": 1})
    assert "my private note" not in text and "tags" in text and "text" in text and rows == 1
    text, *_ = describe("update", "notes", {"updates": [{"q": {"_id": 5}, "u": {"$set": {"text": "secret"}}}]}, {})
    assert "secret" not in text and "$set" in text
    text, summary, features, _ = describe(
        "aggregate", "matches", {"pipeline": [{"$match": {"season": 12}}, {"$group": {"_id": "$type"}}]},
        {"cursor": {"firstBatch": [{}, {}], "id": 0}})
    assert summary == "aggregate on matches: $match, $group" and features == ["$match", "$group"]
    # reads keep their structure but no values: not a search term, an id or a uuid, as SQL shows only %s
    uuid = "635f35ee69ed4f0c94ff26ece4818956"
    text, *_ = describe("find", "notes", {"filter": {"user_id": 12, "$text": {"$search": "secret plan"}},
                                          "sort": {"updated_at": -1}, "limit": 20}, {})
    assert "secret" not in text and "12" not in text and '"$search": "?"' in text and ".limit(20)" in text
    text, *_ = describe("aggregate", "matches", {"pipeline": [
        {"$match": {"players.uuid": uuid, "match_type": 2}}, {"$sort": {"played_at": -1}},
        {"$group": {"_id": "$players.uuid", "wins": {"$sum": {"$cond": [{"$eq": ["$result.uuid", uuid]}, 1, 0]}}}},
        {"$group": {"_id": None, "m": {"$median": {"input": "$t", "method": "approximate"}}}}]}, {})
    assert uuid not in text and '"match_type": "?"' in text and '"played_at": -1' in text
    assert '"$players.uuid"' in text and '"approximate"' in text
    text, *_ = describe("count", "notes", {"query": {"user_id": 12, "$text": {"$search": "current"}}}, {"n": 3})
    assert "12" not in text and "current" not in text            # a syntax word typed as a search is a value

    listener = CommandListener()

    def run(name, command, reply, request_id):
        ids = dict(connection_id=1, request_id=request_id, command_name=name)
        listener.started(SimpleNamespace(command=command, **ids))
        listener.succeeded(SimpleNamespace(duration_micros=1000, reply=reply, **ids))
    with bare_app.test_request_context("/"):
        bare_app.preprocess_request()
        run("find", {"find": "matches", "filter": {}}, {"cursor": {"id": 77, "firstBatch": [{}]}}, 1)
        assert 77 in listener._cursors
        run("killCursors", {"killCursors": "matches", "cursors": [77]}, {"ok": 1}, 2)
        assert listener._cursors == {}


def test_panel_inside_siftstats(app, client, conn, mongo_db):
    app.config["QUERY_BENCHMARKS_ENABLED"] = True
    try:
        body = client.get("/").get_data(as_text=True)
    finally:
        app.config["QUERY_BENCHMARKS_ENABLED"] = False
    assert "Behind this page" in body and "MariaDB" in body
    assert "Dataset size" in body                         # the label from a leading SQL comment
