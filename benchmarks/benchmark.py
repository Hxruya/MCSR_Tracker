"""SQL vs NoSQL benchmarks -> benchmarks/benchmarks.md (results section only; the discussion is kept).

    python benchmarks/benchmark.py [--player edcr] [--rival Infume] [--runs 30] [--warmup 3]

Read-only: uses index hints (IGNORE INDEX) instead of dropping indexes.
"""
import argparse
import json
import platform
import random
import re
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # project root
sys.path.insert(0, str(ROOT))

from app import db_mongo, db_sql  # noqa: E402
from app.importer import sync  # noqa: E402
from benchmarks import engine_comparison  # noqa: E402

OUT = ROOT / "benchmarks" / "benchmarks.md"
START, END = "<!-- BENCHMARK RESULTS START -->", "<!-- BENCHMARK RESULTS END -->"


def timed(fn, runs, warmup):
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(runs):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1000)
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(round(0.95 * len(samples))) - 1)]
    return {"median": statistics.median(samples), "p95": p95, "min": samples[0], "max": samples[-1]}


def row(label, r):
    return f"| {label} | {r['median']:.3f} | {r['p95']:.3f} | {r['min']:.3f} | {r['max']:.3f} |"


HEADER = "| Variant | Median ms | p95 ms | Min ms | Max ms |\n|---|---|---|---|---|"


def sql_all(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def explain(conn, sql, params=()):
    rows = sql_all(conn, "EXPLAIN " + sql, params)
    cols = ["table", "type", "possible_keys", "key", "rows", "Extra"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(c)) for c in cols) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------- benchmark bodies

SQL_MATCH = (
    "SELECT m.match_id, m.season_id, m.match_type, m.played_at, m.duration_ms, m.result_type, "
    "p.uuid, p.nickname, mp.outcome, mp.elo_before, mp.elo_change, mp.finish_time_ms "
    "FROM matches m JOIN match_players mp ON mp.match_id = m.match_id "
    "JOIN players p ON p.player_id = mp.player_id WHERE m.match_id = %s")

SQL_STATS = "SELECT * FROM v_player_stats_lifetime WHERE player_id = %s"


def mongo_stats_pipeline(uuid):
    """Same rules as v_player_stats_lifetime, computed from the Mongo documents."""
    is_winner = {"$eq": ["$result.uuid", uuid]}
    no_winner = {"$eq": [{"$ifNull": ["$result.uuid", None]}, None]}
    return [
        {"$match": {"players.uuid": uuid, "match_type": 2, "raw.decayed": {"$ne": True}}},
        {"$project": {
            "win": {"$cond": [is_winner, 1, 0]},
            "draw": {"$cond": [no_winner, 1, 0]},
            "loss": {"$cond": [{"$or": [is_winner, no_winner]}, 0, 1]},
            # the forfeit rule of app/importer/transform.py: a forfeited 1v1 (unique players) whose winner is listed
            "ff": {"$cond": [{"$and": ["$forfeited", {"$not": [no_winner]}, {"$not": [is_winner]},
                                       {"$eq": [{"$size": {"$setUnion": ["$players.uuid", []]}}, 2]},
                                       {"$in": ["$result.uuid", "$players.uuid"]}]}, 1, 0]},
            "change": {"$first": {"$map": {
                "input": {"$filter": {"input": "$raw.changes", "cond": {"$eq": ["$$this.uuid", uuid]}}},
                "in": "$$this.change"}}},
        }},
        {"$group": {"_id": None, "games_played": {"$sum": 1}, "wins": {"$sum": "$win"},
                    "losses": {"$sum": "$loss"}, "draws": {"$sum": "$draw"}, "forfeits": {"$sum": "$ff"},
                    "total_elo_change": {"$sum": "$change"}}},
    ]


STRUCTURE_PIPELINE = [
    {"$match": {"match_type": 2, "seed.overworld": {"$ne": None}}},
    {"$project": {"_id": 0, "s": ["$seed.overworld", "$seed.nether"]}},
    {"$unwind": "$s"},
    {"$group": {"_id": "$s", "n": {"$sum": 1}}},
    {"$sort": {"n": -1}},
]


