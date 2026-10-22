# Benchmarks: MariaDB vs MongoDB

<!-- BENCHMARK RESULTS START -->
_Generated 2026-10-21 06:51 UTC by `benchmarks/benchmark.py`: 30 timed runs after 3 warm-up runs; times include fetching all rows to Python._

**Dataset:** 47,219 matches (MariaDB) / 47,219 documents (MongoDB), 116,494 match_players rows, 3,740 players.  
**Environment:** MariaDB 13.0.2-MariaDB, MongoDB 9.0.2, Python 3.14.7, PyMySQL / PyMongo, Windows 11, single local machine, both servers on default configuration.

## 1. Fetch one match by id

Random match ids (same 33 ids for both databases, seed 2003).

| Variant | Median ms | p95 ms | Min ms | Max ms |
|---|---|---|---|---|
| MariaDB: matches ⋈ match_players ⋈ players (PK lookups) | 0.230 | 0.342 | 0.193 | 0.416 |
| MongoDB: find_one({_id}) (full document incl. raw) | 0.217 | 0.389 | 0.186 | 0.641 |
| MongoDB: find_one({_id}, {raw: 0}) | 0.276 | 0.385 | 0.239 | 0.535 |

| table | type | possible_keys | key | rows | Extra |
|---|---|---|---|---|---|
| m | const | PRIMARY | PRIMARY | 1 |  |
| mp | ref | PRIMARY,ix_mp_player_match | PRIMARY | 2 |  |
| p | eq_ref | PRIMARY | PRIMARY | 1 |  |

## 2. Count ranked matches per structure type

MongoDB only: seed data is stored only in MongoDB, so there is no SQL equivalent without duplicating it.

| Variant | Median ms | p95 ms | Min ms | Max ms |
|---|---|---|---|---|
| MongoDB: $match → $project → $unwind → $group → $sort | 209.905 | 220.422 | 181.542 | 225.897 |

Result: BURIED_TREASURE 7715, HOUSING 6585, SHIPWRECK 6583, BRIDGE 6542, TREASURE 6484, STABLES 6435, VILLAGE 5125, DESERT_TEMPLE 4900, RUINED_PORTAL 4787, None 3064

```json
[
 {
  "$match": {
   "match_type": 2,
   "seed.overworld": {
    "$ne": null
   }
  }
 },
 {
  "$project": {
   "_id": 0,
   "s": [
    "$seed.overworld",
    "$seed.nether"
   ]
  }
 },
 {
  "$unwind": "$s"
 },
 {
  "$group": {
   "_id": "$s",
   "n": {
    "$sum": 1
   }
  }
 },
 {
  "$sort": {
   "n": -1
  }
 }
]
```

Index check: `find({seed.overworld: 'VILLAGE'})` winning plan uses IXSCAN ix_seed_overworld.

## 3. Player lifetime stats (edcr)

| Variant | Median ms | p95 ms | Min ms | Max ms |
|---|---|---|---|---|
| MariaDB: v_player_stats_lifetime (index ix_mp_player_match) | 10.299 | 12.244 | 10.183 | 12.317 |
| MongoDB: aggregate over players.uuid (index ix_players_type_played) | 83.528 | 84.934 | 33.756 | 86.043 |

| | games_played | wins | losses | draws | forfeits | total_elo_change |
|---|---|---|---|---|---|---|
| MariaDB | 5572 | 3638 | 1833 | 101 | 313 | 6646 |
| MongoDB | 5572 | 3638 | 1833 | 101 | 313 | 6646 |

Results identical: **yes**.

