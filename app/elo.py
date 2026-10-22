"""Profile Elo history from the `elo_history` time-series collection.

One aggregation does everything:
  $setWindowFields  per season (the $match already fixed the player), ordered by time: 20-match moving
                    average, running peak, match number
  $facet            "points" for the chart, and "summary" with $top picks (peak, biggest gain and loss,
                    deepest drop below a previous peak) in the same pass.
"""
from app import util
from app.db_mongo import get_mongo

WINDOW = 20


def pipeline(uuid, season=None):
    match = {"player": uuid}
    if season is not None:
        match["season"] = season
    return [
        {"$match": match},   # metaField + timeField index, created by MongoDB for time-series collections
        {"$setWindowFields": {
            # per season: Elo is soft-reset when a season starts, so a peak or average must not span seasons.
            # Not by player too: $match already fixed it, and the simpler key is faster (35 vs 61 ms for edcr,
            # same output).
            "partitionBy": "$season",
            "sortBy": {"played_at": 1},
            "output": {
                "avg": {"$avg": "$elo", "window": {"documents": [-(WINDOW - 1), 0]}},
                "peak": {"$max": "$elo", "window": {"documents": ["unbounded", "current"]}},
                "n": {"$documentNumber": {}},
            },
        }},
        {"$set": {"below_peak": {"$subtract": ["$elo", "$peak"]}}},
        {"$facet": {
            "points": [{"$sort": {"played_at": 1}},
                       {"$project": {"_id": 0, "played_at": 1, "season": 1, "elo": 1, "avg": 1, "n": 1}}],
            "summary": [{"$group": {
                "_id": None,
                "matches": {"$sum": 1},
                "peak": {"$top": {"sortBy": {"elo": -1, "played_at": 1}, "output": ["$elo", "$played_at"]}},
                # ties go to the earliest match
                "gain": {"$top": {"sortBy": {"change": -1, "played_at": 1},
                                  "output": ["$change", "$match_id", "$played_at"]}},
                "loss": {"$top": {"sortBy": {"change": 1, "played_at": 1},
                                  "output": ["$change", "$match_id", "$played_at"]}},
                "drop": {"$top": {"sortBy": {"below_peak": 1, "played_at": 1},
                                  "output": ["$below_peak", "$peak", "$played_at"]}},
            }}],
        }},
    ]


def history(uuid, season=None):
    """{chart, months, summary} for the profile, or None with fewer than 2 points."""
    out = next(get_mongo()["elo_history"].aggregate(pipeline(uuid, season),
                                                 comment="Elo history: moving average and peak"), None)
    points = out["points"] if out else []
    if len(points) < 2:
        return None
    s = out["summary"][0]
    summary = {
        "matches": s["matches"], "window": WINDOW,
        "peak": {"elo": s["peak"][0], "at": s["peak"][1]},
        "gain": {"change": s["gain"][0], "match_id": s["gain"][1], "at": s["gain"][2]},
        "loss": {"change": s["loss"][0], "match_id": s["loss"][1], "at": s["loss"][2]},
        "drop": {"by": s["drop"][0], "from": s["drop"][1], "at": s["drop"][2]},
    }
    series = [{"name": "Elo", "slot": 1,
               "data": [{"x": util.epoch_ms(p["played_at"]), "y": p["elo"]} for p in points]}]
    # The average starts once a season has a full window; a null point breaks the line between seasons.
    avg, last_season = [], None
    for p in points:
        if p["n"] < WINDOW:
            continue
        if last_season is not None and p["season"] != last_season:
            avg.append({"x": util.epoch_ms(p["played_at"]) - 1, "y": None})
        avg.append({"x": util.epoch_ms(p["played_at"]), "y": round(p["avg"], 1)})
        last_season = p["season"]
    if avg:
        series.append({"name": f"{WINDOW}-match average", "slot": 2, "data": avg})
    months = {}
    for p in points:
        key = p["played_at"].strftime("%Y-%m")
        m = months.setdefault(key, {"month": key, "games": 0, "low": p["elo"], "high": p["elo"]})
        m["games"] += 1
        m["low"], m["high"], m["end"] = min(m["low"], p["elo"]), max(m["high"], p["elo"]), p["elo"]
    return {"chart": {"kind": "line-time", "format": "elo", "series": series},
            "months": list(months.values())[::-1], "summary": summary}
