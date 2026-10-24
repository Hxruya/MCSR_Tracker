"""Split analysis: milestone speedrun times compared against Elo tiers.

Runs across MongoDB match timelines (`match_details.timelines` + `completions`)
and MariaDB player Elo ratings:
- Computes the player's personal milestone times (median, quartiles, reached count)
- Benchmarks against the median times of players in each Elo tier
- Compares completion times across ranks in a visual bar chart matching reference design
"""
import collections
from operator import itemgetter
import statistics

from flask import current_app
from pymongo.errors import PyMongoError

from app import cache, stats
from app.db_mongo import get_mongo
from app.db_sql import query_all
from app.endgame import RANKED, season_label, _rate as pct

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

TIER_NETHERITE = "Netherite (1800+)"
TIER_DIAMOND = "Diamond (1500–1799)"
TIER_EMERALD = "Emerald (1200–1499)"
TIER_GOLD = "Gold (900–1199)"
TIER_IRON = "Iron / Coal (<900)"

TIER_ORDER = [
    (TIER_NETHERITE, "netherite", 2),
    (TIER_DIAMOND, "diamond", 3),
    (TIER_EMERALD, "emerald", 4),
    (TIER_GOLD, "gold", 5),
    (TIER_IRON, "iron", 6),
]


def tier_name_from_elo(elo):
    if elo is None:
        return TIER_IRON
    if elo >= 1800:
        return TIER_NETHERITE
    if elo >= 1500:
        return TIER_DIAMOND
    if elo >= 1200:
        return TIER_EMERALD
    if elo >= 900:
        return TIER_GOLD
    return TIER_IRON


def get_player_tier_map(season=None):
    try:
        if season is None:
            rows = query_all("SELECT uuid, current_elo AS elo FROM players WHERE current_elo IS NOT NULL")
        else:
            rows = query_all("""
                SELECT p.uuid, COALESCE(psr.final_elo, psr.highest_elo, p.current_elo) AS elo
                FROM players p
                LEFT JOIN player_season_results psr ON psr.player_id = p.player_id AND psr.season_id = %s
                WHERE COALESCE(psr.final_elo, psr.highest_elo, p.current_elo) IS NOT NULL
            """, (season,))
        return {r["uuid"]: tier_name_from_elo(r["elo"]) for r in rows if r.get("uuid")}
    except Exception:
        return {}


def _compute_tier_milestones(season=None):
    try:
        db = get_mongo()
        tier_map = get_player_tier_map(season)
        match = {"match_type": 2, "decayed": {"$ne": True}}
        if season is not None:
            match["season"] = season
        pipeline_tier = [
            {"$match": match},
            {"$project": {"events": {"$concatArrays": [
                {"$filter": {"input": "$timelines", "as": "e",
                             "cond": {"$in": ["$$e.type", [key for key, _ in MILESTONES]]}}},
                {"$map": {"input": "$completions", "as": "c",
                          "in": {"uuid": "$$c.uuid", "time": "$$c.time", "type": FINISH}}},
            ]}}},
            {"$unwind": "$events"},
            {"$group": {"_id": {"match": "$_id", "type": "$events.type", "uuid": "$events.uuid"},
                        "time": {"$min": "$events.time"}}},
        ]
        groups = list(db["match_details"].aggregate(pipeline_tier, comment="Tier milestone medians"))
        times_by_type_tier = collections.defaultdict(lambda: collections.defaultdict(list))
        for g in groups:
            m_type = g["_id"]["type"]
            uuid = g["_id"]["uuid"]
            t = g["time"]
            tier = tier_map.get(uuid)
            if tier and t is not None:
                times_by_type_tier[m_type][tier].append(t)

        medians = {}
        for m_type, tiers in times_by_type_tier.items():
            medians[m_type] = {}
            for tier, times in tiers.items():
                if times:
                    medians[m_type][tier] = int(round(statistics.median(times)))
        return medians
    except Exception:
        return {}


def tier_milestone_medians(season=None):
    return cache.cached(("tier_milestone_medians", season), lambda: _compute_tier_milestones(season))