```json
[
 {
  "$match": {
   "players.uuid": "635f35ee69ed4f0c94ff26ece4818956",
   "match_type": 2,
   "raw.decayed": {
    "$ne": true
   }
  }
 },
 {
  "$project": {
   "win": {
    "$cond": [
     {
      "$eq": [
       "$result.uuid",
       "635f35ee69ed4f0c94ff26ece4818956"
      ]
     },
     1,
     0
    ]
   },
   "draw": {
    "$cond": [
     {
      "$eq": [
       {
        "$ifNull": [
         "$result.uuid",
         null
        ]
       },
       null
      ]
     },
     1,
     0
    ]
   },
   "loss": {
    "$cond": [
     {
      "$or": [
       {
        "$eq": [
         "$result.uuid",
         "635f35ee69ed4f0c94ff26ece4818956"
        ]
       },
       {
        "$eq": [
         {
          "$ifNull": [
           "$result.uuid",
           null
          ]
         },
         null
        ]
       }
      ]
     },
     0,
     1
    ]
   },
   "ff": {
    "$cond": [
     {
      "$and": [
       "$forfeited",
       {
        "$not": [
         {
          "$eq": [
           {
            "$ifNull": [
             "$result.uuid",
             null
            ]
           },
           null
          ]
         }
        ]
       },
       {
        "$not": [
         {
          "$eq": [
           "$result.uuid",
           "635f35ee69ed4f0c94ff26ece4818956"
          ]
         }
        ]
       },
       {
        "$eq": [
         {
          "$size": {
           "$setUnion": [
            "$players.uuid",
            []
           ]
          }
         },
         2
        ]
       },
       {
        "$in": [
         "$result.uuid",
         "$players.uuid"
        ]
       }
      ]
     },
     1,
     0
    ]
   },
   "change": {
    "$first": {
     "$map": {
      "input": {
       "$filter": {
        "input": "$raw.changes",
        "cond": {
         "$eq": [
          "$$this.uuid",
          "635f35ee69ed4f0c94ff26ece4818956"
         ]
        }
       }
      },
      "in": "$$this.change"
     }
    }
   }
  }
 },
 {
  "$group": {
   "_id": null,
   "games_played": {
    "$sum": 1
   },
   "wins": {
    "$sum": "$win"
   },
   "losses": {
    "$sum": "$loss"
   },
   "draws": {
    "$sum": "$draw"
   },
   "forfeits": {
    "$sum": "$ff"
   },
   "total_elo_change": {
    "$sum": "$change"
   }
  }
 }
]
```

## 4. Player search by nickname (3740 players)

| Variant | Median ms | p95 ms | Min ms | Max ms |
|---|---|---|---|---|
| exact =, with index | 0.093 | 0.126 | 0.089 | 0.276 |
| exact =, IGNORE INDEX | 0.625 | 0.664 | 0.585 | 0.675 |
| prefix LIKE 'abc%', with index | 0.105 | 0.314 | 0.090 | 0.532 |
| prefix LIKE 'abc%', IGNORE INDEX | 0.630 | 0.704 | 0.560 | 0.865 |
| contains LIKE '%abc%' (search page) | 0.801 | 0.943 | 0.734 | 1.006 |

**EXPLAIN without the index** (`IGNORE INDEX`):

| table | type | possible_keys | key | rows | Extra |
|---|---|---|---|---|---|
| players | ALL | None | None | 3720 | Using where |

**EXPLAIN with `ix_players_nickname`**:

| table | type | possible_keys | key | rows | Extra |
|---|---|---|---|---|---|
| players | ref | ix_players_nickname | ix_players_nickname | 1 | Using index condition |

**EXPLAIN prefix LIKE with index**:

| table | type | possible_keys | key | rows | Extra |
|---|---|---|---|---|---|
| players | range | ix_players_nickname | ix_players_nickname | 5 | Using index condition |

**EXPLAIN contains LIKE '%x%'** (cannot use the B-tree index):

| table | type | possible_keys | key | rows | Extra |
|---|---|---|---|---|---|
| players | ALL | None | None | 3720 | Using where |

## 5. EXPLAIN evidence for the remaining indexes

**Player history (`ix_mp_player_match`)**

| table | type | possible_keys | key | rows | Extra |
|---|---|---|---|---|---|
| mp | ref | PRIMARY,ix_mp_player_match | ix_mp_player_match | 8106 | Using index; Using temporary; Using filesort |
| m | eq_ref | PRIMARY | PRIMARY | 1 |  |

**Season + date (`ix_matches_season_played`)**

| table | type | possible_keys | key | rows | Extra |
|---|---|---|---|---|---|
| matches | range | ix_matches_season_played,ix_matches_played | ix_matches_season_played | 9758 | Using where; Using index |

**Date range (`ix_matches_played`)**

| table | type | possible_keys | key | rows | Extra |
|---|---|---|---|---|---|
| matches | range | ix_matches_played | ix_matches_played | 78 | Using where; Using index |

