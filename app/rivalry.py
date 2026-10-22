"""Rivalries: the head-to-head record of two players on the Compare page, and each player's
most-played opponents on their profile. Ranked matches only, decay excluded.

Head to head is SQL (outcomes, Elo and times live in match_players); seeds for the meetings come from MongoDB.
Top rivals is one MongoDB aggregation over `matches`, which already lists every participant per match.
"""
from app import stats, util
from app.db_mongo import get_mongo, optional
from app.db_sql import query_all, query_one

# Every meeting: A's row (ra) joined to B's row (rb) of the same ranked match. Timed wins come from
# v_completed_wins, the single definition of "a completed win".
MEETINGS = """
FROM v_ranked_match_players ra
JOIN v_ranked_match_players rb ON rb.match_id = ra.match_id AND rb.player_id = %s
LEFT JOIN v_completed_wins wa ON wa.match_id = ra.match_id AND wa.player_id = ra.player_id
LEFT JOIN v_completed_wins wb ON wb.match_id = rb.match_id AND wb.player_id = rb.player_id
WHERE {where}
"""

SUMMARY_SQL = """
/* Head to head: totals over the shared matches (self-join) */
SELECT COUNT(*) AS games, COALESCE(SUM(ra.outcome = 'win'), 0) AS a_wins, COALESCE(SUM(rb.outcome = 'win'), 0) AS b_wins,
       COALESCE(SUM(ra.outcome = 'draw'), 0) AS draws,
       COALESCE(SUM(ra.forfeited = 1), 0) AS a_forfeits, COALESCE(SUM(rb.forfeited = 1), 0) AS b_forfeits,
       SUM(ra.elo_change) AS a_elo, SUM(rb.elo_change) AS b_elo,
       AVG(wa.finish_time_ms) AS a_avg_ms, MIN(wa.finish_time_ms) AS a_best_ms,
       AVG(wb.finish_time_ms) AS b_avg_ms, MIN(wb.finish_time_ms) AS b_best_ms
""" + MEETINGS

# Oldest first, with A's running lead (A wins - B wins) as a window over the same ordering.
TIMELINE_SQL = """
/* Running score: SUM() OVER a named window */
SELECT ra.match_id, ra.played_at, ra.season_id, ra.result_type, ra.outcome AS a_outcome,
       ra.forfeited AS a_forfeited, rb.forfeited AS b_forfeited, ra.elo_change AS a_elo, rb.elo_change AS b_elo,
       COALESCE(wa.finish_time_ms, wb.finish_time_ms) AS win_ms,
       SUM(ra.outcome = 'win') OVER w - SUM(rb.outcome = 'win') OVER w AS a_lead
""" + MEETINGS + """
WINDOW w AS (ORDER BY ra.played_at, ra.match_id)
ORDER BY ra.played_at, ra.match_id
"""

# Streaks by gaps-and-islands: within one outcome, consecutive meetings share (overall position - position
# within that outcome), so each island is one unbroken run. A draw ends both players' runs.
STREAK_SQL = """
/* Streaks: gaps and islands with ROW_NUMBER() */
SELECT outcome, COUNT(*) AS length, MIN(played_at) AS started, MAX(played_at) AS ended, MAX(seq) AS last_seq
FROM (
    SELECT ra.outcome, ra.played_at,
           ROW_NUMBER() OVER (ORDER BY ra.played_at, ra.match_id) AS seq,
           ROW_NUMBER() OVER (ORDER BY ra.played_at, ra.match_id)
             - ROW_NUMBER() OVER (PARTITION BY ra.outcome ORDER BY ra.played_at, ra.match_id) AS island
""" + MEETINGS + """
) runs
GROUP BY outcome, island
"""


def _where(a_id, b_id, season, lo, hi):
    where, params = util.sql_filters(season, lo, hi, prefix="ra.")
    # b_id fills the JOIN placeholder in MEETINGS, then A's id, then the filters
    return " AND ".join(["ra.player_id = %s"] + where), [b_id, a_id] + params


def summary(a_id, b_id, season=None, lo=None, hi=None):
    where, params = _where(a_id, b_id, season, lo, hi)
    row = query_one(SUMMARY_SQL.format(where=where), params)
    row = {k: (int(v) if k in ("a_wins", "b_wins", "draws", "a_forfeits", "b_forfeits") else v) for k, v in row.items()}
    row["a_rate"] = stats.win_rate(row["a_wins"], row["games"], row["draws"])
    row["b_rate"] = stats.win_rate(row["b_wins"], row["games"], row["draws"])
    return row


