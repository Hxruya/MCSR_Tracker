"""Seed analysis: which overworld + bastion combinations are won fastest.

The seed lives in MongoDB (seed.overworld, seed.nether) and finish times live only in MariaDB, so:
  1. one Mongo aggregation groups the filtered matches by (overworld, bastion) and collects their ids;
  2. the ids go into a session-only TEMPORARY table in MariaDB;
  3. one SQL query joins it to the completed wins: COUNT, AVG, MIN and median (PERCENTILE_CONT).
"""
import math

from flask import Blueprint, current_app, flash, render_template, request
from pymongo.errors import PyMongoError

from app import cache, endgame, stats, summary, util
from app.db_mongo import get_mongo
from app.db_sql import get_db, query_one
from app.stats import format_ms as fmt_ms

bp = Blueprint("seeds", __name__)

TYPES = {"ranked": 2, "all": None}
MIN_WINS = 30   # combinations with fewer completed wins are shown but flagged and not used for "fastest"


def options(coll):
    """Dropdown values that actually exist in the data."""
    variations = {}
    for v in sorted(x for x in coll.distinct("seed.variations") if x):
        group = ":".join(v.split(":")[:2])
        variations.setdefault(group, []).append(v)
    return {
        "overworld": sorted(x for x in coll.distinct("seed.overworld") if x),
        "nether": sorted(x for x in coll.distinct("seed.nether") if x),
        "variations": variations,
    }


def build_match(f):
    """Mongo $match document from validated filters."""
    q = {}
    if f["type"] is not None:
        q["match_type"] = f["type"]
    if f["overworld"]:
        q["seed.overworld"] = f["overworld"]
    if f["nether"]:
        q["seed.nether"] = f["nether"]
    if f["variation"]:
        q["seed.variations"] = f["variation"]
    return q


def combo_pipeline(match):
    """Matches grouped by (overworld, bastion), with their ids for the SQL side."""
    return [
        {"$match": {**match, "seed.overworld": match.get("seed.overworld", {"$ne": None}),
                    "seed.nether": match.get("seed.nether", {"$ne": None})}},
        {"$group": {"_id": {"overworld": "$seed.overworld", "nether": "$seed.nether"},
                    "matches": {"$sum": 1}, "ids": {"$push": "$_id"}}},
    ]


# One pass over the completed wins: per-combination median and the overall median are two
# windows over the same rows; the overall average is derived exactly from the per-combination sums.
COMBO_SQL = """
/* Seed combinations: average and median (PERCENTILE_CONT) of the winning times */
SELECT overworld, nether, COUNT(*) AS wins, SUM(t) AS sum_ms, AVG(t) AS avg_ms, MIN(t) AS best_ms,
       MAX(med) AS median_ms, MAX(all_med) AS overall_median_ms
FROM (
    SELECT c.overworld, c.nether, mp.finish_time_ms AS t,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY mp.finish_time_ms)
               OVER (PARTITION BY c.overworld, c.nether) AS med,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY mp.finish_time_ms) OVER () AS all_med
    FROM tmp_seed_combo c
    JOIN v_completed_wins mp ON mp.match_id = c.match_id   -- type already chosen by the Mongo filter
) wins
GROUP BY overworld, nether
"""


def query_with_seeds(rows, sql, params=()):
    """Cross-store join: load (match_id, overworld, nether) rows from Mongo into the session-only MEMORY table
    tmp_seed_combo, run `sql` against it, drop it. Returns the result rows."""
    db = get_db()
    with db.cursor() as cur:
        cur.execute("CREATE TEMPORARY TABLE IF NOT EXISTS tmp_seed_combo (match_id BIGINT UNSIGNED PRIMARY KEY, "
                    "overworld VARCHAR(32) NOT NULL, nether VARCHAR(32) NOT NULL) ENGINE=MEMORY")
        cur.execute("TRUNCATE TABLE tmp_seed_combo")
        for k in range(0, len(rows), 5000):
            cur.executemany("INSERT INTO tmp_seed_combo VALUES (%s, %s, %s)", rows[k:k + 5000])
        cur.execute(sql, params)
        out = cur.fetchall()
        cur.execute("DROP TEMPORARY TABLE tmp_seed_combo")
    db.commit()
    return out


