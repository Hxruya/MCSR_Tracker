"""Importing from the MCSR Ranked API: API JSON to rows and documents, idempotent upserts, Refresh, timelines,
failures, and the API client's retries and pacing.
"""
import copy
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from app.importer import api_client, sync, transform
from app.importer.api_client import ApiClient, ApiError, ApiNotFound
from tests.conftest import A, B, C, TEST_MONGO, TEST_SQL, raw_match, uuid_for


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(api_client.time, "sleep", sleeps.append)
    monkeypatch.setattr(api_client, "MIN_INTERVAL", 0)
    return sleeps


class FakeSession:
    def __init__(self, script):
        self.script, self.headers, self.urls = list(script), {}, []

    def get(self, url, params=None, timeout=None):
        self.urls.append(url)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body


class FakeApi:
    """Serves a fixed match list through the same interface as ApiClient (newest first, cursor paging)."""

    def __init__(self, matches):
        self.matches = sorted(matches, key=lambda m: m["id"], reverse=True)
        self.calls = []

    def leaderboard(self, season=None):
        self.calls.append(("leaderboard", season))
        return {"season": {"number": 12, "startsAt": 1780000000, "endsAt": 1790000000}, "users": []}

    def user_matches(self, uuid, season, before=None, count=100):
        self.calls.append(("matches", season, before))
        rows = [m for m in self.matches if m["season"] == season and any(p["uuid"] == uuid for p in m["players"])]
        if before is not None:
            rows = [m for m in rows if m["id"] < before]
        return rows[:count]

    def match(self, match_id):
        self.calls.append(("match", match_id))
        m = next(m for m in self.matches if m["id"] == match_id)
        return dict(m, completions=[], timelines=[{"uuid": m["players"][0]["uuid"], "time": 1000,
                                                   "type": "story.enter_the_nether"}])

    def user(self, ident):
        self.calls.append(("user", ident))
        return {"uuid": ident, "nickname": "outsider", "eloRate": None, "eloRank": None, "country": None}


def player_row(conn, uuid):
    with conn.cursor() as cur:
        cur.execute("SELECT player_id, uuid, nickname FROM players WHERE uuid = %s", (uuid,))
        return cur.fetchone()


def history(n):
    return [raw_match(10000 + i, [A, B], A[0] if i % 2 else B[0], season=12 if i < n // 2 else 11,
                      date=1790000000 - i * 600) for i in range(n)]


class FailingApi(FakeApi):
    """Serves the first page of a season, then the API goes down."""

    def user_matches(self, uuid, season, before=None, count=100):
        if before is not None:
            raise ApiError("simulated outage")
        return super().user_matches(uuid, season, before, count)


def full_import(api, conn, mongo_db, player):
    """A full import recorded in import_runs, as run_player_import does it."""
    run_id = sync.start_run(mongo_db, player, "full")
    return sync.import_history(api, conn, mongo_db, player, "full", run_id=run_id)


def counts(conn, mongo_db):
    with conn.cursor() as cur:
        out = {}
        for t in ("matches", "match_players", "players", "seasons"):
            cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
            out[t] = cur.fetchone()["n"]
    conn.commit()
    out["mongo"] = mongo_db["matches"].count_documents({})
    return out


def _detail(match_id, events, completions=(), match_type=2, variations=("end_tower:caged:front",)):
    players = [{"uuid": A[0], "nickname": "alpha"}, {"uuid": B[0], "nickname": "bravo"}]
    return {"id": match_id, "type": match_type, "season": 12, "decayed": False, "players": players,
            "seed": {"endTowers": [76, 79, 82, 85], "variations": list(variations)},
            "timelines": [{"uuid": u, "time": t, "type": k} for u, t, k in events],
            "completions": [{"uuid": u, "time": t} for u, t in completions]}


