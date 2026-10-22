"""Load data/dump/ (made by export_data.py) into the databases configured in .env.

    python setup/restore_data.py            # into empty databases (creates the schema first)
    python setup/restore_data.py --replace  # wipe the API-derived data first (asks for 'yes')

MariaDB is wiped (with --replace) and loaded in one transaction: if anything fails, the old rows come back.
MongoDB is only touched after that commit. It can't join a MariaDB transaction (and a standalone mongod has no
multi-document transactions), so if its part fails, run --replace again: every step deletes then inserts, so it
is safe to repeat. The row/document counts are checked against manifest.json at the end. User accounts, bookmarks and notes are never touched by --replace...
unless bookmarks point at players, in which case it refuses rather than delete someone's bookmarks.
The import history (import_runs) is cleared: it describes imports of the data being replaced, and Refresh trusts it
to know which matches are already stored, so the next Refresh of each player re-imports their history in full.
"""
import argparse
import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bson import json_util  # noqa: E402

from app import db_mongo, db_sql  # noqa: E402
from app.config import Config  # noqa: E402
from app.importer import elo_history, sync  # noqa: E402
from setup import init_db  # noqa: E402
from setup.export_data import DUMP, MONGO_COLLECTIONS, SQL_TABLES  # noqa: E402

BATCH = 2000


class RestoreError(Exception):
    pass


def _lines(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        yield from fh


def existing_counts(conn, mongo_db):
    with conn.cursor() as cur:
        sql = {}
        for t in SQL_TABLES + ["bookmarks"]:
            cur.execute(f"SELECT COUNT(*) AS n FROM `{t}`")
            sql[t] = cur.fetchone()["n"]
    conn.commit()
    return sql, {c: mongo_db[c].estimated_document_count() for c in MONGO_COLLECTIONS}


def wipe_sql(cur):
    """Delete the API-derived rows, children first. No commit: the caller's transaction also loads the new ones."""
    for t in ["mv_player_stats_season", "mv_player_stats_lifetime"] + list(reversed(SQL_TABLES)):
        cur.execute(f"DELETE FROM `{t}`")


def wipe_mongo(mongo_db):
    for c in MONGO_COLLECTIONS + [elo_history.COLLECTION, "import_runs"]:
        mongo_db[c].delete_many({})


def restore(database, mongo_db, src=DUMP, replace=False, cfg=Config, log=print):
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    init_db.init_mariadb(database, cfg)
    init_db.init_mongo(db_mongo.client(cfg.MONGO_ADMIN_URI)[mongo_db.name])   # schema work: admin account
    conn = db_sql.connect(database, cfg)
    try:
        sql_n, mongo_n = existing_counts(conn, mongo_db)
        has_data = any(sql_n[t] for t in SQL_TABLES) or any(mongo_n.values())
        if has_data and not replace:
            raise RestoreError("The databases already contain data. Use --replace to overwrite it.")
        if has_data and sql_n["bookmarks"]:
            raise RestoreError("Bookmarks exist and point at players; refusing to replace players under them.")

        with conn.cursor() as cur:  # one transaction: the old rows are only gone if the new ones all went in
            if has_data:
                wipe_sql(cur)
            for table in SQL_TABLES:
                lines = _lines(src / f"mariadb_{table}.jsonl.gz")
                cols = json.loads(next(lines))["columns"]
                sql = (f"INSERT INTO `{table}` ({', '.join(f'`{c}`' for c in cols)}) "
                       f"VALUES ({', '.join(['%s'] * len(cols))})")
                batch, n = [], 0
                for line in lines:
                    batch.append(json.loads(line))
                    if len(batch) >= BATCH:
                        cur.executemany(sql, batch)
                        n += len(batch)
                        batch = []
                if batch:
                    cur.executemany(sql, batch)
                    n += len(batch)
                log(f"  MariaDB {table:<22} {n:>8,} rows")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:   # MariaDB is committed: only now replace the MongoDB side
        wipe_mongo(mongo_db)   # also when there were no matches: an old import history must not survive
        for name in MONGO_COLLECTIONS:
            batch, n = [], 0
            for line in _lines(src / f"mongo_{name}.jsonl.gz"):
                batch.append(json_util.loads(line, json_options=json_util.RELAXED_JSON_OPTIONS))
                if len(batch) >= BATCH:
                    mongo_db[name].insert_many(batch, ordered=False)
                    n += len(batch)
                    batch = []
            if batch:
                mongo_db[name].insert_many(batch, ordered=False)
                n += len(batch)
            log(f"  MongoDB {name:<22} {n:>8,} documents")
    except Exception as exc:
        raise RestoreError(f"MariaDB was restored, but loading MongoDB failed ({type(exc).__name__}: {exc}). "
                           "Run restore_data.py --replace again; it is safe to repeat.") from exc

    conn = db_sql.connect(database, cfg)
    try:
        with conn.cursor() as cur:
            cur.execute("CALL refresh_player_stats(NULL)")
        conn.commit()
        log(f"  MongoDB {elo_history.COLLECTION:<22} {elo_history.rebuild(conn, mongo_db):>8,} points (derived)")
        sql_n, mongo_n = existing_counts(conn, mongo_db)
    finally:
        conn.close()
    bad = {k: (v, sql_n[k]) for k, v in manifest["mariadb"].items() if sql_n[k] != v}
    bad |= {k: (v, mongo_n[k]) for k, v in manifest["mongodb"].items() if mongo_n[k] != v}
    if bad:
        raise RestoreError(f"Counts differ from manifest (expected, got): {bad}")
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=str(DUMP))
    ap.add_argument("--replace", action="store_true", help="overwrite existing API-derived data")
    args = ap.parse_args()
    if args.replace and input(f"This REPLACES the match data in '{Config.MARIADB_DATABASE}' / '{Config.MONGO_DB}'. "
                              "Type yes: ").strip() != "yes":
        print("Aborted.")
        return 1
    print(f"Restoring {args.src} into MariaDB '{Config.MARIADB_DATABASE}' and Mongo '{Config.MONGO_DB}'")
    try:
        m = restore(Config.MARIADB_DATABASE, sync.mongo_database(), Path(args.src), replace=args.replace)
    except RestoreError as e:
        print(f"Restore stopped: {e}")
        return 1
    print(f"Done. Data exported {m['exported_at']}; counts match the manifest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