def combinations(coll, match):
    """Rank every (overworld, bastion) combination by average completed winning time.
    ~300 ms (the $group plus the cross-store join), so cached per filter until the next import (app.cache)."""
    return cache.cached(("seed-combinations", tuple(sorted(match.items()))), lambda: _combinations(coll, match))


def _combinations(coll, match):
    groups = list(coll.aggregate(combo_pipeline(match), comment="Seed combinations: match ids per seed"))
    if not groups:
        return [], None
    rows = [(i, g["_id"]["overworld"], g["_id"]["nether"]) for g in groups for i in g["ids"]]
    times = {(r["overworld"], r["nether"]): r for r in query_with_seeds(rows, COMBO_SQL)}

    total_wins = sum(int(r["wins"]) for r in times.values())
    overall_avg = float(sum(r["sum_ms"] for r in times.values())) / total_wins if total_wins else None
    overall_median = float(next(iter(times.values()))["overall_median_ms"]) if times else None
    out = []
    for g in groups:
        key = (g["_id"]["overworld"], g["_id"]["nether"])
        t = times.get(key)
        avg = float(t["avg_ms"]) if t else None
        out.append({
            "overworld": key[0], "nether": key[1], "matches": g["matches"],
            "wins": int(t["wins"]) if t else 0,
            "avg_ms": avg, "median_ms": float(t["median_ms"]) if t else None,
            "best_ms": int(t["best_ms"]) if t else None,
            "delta_s": None if avg is None or overall_avg is None else round((avg - overall_avg) / 1000, 1),
            "small": (int(t["wins"]) if t else 0) < MIN_WINS,
        })
    out.sort(key=lambda r: (r["avg_ms"] is None, r["avg_ms"] or 0))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    overall = {"wins": total_wins, "avg_ms": overall_avg, "median_ms": overall_median} if total_wins else None
    return out, overall


MIN_PLAYER_GAMES = 20   # a player's seed type with fewer ranked games is flagged and can't be their "best"

# One player's ranked games per seed combination (the Mongo side picked their matches); rolled up per overworld
# and per bastion in Python, since sums and counts add up exactly.
PLAYER_SEED_SQL = """
/* Player seed types: their results joined to the seeds from MongoDB */
SELECT c.overworld, c.nether, COUNT(*) AS games, SUM(r.outcome = 'win') AS wins, SUM(r.outcome = 'draw') AS draws,
       COUNT(w.match_id) AS timed_wins, SUM(w.finish_time_ms) AS sum_ms, MIN(w.finish_time_ms) AS best_ms
FROM tmp_seed_combo c
JOIN v_ranked_match_players r ON r.match_id = c.match_id AND r.player_id = %s
LEFT JOIN v_completed_wins w ON w.match_id = r.match_id AND w.player_id = r.player_id
{season}
GROUP BY c.overworld, c.nether
"""


