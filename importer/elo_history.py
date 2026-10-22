"""Elo history as a MongoDB time-series collection.

One measurement per player per ranked, non-decay match that has an Elo change:
    {played_at (timeField), player: uuid (metaField), match_id, season, elo, change}
MongoDB stores these in compressed buckets per player and time range, and the profile reads them with
$setWindowFields (moving average, running peak). The data is derived from MariaDB match_players, so it is
written alongside each stored match and can be rebuilt from MariaDB at any time:

    python -m app.importer.elo_history            # rebuild everything from MariaDB
"""
import argparse
import sys
from datetime import timezone

from app.importer import transform

COLLECTION = "elo_history"
OPTIONS = {"timeField": "played_at", "metaField": "player", "granularity": "hours"}


def ensure_collection(db):
    """Create the time-series collection (and its match_id index) if missing. Time-series collections can't
    have a $jsonSchema validator ("'timeseries' is not allowed with 'validator'"), so point() checks types."""
    if COLLECTION not in db.list_collection_names():
        db.create_collection(COLLECTION, timeseries=OPTIONS)
    # MongoDB adds the {player, played_at} index itself; match_id is for re-imports replacing a match's points.
    db[COLLECTION].create_index("match_id", name="ix_match_id")


def point(uuid, match_id, played_at, season, elo_before, elo_change):
    """One measurement, or None when the match has no Elo (placements, decay)."""
    if elo_before is None or elo_change is None:
        return None
    if played_at.tzinfo is None:
        played_at = played_at.replace(tzinfo=timezone.utc)
    return {"played_at": played_at, "player": str(uuid), "match_id": int(match_id), "season": int(season),
            "elo": int(elo_before) + int(elo_change), "change": int(elo_change)}


def record_match(db, row, mp_rows, outcome):
    """Called by sync.store_match after MariaDB commits. row: transform.match_row(); mp_rows: match_player_rows().
    'inserted' adds points, 'updated' replaces them, 'unchanged' does nothing."""
    if outcome == "unchanged":
        return
    coll = db[COLLECTION]
    if outcome == "updated":
        coll.delete_many({"match_id": row["match_id"]})
    if row["match_type"] != transform.RANKED or row["is_decayed"]:
        return
    points = [p for p in (point(r["uuid"], row["match_id"], row["played_at"], row["season_id"],
                                r["elo_before"], r["elo_change"]) for r in mp_rows) if p]
    if points:
        coll.insert_many(points, ordered=False)


REBUILD_SQL = """
SELECT p.uuid, r.match_id, r.played_at, r.season_id, r.elo_before, r.elo_change
FROM v_ranked_match_players r JOIN players p ON p.player_id = r.player_id
WHERE r.elo_change IS NOT NULL AND r.elo_before IS NOT NULL
ORDER BY p.uuid, r.played_at
"""


def rebuild(conn, db, batch=10000):
    """Replace the whole collection with points derived from MariaDB. Returns the number written.
    Points go in per player in time order, so MongoDB fills each bucket before opening the next (an import
    adds them newest first, page by page, which leaves many part-filled buckets: 441 for edcr against 23).
    The collection must exist (setup/init_db.py creates it; the app account can't)."""
    coll = db[COLLECTION]
    coll.delete_many({})
    written = 0
    with conn.cursor() as cur:
        cur.execute(REBUILD_SQL)
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            coll.insert_many([point(r["uuid"], r["match_id"], r["played_at"], r["season_id"],
                                    r["elo_before"], r["elo_change"]) for r in rows], ordered=False)
            written += len(rows)
    return written


def main():
    from app import db_mongo, db_sql
    from app.config import Config
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    conn = db_sql.connect()
    try:
        n = rebuild(conn, db_mongo.client(Config.MONGO_URI)[Config.MONGO_DB])
    finally:
        conn.close()
    print(f"OK: {n} Elo points written to '{COLLECTION}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