def sample(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["data"]


FIXTURES = Path(__file__).resolve().parent / "fixtures"  # real API responses, trimmed


NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


def page_matches():
    return sample("match_list")


def test_import_twice_creates_no_duplicates(conn, mongo_db, monkeypatch):
    monkeypatch.setattr(sync, "PAGE_SIZE", 7)  # force several pages per season
    api = FakeApi(history(30))
    sync.store_match(conn, mongo_db, api.matches[0])  # creates the player rows
    player = player_row(conn, A[0])
    first = sync.import_history(api, conn, mongo_db, player, "full")
    after_first = counts(conn, mongo_db)
    stamps = {d["_id"]: d for d in mongo_db["matches"].find({}, {"first_imported_at": 1, "imported_at": 1})}
    second = sync.import_history(api, conn, mongo_db, player, "full")
    assert first["seen"] == 30 and first["failed"] == 0
    assert second == {"seen": 30, "inserted": 0, "updated": 0, "unchanged": 30, "failed": 0}
    assert counts(conn, mongo_db) == after_first == {"matches": 30, "match_players": 60, "players": 2,
                                                     "seasons": 12, "mongo": 30}  # seasons 1..current
    # $setOnInsert: the re-import rewrote each match (imported_at moved on) but kept when it was first stored
    for d in mongo_db["matches"].find({}, {"first_imported_at": 1, "imported_at": 1}):
        assert d["first_imported_at"] == stamps[d["_id"]]["first_imported_at"]
        assert d["imported_at"] > stamps[d["_id"]]["imported_at"]


def test_refresh_only_fetches_new_matches(conn, mongo_db):
    old = history(10)
    api = FakeApi(old)
    sync.store_match(conn, mongo_db, old[0])
    player = player_row(conn, A[0])
    full_import(api, conn, mongo_db, player)
    new = [raw_match(20000 + i, [A, C], A[0], season=12, date=1795000000 + i) for i in range(3)]
    api = FakeApi(old + new)
    stats = sync.import_history(api, conn, mongo_db, player, "refresh")
    assert stats["seen"] == 3 and stats["inserted"] == 3
    assert all(c[1] == 12 for c in api.calls if c[0] == "matches")  # never walks back to older seasons


def test_refresh_does_not_stop_at_matches_stored_by_other_imports(conn, mongo_db):
    """Alpha is imported, then bravo's import stores a newer alpha-vs-bravo match. Alpha's Refresh starts from
    alpha's own last import, not from the newest stored match, so it still fetches the matches in between."""
    now = int(time.time())
    old = [raw_match(30000 - i, [A, C], A[0], season=12, date=now - 86400 - i * 600) for i in range(5)]
    api = FakeApi(old)
    sync.store_match(conn, mongo_db, old[0])
    alpha = player_row(conn, A[0])
    full_import(api, conn, mongo_db, alpha)
    gap = [raw_match(30100 + i, [A, C], A[0], season=12, date=now + 600 + i) for i in range(3)]
    versus_bravo = raw_match(30200, [A, B], B[0], season=12, date=now + 1200)
    sync.store_match(conn, mongo_db, versus_bravo)          # stored by bravo's import, not alpha's
    newest = raw_match(30300, [A, C], A[0], season=12, date=now + 1800)
    stats = sync.import_history(FakeApi(old + gap + [versus_bravo, newest]), conn, mongo_db, alpha, "refresh")
    assert stats["inserted"] == 4 and stats["unchanged"] == 1     # newest + the 3 in the gap; bravo's already stored
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM match_players WHERE player_id = %s", (alpha["player_id"],))
        assert cur.fetchone()["n"] == len(old) + len(gap) + 2


def test_failed_import_still_refreshes_summary_stats(conn, mongo_db, monkeypatch):
    """Matches stored before an import fails are kept, so the summary tables are refreshed for them too."""
    monkeypatch.setattr(sync, "PAGE_SIZE", 4)
    matches = history(10)
    sync.store_match(conn, mongo_db, matches[0])
    sync.refresh_stats(conn)                                   # alpha's summary row: 1 game
    player = player_row(conn, A[0])
    assert sync.run_player_import(player, "full", TEST_SQL, TEST_MONGO, api=FailingApi(matches)) is None
    assert sync.latest_run(mongo_db, A[0])["status"] == "failed"
    conn.commit()   # end this connection's snapshot so it sees the import's own connection's commits
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM match_players WHERE player_id = %s", (player["player_id"],))
        stored = cur.fetchone()["n"]
        cur.execute("SELECT games_played FROM mv_player_stats_lifetime WHERE player_id = %s", (player["player_id"],))
        assert stored == 5 and cur.fetchone()["games_played"] == stored   # the first page's 4 + the one before
    conn.commit()


def test_import_loads_timelines_for_latest_ranked_matches_only(conn, mongo_db, monkeypatch):
    """Timelines cost one call per match, so an import loads them for the latest RECENT_TIMELINES ranked matches
    only, and never twice."""
    monkeypatch.setattr(sync, "RECENT_TIMELINES", 3)
    matches = history(6) + [raw_match(10100, [A, B], A[0], season=12, match_type=3, date=1799999999)]  # private
    api = FakeApi(matches)
    sync.store_match(conn, mongo_db, matches[0])
    player = player_row(conn, A[0])
    stats = sync.run_player_import(player, "full", TEST_SQL, TEST_MONGO, api=api)
    assert stats["timelines"] == 3
    assert sorted(mongo_db["match_details"].distinct("_id")) == [10000, 10001, 10002]   # the newest ranked three
    again = sync.run_player_import(player, "refresh", TEST_SQL, TEST_MONGO, api=FakeApi(matches))
    assert again["timelines"] == 0


def test_bad_match_logged_and_skipped(conn, mongo_db):
    good = history(4)
    bad = raw_match(99999, [A, B], A[0], season=12, date=1799999999)
    bad["players"][1]["uuid"] = "NOT-A-UUID"  # rejected by the Mongo validator
    api = FakeApi(good + [bad])
    sync.store_match(conn, mongo_db, good[0])
    player = player_row(conn, A[0])
    run_id = sync.start_run(mongo_db, player, "full")
    stats = sync.import_history(api, conn, mongo_db, player, "full", run_id=run_id)
    assert stats["failed"] == 1 and stats["seen"] == 5
    run = mongo_db["import_runs"].find_one({"_id": run_id})
    assert run["status"] == "partial" and run["matches_failed"] == 1 and "99999" in run["errors"][0]
    assert run["finished_at"] is not None and sync.latest_run(mongo_db, A[0])["_id"] == run_id
    with conn.cursor() as cur:  # nothing half-written: both stores hold exactly the same matches
        cur.execute("SELECT match_id FROM matches")
        assert {r["match_id"] for r in cur.fetchall()} == set(mongo_db["matches"].distinct("_id"))


def test_refresh_after_a_partial_import_retries_the_failed_match(conn, mongo_db):
    """A partial import doesn't count as finished: Refresh would otherwise stop at matches older than that run and
    never fetch the one that failed. So the next Refresh runs a full import, which stores it."""
    good = history(4)
    bad = raw_match(99999, [A, B], A[0], season=12, date=1799999999)
    broken = copy.deepcopy(bad)
    broken["players"][1]["uuid"] = "NOT-A-UUID"                 # rejected by the Mongo validator this time
    sync.store_match(conn, mongo_db, good[0])
    player = player_row(conn, A[0])
    run_id = sync.start_run(mongo_db, player, "full")
    sync.import_history(FakeApi(good + [broken]), conn, mongo_db, player, "full", run_id=run_id)
    assert sync.latest_run(mongo_db, A[0])["status"] == "partial"
    assert sync.last_finished_run(mongo_db, A[0]) is None and not sync.has_completed_full_import(mongo_db, A[0])
    stats = sync.run_player_import(player, "refresh", TEST_SQL, TEST_MONGO, api=FakeApi(good + [bad]))
    assert stats["inserted"] == 1 and mongo_db["matches"].find_one({"_id": 99999}) is not None
    latest = sync.latest_run(mongo_db, A[0])
    assert (latest["source"], latest["status"]) == ("full", "success")   # the Refresh ran as a full import


def test_winner_outside_players_becomes_a_participant(conn, mongo_db):
    """Private match whose winner isn't listed (771033): the listed players lost, and the winner is added as a
    participant row, so every non-draw has exactly one winner; storing it fetches the winner's nickname."""
    m = sample("match_detail_odd_771033")
    winner = m["result"]["uuid"]
    assert transform.outside_winner(m) == winner
    rows = transform.match_player_rows(m)
    listed = [r for r in rows if r["uuid"] != winner]
    assert {r["outcome"] for r in listed} == {"loss"} and all(r["forfeited"] is None for r in listed)
    added = [r for r in rows if r["uuid"] == winner]
    assert added == [{"uuid": winner, "elo_before": None, "elo_change": None, "outcome": "win", "forfeited": 0,
                      "finish_time_ms": None}]   # a forfeit win has no finish time
    assert transform.match_row(m)["result_type"] == "forfeit"

    api = FakeApi([])
    assert sync.store_match(conn, mongo_db, raw_match(5, [A, B], uuid_for(77), match_type=3, forfeited=True),
                            api=api) == "inserted"
    assert ("user", uuid_for(77)) in api.calls
    with conn.cursor() as cur:
        cur.execute("SELECT p.nickname, mp.outcome FROM match_players mp JOIN players p USING (player_id) "
                    "WHERE mp.match_id = 5 ORDER BY p.nickname")
        assert cur.fetchall() == [{"nickname": "alpha", "outcome": "loss"}, {"nickname": "bravo", "outcome": "loss"},
                                  {"nickname": "outsider", "outcome": "win"}]


def test_client_retries_only_what_can_succeed(no_sleep):
    """429 waits for Retry-After and tries again; network errors are retried 3 times, then raise; a 400 (unknown
    player) is final at once."""
    s = FakeSession([FakeResponse(429, {"status": "error", "data": "slow down"}, {"Retry-After": "7"}),
                     FakeResponse(200, {"status": "success", "data": {"ok": 1}})])
    assert ApiClient(base="http://x", session=s).get("/users/a") == {"ok": 1}
    assert 7 in no_sleep
    s = FakeSession([requests.ConnectionError("dns")] * 4)
    with pytest.raises(ApiError):
        ApiClient(base="http://x", session=s).get("/matches/1")
    assert len(s.urls) == 4
    s = FakeSession([FakeResponse(400, {"status": "error", "data": {"params": {"identifier": ["Invalid"]}}})])
    with pytest.raises(ApiNotFound):
        ApiClient(base="http://x", session=s).user("nobody")
    assert len(s.urls) == 1


def test_client_respects_the_rate_limit(monkeypatch):
    """However many calls are queued, no 10-minute window holds more than 480 (the API allows 500), and when the
    API's RateLimit header says none are left, the client waits for the reset."""
    clock, sleeps = [0.0], []

    def sleep(s):
        sleeps.append(s)
        clock[0] += s
    monkeypatch.setattr(api_client.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(api_client.time, "sleep", sleep)
    monkeypatch.setattr(api_client, "_next_allowed", 0.0)
    starts = []
    for _ in range(1500):
        api_client._wait_turn()
        starts.append(clock[0])
    assert max(sum(1 for t in starts if s <= t < s + 600) for s in starts) == 480

    sleeps.clear()
    s = FakeSession([FakeResponse(200, {"status": "success", "data": 1}, {"RateLimit": '"500-in-10min"; r=0; t=30'}),
                     FakeResponse(200, {"status": "success", "data": 2})])
    c = ApiClient(base="http://x", session=s)
    c.get("/a")
    c.get("/b")
    assert any(x >= 29 for x in sleeps)


def test_each_result_type_maps_to_rows():
    """completed, forfeit, draw and decay, from real API responses."""
    m = next(x for x in page_matches() if x["type"] == 2 and not x["forfeited"])
    row = transform.match_row(m)
    assert row["result_type"] == "completed" and row["duration_ms"] == m["result"]["time"]
    assert row["played_at"] == datetime.fromtimestamp(m["date"], tz=timezone.utc).replace(tzinfo=None)
    mps = {r["uuid"]: r for r in transform.match_player_rows(m)}
    win = mps[m["result"]["uuid"]]
    assert win["outcome"] == "win" and win["finish_time_ms"] == m["result"]["time"] and win["forfeited"] == 0
    loss = next(r for u, r in mps.items() if u != m["result"]["uuid"])
    assert loss["outcome"] == "loss" and loss["finish_time_ms"] is None and loss["forfeited"] == 0

    detail = sample("match_detail_forfeit_13890182")   # the forfeiter is the one with a forfeit timeline event
    forfeiter = next(t["uuid"] for t in detail["timelines"] if t["type"] == "projectelo.timeline.forfeit")
    assert transform.match_row(detail)["result_type"] == "forfeit"
    mps = {r["uuid"]: r for r in transform.match_player_rows(detail)}
    assert mps[forfeiter]["forfeited"] == 1 and mps[forfeiter]["outcome"] == "loss"
    winner = mps[detail["result"]["uuid"]]
    assert winner["forfeited"] == 0 and winner["finish_time_ms"] is None   # a forfeit win has no time

    detail = sample("match_detail_draw_13816113")
    assert transform.match_row(detail)["result_type"] == "draw"
    for r in transform.match_player_rows(detail):
        assert r["outcome"] == "draw" and r["forfeited"] == 0 and r["finish_time_ms"] is None

    m = sample("match_detail_odd_663708")             # season 3 decay, result: null
    assert m["result"] is None and m["decayed"] is True
    row = transform.match_row(m)
    assert row["result_type"] == "decay" and row["duration_ms"] is None and row["is_decayed"] == 1
    rows = transform.match_player_rows(m)
    assert [(r["outcome"], r["forfeited"], r["finish_time_ms"]) for r in rows] == [("decay", 0, None)]
    assert rows[0]["elo_change"] < 0                  # the Elo penalty is kept
    assert transform.mongo_match_doc(m, NOW)["result"] is None


def test_elo_columns():
    """elo_before/elo_change come from the match's changes; a placement match has neither."""
    m = next(x for x in page_matches() if x["type"] == 2 and all(c["change"] is not None for c in x["changes"]))
    rows = {r["uuid"]: r for r in transform.match_player_rows(m)}
    for c in m["changes"]:
        assert (rows[c["uuid"]]["elo_before"], rows[c["uuid"]]["elo_change"]) == (c["eloRate"], c["change"])
    old = sample("match_detail_old_season6")          # first season-6 match: placement for edcr
    edcr = {r["uuid"]: r for r in transform.match_player_rows(old)}["635f35ee69ed4f0c94ff26ece4818956"]
    assert edcr["elo_change"] is None and edcr["elo_before"] is None


def test_private_match_many_players_forfeit_unknown():
    m = next(x for x in page_matches() if x["type"] == 3 and len(x["players"]) > 2 and x["result"]["uuid"])
    rows = transform.match_player_rows(m)
    losers = [r for r in rows if r["outcome"] == "loss"]
    assert m["forfeited"] and len(rows) == len(m["players"])
    assert all(r["forfeited"] is None for r in losers)      # who forfeited a 5-player room is unknown


def test_malformed_api_data_is_stored_safely():
    """Missing fields become NULL / empty, and a player listed twice is stored once."""
    old = sample("match_detail_old_season6")          # no vod key, old seed format
    doc = transform.mongo_match_doc(old, NOW)
    assert doc["videos"] == [] and doc["seed"]["id"] is None and doc["seed"]["endTowers"] is None
    m = copy.deepcopy(sample("match_detail_forfeit_13890182"))
    for key in ("category", "seed", "vod", "changes", "decayed"):
        m.pop(key)
    m["players"][0].pop("country")
    m["players"][0]["eloRate"] = None
    assert transform.match_row(m)["is_decayed"] == 0
    for r in transform.match_player_rows(m):
        assert r["elo_before"] is None and r["elo_change"] is None
    p = transform.player_row(m["players"][0])
    assert p["country"] is None and p["current_elo"] is None
    doc = transform.mongo_match_doc(m, NOW)
    assert doc["seed"] is None and doc["videos"] == [] and doc["category"] is None
    m = copy.deepcopy(sample("match_detail_forfeit_13890182"))
    m["players"].append(dict(m["players"][0]))
    assert len(transform.match_player_rows(m)) == 2 and transform.outside_winner(m) is None


def test_end_splits_extraction():
    from app.importer.transform import end_splits
    d = _detail(1, [(A[0], 400000, "story.enter_the_end"), (A[0], 410000, "projectelo.timeline.death"),
                    (A[0], 430000, "story.enter_the_end"),                # re-entry after dying: first entry counts
                    (A[0], 470000, "projectelo.timeline.dragon_death"),
                    (B[0], 450000, "story.enter_the_end"),                # entered, never killed the dragon
                    (B[0], 300000, "projectelo.timeline.death")],         # death before the End: not counted
                [(A[0], 490000)])
    s = {x["uuid"]: x for x in end_splits(d)}
    assert s[A[0]] == {"uuid": A[0], "entered_end": 400000, "dragon_kill": 470000, "dragon_split": 70000,
                       "end_deaths": 1, "finished": True}
    assert s[B[0]]["dragon_split"] is None and s[B[0]]["end_deaths"] == 0 and not s[B[0]]["finished"]


def test_mongo_doc_shape():
    m = sample("match_detail_forfeit_13890182")
    doc = transform.mongo_match_doc(m, NOW)
    assert doc["_id"] == m["id"] and doc["raw"] is m
    assert doc["played_at"].tzinfo is not None
    assert doc["players"] == [{"uuid": p["uuid"], "nickname": p["nickname"]} for p in m["players"]]
    assert doc["videos"] == m["vod"]
    assert transform.match_row(m)["match_id"] == doc["_id"]   # the link between the stores is the shared id


def test_season_result_rows():
    rows = {r["season_id"]: r for r in transform.season_result_rows(sample("user_seasons"))}
    assert set(rows) == {6, 7, 8, 9, 10, 11, 12}
    s = sample("user_seasons")["seasonResults"]["11"]
    assert rows[11]["final_elo"] == s["last"]["eloRate"] and rows[11]["highest_elo"] == s["highest"]