def _rollup(rows, key, overall_rate):
    per = {}
    for r in rows:
        acc = per.setdefault(r[key], {"type": r[key], "games": 0, "wins": 0, "draws": 0, "timed_wins": 0,
                                      "sum_ms": 0, "best_ms": None})
        acc["games"] += int(r["games"])
        acc["wins"] += int(r["wins"])
        acc["draws"] += int(r["draws"])
        acc["timed_wins"] += int(r["timed_wins"])
        acc["sum_ms"] += int(r["sum_ms"] or 0)
        if r["best_ms"] is not None and (acc["best_ms"] is None or r["best_ms"] < acc["best_ms"]):
            acc["best_ms"] = int(r["best_ms"])
    out = []
    for acc in per.values():
        rate = stats.win_rate(acc["wins"], acc["games"], acc["draws"])
        decisive = acc["games"] - acc["draws"]
        p = acc["wins"] / decisive if decisive else None
        out.append(dict(acc, win_rate=rate, small=acc["games"] < MIN_PLAYER_GAMES,
                        losses=decisive - acc["wins"],
                        # 95% margin of a proportion, over the decisive games the rate is based on
                        margin_pp=196 * math.sqrt(p * (1 - p) / decisive) if decisive else None,
                        avg_ms=acc["sum_ms"] / acc["timed_wins"] if acc["timed_wins"] else None,
                        delta_pp=None if rate is None or overall_rate is None else float(rate - overall_rate)))
    # Best = highest win rate. Both players get the same seed, so win rate isn't skewed by how fast a seed type
    # is for everyone (average time is: Ruined Portal would top every player's list).
    out.sort(key=lambda r: (r["small"], -(r["win_rate"] or 0), r["avg_ms"] or float("inf")))
    return out


def player_seed_types(uuid, player_id, season=None):
    """A player's ranked win rate and average winning time per overworld type and per bastion type.
    Mongo finds their matches by players.uuid (indexed) and supplies the seed; MariaDB supplies outcomes and
    times. Returns None when no stored ranked match of theirs has a seed."""
    q = {"players.uuid": uuid, "match_type": 2, "seed.overworld": {"$ne": None}, "seed.nether": {"$ne": None}}
    if season is not None:
        q["season"] = season
    docs = get_mongo()["matches"].find(q, {"seed.overworld": 1, "seed.nether": 1},
                                   comment="This player's seeds, for the join in MariaDB")
    rows = [(d["_id"], d["seed"]["overworld"], d["seed"]["nether"]) for d in docs]
    if not rows:
        return None
    params = [player_id]
    where = ""
    if season is not None:
        where, params = "WHERE r.season_id = %s", params + [season]
    combos = query_with_seeds(rows, PLAYER_SEED_SQL.format(season=where), params)
    games = sum(int(r["games"]) for r in combos)
    if not games:
        return None
    overall = stats.win_rate(sum(int(r["wins"]) for r in combos), games, sum(int(r["draws"]) for r in combos))
    timed = sum(int(r["timed_wins"]) for r in combos)
    out = {"games": games, "win_rate": overall, "min_games": MIN_PLAYER_GAMES,
           "avg_ms": sum(int(r["sum_ms"] or 0) for r in combos) / timed if timed else None}
    for key in ("overworld", "nether"):
        types = _rollup(combos, key, overall)
        best = next((t for t in types if not t["small"]), None)
        # Points vs the player's own overall win rate: the raw rates sit within a few points of each other.
        out[key] = {"types": types, "best": best, "chart": {
            "kind": "hbar", "format": "pp", "diverging": True, "higherIsBetter": True,
            "labels": [nice(t["type"]) for t in types],
            "series": [{"name": "vs overall win rate", "slot": 1, "data": [
                None if t["delta_pp"] is None else round(t["delta_pp"], 1) for t in types]}],
            "legend": [{"name": "Above overall", "slot": 1}, {"name": "Below overall", "slot": 2}],
            "extra": {"vs overall win rate": [
                f"{t['win_rate']}% · {t['wins']}W {t['losses']}L {t['draws']}D · avg win {fmt_ms(t['avg_ms'])}"
                for t in types]},
        }}
    return out


def nice(v):
    return (v or "–").replace("_", " ").title()


