"""Check that MariaDB and MongoDB hold the same matches.

    python setup/check_consistency.py

The two stores share no transaction, so the importer keeps them in step by ordering, a compensating delete and
idempotent re-runs (app/importer/sync.py). This verifies the result: every match id in MariaDB `matches` should
have a MongoDB `matches` document and the other way round, every cached timeline (`match_details`) should
belong to a stored match, the Elo time series (`elo_history`) should have points for exactly the ranked
matches with an Elo change, and every note (`notes`) should belong to an existing user and match: MariaDB can't
enforce those references with foreign keys, so this checks them. It only reads. Exit code 0 when everything agrees, 1 otherwise.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the project root

from app import db_mongo, db_sql  # noqa: E402
from app.config import Config  # noqa: E402
from app.importer import elo_history  # noqa: E402

SHOW = 10   # ids listed per kind of difference


def differences(conn, mongo_db):
    """{"sql": n, "mongo": n, "only_sql": [ids], "only_mongo": [ids], "orphan_timelines": [ids],
    "missing_points": [ids], "stray_points": [ids], "orphan_notes": [note ids]}"""
    with conn.cursor() as cur:
        cur.execute("SELECT match_id FROM matches")
        sql_ids = {r["match_id"] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT match_id FROM v_ranked_match_players WHERE elo_before IS NOT NULL "
                    "AND elo_change IS NOT NULL")
        point_ids = {r["match_id"] for r in cur.fetchall()}
        cur.execute("SELECT user_id FROM users")
        user_ids = {r["user_id"] for r in cur.fetchall()}
    conn.commit()
    mongo_ids = set(mongo_db["matches"].distinct("_id"))
    timeline_ids = set(mongo_db["match_details"].distinct("_id"))
    stored_points = set(mongo_db[elo_history.COLLECTION].distinct("match_id"))
    # A note whose owner was deleted outside the app, or whose match is gone
    orphan_notes = [str(n["_id"]) for n in mongo_db["notes"].find({}, {"user_id": 1, "match_id": 1}).sort("_id", 1)
                    if n["user_id"] not in user_ids or n["match_id"] not in sql_ids]
    return {"sql": len(sql_ids), "mongo": len(mongo_ids),
            "only_sql": sorted(sql_ids - mongo_ids), "only_mongo": sorted(mongo_ids - sql_ids),
            "orphan_timelines": sorted(timeline_ids - sql_ids),
            "missing_points": sorted(point_ids - stored_points), "stray_points": sorted(stored_points - point_ids),
            "orphan_notes": orphan_notes}


def main():
    conn = db_sql.connect()
    try:
        d = differences(conn, db_mongo.client(Config.MONGO_URI)[Config.MONGO_DB])
    finally:
        conn.close()
    print(f"{'Matches in MariaDB:':<36}{d['sql']:,}")
    print(f"{'Matches in MongoDB:':<36}{d['mongo']:,}")
    problems = 0
    for key, label in (("only_sql", "Only in MariaDB"), ("only_mongo", "Only in MongoDB"),
                       ("orphan_timelines", "Timelines without a stored match"),
                       ("missing_points", "Ranked matches without Elo points"),
                       ("stray_points", "Elo points without a ranked match"),
                       ("orphan_notes", "Notes without a user or match")):
        ids = d[key]
        problems += len(ids)
        more = f" (first {SHOW}: {', '.join(map(str, ids[:SHOW]))})" if ids else ""
        print(f"{label + ':':<36}{len(ids):,}{more}")
    if problems:
        print("Differences found. Re-importing the affected players (Refresh) repairs missing matches: every write is "
              "an upsert. Elo points are derived, so python -m app.importer.elo_history rebuilds them all. Notes "
              "without a user or match can't be reattached: delete them by id in mongosh.")
        return 1
    print("OK: both stores hold the same matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
