"""Data for the landing page: stored data, plus the top 5 of the official leaderboard (app.leaderboard)."""
from app import cache, rivalry, seeds, summary
from app.db_mongo import get_mongo, optional
from app.db_sql import query_all, query_one
from app.leaderboard import official


def landing_data():
    counts = query_one(
        "/* Dataset size */ SELECT (SELECT COUNT(*) FROM matches) AS matches, "
        "(SELECT COUNT(*) FROM players) AS players, "
        "(SELECT MAX(played_at) FROM matches) AS latest_match")
    board = official()   # None if the API can't be reached
    top = board["rows"][:5] if board else None
    recent = query_all(
        "SELECT player_id, uuid, nickname, current_elo, country FROM players "
        "WHERE profile_status = 'full' ORDER BY profile_fetched_at DESC LIMIT 6")
    summary.with_win_rates(recent)
    fastest = fastest_players(1)
    return {"counts": counts, "top": top, "recent": recent, "record": fastest[0] if fastest else None,
            "suggestions": query_all("SELECT uuid, nickname FROM players WHERE profile_status = 'full' "
                                     "ORDER BY current_elo IS NULL, current_elo DESC LIMIT 4"),
            "spotlight": spotlight()}


def spotlight():
    """Real examples for the home page's question cards: the top player's main rivalry and the fastest seed
    combination. Cached until the next import (app.cache): they change slowly and cost ~150 ms to build."""
    return cache.cached("home-spotlight", _spotlight)


def _spotlight():
    data = {"rivalry": None, "combo": None, "form_player": None}
    best = query_one("SELECT player_id, uuid, nickname FROM players WHERE profile_status = 'full' "
                     "ORDER BY current_elo IS NULL, current_elo DESC LIMIT 1")
    if best:
        data["form_player"] = best
        rivals = optional(rivalry.top_rivals, best["uuid"], limit=1, what="home rivalry") or []
        opponent = rivals and query_one("SELECT player_id, uuid, nickname FROM players WHERE uuid = %s",
                                        (rivals[0]["uuid"],))
        if opponent:
            s = rivalry.summary(best["player_id"], opponent["player_id"])
            if s["games"]:
                data["rivalry"] = {"a": best, "b": opponent, "s": s}
    combos = optional(lambda: seeds.combinations(get_mongo()["matches"], seeds.build_match(
        {"type": 2, "overworld": None, "nether": None, "variation": None}))[0], what="home seeds") or []
    ranked = [c for c in combos if c["avg_ms"] is not None and not c["small"]]
    if ranked:
        data["combo"] = ranked[0]
    return data


CANDIDATES = 500


def fastest_players(n):
    """Top n players by their best completed ranked win.

    Walks wins in finish-time order through ix_mp_finish and keeps each player's first (= best) row,
    instead of evaluating v_fastest_wins_lifetime for every player. Falls back to that view when the first
    CANDIDATES wins don't contain n different players, so the result is always exact.

    Deliberately NOT written against v_completed_wins: inside this nested query the optimiser
    then scans all matches (37 ms) instead of the index (2-3 ms), and an index hint can't be put on a view.
    The filter below must stay identical to v_completed_wins + match_type = 2 (a test checks the results).
    """
    rows = query_all(
        "/* Fastest stored win: walk ix_mp_finish, best row per player */ "
        "SELECT match_id, finish_time_ms, season_id, uuid, nickname FROM ("
        "  SELECT c.*, ROW_NUMBER() OVER (PARTITION BY c.player_id ORDER BY c.finish_time_ms, c.match_id) AS rn"
        "  FROM (SELECT mp.player_id, mp.match_id, mp.finish_time_ms, m.season_id, p.uuid, p.nickname"
        "        FROM match_players mp FORCE INDEX (ix_mp_finish)"
        "        JOIN matches m ON m.match_id = mp.match_id JOIN players p ON p.player_id = mp.player_id"
        "        WHERE mp.finish_time_ms IS NOT NULL AND mp.outcome = 'win' AND m.result_type = 'completed'"
        "          AND m.match_type = 2 AND m.is_decayed = 0"
        "        ORDER BY mp.finish_time_ms, mp.match_id LIMIT %s) c"
        ") best WHERE rn = 1 ORDER BY finish_time_ms, match_id LIMIT %s", (CANDIDATES, n))
    if len(rows) < n:
        rows = query_all(
            "SELECT f.match_id, f.finish_time_ms, f.season_id, p.uuid, p.nickname FROM v_fastest_wins_lifetime f "
            "JOIN players p ON p.player_id = f.player_id ORDER BY f.finish_time_ms, f.match_id LIMIT %s", (n,))
    return rows
