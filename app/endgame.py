"""End-game analysis from cached match timelines (MongoDB `match_details.end_splits`).

dragon_split = time from first entering the End to the dragon dying, for the player who killed it.
A kill within ZERO_CYCLE_MAX_MS counts as a zero cycle (cut-off chosen from the observed
distribution). Only ranked, non-decay matches count. Results cover only matches whose
timeline has been fetched, so every figure carries its sample size.
"""
import statistics

from flask import current_app
from pymongo.errors import PyMongoError

from app.db_mongo import get_mongo
from app.db_sql import query_all

ZERO_CYCLE_MAX_MS = 60_000   # the low point in 763 sampled dragon kills: 2 in 55-60 s, against 50 in 40-45 s
BIN_MS = 5_000
HIST_MAX_MS = 180_000

RANKED = {"match_type": 2, "decayed": {"$ne": True}}


def _splits(match, extra=None):
    pipeline = [{"$match": {**RANKED, **match}}, {"$unwind": "$end_splits"}]
    if extra:
        pipeline.append({"$match": extra})
    return pipeline


def _rate(part, whole):
    return None if not whole else round(100 * part / whole, 1)


def summary(uuid=None, season=None):
    """Counts for one player (uuid) or everyone: End entries, dragon kills, zero cycles, End deaths."""
    match = {} if season is None else {"season": season}
    extra = {"end_splits.uuid": uuid} if uuid else None
    if uuid:
        match["end_splits.uuid"] = uuid
    rows = list(get_mongo()["match_details"].aggregate(_splits(match, extra) + [
        {"$group": {
            "_id": None,
            "entries": {"$sum": 1},
            "kills": {"$sum": {"$cond": [{"$ne": ["$end_splits.dragon_split", None]}, 1, 0]}},
            "zero": {"$sum": {"$cond": [{"$and": [{"$ne": ["$end_splits.dragon_split", None]},
                                                   {"$lte": ["$end_splits.dragon_split", ZERO_CYCLE_MAX_MS]}]}, 1, 0]}},
            "died_in_end": {"$sum": {"$cond": [{"$gt": ["$end_splits.end_deaths", 0]}, 1, 0]}},
            "splits": {"$push": "$end_splits.dragon_split"},
        }},
    ]))
    if not rows:
        return None
    r = rows[0]
    kills = sorted(s for s in r["splits"] if s is not None)
    median = statistics.median(kills) if kills else None   # PERCENTILE_CONT(0.5)'s definition, as on Seeds
    zero_times = [s for s in kills if s <= ZERO_CYCLE_MAX_MS]
    return {
        "entries": r["entries"], "kills": r["kills"], "zero": r["zero"],
        "zero_rate": _rate(r["zero"], r["kills"]),                 # share of dragon kills that were zero cycles
        "died_in_end_rate": _rate(r["died_in_end"], r["entries"]),  # hint of failed bed attempts
        "median_split": median,
        "best_zero": zero_times[0] if zero_times else None,
        "histogram": histogram(kills),
    }


def histogram(splits):
    """5-second bins up to 3 minutes (longer kills fold into the last bin), split at the cut-off."""
    bins = list(range(0, HIST_MAX_MS, BIN_MS))
    counts = [0] * len(bins)
    for s in splits:
        counts[min(s // BIN_MS, len(bins) - 1)] += 1
    labels = [f"{b // 1000}s" if b < HIST_MAX_MS - BIN_MS else f"{b // 1000}s+" for b in bins]
    zero = [c if b + BIN_MS <= ZERO_CYCLE_MAX_MS else 0 for b, c in zip(bins, counts)]
    other = [0 if b + BIN_MS <= ZERO_CYCLE_MAX_MS else c for b, c in zip(bins, counts)]
    return {"labels": labels, "zero": zero, "other": other, "counts": counts}


def by_season(uuid):
    rows = get_mongo()["match_details"].aggregate(_splits({"end_splits.uuid": uuid}, {"end_splits.uuid": uuid}) + [
        {"$match": {"end_splits.dragon_split": {"$ne": None}}},
        {"$group": {"_id": "$season", "kills": {"$sum": 1},
                    "zero": {"$sum": {"$cond": [{"$lte": ["$end_splits.dragon_split", ZERO_CYCLE_MAX_MS]}, 1, 0]}}}},
        {"$sort": {"_id": 1}},
    ])
    return [{"season": r["_id"], "kills": r["kills"], "zero": r["zero"], "rate": _rate(r["zero"], r["kills"])}
            for r in rows]


# ---------------------------------------------------------------- coverage (how much of a sample this is)

def coverage(ranked_ids):
    """How much of a set of ranked matches has a timeline, and which seasons those timelines cover."""
    coll = get_mongo()["match_details"]
    query = {"_id": {"$in": list(ranked_ids)}, "end_splits": {"$exists": True}}
    have = coll.count_documents(query)
    seasons = sorted(s for s in coll.distinct("season", query) if s is not None)
    return {"have": have, "total": len(ranked_ids), "pct": _rate(have, len(ranked_ids)), "seasons": seasons}


def season_label(seasons):
    if not seasons:
        return "no seasons"
    if len(seasons) == 1:
        return f"season {seasons[0]}"
    if seasons == list(range(seasons[0], seasons[-1] + 1)):
        return f"seasons {seasons[0]}–{seasons[-1]}"
    return "seasons " + ", ".join(map(str, seasons))


# ---------------------------------------------------------------- page helpers

def dragon_chart(h):
    """Chart spec for the dragon-time histogram, split at the zero-cycle cut-off (profile and seed page)."""
    return {"kind": "column", "stacked": True, "format": "int", "labels": h["labels"],
            "series": [{"name": f"Zero cycle (≤ {ZERO_CYCLE_MAX_MS // 1000}s)", "slot": 1, "data": h["zero"]},
                       {"name": "Longer", "slot": 2, "data": h["other"]}]}


def profile_view(uuid, season):
    """Zero-cycle stats + dragon-time histogram for the profile (None when no timelines cover this player)."""
    sql = "SELECT r.match_id FROM v_ranked_match_players r JOIN players p ON p.player_id = r.player_id WHERE p.uuid = %s"
    params = [uuid]
    if season is not None:
        sql += " AND r.season_id = %s"
        params.append(season)
    ranked_ids = [r["match_id"] for r in query_all(sql, params)]
    try:
        cov = coverage(ranked_ids)
        s = summary(uuid, season)
    except PyMongoError:
        current_app.logger.warning("end-game stats unavailable", exc_info=True)
        return None
    cov["seasons_label"] = season_label(cov["seasons"])
    if not s or not s["kills"]:
        return {"kills": 0, "coverage": cov}
    s["coverage"] = cov
    s["chart"] = dragon_chart(s["histogram"])
    s["seasons"] = by_season(uuid) if season is None else []
    s["cutoff_s"] = ZERO_CYCLE_MAX_MS // 1000
    return s