def matches_of(uuid, season=None):
    match = {**RANKED, "timelines.uuid": uuid}
    if season is not None:
        match["season"] = season
    return match


def player_pipeline(uuid, season=None):
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
        {"$match": {"events.uuid": uuid}},
        {"$group": {"_id": {"match": "$_id", "type": "$events.type"},
                    "season": {"$first": "$season"}, "time": {"$min": "$events.time"}}},
        {"$group": {
            "_id": "$_id.type",
            "reached": {"$sum": 1},
            "median": {"$median": {"input": "$time", **approx}},
            "quartiles": {"$percentile": {"input": "$time", "p": [0.25, 0.75], **approx}},
            "seasons": {"$addToSet": "$season"},
        }},
    ]


pipeline = player_pipeline


def _int(v):
    return None if v is None else int(round(v))


def _quartiles(vals):
    if not vals:
        return None, None
    s = sorted(vals)
    n = len(s)
    if n == 1:
        return s[0], s[0]
    if n == 2:
        return s[0], s[1]
    if n == 3:
        return s[0], s[2]
    q = statistics.quantiles(s, n=4, method="inclusive")
    return int(round(q[0])), int(round(q[2]))


MILESTONE_KEYS = {key for key, _ in MILESTONES}


def analyse(uuid, season=None):
    db = get_mongo()
    match_filter = matches_of(uuid, season)
    matches_docs = list(db["match_details"].find(
        match_filter,
        {"season": 1, "timelines": 1, "completions": 1}
    ))
    matches = len(matches_docs)
    tier_map = get_player_tier_map(season)
    player_tier = tier_map.get(uuid, TIER_DIAMOND)
    tier_medians = tier_milestone_medians(season)

    by_type = collections.defaultdict(lambda: {
        "p_times": [],
        "o_times": [],
        "paired": [],
        "seasons": set(),
    })
    for doc in matches_docs:
        s = doc.get("season")
        p_first = {}
        o_first = {}
        for e in doc.get("timelines") or []:
            t = e.get("time")
            k = e.get("type")
            u = e.get("uuid")
            if t is None or k not in MILESTONE_KEYS:
                continue
            if u == uuid:
                if k not in p_first or t < p_first[k]:
                    p_first[k] = t
            else:
                if k not in o_first or t < o_first[k]:
                    o_first[k] = t

        for c in doc.get("completions") or []:
            t = c.get("time")
            u = c.get("uuid")
            if t is None:
                continue
            if u == uuid:
                if FINISH not in p_first or t < p_first[FINISH]:
                    p_first[FINISH] = t
            else:
                if FINISH not in o_first or t < o_first[FINISH]:
                    o_first[FINISH] = t

        for k, t in p_first.items():
            by_type[k]["p_times"].append(t)
            if s is not None:
                by_type[k]["seasons"].add(s)
            if k in o_first and k != FINISH:
                by_type[k]["paired"].append(t - o_first[k])

        for k, t in o_first.items():
            by_type[k]["o_times"].append(t)

    rows, seasons, previous_gap = [], set(), None
    for key, label in STEPS:
        info = by_type.get(key)
        if not info or not info["p_times"]:
            continue
        seasons.update(info["seasons"])
        p_times = info["p_times"]
        o_times = info["o_times"]
        paired_diffs = info["paired"]

        reached = len(p_times)
        med = int(round(statistics.median(p_times)))
        q1, q3 = _quartiles(p_times)

        opp_reached = len(o_times)
        opp_median = int(round(statistics.median(o_times))) if o_times else None
        paired = len(paired_diffs)
        gap = int(round(statistics.median(paired_diffs))) if paired_diffs else None
        gap_change = None if gap is None or previous_gap is None else gap - previous_gap
        if gap is not None:
            previous_gap = gap

        tier_med = tier_medians.get(key, {}).get(player_tier)
        diff_flag = None
        diff_pct = None
        if med is not None and tier_med is not None and tier_med > 0:
            diff_pct = round(((med - tier_med) / tier_med) * 100, 1)
            if med < tier_med * 0.95:
                diff_flag = "faster"
            elif med > tier_med * 1.05:
                diff_flag = "slower"

        rows.append({
            "key": key,
            "label": label,
            "reached": reached,
            "reached_pct": pct(reached, matches),
            "median": med,
            "q1": q1,
            "q3": q3,
            "opp_reached": opp_reached,
            "opp_median": opp_median,
            "paired": paired,
            "gap": gap,
            "gap_change": gap_change,
            "tier_median": tier_med,
            "diff_flag": diff_flag,
            "diff_pct": diff_pct,
            "tier_values": [
                {"key": "netherite", "name": TIER_NETHERITE, "ms": tier_medians.get(key, {}).get(TIER_NETHERITE)},
                {"key": "diamond", "name": TIER_DIAMOND, "ms": tier_medians.get(key, {}).get(TIER_DIAMOND)},
                {"key": "emerald", "name": TIER_EMERALD, "ms": tier_medians.get(key, {}).get(TIER_EMERALD)},
                {"key": "gold", "name": TIER_GOLD, "ms": tier_medians.get(key, {}).get(TIER_GOLD)},
                {"key": "iron", "name": TIER_IRON, "ms": tier_medians.get(key, {}).get(TIER_IRON)},
            ],
            "all_tiers": tier_medians.get(key, {}),
        })

    # Build completion bars for "AVG COMPLETION TIME BY RANK" matching reference.png
    finish_r = by_type.get(FINISH)
    my_finish_ms = int(round(statistics.median(finish_r["p_times"]))) if finish_r and finish_r["p_times"] else None

    tier_finish_rows = [
        {"key": "iron", "label": "IRON / COAL", "ms": tier_medians.get(FINISH, {}).get(TIER_IRON)},
        {"key": "gold", "label": "GOLD", "ms": tier_medians.get(FINISH, {}).get(TIER_GOLD)},
        {"key": "emerald", "label": "EMERALD", "ms": tier_medians.get(FINISH, {}).get(TIER_EMERALD)},
        {"key": "diamond", "label": "DIAMOND", "ms": tier_medians.get(FINISH, {}).get(TIER_DIAMOND)},
        {"key": "netherite", "label": "NETHERITE", "ms": tier_medians.get(FINISH, {}).get(TIER_NETHERITE)},
    ]
    if my_finish_ms is not None:
        tier_finish_rows.append({"key": "you", "label": "YOU", "ms": my_finish_ms})

    valid_ms = [r["ms"] for r in tier_finish_rows if r["ms"] is not None]
    max_ms = max(valid_ms) if valid_ms else 1

    completion_bars = []
    for r in tier_finish_rows:
        ms = r["ms"]
        if ms is not None:
            m = ms // 60000
            s = (ms // 1000) % 60
            time_fmt = f"{m:02d}:{s:02d}"
            pct_val = max(6.0, round((ms / max_ms) * 100, 1))
        else:
            time_fmt = "no completions"
            pct_val = 0.0
        completion_bars.append({
            "key": r["key"],
            "label": r["label"],
            "ms": ms,
            "time_fmt": time_fmt,
            "pct": pct_val,
        })

    return {
        "matches": matches,
        "seasons": sorted(seasons),
        "rows": rows,
        "tier_name": player_tier,
        "completion_bars": completion_bars,
    }


def profile_view(uuid, season):
    try:
        out = analyse(uuid, season)
    except PyMongoError:
        current_app.logger.warning("split analysis unavailable", exc_info=True)
        return None
    out["seasons_label"] = season_label(out["seasons"])
    change = itemgetter("gap_change")
    rows = [r for r in out["rows"] if r["gap_change"] is not None]
    out["worst"] = max((r for r in rows if change(r) > 0), key=change, default=None)
    out["best"] = min((r for r in rows if change(r) < 0), key=change, default=None)
    out["finish"] = next((r for r in out["rows"] if r["key"] == FINISH), None)
    return out
