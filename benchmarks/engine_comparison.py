"""Section 7 of benchmarks/benchmarks.md: the same questions answered by MariaDB and by MongoDB.

For each question both implementations are run on the same data and compared on: identical results, median
time, how each engine executed it (EXPLAIN / explain()), and the size of the query text. Used by benchmark.py.
"""
import json
import re
from typing import NamedTuple

from app import facets, rivalry
from app.elo import WINDOW
from app.matches import TYPES

# ---------------------------------------------------------------- the paired implementations

H2H_MONGO = lambda a, b: [   # noqa: E731
    {"$match": {"players.uuid": {"$all": [a, b]}, "match_type": 2, "raw.decayed": {"$ne": True}}},
    {"$group": {"_id": None, "games": {"$sum": 1},
                "a_wins": {"$sum": {"$cond": [{"$eq": ["$result.uuid", a]}, 1, 0]}},
                "b_wins": {"$sum": {"$cond": [{"$eq": ["$result.uuid", b]}, 1, 0]}},
                "draws": {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$result.uuid", None]}, None]}, 1, 0]}}}},
]
H2H_SQL = ("SELECT COUNT(*) AS games, SUM(ra.outcome = 'win') AS a_wins, SUM(rb.outcome = 'win') AS b_wins, "
           "SUM(ra.outcome = 'draw') AS draws FROM v_ranked_match_players ra "
           "JOIN v_ranked_match_players rb ON rb.match_id = ra.match_id AND rb.player_id = %s WHERE ra.player_id = %s")

RIVALS_SQL = (
    "SELECT p.uuid, COUNT(*) AS games, SUM(me.outcome = 'win') AS wins, SUM(opp.outcome = 'win') AS losses, "
    "MAX(me.played_at) AS last_met FROM v_ranked_match_players me "
    "JOIN v_ranked_match_players opp ON opp.match_id = me.match_id AND opp.player_id <> me.player_id "
    "JOIN players p ON p.player_id = opp.player_id WHERE me.player_id = %s "
    "GROUP BY opp.player_id, p.uuid ORDER BY games DESC, last_met DESC LIMIT 10")

ELO_SQL = (
    "SELECT played_at, season_id, elo_after, "
    f"AVG(elo_after) OVER (PARTITION BY season_id ORDER BY played_at, match_id ROWS BETWEEN {WINDOW - 1} PRECEDING "
    "AND CURRENT ROW) AS avg_elo, "
    "MAX(elo_after) OVER (PARTITION BY season_id ORDER BY played_at, match_id ROWS UNBOUNDED PRECEDING) AS peak "
    "FROM v_ranked_match_players WHERE player_id = %s AND elo_after IS NOT NULL ORDER BY played_at, match_id")
ELO_MONGO = lambda uuid: [   # noqa: E731  (the same windows as app/elo.py, without the chart/summary facet)
    {"$match": {"player": uuid}},
    {"$setWindowFields": {"partitionBy": "$season", "sortBy": {"played_at": 1},
                          "output": {"avg": {"$avg": "$elo", "window": {"documents": [-(WINDOW - 1), 0]}},
                                     "peak": {"$max": "$elo", "window": {"documents": ["unbounded", "current"]}}}}},
    {"$sort": {"played_at": 1}},
    {"$project": {"_id": 0, "elo": 1, "avg": 1, "peak": 1}},
]

FACET_SQL = ("SELECT m.season_id, m.match_type, mp.outcome, mp.forfeited, COUNT(*) AS n "
             "FROM match_players mp JOIN matches m ON m.match_id = mp.match_id WHERE mp.player_id = %s "
             "GROUP BY m.season_id, m.match_type, mp.outcome, mp.forfeited")


def facet_counts_from_sql(rows):
    """Roll the SQL cells up the way facets.counts does (no other filters active)."""
    out = {"season": {}, "type": {}, "result": {"win": 0, "loss": 0, "draw": 0, "decay": 0, "forfeit": 0, "": 0}}
    for r in rows:
        n = int(r["n"])
        out["season"][r["season_id"]] = out["season"].get(r["season_id"], 0) + n
        out["type"][r["match_type"]] = out["type"].get(r["match_type"], 0) + n
        out["result"][r["outcome"]] += n
        out["result"][""] += n
        out["result"]["forfeit"] += n if r["forfeited"] == 1 else 0
    return out


class Comparison(NamedTuple):
    """One question answered by both databases."""
    question: str
    sql_approach: str
    mongo_approach: str
    same: bool
    result: str
    sql_time: dict
    mongo_time: dict
    sql_plan: str
    mongo_plan: str
    sql_size: int
    mongo_size: int


# ---------------------------------------------------------------- plan summaries

def sql_plan(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute("EXPLAIN " + sql, params)
        rows = cur.fetchall()
    keys = sorted({r["key"] for r in rows if r.get("key")})
    scans = [r["table"] for r in rows if r.get("type") == "ALL" and not str(r.get("table", "")).startswith("<")]
    extra = "; ".join(sorted({e.strip() for r in rows for e in str(r.get("Extra") or "").split(";")
                              if e.strip() in ("Using temporary", "Using filesort")}))
    return (f"indexes: {', '.join(keys) or 'none'}" + (f"; full scan: {', '.join(scans)}" if scans else "")
            + (f"; {extra}" if extra else ""))


def _without_rejected(node):
    """The explain output minus rejectedPlans: only the plan MongoDB actually ran."""
    if isinstance(node, dict):
        return {k: _without_rejected(v) for k, v in node.items() if k != "rejectedPlans"}
    if isinstance(node, list):
        return [_without_rejected(v) for v in node]
    return node


def mongo_plan(db, coll, pipeline, **kw):
    plan = db.command("explain", {"aggregate": coll, "pipeline": pipeline, "cursor": {}, **kw},
                      verbosity="executionStats")
    s = json.dumps(_without_rejected(plan), default=str)
    idx = sorted(set(re.findall(r'"indexName": "([^"]+)"', s)))
    docs = sum(int(x) for x in re.findall(r'"totalDocsExamined": (\d+)', s))
    keys = sum(int(x) for x in re.findall(r'"totalKeysExamined": (\d+)', s))
    kind = "COLLSCAN" if "COLLSCAN" in s else ("IXSCAN" if "IXSCAN" in s else "?")
    return f"{kind} {', '.join(idx)}; keys {keys:,}, docs {docs:,}".replace(" ;", ";")


def size(text):
    return len(re.sub(r"\s+", " ", text if isinstance(text, str) else json.dumps(text)))


# ---------------------------------------------------------------- section

def section(conn, mongo, player, rival, timed, runs, warmup):
    a_uuid, b_uuid = player["uuid"], rival["uuid"]
    matches = mongo["matches"]

    def sql(q, p):
        with conn.cursor() as cur:
            cur.execute(q, p)
            return cur.fetchall()

    tasks = []

    # 1. head to head
    h_sql = sql(H2H_SQL, (rival["player_id"], player["player_id"]))[0]
    h_mongo = next(matches.aggregate(H2H_MONGO(a_uuid, b_uuid)))
    keys = ("games", "a_wins", "b_wins", "draws")
    tasks.append(Comparison(
        f"Head to head, {player['nickname']} vs {rival['nickname']}",
        "self-join of v_ranked_match_players on match_id", "$match players.uuid $all [A, B] → $group",
        all(int(h_sql[k]) == int(h_mongo[k]) for k in keys),
        f"{h_sql['games']} meetings, {h_sql['a_wins']}–{h_sql['b_wins']}–{h_sql['draws']}",
        timed(lambda: sql(H2H_SQL, (rival["player_id"], player["player_id"])), runs, warmup),
        timed(lambda: list(matches.aggregate(H2H_MONGO(a_uuid, b_uuid))), runs, warmup),
        sql_plan(conn, H2H_SQL, (rival["player_id"], player["player_id"])),
        mongo_plan(mongo, "matches", H2H_MONGO(a_uuid, b_uuid)),
        size(H2H_SQL), size(H2H_MONGO(a_uuid, b_uuid))))

    # 2. top rivals
    r_sql = [(r["uuid"], int(r["games"]), int(r["wins"]), int(r["losses"])) for r in sql(RIVALS_SQL, (player["player_id"],))]
    pipe = rivalry.rivals_pipeline(a_uuid)
    r_mongo = [(r["_id"], r["games"], r["wins"], r["losses"]) for r in matches.aggregate(pipe)]
    tasks.append(Comparison(
        f"Top 10 rivals of {player['nickname']}", "self-join → GROUP BY opponent → ORDER BY → LIMIT",
        "$match → $unwind players → $group by opponent → $sort → $limit",
        r_sql == r_mongo, f"top: {r_mongo[0][1]} games" if r_mongo else "none",
        timed(lambda: sql(RIVALS_SQL, (player["player_id"],)), runs, warmup),
        timed(lambda: list(matches.aggregate(pipe)), runs, warmup),
        sql_plan(conn, RIVALS_SQL, (player["player_id"],)), mongo_plan(mongo, "matches", pipe),
        size(RIVALS_SQL), size(pipe)))

    # 3. Elo moving average + running peak
    e_sql = sql(ELO_SQL, (player["player_id"],))
    e_mongo = list(mongo["elo_history"].aggregate(ELO_MONGO(a_uuid)))
    # MariaDB returns AVG() as DECIMAL rounded to 4 places (div_precision_increment); MongoDB as a full double.
    same = (len(e_sql) == len(e_mongo) and all(
        int(s["elo_after"]) == m["elo"] and int(s["peak"]) == m["peak"] and abs(float(s["avg_elo"]) - m["avg"]) <= 5e-5
        for s, m in zip(e_sql, e_mongo)))
    tasks.append(Comparison(
        f"Elo {WINDOW}-match moving average and running peak, per season ({player['nickname']})",
        "AVG() / MAX() OVER (PARTITION BY season ROWS …) on the view",
        "$setWindowFields on the elo_history time-series collection",
        same, f"{len(e_sql):,} points; averages equal to MariaDB's 4 decimal places",
        timed(lambda: sql(ELO_SQL, (player["player_id"],)), runs, warmup),
        timed(lambda: list(mongo["elo_history"].aggregate(ELO_MONGO(a_uuid))), runs, warmup),
        sql_plan(conn, ELO_SQL, (player["player_id"],)), mongo_plan(mongo, "elo_history", ELO_MONGO(a_uuid)),
        size(ELO_SQL), size(ELO_MONGO(a_uuid))))

    # 4. filter counts for the match history
    f_sql = facet_counts_from_sql(sql(FACET_SQL, (player["player_id"],)))
    f_mongo = facets.counts(matches, uuid=a_uuid, match_type=TYPES["all"])
    fpipe = facets.pipeline(uuid=a_uuid)
    tasks.append(Comparison(
        f"Match-history filter counts ({player['nickname']}: season, type, result)",
        "GROUP BY season, type, outcome, forfeited → roll up", "$group into cells → $facet per filter",
        f_sql == f_mongo, f"{f_mongo['result']['']:,} matches in {len(f_mongo['season'])} seasons",
        timed(lambda: sql(FACET_SQL, (player["player_id"],)), runs, warmup),
        timed(lambda: list(matches.aggregate(fpipe)), runs, warmup),
        sql_plan(conn, FACET_SQL, (player["player_id"],)), mongo_plan(mongo, "matches", fpipe),
        size(FACET_SQL), size(fpipe)))

    out = ["## 7. The same question in both databases\n",
           "Each question is implemented once in SQL and once as a MongoDB aggregation and run on the same data. "
           "\"Same result\" compares every returned value. Plans come from `EXPLAIN` and `explain(\"executionStats\")`; "
           "query size is the length of the query text with whitespace collapsed (the MongoDB pipeline as JSON).\n",
           "| Question | Same result | MariaDB median ms | MongoDB median ms | Query size SQL / MongoDB (chars) |",
           "|---|---|---|---|---|"]
    for t in tasks:
        out.append(f"| {t.question} | {'yes' if t.same else '**NO**'} ({t.result}) | {t.sql_time['median']:.2f} | "
                   f"{t.mongo_time['median']:.2f} | {t.sql_size:,} / {t.mongo_size:,} |")
    out.append("\n**How each engine ran it**\n")
    out.append("| Question | MariaDB approach and plan | MongoDB approach and plan |\n|---|---|---|")
    for t in tasks:
        out.append(f"| {t.question} | {t.sql_approach}. {t.sql_plan} | {t.mongo_approach}. {t.mongo_plan} |")
    out.append("")
    for line in out:
        print(line)
    return out
