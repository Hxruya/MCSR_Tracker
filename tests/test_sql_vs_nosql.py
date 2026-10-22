"""SQL vs NoSQL: the same question answered by MariaDB and MongoDB gives the same result, and the two stores stay
consistent (compensation, the consistency check).
"""
import re
from urllib.parse import urlencode

import pytest

from app.importer import sync
from app.matches import TYPES
from tests.conftest import A, B, C, raw_match, seed_season


def counts(app, **kw):
    from app import facets
    from app.db_mongo import get_mongo
    with app.test_request_context():
        return facets.counts(get_mongo()["matches"], **kw)


def page_total(client, params):
    return int(TOTAL.search(client.get("/matches?" + urlencode(params)).get_data(as_text=True)).group(1))


def setup(conn, mongo_db):
    seed_season(conn, 11)
    seed_season(conn, 12)
    for m in [
        raw_match(1, [A, B], A[0], season=11),                       # win
        raw_match(2, [A, B], B[0], season=11),                       # loss
        raw_match(3, [A, B], B[0], season=12, forfeited=True),       # loss, alpha forfeited
        raw_match(4, [A, B], None, season=12),                       # draw
        raw_match(5, [A, C], A[0], season=12, match_type=1),         # casual win
        raw_match(6, [A, C], C[0], season=12, match_type=3),         # private loss
        raw_match(7, [A], None, season=12, decayed=True, change=-5),  # decay: its own result, not a draw
        raw_match(8, [B, C], B[0], season=12),                       # alpha not involved
    ]:
        sync.store_match(conn, mongo_db, m)


TOTAL = re.compile(r"([\d,]+) matches</span>")


def ids(app):
    from app.db_sql import query_one
    with app.test_request_context():
        return [query_one("SELECT player_id, uuid, nickname FROM players WHERE uuid = %s", (u,)) for u, _ in (A, B)]


def rivalry_setup(conn, mongo_db):
    seed_season(conn)
    # alpha vs bravo, oldest first: A A B draw B B B(forfeit) A
    winners = [A, A, B, None, B, B, B, A]
    for i, w in enumerate(winners, 1):
        sync.store_match(conn, mongo_db, raw_match(
            i, [A, B], w[0] if w else None, date=1790000000 + i * 100, forfeited=i == 7,
            time_ms={1: 400000, 2: 500000, 8: 600000}.get(i, 500000),
            overworld="SHIPWRECK" if i <= 2 else "VILLAGE"))
    for i in (20, 21):   # alpha beats charlie twice
        sync.store_match(conn, mongo_db, raw_match(i, [A, C], A[0], date=1790000000 + i * 100))
    sync.store_match(conn, mongo_db, raw_match(30, [A, B], B[0], match_type=3))           # private: excluded
    sync.store_match(conn, mongo_db, raw_match(31, [A], None, decayed=True, change=-5))   # decay: excluded


def importer_counts(conn, mongo_db):
    with conn.cursor() as cur:
        out = {}
        for t in ("matches", "match_players", "players", "seasons"):
            cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
            out[t] = cur.fetchone()["n"]
    conn.commit()
    out["mongo"] = mongo_db["matches"].count_documents({})
    return out


def failing_sql(monkeypatch):
    """Make the MariaDB part of store_match fail after MongoDB has been written."""
    def boom(*_a, **_k):
        raise RuntimeError("simulated MariaDB failure")
    monkeypatch.setattr(sync, "upsert_players", boom)


def test_counts_for_a_player(app, conn, mongo_db):
    setup(conn, mongo_db)
    c = counts(app, uuid=A[0])
    assert c["season"] == {11: 2, 12: 5}
    assert c["type"] == {2: 5, 1: 1, 3: 1}
    assert c["result"] == {"win": 2, "loss": 3, "draw": 1, "decay": 1, "forfeit": 1, "": 7}
    # each facet ignores its own filter but applies the others
    c = counts(app, uuid=A[0], season=12, match_type=2, result="loss")
    assert c["season"] == {11: 1, 12: 1}             # ranked losses per season
    assert c["type"] == {2: 1, 3: 1}                 # season-12 losses per type
    assert c["result"]["win"] == 0 and c["result"]["draw"] == 1 and c["result"]["loss"] == 1
    assert c["result"]["decay"] == 1


