"""Efficiency: every optimisation (summary tables, deferred joins, the index walk, merged views, caching) returns
exactly what the plain query returns.
"""
import random
import re
from decimal import Decimal

from app import stats
from app.importer import sync
from tests.conftest import A, B, C, raw_match, seed_season


def load(conn, mongo_db, n=60):
    seed_season(conn)
    seed_season(conn, 11)
    rng = random.Random(3)
    for i in range(n):
        p, q = rng.sample([A, B, C], 2)
        kind = rng.choice(["win", "loss", "draw", "ff"])
        winner = {"win": p[0], "loss": q[0], "draw": None, "ff": q[0]}[kind]
        sync.store_match(conn, mongo_db, raw_match(100 + i, [p, q], winner, forfeited=kind in ("draw", "ff"),
                                                   season=11 if i % 3 else 12, date=1780000000 + i * 600,
                                                   time_ms=300000 + i * 997, change=rng.choice([None, 7, 15])))


def pid(conn, uuid):
    with conn.cursor() as cur:
        cur.execute("SELECT player_id FROM players WHERE uuid = %s", (uuid,))
        return cur.fetchone()["player_id"]


def test_pct_function_matches_stats_py(conn):
    with conn.cursor() as cur:
        for part, whole in [(0, 0), (1, 3), (2, 3), (2, 101), (1, 32), (7, 8), (50, 50), (13, 4097)]:
            cur.execute("SELECT pct(%s, %s) AS v", (part, whole))
            assert cur.fetchone()["v"] == stats.rate(part, whole), (part, whole)
        for wins, games, draws in [(1, 2, 1), (1, 4, 1), (0, 3, 3), (56, 151, 2), (3, 8, None), (0, 0, 0)]:
            cur.execute("SELECT win_pct(%s, %s, %s) AS v", (wins, games, draws))
            assert cur.fetchone()["v"] == stats.win_rate(wins, games, draws), (wins, games, draws)


def test_summary_tables_equal_views_after_refresh(conn, mongo_db):
    load(conn, mongo_db)
    sync.refresh_stats(conn)
    cols = "player_id, games_played, wins, losses, draws, forfeits, win_rate, forfeit_rate, total_elo_change"
    with conn.cursor() as cur:
        for mv, view, extra in (("mv_player_stats_lifetime", "v_player_stats_lifetime", ""),
                                ("mv_player_stats_season", "v_player_stats_season", ", season_id")):
            for a, b in ((view, mv), (mv, view)):
                cur.execute(f"SELECT COUNT(*) AS n FROM (SELECT {cols}{extra} FROM {a} EXCEPT SELECT {cols}{extra} FROM {b}) d")
                assert cur.fetchone()["n"] == 0, (a, b)
        # refreshing one player leaves the others untouched
        cur.execute("SELECT COUNT(*) AS n FROM mv_player_stats_lifetime")
        before = cur.fetchone()["n"]
    sync.refresh_stats(conn, pid(conn, A[0]))
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM mv_player_stats_lifetime")
        assert cur.fetchone()["n"] == before


def test_pages_use_summary_and_fall_back_to_view(app, client, conn, mongo_db):
    load(conn, mongo_db)
    a = pid(conn, A[0])
    from app import summary
    with app.app_context():  # each context = a new connection, like a request (fresh REPEATABLE READ snapshot)
        assert not summary.refreshed(a)
        live = summary.lifetime([a])[a]                 # no summary row yet -> live view
    sync.refresh_stats(conn)
    with app.app_context():
        assert summary.refreshed(a)
        stored = summary.lifetime([a])[a]
        assert {k: Decimal(str(v)) if v is not None else None for k, v in live.items()} == \
               {k: Decimal(str(v)) if v is not None else None for k, v in stored.items()}
        # a stale summary row is what pages would show until the next refresh: prove pages read the table
        with conn.cursor() as cur:
            cur.execute("UPDATE mv_player_stats_lifetime SET games_played = games_played + 1000, "
                        "wins = wins + 1000 WHERE player_id = %s", (a,))
        conn.commit()
    body = client.get(f"/players/{A[0]}").get_data(as_text=True)
    assert str(int(stored["games_played"]) + 1000) in body


def test_deferred_join_history_matches_naive_query(app, conn, mongo_db):
    load(conn, mongo_db)
    a = pid(conn, A[0])
    naive = ("SELECT m.match_id, mp.outcome, "
             "(SELECT COUNT(*) FROM match_players x WHERE x.match_id = m.match_id) AS n_players, "
             "(SELECT p2.nickname FROM match_players x JOIN players p2 ON p2.player_id = x.player_id "
             " WHERE x.match_id = m.match_id AND x.player_id <> mp.player_id ORDER BY p2.nickname LIMIT 1) AS opponent "
             "FROM match_players mp JOIN matches m ON m.match_id = mp.match_id WHERE mp.player_id = %s "
             "ORDER BY m.played_at DESC, m.match_id DESC LIMIT 20 OFFSET 20")
    with conn.cursor() as cur:
        cur.execute(naive, (a,))
        expected = [(r["match_id"], r["outcome"], r["opponent"]) for r in cur.fetchall()]
    conn.commit()
    client = app.test_client()
    body = client.get("/matches?player=alpha&page=2").get_data(as_text=True)
    shown = [int(x) for x in re.findall(r'class="stretched matchup" href="/matches/(\d+)"', body)]
    assert shown == [e[0] for e in expected]