def streaks(where, params, games, a, b):
    """Longest run of wins for each player and the run the rivalry is currently on.
    A's outcome 'win' is A's run, 'loss' is B's run."""
    def describe(run):
        if run is None:
            return None
        who = {"win": a["nickname"], "loss": b["nickname"]}.get(run["outcome"])
        noun = "win" if who else "draw"
        return dict(run, who=who, label=f"{run['length']} {noun}{'s' if run['length'] != 1 else ''}")

    runs = query_all(STREAK_SQL.format(where=where), params)
    longest = {o: max((r for r in runs if r["outcome"] == o), key=lambda r: r["length"], default=None)
               for o in ("win", "loss")}
    current = next((r for r in runs if r["last_seq"] == games), None)
    return {"a_longest": describe(longest["win"]), "b_longest": describe(longest["loss"]),
            "current": describe(current)}


def seed_split(meetings):
    """Who wins the meetings on each overworld and bastion type (seeds from MongoDB)."""
    winner = {m["match_id"]: m["a_outcome"] for m in meetings}
    docs = get_mongo()["matches"].find({"_id": {"$in": list(winner)}}, {"seed.overworld": 1, "seed.nether": 1},
                                    comment="Seeds of these meetings")
    out = {"overworld": {}, "nether": {}}
    for d in docs:
        for key in out:
            kind = (d.get("seed") or {}).get(key)
            if not kind:
                continue
            row = out[key].setdefault(kind, {"type": kind, "games": 0, "a_wins": 0, "b_wins": 0})
            row["games"] += 1
            row["a_wins"] += winner[d["_id"]] == "win"
            row["b_wins"] += winner[d["_id"]] == "loss"
    return {k: sorted(v.values(), key=lambda r: (-r["games"], r["type"])) for k, v in out.items()}


def head_to_head(a, b, season=None, lo=None, hi=None, page=1):
    """Everything the Compare page shows about two players' meetings, or None if they never met."""
    s = summary(a["player_id"], b["player_id"], season, lo, hi)
    if not s["games"]:
        return None
    where, params = _where(a["player_id"], b["player_id"], season, lo, hi)
    meetings = query_all(TIMELINE_SQL.format(where=where), params)

    by_season = {}
    for m in meetings:
        r = by_season.setdefault(m["season_id"], {"season": m["season_id"], "games": 0, "a_wins": 0, "b_wins": 0})
        r["games"] += 1
        r["a_wins"] += m["a_outcome"] == "win"
        r["b_wins"] += m["a_outcome"] == "loss"
    chart = None
    if len(meetings) >= 2:
        chart = {"kind": "line-time", "format": "int", "series": [{
            "name": f"{a['nickname']}'s lead", "slot": 1,
            "data": [{"x": util.epoch_ms(m["played_at"]), "y": int(m["a_lead"])}
                     for m in meetings]}]}
    newest = meetings[::-1]
    start = (page - 1) * util.PER_PAGE
    return {
        "summary": s, "streaks": streaks(where, params, s["games"], a, b), "chart": chart,
        "seasons": sorted(by_season.values(), key=lambda r: r["season"], reverse=True),
        "seeds": optional(seed_split, meetings, what="head-to-head seeds"), "meetings": newest[start:start + util.PER_PAGE],
        "paging": util.pages(len(meetings), page),
    }


def rivals_pipeline(uuid, season=None, limit=10):
    """Most-played ranked opponents with this player's record against each. Decay matches have a single
    participant, so they drop out after $unwind removes the player themself."""
    match = {"players.uuid": uuid, "match_type": 2}
    if season is not None:
        match["season"] = season
    return [
        {"$match": match},
        # Sorted straight after $match, ix_players_type_played (players.uuid, match_type, played_at desc) supplies
        # the order and no $sort stage runs; with $project in between, MongoDB sorted all 5,604 in memory.
        {"$sort": {"played_at": -1}},                         # so $first picks the latest nickname
        # Drop `raw` (the full API JSON) before $unwind copies each document per participant.
        {"$project": {"players": 1, "result.uuid": 1, "played_at": 1}},
        {"$unwind": "$players"},
        {"$match": {"players.uuid": {"$ne": uuid}}},
        {"$group": {
            "_id": "$players.uuid",
            "nickname": {"$first": "$players.nickname"},
            "games": {"$sum": 1},
            "wins": {"$sum": {"$cond": [{"$eq": ["$result.uuid", uuid]}, 1, 0]}},
            "losses": {"$sum": {"$cond": [{"$eq": ["$result.uuid", "$players.uuid"]}, 1, 0]}},
            "last_met": {"$max": "$played_at"},
        }},
        {"$sort": {"games": -1, "last_met": -1}},
        {"$limit": limit},
    ]


def top_rivals(uuid, season=None, limit=10):
    rows = list(get_mongo()["matches"].aggregate(rivals_pipeline(uuid, season, limit), comment="Top rivals"))
    for r in rows:
        r["uuid"] = r.pop("_id")
        r["draws"] = r["games"] - r["wins"] - r["losses"]
        r["win_rate"] = stats.win_rate(r["wins"], r["games"], r["draws"])
    return rows

