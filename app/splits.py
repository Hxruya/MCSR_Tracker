"""Split analysis: when a player reaches each speedrun milestone, against their opponents in the same matches.

Runs entirely in MongoDB over cached match timelines (`match_details.timelines` + `completions`):

  $match    ranked, non-decay matches whose timeline includes the player (ix_timelines_uuid)
  $project  keep only milestone events, plus each completion as a "finish" event ($filter, $map, $concatArrays)
  $unwind   one document per event
  $group    first time per (match, player, milestone): repeated events (e.g. re-entering the Nether) count once
  $group    per (match, milestone): this player's time and the opponent's, side by side
  $group    per milestone: $median / $percentile of the player's times, the opponents' median, and the median
            of the paired difference (player - opponent, only where both reached it in the same match)

The paired gap compares like with like: it answers "in matches where we both got there, how far behind was I",
which a difference of two medians over different matches wouldn't. MongoDB's $median and $percentile only offer
method "approximate" (t-digest): for small samples they return an actual value from the data, so the median of an
even-sized sample is the lower middle value, not the average of the two (PERCENTILE_CONT on the Seeds page).
"""
from operator import itemgetter

from flask import current_app
from pymongo.errors import PyMongoError

from app.db_mongo import get_mongo
from app.endgame import RANKED, season_label, _rate as pct

# Speedrun milestones from timelines[].type (seen in real /matches/{id} responses, e.g. tests/fixtures/), in run
# order. Also the rows of the match page's split table (app/matches.py).
MILESTONES = [
    ("story.enter_the_nether", "Enter the Nether"),
    ("nether.find_bastion", "Find bastion"),
    ("nether.find_fortress", "Find fortress"),
    ("nether.obtain_blaze_rod", "First blaze rod"),
    ("projectelo.timeline.blind_travel", "Blind travel"),
    ("story.follow_ender_eye", "Find stronghold"),
    ("story.enter_the_end", "Enter the End"),
]
FINISH = "finish"
STEPS = MILESTONES + [(FINISH, "Finish")]


def matches_of(uuid, season=None):
    """Ranked, non-decay timelines that include this player (ix_timelines_uuid)."""
    match = {**RANKED, "timelines.uuid": uuid}
    if season is not None:
        match["season"] = season
    return match


def pipeline(uuid, season=None):
    approx = {"method": "approximate"}
    return [
        {"$match": matches_of(uuid, season)},
        {"$project": {"season": 1, "events": {"$concatArrays": [
            {"$filter": {"input": "$timelines", "as": "e",
                         "cond": {"$in": ["$$e.type", [key for key, _ in MILESTONES]]}}},
            {"$map": {"input": "$completions", "as": "c",
                      "in": {"uuid": "$$c.uuid", "time": "$$c.time", "type": FINISH}}},
        ]}}},
        {"$unwind": "$events"},
        {"$group": {"_id": {"match": "$_id", "type": "$events.type", "uuid": "$events.uuid"},
                    "season": {"$first": "$season"}, "time": {"$min": "$events.time"}}},
        {"$group": {"_id": {"match": "$_id.match", "type": "$_id.type"},
                    "season": {"$first": "$season"},
                    "mine": {"$max": {"$cond": [{"$eq": ["$_id.uuid", uuid]}, "$time", None]}},
                    "theirs": {"$min": {"$cond": [{"$eq": ["$_id.uuid", uuid]}, None, "$time"]}}}},
        {"$group": {
            "_id": "$_id.type",
            # $median / $percentile skip null (non-numeric) inputs, so each statistic counts only the matches
            # where that side (or, for the gap, both sides) reached the milestone
            "reached": {"$sum": {"$cond": [{"$ne": ["$mine", None]}, 1, 0]}},
            "median": {"$median": {"input": "$mine", **approx}},
            "quartiles": {"$percentile": {"input": "$mine", "p": [0.25, 0.75], **approx}},
            "opp_reached": {"$sum": {"$cond": [{"$ne": ["$theirs", None]}, 1, 0]}},
            "opp_median": {"$median": {"input": "$theirs", **approx}},
            "paired": {"$sum": {"$cond": [{"$and": [{"$ne": ["$mine", None]}, {"$ne": ["$theirs", None]}]}, 1, 0]}},
            "gap": {"$median": {"input": {"$subtract": ["$mine", "$theirs"]}, **approx}},
            "seasons": {"$addToSet": "$season"},
        }},
    ]


def analyse(uuid, season=None):
    """{"matches": n, "seasons": [...], "rows": [one per milestone reached by anyone, in run order]}"""
    db = get_mongo()
    matches = db["match_details"].count_documents(matches_of(uuid, season))
    by_type = {r["_id"]: r for r in db["match_details"].aggregate(pipeline(uuid, season),
                                                                 comment="Split analysis: medians per milestone")}
    rows, seasons, previous_gap = [], set(), None
    for key, label in STEPS:
        r = by_type.get(key)
        if r is None:
            continue
        seasons.update(s for s in r["seasons"] if s is not None)
        gap = _int(r["gap"])
        q1, q3 = (r["quartiles"] or [None, None])
        rows.append({
            "key": key, "label": label, "reached": r["reached"], "reached_pct": pct(r["reached"], matches),
            "median": _int(r["median"]), "q1": _int(q1), "q3": _int(q3),
            "opp_reached": r["opp_reached"], "opp_median": _int(r["opp_median"]), "paired": r["paired"], "gap": gap,
            # how much the gap grew since the previous milestone: where time is lost (+) or won (-)
            "gap_change": None if gap is None or previous_gap is None else gap - previous_gap,
        })
        if gap is not None:
            previous_gap = gap
    return {"matches": matches, "seasons": sorted(seasons), "rows": rows}


def _int(v):
    return None if v is None else int(round(v))


def profile_view(uuid, season):
    """Split table for the profile, or None when MongoDB is unavailable. Rows are empty without timelines."""
    try:
        out = analyse(uuid, season)
    except PyMongoError:
        current_app.logger.warning("split analysis unavailable", exc_info=True)
        return None
    out["seasons_label"] = season_label(out["seasons"])
    change = itemgetter("gap_change")
    rows = [r for r in out["rows"] if r["gap_change"] is not None]
    out["worst"] = max((r for r in rows if change(r) > 0), key=change, default=None)   # where most time is lost
    out["best"] = min((r for r in rows if change(r) < 0), key=change, default=None)    # where most time is won
    out["finish"] = next((r for r in out["rows"] if r["key"] == FINISH), None)
    return out