**Fastest times (`ix_mp_finish`)**

| table | type | possible_keys | key | rows | Extra |
|---|---|---|---|---|---|
| match_players | range | ix_mp_finish | ix_mp_finish | 79670 | Using where; Using index |

## 6. Throughput under concurrent clients

Each cell: operations per second over 4 s with N client threads (each with its own connection), and mean latency per operation. Random ids are the same for both databases.

| Workload | Database | 1 thread | 4 threads | 8 threads |
|---|---|---|---|---|
| Fetch one match by id | MariaDB 3-table join | 3,657/s (0.27 ms) | 5,920/s (0.68 ms) | 5,822/s (1.37 ms) |
| Fetch one match by id | MongoDB find_one | 4,771/s (0.21 ms) | 6,460/s (0.62 ms) | 6,452/s (1.24 ms) |
| Lifetime stats (edcr) | MariaDB view | 90/s (11.14 ms) | 279/s (14.33 ms) | 428/s (18.75 ms) |
| Lifetime stats (edcr) | MongoDB aggregate | 12/s (80.13 ms) | 50/s (81.02 ms) | 95/s (84.61 ms) |

**Write throughput** (20,000 rows/documents; MariaDB `TEMPORARY` table, MongoDB collection in the test database, both dropped afterwards):

| Method | MariaDB | MongoDB |
|---|---|---|
| Single-row insert, committed each time (2,000 rows) | 6,022 rows/s | 5,860 docs/s |
| Batched insert, 1,000 per call (20,000 rows) | 278,746 rows/s | 207,414 docs/s |

## 7. The same question in both databases

Each question is implemented once in SQL and once as a MongoDB aggregation and run on the same data. "Same result" compares every returned value. Plans come from `EXPLAIN` and `explain("executionStats")`; query size is the length of the query text with whitespace collapsed (the MongoDB pipeline as JSON).