def test_counts_equal_page_totals(app, client, conn, mongo_db):
    setup(conn, mongo_db)
    for base in ({}, {"player": A[0]}, {"player": A[0], "season": 12}, {"player": A[0], "type": "ranked"}):
        c = counts(app, uuid=base.get("player"), season=base.get("season"),
                   match_type=TYPES[base.get("type", "all")])
        for s, n in c["season"].items():
            assert n == page_total(client, base | {"season": s}), (base, s)
        for name, code in TYPES.items():
            if code is not None:
                assert c["type"].get(code, 0) == page_total(client, base | {"type": name}), (base, name)
        if c["result"]:
            for r in ("win", "loss", "draw", "forfeit", "decay"):
                assert c["result"][r] == page_total(client, base | {"result": r}), (base, r)


def test_top_rivals_aggregation_matches_sql(app, conn, mongo_db):
    from app import rivalry
    rivalry_setup(conn, mongo_db)
    a, b = ids(app)
    with app.test_request_context():
        rivals = rivalry.top_rivals(A[0])
        sql = rivalry.summary(a["player_id"], b["player_id"])
    assert [(r["nickname"], r["games"], r["wins"], r["losses"], r["draws"]) for r in rivals] == [
        ("bravo", 8, 3, 4, 1), ("charlie", 2, 2, 0, 0)]            # private and decay matches excluded
    assert (rivals[0]["games"], rivals[0]["wins"], rivals[0]["losses"]) == (sql["games"], sql["a_wins"], sql["b_wins"])
    assert str(rivals[0]["win_rate"]) == "42.86"


def test_failed_sql_write_is_compensated(conn, mongo_db, monkeypatch):
    """Compensation: MongoDB is written first, so if MariaDB then fails, a new match's document is removed again,
    and a failed re-import leaves an existing match where it was, in both stores."""
    sync.store_match(conn, mongo_db, raw_match(42, [A, B], A[0]))
    failing_sql(monkeypatch)
    for match in (raw_match(41, [A, B], A[0]), raw_match(42, [A, B], B[0])):
        with pytest.raises(RuntimeError):
            sync.store_match(conn, mongo_db, match)
    assert mongo_db["matches"].find_one({"_id": 41}) is None
    assert mongo_db["matches"].find_one({"_id": 42}) is not None
    assert importer_counts(conn, mongo_db)["matches"] == 1


def test_consistency_check_finds_differences(conn, mongo_db):
    from datetime import datetime, timezone

    from app.importer import transform
    from setup.check_consistency import differences
    for m in (raw_match(51, [A, B], A[0]), raw_match(52, [A, B], B[0])):
        sync.store_match(conn, mongo_db, m)
    assert differences(conn, mongo_db) == {"sql": 2, "mongo": 2, "only_sql": [], "only_mongo": [],
                                           "orphan_timelines": [], "missing_points": [],
                                           "stray_points": [], "orphan_notes": []}
    now = datetime.now(timezone.utc)
    mongo_db["matches"].insert_one(transform.mongo_match_doc(raw_match(53, [A, B], A[0]), now))   # MongoDB only
    with conn.cursor() as cur:
        cur.execute("DELETE FROM matches WHERE match_id = 52")                                   # MongoDB only
    conn.commit()
    mongo_db["match_details"].insert_one(transform.mongo_detail_doc(
        dict(raw_match(99, [A, B], A[0]), timelines=[], completions=[]), now))                  # stray timeline
    mongo_db["elo_history"].delete_many({"match_id": 51})                                      # lost points
    note = mongo_db["notes"].insert_one({"user_id": 999, "match_id": 51, "text": "x", "tags": [],
                                         "created_at": now, "updated_at": now}).inserted_id         # no such user
    d = differences(conn, mongo_db)
    assert (d["only_sql"], d["only_mongo"], d["orphan_timelines"]) == ([], [52, 53], [99])
    assert (d["missing_points"], d["stray_points"]) == ([51], [52])   # 52's points outlived its row
    assert d["orphan_notes"] == [str(note)]
