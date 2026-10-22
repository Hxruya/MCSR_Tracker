"""Complex queries: the statistics views and rate rules, window functions (recent form, Elo averages, streaks),
aggregates, the seed ranking's cross-store join and End-game aggregations, checked against independent
calculations.
"""
import random
from decimal import ROUND_HALF_UP, Decimal

import pytest

from app import stats
from app.importer import elo_history, sync
from tests.conftest import A, B, C, raw_match, seed_season


def summarise(outcomes):
    """The view's columns computed in Python from participant rows ('outcome', 'forfeited', 'elo_change'): the
    independent calculation the SQL views and window functions are checked against."""
    rows = list(outcomes)
    games = len(rows)
    wins = sum(1 for r in rows if r["outcome"] == "win")
    draws = sum(1 for r in rows if r["outcome"] == "draw")
    forfeits = sum(1 for r in rows if r.get("forfeited") == 1)
    changes = [r["elo_change"] for r in rows if r.get("elo_change") is not None]
    return {
        "games_played": games, "wins": wins, "losses": sum(1 for r in rows if r["outcome"] == "loss"),
        "draws": draws, "forfeits": forfeits,
        "win_rate": stats.win_rate(wins, games, draws), "forfeit_rate": stats.rate(forfeits, games),
        "total_elo_change": sum(changes) if changes else None,
        "avg_elo_change": (Decimal(sum(changes)) / len(changes)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if changes else None,
    }


def rows(wins=0, losses=0, draws=0, forfeits=0):
    """forfeits are a subset of losses."""
    out = [{"outcome": "win", "forfeited": 0, "elo_change": 10} for _ in range(wins)]
    out += [{"outcome": "loss", "forfeited": 1 if i < forfeits else 0, "elo_change": -10} for i in range(losses)]
    out += [{"outcome": "draw", "forfeited": 0, "elo_change": 0} for _ in range(draws)]
    return out


def _detail(match_id, events, completions=(), match_type=2, variations=("end_tower:caged:front",)):
    players = [{"uuid": A[0], "nickname": "alpha"}, {"uuid": B[0], "nickname": "bravo"}]
    return {"id": match_id, "type": match_type, "season": 12, "decayed": False, "players": players,
            "seed": {"endTowers": [76, 79, 82, 85], "variations": list(variations)},
            "timelines": [{"uuid": u, "time": t, "type": k} for u, t, k in events],
            "completions": [{"uuid": u, "time": t} for u, t in completions]}


def ranked_series(n, start_id=1000, p=A, q=B):
    """n ranked matches between p and q, alternating winners, with a forfeit and a draw mixed in."""
    out = []
    for i in range(n):
        mid = start_id + i
        if i % 10 == 9:
            out.append(raw_match(mid, [p, q], None, forfeited=True, date=1790000000 + i * 600))
        elif i % 7 == 3:
            out.append(raw_match(mid, [p, q], q[0], forfeited=True, time_ms=200000, date=1790000000 + i * 600))
        else:
            out.append(raw_match(mid, [p, q], p[0] if i % 2 else q[0], time_ms=400000 + i * 1000,
                                 date=1790000000 + i * 600))
    return out


def store(conn, mongo_db, matches):
    for m in matches:
        assert sync.store_match(conn, mongo_db, m) == "inserted"


def ids(app):
    from app.db_sql import query_one
    with app.test_request_context():
        return [query_one("SELECT player_id, uuid, nickname FROM players WHERE uuid = %s", (u,)) for u, _ in (A, B)]


def setup(conn, mongo_db):
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


def seed_types(app, uuid, season=None):
    from app.db_sql import query_one
    from app.seeds import player_seed_types
    with app.test_request_context():
        pid = query_one("SELECT player_id FROM players WHERE uuid = %s", (uuid,))["player_id"]
        return player_seed_types(uuid, pid, season)


def seeds_setup(conn, mongo_db):
    seed_season(conn)
    for m in [
        # Village + Bridge: 400 s, 500 s, 900 s -> average 600 s (10:00), median 500 s (08:20)
        raw_match(1, [A, B], A[0], overworld="VILLAGE", nether="BRIDGE", time_ms=400000),
        raw_match(2, [A, B], B[0], overworld="VILLAGE", nether="BRIDGE", time_ms=500000),
        raw_match(3, [A, B], A[0], overworld="VILLAGE", nether="BRIDGE", time_ms=900000),
        # Shipwreck + Bridge: one completed win at 300 s; the 100 s forfeit win must not count
        raw_match(4, [A, B], A[0], overworld="SHIPWRECK", nether="BRIDGE", time_ms=300000),
        raw_match(5, [A, B], A[0], overworld="SHIPWRECK", nether="BRIDGE", forfeited=True, time_ms=100000),
        # Village + Stables: 700 s
        raw_match(6, [A, B], B[0], overworld="VILLAGE", nether="STABLES", time_ms=700000),
        # private match: excluded from the ranked view
        raw_match(7, [A, B], A[0], overworld="DESERT_TEMPLE", nether="HOUSING", match_type=3, time_ms=200000),
    ]:
        sync.store_match(conn, mongo_db, m)


def combos(app, **filters):
    from app.db_mongo import get_mongo
    from app.seeds import build_match, combinations
    f = {"type": 2, "overworld": None, "nether": None, "variation": None} | filters
    with app.test_request_context():
        return combinations(get_mongo()["matches"], build_match(f))


def elo_history_setup(conn, mongo_db, n=25):
    """alpha's Elo: 1500 before each match, +10 on a win, -10 on a loss; wins on odd ids."""
    seed_season(conn)
    for i in range(1, n + 1):
        sync.store_match(conn, mongo_db, raw_match(i, [A, B], A[0] if i % 2 else B[0], date=1790000000 + i * 100,
                                                   elo=1500 + i, change=10))
    sync.store_match(conn, mongo_db, raw_match(90, [A, B], A[0], match_type=3))           # private: no points
    sync.store_match(conn, mongo_db, raw_match(91, [A], None, decayed=True, change=-5))  # decay: no points


def test_rate_rules():
    """The rules every view is checked against: win rate = wins / decisive games (draws left out), forfeit rate
    over all games, placement matches (no Elo change) still count as games, 2-dp half-up rounding after a 4-dp
    intermediate step like MariaDB, and no games gives no rate rather than 0 %."""
    s = summarise(rows(wins=3, losses=1, forfeits=1))
    assert (s["games_played"], s["win_rate"], s["forfeit_rate"]) == (4, Decimal("75.00"), Decimal("25.00"))
    s = summarise(rows(wins=1, losses=2, draws=1))
    assert (s["games_played"], s["draws"], s["win_rate"]) == (4, 1, Decimal("33.33"))
    assert summarise(rows(draws=2))["win_rate"] is None        # only draws: no decisive games
    s = summarise([])
    assert s["win_rate"] is None and s["forfeit_rate"] is None and s["total_elo_change"] is None
    s = summarise([{"outcome": "win", "forfeited": 0, "elo_change": None},
                         {"outcome": "win", "forfeited": 0, "elo_change": 5}])
    assert s["games_played"] == 2 and s["total_elo_change"] == 5
    assert stats.rate(2, 3) == Decimal("66.67") and stats.rate(1, 32) == Decimal("3.13")   # half up
    assert stats.rate(2, 101) == Decimal("1.98")      # 1.980198 -> 1.9802 -> 1.98


def test_recent_form_buckets_match_python(app, conn, mongo_db):
    seed_season(conn)
    store(conn, mongo_db, ranked_series(45))
    from app.compare import recent_form
    with app.app_context():
        from app.db_sql import query_one
        pid = query_one("SELECT player_id FROM players WHERE uuid = %s", (A[0],))["player_id"]
        total, buckets, strip = recent_form(pid)
    assert total == 45
    assert len(strip) == 40 and strip[0]["played_at"] <= strip[-1]["played_at"]  # oldest first
    with conn.cursor() as cur:
        cur.execute("SELECT outcome, forfeited, elo_change FROM v_ranked_match_players WHERE player_id = %s "
                    "ORDER BY played_at DESC, match_id DESC", (pid,))
        rows = cur.fetchall()
    for name, chunk in (("latest", rows[:20]), ("previous", rows[20:40])):
        expected = summarise(chunk)
        assert buckets[name]["win_rate"] == expected["win_rate"]
        assert buckets[name]["forfeit_rate"] == expected["forfeit_rate"]
        assert buckets[name]["draws"] == expected["draws"]
        assert buckets[name]["avg_elo"] == expected["avg_elo_change"]


def test_view_equals_stats_py(conn, mongo_db):
    """SQL view rates == app.stats for the same fixture data, including awkward denominators."""
    seed_season(conn)
    rng = random.Random(7)
    matches = []
    for i in range(101):  # 101 games: exercises the 4-dp intermediate rounding
        kind = rng.choice(["win", "loss", "ff", "draw", "ffwin"])
        winner = {"win": A[0], "ffwin": A[0], "loss": B[0], "ff": B[0], "draw": None}[kind]
        matches.append(raw_match(5000 + i, [A, B], winner, forfeited=kind in ("ff", "ffwin", "draw"),
                                 change=rng.choice([None, 5, 12]), date=1790000000 + i))
    store(conn, mongo_db, matches)
    with conn.cursor() as cur:
        cur.execute("SELECT player_id FROM players WHERE uuid IN (%s, %s)", (A[0], B[0]))
        for pid in [r["player_id"] for r in cur.fetchall()]:
            cur.execute("SELECT * FROM v_player_stats_lifetime WHERE player_id = %s", (pid,))
            view = cur.fetchone()
            cur.execute("SELECT outcome, forfeited, elo_change FROM v_ranked_match_players WHERE player_id = %s", (pid,))
            py = summarise(cur.fetchall())
            for key in ("games_played", "wins", "losses", "draws", "forfeits", "win_rate", "forfeit_rate"):
                assert Decimal(view[key]) == Decimal(py[key]), key
            assert view["total_elo_change"] == py["total_elo_change"]


def test_stats_exclude_non_ranked_and_decay(conn, mongo_db):
    seed_season(conn)
    store(conn, mongo_db, [
        raw_match(1, [A, B], A[0]),
        raw_match(2, [A, B], A[0], match_type=3),            # private: not counted
        raw_match(3, [A], None, decayed=True, change=-5),   # decay: not counted
    ])
    with conn.cursor() as cur:
        cur.execute("SELECT s.games_played, s.wins FROM v_player_stats_lifetime s JOIN players p "
                    "ON p.player_id = s.player_id WHERE p.uuid = %s", (A[0],))
        assert cur.fetchone() == {"games_played": 1, "wins": 1}


def test_compare_times_use_the_timed_win_view(app, conn, mongo_db):
    """Compare's best and average time come from v_completed_wins, so they agree with Fastest Wins: forfeit wins,
    draws, losses and decays never count, however fast."""
    seed_season(conn)
    store(conn, mongo_db, [
        raw_match(1, [A, B], A[0], time_ms=400000), raw_match(2, [A, B], A[0], time_ms=500000),
        raw_match(3, [A, B], A[0], forfeited=True, time_ms=100000),     # forfeit win: not timed
        raw_match(4, [A, B], B[0], time_ms=50000),                       # a loss: not alpha's time
        raw_match(5, [A], None, decayed=True),                           # decay
        raw_match(6, [A, B], None, forfeited=True, time_ms=30000)])      # draw
    from app.compare import filtered_stats
    with app.app_context():
        from app.db_sql import query_one
        pid = query_one("SELECT player_id FROM players WHERE uuid = %s", (A[0],))["player_id"]
        s = filtered_stats(pid, None, None, None)
        fastest = query_one("SELECT finish_time_ms FROM v_fastest_wins_lifetime WHERE player_id = %s", (pid,))
    assert s["best_time"] == fastest["finish_time_ms"] == 400000 and s["avg_time"] == 450000
    assert (s["games_played"], s["wins"], s["losses"]) == (5, 3, 1)   # the join changes no count


def test_history_filters(client, conn, mongo_db):
    seed_season(conn)
    seed_season(conn, 11)
    store(conn, mongo_db, [
        raw_match(1, [A, B], A[0], date=1767225600),                     # 2026-01-01
        raw_match(2, [A, B], B[0], forfeited=True, date=1767312000),     # alpha forfeits
        raw_match(3, [A, C], None, forfeited=True, season=11, date=1767398400),
    ])
    get = lambda q: client.get("/matches?player=alpha" + q).get_data(as_text=True)
    assert get("&result=win").count("/matches/1\"") == 1 and "/matches/2\"" not in get("&result=win")
    assert "/matches/2\"" in get("&result=forfeit") and "/matches/1\"" not in get("&result=forfeit")
    assert "/matches/3\"" in get("&result=draw")
    assert "/matches/3\"" in get("&season=11") and "/matches/1\"" not in get("&season=11")
    assert "/matches/1\"" in get("&from=2026-01-01&to=2026-01-01") and "/matches/2\"" not in get("&from=2026-01-01&to=2026-01-01")


def test_endgame_summary_counts_zero_cycles(app, mongo_db):
    from datetime import datetime, timezone
    from app import endgame
    from app.importer.transform import mongo_detail_doc
    now = datetime.now(timezone.utc)
    cut = endgame.ZERO_CYCLE_MAX_MS
    docs = [
        _detail(1, [(A[0], 0, "story.enter_the_end"), (A[0], cut - 1000, "end.kill_dragon")]),        # zero
        _detail(2, [(A[0], 0, "story.enter_the_end"), (A[0], cut + 30000, "end.kill_dragon")]),       # longer
        _detail(3, [(A[0], 0, "story.enter_the_end")]),                                                # no kill
        _detail(4, [(A[0], 0, "story.enter_the_end"), (A[0], 1000, "end.kill_dragon")], match_type=3),  # private
    ]
    for d in docs:
        mongo_db["match_details"].insert_one(mongo_detail_doc(d, now))
    with app.app_context():
        s = endgame.summary(A[0])
        cov = endgame.coverage([1, 2, 3, 4, 99])
    assert (s["entries"], s["kills"], s["zero"]) == (3, 2, 1)   # private match ignored
    assert s["zero_rate"] == 50.0 and s["best_zero"] == cut - 1000
    assert sum(s["histogram"]["zero"]) == 1 and sum(s["histogram"]["other"]) == 1
    # a true median: with an even count, the mean of the middle two (PERCENTILE_CONT(0.5), as on Seeds)
    assert s["median_split"] == ((cut - 1000) + (cut + 30000)) / 2

    assert cov["have"] == 4 and cov["total"] == 5 and cov["seasons"] == [12]
    assert endgame.season_label([11, 12]) == "seasons 11–12" and endgame.season_label([]) == "no seasons"


NETHER, BASTION = "story.enter_the_nether", "nether.find_bastion"


def split_details(mongo_db):
    """Three ranked A-vs-B timelines with hand-picked times (odd counts, so each median is the middle value), one
    in season 11, plus a casual match that must be ignored."""
    from datetime import datetime, timezone
    from app.importer.transform import mongo_detail_doc
    docs = [
        _detail(1, [(A[0], 100, NETHER), (B[0], 110, NETHER), (A[0], 200, BASTION), (B[0], 230, BASTION)],
                completions=[(A[0], 500)]),
        _detail(2, [(A[0], 120, NETHER), (B[0], 100, NETHER), (A[0], 300, NETHER),     # re-entry: first one counts
                    (A[0], 260, BASTION), (B[0], 220, BASTION)], completions=[(B[0], 480)]),
        dict(_detail(3, [(A[0], 90, NETHER), (B[0], 95, NETHER), (A[0], 240, BASTION), (B[0], 250, BASTION)],
                     completions=[(A[0], 450)]), season=11),
        _detail(4, [(A[0], 1, NETHER), (B[0], 999, NETHER)], match_type=1),            # casual: ignored
    ]
    now = datetime.now(timezone.utc)
    for d in docs:
        mongo_db["match_details"].insert_one(mongo_detail_doc(d, now))


def test_split_analysis_medians_and_paired_gaps(app, mongo_db):
    from app import splits
    split_details(mongo_db)
    with app.app_context():
        out = splits.analyse(A[0])
        s11 = splits.analyse(A[0], season=11)
        view = splits.profile_view(A[0], None)
    assert out["matches"] == 3 and out["seasons"] == [11, 12]
    rows = {r["key"]: r for r in out["rows"]}
    assert [r["label"] for r in out["rows"]] == ["Enter the Nether", "Find bastion", "Finish"]   # run order, only reached
    nether, bastion, finish = rows[NETHER], rows[BASTION], rows[splits.FINISH]
    # A: 100, 120, 90 -> 100; B: 110, 100, 95 -> 100; paired A - B: -10, 20, -5 -> -5
    assert (nether["reached"], nether["median"], nether["opp_median"], nether["paired"], nether["gap"]) ==         (3, 100, 100, 3, -5)
    assert (nether["q1"], nether["q3"]) == (90, 120)
    # A: 200, 260, 240 -> 240; B: 230, 220, 250 -> 230; paired: -30, 40, -10 -> -10; the gap grew by -5
    assert (bastion["median"], bastion["opp_median"], bastion["gap"], bastion["gap_change"]) == (240, 230, -10, -5)
    # only one player finishes a match, so finish times are never paired
    assert (finish["reached"], finish["opp_reached"], finish["opp_median"], finish["paired"], finish["gap"]) ==         (2, 1, 480, 0, None)
    assert finish["reached_pct"] == 66.7
    # season filter: match 3 only
    assert s11["matches"] == 1 and {r["key"]: r["gap"] for r in s11["rows"]} == {NETHER: -5, BASTION: -10,
                                                                               splits.FINISH: None}
    assert view["best"]["key"] == BASTION and view["worst"] is None    # A only gains time between milestones
    assert view["seasons_label"] == "seasons 11–12"


def test_head_to_head_summary_streaks_and_lead(app, conn, mongo_db):
    from datetime import datetime, timezone
    from app import rivalry
    setup(conn, mongo_db)
    a, b = ids(app)
    with app.test_request_context():
        h = rivalry.head_to_head(a, b)
    s = h["summary"]
    assert (s["games"], s["a_wins"], s["b_wins"], s["draws"]) == (8, 3, 4, 1)
    assert str(s["a_rate"]) == "42.86" and str(s["b_rate"]) == "57.14"   # 3 and 4 of 7 decisive meetings
    assert (s["a_elo"], s["b_elo"]) == (-10, 10)
    assert (s["a_avg_ms"], s["a_best_ms"]) == (500000, 400000)
    assert s["b_avg_ms"] == 500000          # the forfeit win (match 7) has no finish time
    st = h["streaks"]
    assert (st["a_longest"]["length"], st["b_longest"]["length"]) == (2, 3)
    assert (st["current"]["who"], st["current"]["label"]) == ("alpha", "1 win")
    assert [p["y"] for p in h["chart"]["series"][0]["data"]] == [1, 2, 1, 1, 0, -1, -2, -1]
    assert [m["match_id"] for m in h["meetings"]] == [8, 7, 6, 5, 4, 3, 2, 1]   # newest first, no private
    seeds = {r["type"]: (r["games"], r["a_wins"], r["b_wins"]) for r in h["seeds"]["overworld"]}
    assert seeds == {"VILLAGE": (6, 1, 4), "SHIPWRECK": (2, 2, 0)}
    with app.test_request_context():   # filters narrow the meetings
        assert rivalry.head_to_head(a, b, season=1) is None
        lo = datetime.fromtimestamp(1790000000 + 450, timezone.utc).replace(tzinfo=None)   # meetings 5-8 only
        h = rivalry.head_to_head(a, b, lo=lo)
    assert (h["summary"]["games"], h["summary"]["a_wins"], h["summary"]["b_wins"]) == (4, 1, 3)


def test_ranking_average_median_and_forfeits(app, conn, mongo_db):
    import pymysql
    from app.db_mongo import get_mongo
    from app.db_sql import get_db
    from app.seeds import build_match, combinations
    seeds_setup(conn, mongo_db)
    rows, overall = combos(app)
    order = [(r["overworld"], r["nether"]) for r in rows]
    assert order == [("SHIPWRECK", "BRIDGE"), ("VILLAGE", "BRIDGE"), ("VILLAGE", "STABLES")]
    vb = rows[1]
    assert (vb["wins"], vb["avg_ms"], vb["median_ms"], vb["best_ms"]) == (3, 600000.0, 500000.0, 400000)
    sb = rows[0]
    assert sb["wins"] == 1 and sb["best_ms"] == 300000 and sb["matches"] == 2   # forfeit counted as a match, not a win
    # overall: 300, 400, 500, 700, 900 -> average 560 s, median 500 s; deltas relative to it
    assert overall == {"wins": 5, "avg_ms": 560000.0, "median_ms": 500000.0}
    assert vb["delta_s"] == 40.0 and sb["delta_s"] == -260.0
    assert all(r["small"] for r in rows)          # fewer than MIN_WINS wins each
    rows, _ = combos(app, type=None)               # all types: the private match appears
    assert ("DESERT_TEMPLE", "HOUSING") in [(r["overworld"], r["nether"]) for r in rows]
    rows, _ = combos(app, overworld="VILLAGE")
    assert {(r["overworld"], r["nether"]) for r in rows} == {("VILLAGE", "BRIDGE"), ("VILLAGE", "STABLES")}    # the cross-store join's TEMPORARY table is dropped; it is per-session, so check the request's own connection
    with app.test_request_context():
        combinations(get_mongo()["matches"], build_match({"type": 2, "overworld": None, "nether": None, "variation": None}))
        with get_db().cursor() as cur, pytest.raises(pymysql.err.ProgrammingError):
            cur.execute("SELECT COUNT(*) FROM tmp_seed_combo")


def test_player_seed_types(app, conn, mongo_db, monkeypatch):
    seeds_setup(conn, mongo_db)
    r = seed_types(app, A[0])
    # alpha's ranked games: Village 4 (2 wins: 400 s, 900 s), Shipwreck 2 (2 wins, one a forfeit win);
    # Bridge 5 (4 wins), Stables 1 (0 wins). The private match (Desert Temple + Housing) is excluded.
    assert (r["games"], str(r["win_rate"]), r["avg_ms"]) == (6, "66.67", 533333.3333333334)
    ow = {t["type"]: t for t in r["overworld"]["types"]}
    assert set(ow) == {"VILLAGE", "SHIPWRECK"}
    v, s = ow["VILLAGE"], ow["SHIPWRECK"]
    assert (v["games"], v["wins"], str(v["win_rate"]), v["avg_ms"], v["best_ms"]) == (4, 2, "50.00", 650000.0, 400000)
    assert (s["games"], s["wins"], s["timed_wins"], s["avg_ms"]) == (2, 2, 1, 300000.0)  # forfeit win: no time
    assert round(s["delta_pp"], 2) == 33.33
    nether = {t["type"]: (t["games"], t["wins"]) for t in r["nether"]["types"]}
    assert nether == {"BRIDGE": (5, 4), "STABLES": (1, 0)}
    assert r["overworld"]["best"] is None                 # every type is below MIN_PLAYER_GAMES
    monkeypatch.setattr("app.seeds.MIN_PLAYER_GAMES", 1)
    r = seed_types(app, A[0])
    assert r["overworld"]["best"]["type"] == "SHIPWRECK"  # highest win rate, not fastest average
    assert r["nether"]["best"]["type"] == "BRIDGE"
    assert seed_types(app, B[0])["overworld"]["best"]["type"] == "VILLAGE"   # bravo: 2 of 4 vs 0 of 2


def test_moving_average_peak_and_summary(app, conn, mongo_db):
    from app import elo
    elo_history_setup(conn, mongo_db)
    with app.test_request_context():
        h = elo.history(A[0])
    series = {s["name"]: s["data"] for s in h["chart"]["series"]}
    assert len(series["Elo"]) == 25
    avg = series["20-match average"]
    assert len(avg) == 6                                         # matches 20..25 have a full window
    # window of matches 1..20: elo = 1500 + i + (10 if odd else -10); mean of i is 10.5 and the +/-10 cancel
    assert avg[0]["y"] == 1510.5
    s = h["summary"]
    assert s["matches"] == 25 and s["peak"]["elo"] == 1535      # match 25 (odd): 1525 + 10
    assert (s["gain"]["change"], s["loss"]["change"]) == (10, -10)
    assert s["drop"]["by"] == -19 and s["drop"]["from"] == 1511  # first time: 1492 (match 2) below 1511 (match 1)


def test_windows_reset_each_season(app, conn, mongo_db):
    """A season reset (high Elo in season 11, low in 12) is not a slump, and the average restarts."""
    from app import elo
    seed_season(conn, 11)
    seed_season(conn, 12)
    for i in range(1, 21):
        sync.store_match(conn, mongo_db, raw_match(i, [A, B], A[0], season=11, date=1780000000 + i, elo=2000, change=5))
    for i in range(21, 41):
        sync.store_match(conn, mongo_db, raw_match(i, [A, B], A[0], season=12, date=1790000000 + i, elo=1200, change=5))
    with app.test_request_context():
        h = elo.history(A[0])
    assert h["summary"]["drop"]["by"] == 0                       # 2005 -> 1205 crosses a season boundary
    avg = h["chart"]["series"][1]["data"]
    assert [p["y"] for p in avg] == [2005.0, None, 1205.0]       # one full window per season, line broken between


def test_elo_history_follows_matches(conn, mongo_db):
    """The Elo time series is a derived copy: one point per player per ranked match, unchanged by re-storing,
    replaced when a match's Elo is corrected, and identical when rebuilt from MariaDB."""
    elo_history_setup(conn, mongo_db, n=3)
    pts = sorted(mongo_db["elo_history"].find({"player": A[0]}, {"_id": 0}), key=lambda p: p["match_id"])
    assert [(p["match_id"], p["elo"], p["change"]) for p in pts] == [(1, 1511, 10), (2, 1492, -10), (3, 1513, 10)]
    assert mongo_db["elo_history"].count_documents({}) == 6                      # both players, ranked only
    # re-storing an unchanged match adds nothing; rebuild from MariaDB gives the same points
    sync.store_match(conn, mongo_db, raw_match(1, [A, B], A[0], date=1790000100, elo=1501, change=10))
    assert mongo_db["elo_history"].count_documents({}) == 6
    assert elo_history.rebuild(conn, mongo_db) == 6
    again = sorted(mongo_db["elo_history"].find({"player": A[0]}, {"_id": 0}), key=lambda p: p["match_id"])
    assert [(p["match_id"], p["elo"]) for p in again] == [(p["match_id"], p["elo"]) for p in pts]
    # an Elo correction changes only match_players rows; the match still counts as updated and its point is replaced
    out = sync.store_match(conn, mongo_db, raw_match(1, [A, B], A[0], date=1790000100, elo=1501, change=12))
    pts = list(mongo_db["elo_history"].find({"match_id": 1, "player": A[0]}))
    assert out == "updated" and len(pts) == 1 and (pts[0]["change"], pts[0]["elo"]) == (12, 1513)