| Question | Same result | MariaDB median ms | MongoDB median ms | Query size SQL / MongoDB (chars) |
|---|---|---|---|---|
| Head to head, edcr vs Infume | yes (151 meetings, 56–93–2) | 12.25 | 21.03 | 266 / 506 |
| Top 10 rivals of edcr | yes (top: 369 games) | 22.28 | 49.75 | 404 / 659 |
| Elo 20-match moving average and running peak, per season (edcr) | yes (5,542 points; averages equal to MariaDB's 4 decimal places) | 88.91 | 39.07 | 378 / 373 |
| Match-history filter counts (edcr: season, type, result) | yes (8,106 matches in 7 seasons) | 11.09 | 65.99 | 223 / 1,156 |

**How each engine ran it**

| Question | MariaDB approach and plan | MongoDB approach and plan |
|---|---|---|
| Head to head, edcr vs Infume | self-join of v_ranked_match_players on match_id. indexes: PRIMARY, ix_mp_player_match | $match players.uuid $all [A, B] → $group. IXSCAN ix_players_type_played; keys 5,142, docs 5,142 |
| Top 10 rivals of edcr | self-join → GROUP BY opponent → ORDER BY → LIMIT. indexes: PRIMARY, ix_mp_player_match; Using filesort; Using temporary | $match → $unwind players → $group by opponent → $sort → $limit. IXSCAN ix_players_type_played; keys 5,604, docs 5,604 |
| Elo 20-match moving average and running peak, per season (edcr) | AVG() / MAX() OVER (PARTITION BY season ROWS …) on the view. indexes: PRIMARY, ix_mp_player_match; Using filesort; Using temporary | $setWindowFields on the elo_history time-series collection. IXSCAN player_1_played_at_1; keys 24, docs 24 |
| Match-history filter counts (edcr: season, type, result) | GROUP BY season, type, outcome, forfeited → roll up. indexes: PRIMARY, ix_mp_player_match; Using filesort; Using temporary | $group into cells → $facet per filter. IXSCAN ix_players_uuid; keys 8,106, docs 8,106 |

<!-- BENCHMARK RESULTS END -->

## Discussion

These notes match the results above (the run of 2026-10-21 on the bundled dataset exported 17 October, 47,219 matches). Where a number changed because of a schema change, the 17 October figure is given for comparison. Re-running the script updates the numbers above but not this text.

### Lookups and aggregations (sections 1 to 4)

Fetching one match by id takes about 0.2 ms in both stores (MariaDB 0.23 ms, MongoDB 0.22 ms). MariaDB does three primary-key lookups (EXPLAIN: `const`, `ref`, `eq_ref`) and MongoDB one `_id` lookup. At this size round-trip and driver overhead dominate, so the gap is within the noise (limitation 3). Leaving out the large `raw` field means less BSON to send and decode, but for a single match the effect is inside the noise: 0.16 ms without it on 17 October, 0.28 ms in this run. One MongoDB read returns the whole match, with its seed, videos and players, without a join.

The structure count runs only in MongoDB and takes about 210 ms. It reads every ranked document that has a seed (29,110 of the 30,136 ranked matches). `$match` can use an index, but `$project`, `$unwind` and `$group` touch every document, so the time grows with the collection. "None 3,064" in the result is ranked matches with an overworld type but no bastion recorded. There is no SQL version because seed data lives only in MongoDB; copying seed types into SQL would make a second authoritative copy that could disagree with the first. The Seeds page caches its combination ranking instead (`app/cache.py`, cleared after every import), which brought repeat loads from about 480 ms to about 80 ms. The cache is a derived copy that can be rebuilt, which the design allows (see "One authoritative store per fact" in [mongo_model.md](../extras/mongo_model.md)).

Lifetime stats for edcr take 10.3 ms in MariaDB and 83.5 ms in MongoDB (MariaDB about 8 times faster), with identical results. The view reads edcr's 8,106 narrow `match_players` index entries through `ix_mp_player_match`, joins each to `matches` by primary key and keeps the 5,572 ranked, non-decay games. MongoDB finds edcr's 5,604 ranked documents through the multikey `(players.uuid, match_type, played_at)` index (before that index existed it read all 8,106 through `players.uuid`, in 91.6 ms), but loads each one in full (`raw` included) and evaluates `$filter`/`$cond` on it. For a filter followed by a group and count, narrow normalised rows are faster. Because both pipelines return the same numbers, they also cross-check the forfeit and draw rules.

With `ix_players_nickname`, an exact nickname lookup takes 0.09 ms instead of 0.63 ms: EXPLAIN goes from `type ALL` over an estimated 3,720 rows to `ref` reading 1 row. A prefix `LIKE 'abc%'` also uses the index (`range`, 0.11 ms instead of 0.63 ms). The search page's "contains" search, `LIKE '%abc%'`, can't use a B-tree index and scans the table in about 0.8 ms. That's fine for 3,740 players; a much larger table would need a FULLTEXT or n-gram index.

### Throughput (section 6)

Going from 1 to 8 client threads raises point-read throughput only 1.4 to 1.6 times (MariaDB 3,657 to 5,822/s, MongoDB 4,771 to 6,452/s), while latency per operation rises from about 0.21–0.27 ms to 1.2–1.4 ms. With queries this short, the bottleneck is the Python client (driver work runs under the GIL) and the shared laptop, not the databases.

Heavy aggregations scale better because the time is spent inside the server. MariaDB's stats view goes from 90 to 428/s (4.8 times) and MongoDB's aggregation from 12 to 95/s (7.9 times), because MongoDB runs concurrent aggregations on separate cores. MariaDB is still about 4.5 times ahead at 8 clients, for the same reason as in section 3.

A single insert committed on its own runs at about 6.0k rows/s in MariaDB and 5.9k docs/s in MongoDB. Batches of 1,000 reach about 279k rows/s and 207k docs/s, roughly 35 to 46 times faster, which is why the importer sends `match_players` rows with `executemany` and `restore_data.py` batches its inserts. The test table has no foreign keys or triggers, so real match imports (FK checks, triggers, two stores) are slower. The importer is limited by the API rate limit in any case.

### The same question in both databases (section 7)

All four questions give the same answers in both stores, down to every count, rival, Elo point and running peak. Writing each rule twice found a mismatch along the way: the forfeit rule only agreed once both versions counted unique players and required the winner to be listed. The one remaining difference is a type: MariaDB returns `AVG()` as a DECIMAL rounded to 4 places, MongoDB as a double.

MariaDB is faster on slices of relational rows: head to head 12.3 vs 21.0 ms, top rivals 22.3 vs 49.8 ms, filter counts 11.1 vs 66.0 ms. EXPLAIN shows it reading only the player's `match_players` rows through `ix_mp_player_match`. MongoDB uses an index too, but `explain()` shows it then reading every matching document in full (keys examined = documents examined), including the `raw` field it doesn't need. The compound index `(players.uuid, match_type, played_at desc)`, added for the profile's Top rivals, narrowed the two ranked-only questions: head to head now reads 5,142 documents instead of 7,899, and top rivals 5,604 instead of 8,106, taking MongoDB from 22.7 to 21.0 ms and from 63.9 to 49.8 ms (17 October run against this one). Filter counts cover every match type, so they still read all 8,106 documents through `ix_players_uuid`. The gap to MariaDB is smaller but remains, because each document is still read whole.

MongoDB is faster on the Elo moving average and running peak: 39.1 ms vs 88.9 ms. The time-series collection keeps edcr's 5,542 points in 24 compressed buckets, so `explain()` shows 24 index keys and 24 bucket documents read. MariaDB reads 5,542 rows through the view's join and sorts them for the window partitions (`Using temporary; Using filesort`). The pipeline partitions by season only, as the app does, because `$match` already fixes the player; adding the player to the partition key gave the same output in about 61 ms (measured on the previous dataset). Insert order matters too. The live import adds points newest first, page by page, which left edcr's points in 441 part-filled buckets. The rebuild that a restore runs inserts them in time order, packing them into 24, and these numbers use that layout.

The SQL is shorter in every case (223 to 404 characters, against 373 to 1,156 for the pipelines). Pipelines are plain data structures, though, so the filter-count pipeline is built from one dictionary per active filter, which is awkward to do safely with SQL strings.

### Which store suits what

MariaDB handles aggregates over relational slices (stats, recent form with window functions, head to head, rivals), integrity (foreign keys, CHECK constraints, triggers, unique bookmarks, ownership checks) and joins across players, matches and users. MongoDB holds the variable, nested, read-mostly data: seeds whose shape differs by season, `variations` arrays queried with multikey indexes and `$unwind`, raw API payloads, cached timelines, whole matches fetched in one read, and the Elo time series.

The cost of using two stores is keeping them consistent. Matches share one id. MongoDB is written first, then one MariaDB transaction; if that transaction fails, the MongoDB document it just created is deleted again. Every write is an upsert, so a re-run repairs anything an interruption left behind, and `setup/check_consistency.py` checks that both stores hold the same matches. A production system would use a transactional outbox instead.

### Limitations

The throughput figures share all of these and also measure the Python client; a multi-process or compiled load generator would push both databases harder.

1. One laptop runs the client and both servers, sharing CPU, memory and disk. The absolute numbers don't transfer to other machines; compare the ratios.
2. The data fits in memory (InnoDB buffer pool, WiredTiger cache). After the warm-up runs every query is served from RAM, so these are hot-cache numbers and disk I/O is never measured. A cold start would be slower for both.
3. Sub-millisecond results are close to the timer and driver noise; differences under about 0.1 ms don't mean anything.
4. Times include Python driver overhead (PyMySQL is pure Python; PyMongo decodes BSON in C), which slightly favours MongoDB for large documents and MariaDB for many small rows.
5. Both servers run their default configuration. MariaDB's query cache is off by default, so repeated SQL isn't served from a result cache.
6. Each variant runs 30 times: the medians are stable, but p95 rests on only one or two samples.

## Query optimisation (profiling-driven)

Every main page was loaded through the app with a cursor that timed each SQL statement and reported the total SQL time per page and the slowest statements, taking the median of 5 to 7 loads on the dataset of the time (52,734 matches; the bundled dataset now has 47,219). Each slow statement was diagnosed with `EXPLAIN`, rewritten, checked to return identical rows (tests in `tests/test_optimisation.py`), and measured again.

| Page | SQL before | SQL after | Speed-up | What changed |
|---|---|---|---|---|
| Home | 110.4 ms | 13.3 ms | 8.3× | Summary table for "recently searched"; "fastest 5" walks `ix_mp_finish` (37.6 → 2.3 ms) |
| Match history (player) | 46.5 ms | 10.8 ms | 4.3× | Deferred join: pick the page's 20 ids first, then per-row subqueries (42 → 6 ms) |
| Profile, season 11 | 84.8 ms | 29.5 ms | 2.9× | Summary table + deferred join for recent matches |
| Profile, lifetime | 103.0 ms | 44.2 ms | 2.3× | Same |
| Fastest wins (lifetime) | 71.5 ms | 37.1 ms | 1.9× | `COUNT(*) OVER ()` in the same pass instead of a second scan of the view |
| Fastest wins (season 11) | 24.0 ms | 12.8 ms | 1.9× | Same |
| Seeds (all combinations) | 178.3 ms | 114.5 ms | 1.6× | Overall median as a second window in the same pass; overall average derived from per-combination sums |
| Match history (ranked, S11) | 17.4 ms | 9.9 ms | 1.8× | Deferred join |
| Compare, recent form | 57.7 / 27.7 ms | 57.1 / 27.1 ms | — | Not changed: filtered by date/season, so they can't use the summary table |
| Player search ("contains") | 2.4 ms | 2.3 ms | — | Not changed: a FULLTEXT index was considered, but at 4,857 players (3,740 in the bundled dataset) a full scan takes 2 ms, so it wasn't justified |

### Techniques
1. Deferred join (late row lookup). The history query computed two correlated subqueries (opponent, player count) for all ~8,000 of a player's matches before sorting and keeping 20. Selecting the page's ids first and joining back runs the subqueries 20 times instead of 8,000.
2. One pass instead of two. The fastest-wins page evaluated its window-function view twice (rows + `COUNT(*)`). `COUNT(*) OVER ()` returns the total with the rows. The seed page computed the overall median in a second query; a second `PERCENTILE_CONT … OVER ()` window in the same derived table gives it without a second query.
3. Index walk with an exact fallback. The home page's top 5 needed only the fastest wins, but the view ranks every player's best. Reading completed wins in `finish_time_ms` order through `ix_mp_finish` (EXPLAIN: `range` scan stopping at `LIMIT 500`) and keeping each player's first row gives the same answer. If those 500 rows hold fewer than 5 players, it falls back to the view.
4. Materialised summary tables. `mv_player_stats_lifetime` / `mv_player_stats_season` are stored copies of the stats views, rebuilt by the stored procedure `refresh_player_stats(p_player_id)` (615 ms for everyone, 30 ms for one player) when an import or restore finishes. The rate formulas live in two stored functions, `pct()` and `win_pct()`, used by the views. Pages read the summary table and fall back to the live view for a player not yet refreshed.

### Trade-offs and limits
- Summary tables trade write-time work and possible staleness for read speed. They are refreshed at the only points where data changes (end of import, on-demand first page, restore), and the fallback keeps unrefreshed players correct. A test proves pages read the table, and another that the tables equal the views after a refresh.
- `FORCE INDEX` pins the index walk. Without it the optimiser may choose a full join plan on small test data. It is only correct because of the exact fallback.
- These are SQL times on one laptop with hot caches. Whole-page times also include Jinja rendering and MongoDB calls, which dominate on the seed page (≈0.45 s), mostly the `$group` that collects 33k match ids.

### Views: abstraction vs speed

Two views were added to remove duplicated rules: `v_completed_wins` (what counts as a timed win) and `v_match_summary` (winner-first matchup for lists). Each was profiled before being kept:

| Query | Without view | With view | Outcome |
|---|---|---|---|
| Match summaries for a 20-row list | 0.40 ms (inline subqueries) | 1.95 ms with `GROUP BY` → 0.56 ms with per-row subqueries | Kept, redefined so MariaDB can merge it |
| Seed ranking join | 3.6 ms | 3.6 ms (identical plan) | Kept |
| Home "fastest 5" | 2.3 ms (`FORCE INDEX`) | 37 ms (optimiser chose a full scan) | Not used: an index hint can't be applied through a view |

Lesson: in MariaDB a view is a stored *query*, not stored *results*. Simple views (filters, joins, per-row subqueries) merge into the caller at no cost. Views with `GROUP BY` or window functions are materialised as temporary tables unless the filter can be pushed inside, and wrapping a view can hide index choices the optimiser needs. Where speed mattered more than reuse, the query stays hand-written, with a test that it agrees with the view.