# ---------------------------------------------------------------- throughput

THREADS = (1, 4, 8)


def run_concurrent(make_op, threads, seconds):
    """Each thread gets its own op (own SQL connection); returns (ops per second, mean ms per op)."""
    ops = [make_op(i) for i in range(threads)]
    counts, lat = [0] * threads, [0.0] * threads
    start_gate = threading.Barrier(threads + 1)
    deadline = [0.0]

    def worker(i):
        start_gate.wait()
        while time.perf_counter() < deadline[0]:
            t = time.perf_counter()
            ops[i]()
            lat[i] += time.perf_counter() - t
            counts[i] += 1

    pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for p in pool:
        p.start()
    deadline[0] = time.perf_counter() + seconds
    start_gate.wait()
    for p in pool:
        p.join()
    total = sum(counts)
    return total / seconds, (sum(lat) / total * 1000 if total else float("nan"))


def throughput_section(player, ids, seconds):
    from app.config import Config
    mongo = sync.mongo_database()
    coll = mongo["matches"]
    conns = []

    def sql_op(query, param_fn):
        def make(i):
            c = db_sql.connect()
            c.autocommit(True)
            conns.append(c)
            rng = random.Random(i)
            def op():
                with c.cursor() as cur:
                    cur.execute(query, param_fn(rng))
                    cur.fetchall()
            return op
        return make

    def mongo_op(fn):
        def make(i):
            rng = random.Random(i)
            return lambda: fn(rng)
        return make

    pipeline = mongo_stats_pipeline(player["uuid"])
    workloads = [
        ("Fetch one match by id", "MariaDB 3-table join", sql_op(SQL_MATCH, lambda r: (r.choice(ids),)),
         "MongoDB find_one", mongo_op(lambda r: coll.find_one({"_id": r.choice(ids)}, {"raw": 0}))),
        (f"Lifetime stats ({player['nickname']})", "MariaDB view", sql_op(SQL_STATS, lambda r: (player["player_id"],)),
         "MongoDB aggregate", mongo_op(lambda r: list(coll.aggregate(pipeline)))),
    ]
    lines = ["## 6. Throughput under concurrent clients\n",
             f"Each cell: operations per second over {seconds:g} s with N client threads (each with its own "
             "connection), and mean latency per operation. Random ids are the same for both databases.\n",
             "| Workload | Database | " + " | ".join(f"{n} thread{'s' if n > 1 else ''}" for n in THREADS) + " |",
             "|---|---|" + "---|" * len(THREADS)]
    for title, sql_label, sql_make, mongo_label, mongo_make in workloads:
        for label, make in ((sql_label, sql_make), (mongo_label, mongo_make)):
            cells = []
            for n in THREADS:
                rate, mean = run_concurrent(make, n, seconds)
                cells.append(f"{rate:,.0f}/s ({mean:.2f} ms)")
                for c in conns:
                    c.close()
                conns.clear()
            lines.append(f"| {title} | {label} | " + " | ".join(cells) + " |")
            print(lines[-1])

    # writes: a session-only TEMPORARY table and a collection in the throwaway test database
    rows = 20000
    data = [(i, random.randint(1, 5000), random.randint(-40, 40), f"note {i}") for i in range(rows)]
    wlines = ["\n**Write throughput** (20,000 rows/documents; MariaDB `TEMPORARY` table, MongoDB collection in the "
              "test database, both dropped afterwards):\n", "| Method | MariaDB | MongoDB |", "|---|---|---|"]
    c = db_sql.connect()
    try:
        with c.cursor() as cur:
            cur.execute("CREATE TEMPORARY TABLE bench_writes (id INT PRIMARY KEY, player_id INT NOT NULL, "
                        "elo_change INT NOT NULL, note VARCHAR(32) NOT NULL) ENGINE=InnoDB")
        tmp = db_mongo.client(Config.MONGO_ADMIN_URI)[Config.MONGO_TEST_DB]["bench_writes"]   # created and dropped
        tmp.drop()
        single = 2000
        t = time.perf_counter()
        with c.cursor() as cur:
            for r in data[:single]:
                cur.execute("INSERT INTO bench_writes VALUES (%s, %s, %s, %s)", r)
                c.commit()  # one transaction per row, like a web request
        sql_single = single / (time.perf_counter() - t)
        t = time.perf_counter()
        for r in data[:single]:
            tmp.insert_one({"_id": r[0], "player_id": r[1], "elo_change": r[2], "note": r[3]})
        mongo_single = single / (time.perf_counter() - t)
        with c.cursor() as cur:
            cur.execute("DELETE FROM bench_writes")
        c.commit()
        tmp.delete_many({})
        t = time.perf_counter()
        with c.cursor() as cur:
            for i in range(0, rows, 1000):
                cur.executemany("INSERT INTO bench_writes VALUES (%s, %s, %s, %s)", data[i:i + 1000])
        c.commit()
        sql_batch = rows / (time.perf_counter() - t)
        t = time.perf_counter()
        for i in range(0, rows, 1000):
            tmp.insert_many([{"_id": r[0], "player_id": r[1], "elo_change": r[2], "note": r[3]}
                             for r in data[i:i + 1000]], ordered=False)
        mongo_batch = rows / (time.perf_counter() - t)
        tmp.drop()
    finally:
        c.close()  # the TEMPORARY table disappears with the session
    wlines += [f"| Single-row insert, committed each time ({single:,} rows) | {sql_single:,.0f} rows/s | {mongo_single:,.0f} docs/s |",
               f"| Batched insert, 1,000 per call ({rows:,} rows) | {sql_batch:,.0f} rows/s | {mongo_batch:,.0f} docs/s |"]
    print("\n".join(wlines[-2:]))
    return lines + wlines + [""]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--player", default="edcr")
    ap.add_argument("--rival", default="Infume", help="second player for the head-to-head comparison")
    ap.add_argument("--runs", type=int, default=30)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=4.0, help="duration of each throughput run")
    ap.add_argument("--no-throughput", action="store_true", help="skip section 6")
    args = ap.parse_args()
    runs, warmup = args.runs, args.warmup

    # EXPLAIN over a view needs SHOW VIEW, which the restricted app account doesn't have; the queries are the same
    conn = db_sql.connect(admin=True)
    conn.autocommit(True)  # each read sees fresh data; no long snapshot
    mongo = sync.mongo_database()
    coll = mongo["matches"]

    size = {t: sql_all(conn, f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in ("players", "matches", "match_players")}
    size["mongo_matches"] = coll.count_documents({})
    version = sql_all(conn, "SELECT VERSION() AS v")[0]["v"]
    mongo_version = mongo.command("buildInfo")["version"]
    player = sql_all(conn, "SELECT player_id, uuid, nickname FROM players WHERE nickname = %s LIMIT 1", (args.player,))
    if not player:
        sys.exit(f"Player {args.player!r} is not stored; import them first.")
    player = player[0]

    rng = random.Random(2003)
    ids = [r["match_id"] for r in sql_all(conn, "SELECT match_id FROM matches")]
    sample = [rng.choice(ids) for _ in range(runs + warmup)]
    out = []

    # 1. Fetch one match by id ------------------------------------------------
    it_sql, it_mongo, it_slim = iter(sample), iter(sample), iter(sample)
    r_sql = timed(lambda: sql_all(conn, SQL_MATCH, (next(it_sql),)), runs, warmup)
    r_mongo = timed(lambda: coll.find_one({"_id": next(it_mongo)}), runs, warmup)
    r_mongo_slim = timed(lambda: coll.find_one({"_id": next(it_slim)}, {"raw": 0}), runs, warmup)
    out.append("## 1. Fetch one match by id\n")
    out.append(f"Random match ids (same {runs + warmup} ids for both databases, seed 2003).\n")
    out.append(HEADER)
    out += [row("MariaDB: matches ⋈ match_players ⋈ players (PK lookups)", r_sql),
            row("MongoDB: find_one({_id}) (full document incl. raw)", r_mongo),
            row("MongoDB: find_one({_id}, {raw: 0})", r_mongo_slim)]
    out.append("\n" + explain(conn, SQL_MATCH, (sample[0],)) + "\n")

    # 2. Count matches per structure type -------------------------------------
    r_agg = timed(lambda: list(coll.aggregate(STRUCTURE_PIPELINE)), runs, warmup)
    result = list(coll.aggregate(STRUCTURE_PIPELINE))
    out.append("## 2. Count ranked matches per structure type\n")
    out.append("MongoDB only: seed data is stored only in MongoDB, so there is no SQL "
               "equivalent without duplicating it.\n")
    out.append(HEADER)
    out.append(row("MongoDB: $match → $project → $unwind → $group → $sort", r_agg))
    out.append("\nResult: " + ", ".join(f"{d['_id']} {d['n']}" for d in result) + "\n")
    out.append("```json\n" + json.dumps(STRUCTURE_PIPELINE, indent=1) + "\n```\n")
    plan = coll.find({"match_type": 2, "seed.overworld": "VILLAGE"}).explain()
    winning = json.dumps(plan["queryPlanner"]["winningPlan"])
    out.append(f"Index check: `find({{seed.overworld: 'VILLAGE'}})` winning plan uses "
               f"{'IXSCAN ' + ', '.join(sorted(set(re.findall(r'ix_[a-z_]+', winning)))) if 'IXSCAN' in winning else 'COLLSCAN'}.\n")

    # 3. Player lifetime stats ------------------------------------------------
    pipeline = mongo_stats_pipeline(player["uuid"])
    r_view = timed(lambda: sql_all(conn, SQL_STATS, (player["player_id"],)), runs, warmup)
    r_magg = timed(lambda: list(coll.aggregate(pipeline)), runs, warmup)
    sql_res = sql_all(conn, SQL_STATS, (player["player_id"],))[0]
    mongo_res = list(coll.aggregate(pipeline))[0]
    keys = ("games_played", "wins", "losses", "draws", "forfeits", "total_elo_change")
    same = all(int(sql_res[k] or 0) == int(mongo_res[k] or 0) for k in keys)
    out.append(f"## 3. Player lifetime stats ({player['nickname']})\n")
    out.append(HEADER)
    out += [row("MariaDB: v_player_stats_lifetime (index ix_mp_player_match)", r_view),
            row("MongoDB: aggregate over players.uuid (index ix_players_type_played)", r_magg)]
    out.append("\n| | " + " | ".join(keys) + " |\n|---|" + "---|" * len(keys))
    out.append("| MariaDB | " + " | ".join(str(sql_res[k]) for k in keys) + " |")
    out.append("| MongoDB | " + " | ".join(str(mongo_res[k]) for k in keys) + " |")
    out.append(f"\nResults identical: **{'yes' if same else 'NO'}**.\n")
    out.append("```json\n" + json.dumps(pipeline, indent=1) + "\n```\n")

    # 4. Nickname search: index vs no index -----------------------------------
    names = [r["nickname"] for r in sql_all(conn, "SELECT nickname FROM players")]
    picks = [rng.choice(names) for _ in range(runs + warmup)]
    q_idx = "SELECT player_id, uuid FROM players WHERE nickname = %s"
    q_noidx = "SELECT player_id, uuid FROM players IGNORE INDEX (ix_players_nickname) WHERE nickname = %s"
    q_prefix = "SELECT player_id, uuid FROM players WHERE nickname LIKE %s"
    q_prefix_no = "SELECT player_id, uuid FROM players IGNORE INDEX (ix_players_nickname) WHERE nickname LIKE %s"
    q_contains = "SELECT player_id, uuid FROM players WHERE nickname LIKE %s"
    res = {}
    for label, q, mk in [("exact =, with index", q_idx, lambda n: n), ("exact =, IGNORE INDEX", q_noidx, lambda n: n),
                         ("prefix LIKE 'abc%', with index", q_prefix, lambda n: n[:3] + "%"),
                         ("prefix LIKE 'abc%', IGNORE INDEX", q_prefix_no, lambda n: n[:3] + "%"),
                         ("contains LIKE '%abc%' (search page)", q_contains, lambda n: "%" + n[:3] + "%")]:
        it = iter(picks)
        res[label] = timed(lambda: sql_all(conn, q, (mk(next(it)),)), runs, warmup)
    out.append(f"## 4. Player search by nickname ({size['players']} players)\n")
    out.append(HEADER)
    out += [row(k, v) for k, v in res.items()]
    out.append("\n**EXPLAIN without the index** (`IGNORE INDEX`):\n\n" + explain(conn, q_noidx, ("edcr",)))
    out.append("\n**EXPLAIN with `ix_players_nickname`**:\n\n" + explain(conn, q_idx, ("edcr",)))
    out.append("\n**EXPLAIN prefix LIKE with index**:\n\n" + explain(conn, q_prefix, ("edc%",)))
    out.append("\n**EXPLAIN contains LIKE '%x%'** (cannot use the B-tree index):\n\n" + explain(conn, q_contains, ("%dcr%",)))
    out.append("")

    # 5. EXPLAIN evidence for the other indexes -------------
    out.append("## 5. EXPLAIN evidence for the remaining indexes\n")
    history = ("SELECT m.match_id FROM match_players mp JOIN matches m ON m.match_id = mp.match_id "
               "WHERE mp.player_id = %s ORDER BY m.played_at DESC LIMIT 20")
    season_q = "SELECT COUNT(*) FROM matches WHERE season_id = %s AND played_at >= %s"
    date_q = "SELECT match_id FROM matches WHERE played_at >= %s AND played_at < %s"
    fastest_q = "SELECT match_id FROM match_players WHERE finish_time_ms IS NOT NULL ORDER BY finish_time_ms LIMIT 20"
    for title, q, p in [("Player history (`ix_mp_player_match`)", history, (player["player_id"],)),
                        ("Season + date (`ix_matches_season_played`)", season_q, (11, "2026-06-01")),
                        ("Date range (`ix_matches_played`)", date_q, ("2026-09-01", "2026-09-02")),
                        ("Fastest times (`ix_mp_finish`)", fastest_q, ())]:
        out.append(f"**{title}**\n\n" + explain(conn, q, p) + "\n")

    # 6. Throughput ---------------------------------------------
    if not args.no_throughput:
        out += throughput_section(player, ids, args.seconds)

    # 7. Same question, both databases --------------------------
    rival = sql_all(conn, "SELECT player_id, uuid, nickname FROM players WHERE nickname = %s LIMIT 1", (args.rival,))
    if rival:
        out += engine_comparison.section(conn, mongo, player, rival[0], timed, runs, warmup)

    header = (f"_Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `benchmarks/benchmark.py`: "
              f"{runs} timed runs after {warmup} warm-up runs; times include fetching all rows to Python._\n\n"
              f"**Dataset:** {size['matches']:,} matches (MariaDB) / {size['mongo_matches']:,} documents (MongoDB), "
              f"{size['match_players']:,} match_players rows, {size['players']:,} players.  \n"
              f"**Environment:** MariaDB {version}, MongoDB {mongo_version}, Python {platform.python_version()}, "
              f"PyMySQL / PyMongo, {platform.system()} {platform.release()}, single local machine, both servers on "
              f"default configuration.\n")
    results = START + "\n" + header + "\n" + "\n".join(out) + "\n" + END
    if OUT.exists() and START in OUT.read_text(encoding="utf-8"):
        text = OUT.read_text(encoding="utf-8")
        text = text[:text.index(START)] + results + text[text.index(END) + len(END):]
    else:
        text = "# Benchmarks: MariaDB vs MongoDB\n\n" + results + "\n\n## Discussion\n\n_TODO_\n"
    OUT.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT}")
    print("\n".join(line for line in out if line.startswith("| ") and "ms" not in line[:6])[:3000])


if __name__ == "__main__":
    main()
