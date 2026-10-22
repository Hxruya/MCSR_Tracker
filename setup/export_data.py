"""Export the API-derived data of both databases to data/dump/ (gzipped JSON lines), for the submission zip.

    python setup/export_data.py

User data (users, bookmarks, notes) and import history (import_runs) are never exported. Restore with setup/restore_data.py.
"""
import argparse
import gzip
import json
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bson import json_util  # noqa: E402

from app import db_sql  # noqa: E402
from app.config import Config  # noqa: E402
from app.importer import sync, transform  # noqa: E402

DUMP = ROOT / "data" / "dump"
# Parents before children, so a restore can insert in this order (foreign keys).
SQL_TABLES = ["seasons", "players", "player_season_results", "matches", "match_players"]
MONGO_COLLECTIONS = ["matches", "match_details", "players"]
BATCH = 5000


def _json_default(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    raise TypeError(f"cannot serialise {type(v).__name__}")


def stored_columns(cur, table):
    """Columns that hold data (VIRTUAL generated columns are recomputed by the database)."""
    cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = %s AND EXTRA NOT LIKE %s ORDER BY ORDINAL_POSITION", (table, "%GENERATED%"))
    return [r["COLUMN_NAME"] for r in cur.fetchall()]


def export(database, mongo_db, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "mariadb": {}, "mongodb": {}, "format": "gzip JSON lines; Mongo in relaxed Extended JSON"}
    conn = db_sql.connect(database)
    try:
        with conn.cursor() as cur:
            for table in SQL_TABLES:
                cols = stored_columns(cur, table)
                n = 0
                with gzip.open(out_dir / f"mariadb_{table}.jsonl.gz", "wt", encoding="utf-8") as fh:
                    fh.write(json.dumps({"columns": cols}) + "\n")
                    # table/column names come from the hardcoded list and information_schema, not user input
                    cur.execute(f"SELECT {', '.join(f'`{c}`' for c in cols)} FROM `{table}`")
                    while rows := cur.fetchmany(BATCH):
                        for r in rows:
                            fh.write(json.dumps([r[c] for c in cols], default=_json_default) + "\n")
                        n += len(rows)
                manifest["mariadb"][table] = n
                print(f"  MariaDB {table:<22} {n:>8,} rows")
    finally:
        conn.close()
    for name in MONGO_COLLECTIONS:
        n = 0
        with gzip.open(out_dir / f"mongo_{name}.jsonl.gz", "wt", encoding="utf-8") as fh:
            for doc in mongo_db[name].find().sort("_id", 1).batch_size(BATCH):
                if name == "players":  # never stored since the importer drops them; kept as a safety net
                    for field in transform.UNSTORED_PROFILE_FIELDS:
                        doc.get("profile", {}).pop(field, None)
                fh.write(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS) + "\n")
                n += 1
        manifest["mongodb"][name] = n
        print(f"  MongoDB {name:<22} {n:>8,} documents")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(DUMP), help="output folder (default data/dump)")
    args = ap.parse_args()
    out = Path(args.out)
    print(f"Exporting MariaDB '{Config.MARIADB_DATABASE}' and Mongo '{Config.MONGO_DB}' to {out}")
    export(Config.MARIADB_DATABASE, sync.mongo_database(), out)
    size = sum(p.stat().st_size for p in out.glob("*.gz"))
    print(f"Done: {size / 1e6:.1f} MB in {out}")


if __name__ == "__main__":
    main()
