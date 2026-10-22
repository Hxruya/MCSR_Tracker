"""Summary reads: player statistics (materialised tables with live-view fallback) and match
summaries for lists (v_match_summary).

A player's mv_player_stats_lifetime row is the "refreshed" marker: if it exists, both summary tables are
current for that player (refresh_player_stats() rewrites both together); otherwise the views are used.
"""
from app.db_sql import query_all, query_one

COLS = "player_id, games_played, wins, losses, draws, forfeits, win_rate, forfeit_rate, total_elo_change"


def lifetime(player_ids):
    """{player_id: stats row} for the given players."""
    ids = list(dict.fromkeys(player_ids))
    if not ids:
        return {}
    marks = ", ".join(["%s"] * len(ids))
    out = {r["player_id"]: r for r in query_all(
        f"/* Stats from the summary table */ SELECT {COLS} FROM mv_player_stats_lifetime "
        f"WHERE player_id IN ({marks})", ids)}
    missing = [i for i in ids if i not in out]
    if missing:
        marks = ", ".join(["%s"] * len(missing))
        out.update({r["player_id"]: r for r in query_all(
            f"SELECT {COLS} FROM v_player_stats_lifetime WHERE player_id IN ({marks})", missing)})
    return out


def with_win_rates(rows):
    """Add games_played and win_rate (lifetime, ranked) to player rows that have a player_id."""
    stats = lifetime(r["player_id"] for r in rows)
    for r in rows:
        s = stats.get(r["player_id"]) or {}
        r["games_played"], r["win_rate"] = s.get("games_played", 0), s.get("win_rate")
    return rows


def refreshed(player_id):
    return query_one("SELECT 1 AS ok FROM mv_player_stats_lifetime WHERE player_id = %s", (player_id,)) is not None


def season(player_id, season_id):
    source = "mv_player_stats_season" if refreshed(player_id) else "v_player_stats_season"
    return query_one(f"SELECT {COLS}, season_id FROM {source} WHERE player_id = %s AND season_id = %s",
                     (player_id, season_id))


def seasons(player_id):
    source = "mv_player_stats_season" if refreshed(player_id) else "v_player_stats_season"
    return query_all(f"SELECT {COLS}, season_id FROM {source} WHERE player_id = %s ORDER BY season_id", (player_id,))


def player_matches(player_id, where=(), params=(), limit=20, offset=0):
    """One page of a player's matches, newest first, with the opponent and player count. `where` filters on
    `mp` (match_players) and `m` (matches). Deferred join: pick the page's match ids first, then run the per-row
    subqueries for those rows only (42 ms -> 6 ms for edcr's ~8,000 matches)."""
    cond = " AND ".join(["mp.player_id = %s", *where])
    return query_all(
        "/* A player's matches (deferred join) */ "
        "SELECT m2.match_id, m2.played_at, m2.season_id, m2.match_type, m2.result_type, m2.duration_ms, "
        "m2.is_decayed, mp2.outcome, mp2.forfeited, mp2.elo_before, mp2.elo_change, mp2.finish_time_ms, "
        "(SELECT COUNT(*) FROM match_players x WHERE x.match_id = m2.match_id) AS n_players, "
        "(SELECT p2.nickname FROM match_players x JOIN players p2 ON p2.player_id = x.player_id "
        " WHERE x.match_id = m2.match_id AND x.player_id <> mp2.player_id ORDER BY p2.nickname LIMIT 1) AS opponent "
        "FROM (SELECT m.match_id FROM match_players mp JOIN matches m ON m.match_id = mp.match_id "
        f"      WHERE {cond} ORDER BY m.played_at DESC, m.match_id DESC LIMIT %s OFFSET %s) page "
        "JOIN matches m2 ON m2.match_id = page.match_id "
        "JOIN match_players mp2 ON mp2.match_id = m2.match_id AND mp2.player_id = %s "
        "ORDER BY m2.played_at DESC, m2.match_id DESC",
        [player_id, *params, limit, offset, player_id])


def matches_by_id(match_ids):
    """{match_id: row} from v_match_summary, with `names` as a list (winner first) and a
    ready-made `players` label like "alpha vs bravo +3". Always called with an id list: the view is merged into
    this query (ALGORITHM = MERGE), so only those matches are read."""
    ids = list(dict.fromkeys(match_ids))
    if not ids:
        return {}
    marks = ", ".join(["%s"] * len(ids))
    out = {}
    for r in query_all(f"/* Match summaries (view) */ SELECT * FROM v_match_summary WHERE match_id IN ({marks})", ids):
        names = (r["names"] or "").split("\t")
        label = " vs ".join(names[:2])
        if r["n_players"] > 2:
            label += f" +{r['n_players'] - 2}"
        out[r["match_id"]] = dict(r, names=names, players=label)
    return out