def parse_filters(args, opts):
    """Validate the query string against the values that exist in the data. Returns (filters, page, errors)."""
    errors = []
    f = {"overworld": args.get("overworld") or None, "nether": args.get("nether") or None,
         "variation": args.get("variation") or None, "type": args.get("type") or "ranked"}
    all_variations = {v for group in opts["variations"].values() for v in group}
    if f["overworld"] and f["overworld"] not in opts["overworld"]:
        errors.append("Unknown overworld structure.")
    if f["nether"] and f["nether"] not in opts["nether"]:
        errors.append("Unknown bastion type.")
    if f["variation"] and f["variation"] not in all_variations:
        errors.append("Unknown seed variation.")
    if f["type"] not in TYPES:
        errors.append("Type must be ranked or all.")
    try:
        page = util.parse_page(args.get("page"))
    except util.BadParam as e:
        errors.append(str(e))
        page = 1
    return f, page, errors


def combo_chart(combos):
    """Seconds vs the overall average per combination (the values sit close together, so the chart shows the
    difference, not raw bars), or None when no combination has a winning time."""
    shown = [c for c in combos if c["delta_s"] is not None]
    if not shown:
        return None
    return {
        "kind": "hbar", "format": "sec", "diverging": True,
        "labels": [f"{nice(c['overworld'])} + {nice(c['nether'])}" for c in shown],
        "series": [{"name": "Seconds vs overall average", "slot": 1, "data": [c["delta_s"] for c in shown]}],
        "legend": [{"name": "Faster than average", "slot": 1}, {"name": "Slower than average", "slot": 2}],
        "extra": {"Seconds vs overall average": [
            f"average {fmt_ms(c['avg_ms'])} · median {fmt_ms(c['median_ms'])} · {c['wins']} wins" for c in shown]},
    }


def end_game_view():
    """Everyone's End-game stats from the cached timelines, and how many ranked matches they cover."""
    details, ranked = get_mongo()["match_details"], {**endgame.RANKED, "end_splits": {"$exists": True}}
    coverage = {"matches": details.count_documents(ranked),
                "seasons": endgame.season_label(sorted(s for s in details.distinct("season", ranked) if s))}
    return endgame.summary(), coverage


@bp.route("/seeds")
def index():
    try:
        coll = get_mongo()["matches"]
        opts = options(coll)
    except PyMongoError:
        current_app.logger.exception("Mongo unavailable")
        flash("The seed database is unavailable right now.", "error")
        return render_template("seeds/index.html", opts=None, f={}, rows=[], paging=util.pages(0, 1)), 503

    f, page, errors = parse_filters(request.args, opts)
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("seeds/index.html", opts=opts, f=f, rows=[], paging=util.pages(0, 1)), 400
    f["type_name"] = f["type"]
    f["type"] = TYPES[f["type"]]
    match = build_match(f)

    combos, overall = combinations(coll, match)
    ranked_combos = [c for c in combos if c["avg_ms"] is not None and not c["small"]]
    end, end_cov = end_game_view()
    charts = {}
    if chart := combo_chart(combos):
        charts["combos"] = chart
    if end and end["kills"]:
        charts["dragon"] = endgame.dragon_chart(end["histogram"])

    # find(): matching matches, newest first, then ONE SQL query for their summaries.
    total = coll.count_documents(match)
    docs = list(coll.find(match, {"seed": 1, "played_at": 1, "season": 1}, comment="Matches with this seed")
                .sort([("played_at", -1), ("_id", -1)]).skip((page - 1) * util.PER_PAGE).limit(util.PER_PAGE))
    summaries = summary.matches_by_id(d["_id"] for d in docs)
    rows = [{"doc": d, "sql": summaries.get(d["_id"])} for d in docs]

    return render_template(
        "seeds/index.html", opts=opts, f=f, rows=rows, total=total, paging=util.pages(total, page),
        combos=combos, overall=overall, fastest=ranked_combos[0] if ranked_combos else None,
        slowest=ranked_combos[-1] if ranked_combos else None, min_wins=MIN_WINS, charts=charts, end=end, end_cov=end_cov,
        cutoff_s=endgame.ZERO_CYCLE_MAX_MS // 1000,
        full_players=query_one("SELECT COUNT(*) AS n FROM players WHERE profile_status = 'full'")["n"],
    )