def test_v_completed_wins_matches_old_filter(conn, mongo_db):
    load(conn, mongo_db)
    sync.store_match(conn, mongo_db, raw_match(999, [A, B], A[0], match_type=3, time_ms=111000))  # private
    old = ("SELECT match_id, player_id, finish_time_ms FROM v_ranked_match_players "
           "WHERE outcome = 'win' AND result_type = 'completed' AND finish_time_ms IS NOT NULL")
    new = "SELECT match_id, player_id, finish_time_ms FROM v_completed_wins WHERE match_type = 2"
    with conn.cursor() as cur:
        for a, b in ((old, new), (new, old)):
            cur.execute(f"SELECT COUNT(*) AS n FROM ({a} EXCEPT {b}) d")
            assert cur.fetchone()["n"] == 0
        cur.execute("SELECT match_type FROM v_completed_wins WHERE match_id = 999")
        assert cur.fetchone()["match_type"] == 3          # non-ranked wins are in the view, flagged by type
        cur.execute("SELECT COUNT(*) AS n FROM v_fastest_wins WHERE match_id = 999")
        assert cur.fetchone()["n"] == 0                   # ...but never in the ranked fastest view


def test_fastest_shortcuts_match_the_views(app, client, conn, mongo_db, monkeypatch):
    """Fastest Wins takes its total from COUNT(*) OVER() on the page it already reads, and the home page keeps its
    own filter for the index hint; both must agree with the plain views."""
    load(conn, mongo_db)
    from app.home import fastest_players
    with app.app_context():
        got = [(r["match_id"], r["finish_time_ms"]) for r in fastest_players(3)]
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM v_fastest_wins_lifetime")
        n = cur.fetchone()["n"]
        cur.execute("SELECT match_id, finish_time_ms FROM v_fastest_wins_lifetime ORDER BY finish_time_ms, match_id LIMIT 3")
        assert got == [(r["match_id"], r["finish_time_ms"]) for r in cur.fetchall()]
    conn.commit()
    from app import util
    pages = util.pages
    monkeypatch.setattr(util, "PER_PAGE", 2)   # more than one page, so the pager shows the total
    monkeypatch.setattr(util, "pages", lambda total, page: pages(total, page, 2))
    assert n > 2 and f"({n} total)" in client.get("/fastest").get_data(as_text=True)


def explain(coll, pipeline):
    """MongoDB's explain output for an aggregation: the stage names in order, and the whole output as text."""
    out = coll.database.command("aggregate", coll.name, pipeline=pipeline, explain=True)
    return [next(iter(s)) for s in out.get("stages", [])], str(out)


def test_profile_pipelines_use_their_indexes(conn, mongo_db):
    """Top rivals reads one player's ranked matches in index order (ix_players_type_played, so no in-memory sort),
    and the split analysis finds the player's timelines through ix_timelines_uuid."""
    from datetime import datetime, timezone

    from app import rivalry, splits
    from app.importer.transform import mongo_detail_doc
    load(conn, mongo_db)
    stages, text = explain(mongo_db["matches"], rivalry.rivals_pipeline(A[0]))
    assert "ix_players_type_played" in text
    assert stages[:2] == ["$cursor", "$unwind"]   # no $sort after the index scan: the index supplied the order
    now = datetime.now(timezone.utc)
    for i in range(12):   # mostly other players' timelines, so the uuid index is the selective one
        who = [A, B] if i < 2 else [B, C]
        mongo_db["match_details"].insert_one(mongo_detail_doc(dict(raw_match(700 + i, who, who[0][0]), timelines=[
            {"uuid": who[0][0], "time": 60000, "type": "story.enter_the_nether"}], completions=[]), now))
    assert "ix_timelines_uuid" in explain(mongo_db["match_details"], splits.pipeline(A[0]))[1]


def test_list_views_are_merged(conn):
    """v_match_summary and v_completed_wins are only fast because MariaDB merges them into the calling query.
    They declare ALGORITHM = MERGE; MariaDB records UNDEFINED instead if an edit makes merging impossible."""
    with conn.cursor() as cur:
        cur.execute("SELECT TABLE_NAME, ALGORITHM FROM information_schema.VIEWS WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME IN ('v_match_summary', 'v_completed_wins') ORDER BY TABLE_NAME")
        assert [(r["TABLE_NAME"], r["ALGORITHM"]) for r in cur.fetchall()] == [
            ("v_completed_wins", "MERGE"), ("v_match_summary", "MERGE")]
    conn.commit()


def test_cache(monkeypatch):
    """A cached value is served until it expires (each entry has its own lifetime: the current leaderboard 30 s, a
    finished season a day) or until the importer clears the cache after storing new matches."""
    from app import cache
    cache.clear()
    now = [1000.0]
    monkeypatch.setattr(cache.time, "monotonic", lambda: now[0])
    calls = []
    compute = lambda: calls.append(1) or len(calls)
    assert cache.cached("short", compute, ttl=30) == 1
    assert cache.cached("short", compute, ttl=30) == 1               # served from the cache
    now[0] += 31
    assert cache.cached("short", compute, ttl=30) == 2               # expired after 30 s
    assert cache.cached("long", compute, ttl=86400) == 3
    now[0] += 3600
    assert cache.cached("long", compute, ttl=86400) == 3             # still cached an hour later
    cache.clear()
    assert cache.cached("long", compute, ttl=86400) == 4             # cleared
    cache.clear()
