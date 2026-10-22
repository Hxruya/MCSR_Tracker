"""Create (idempotently) the MariaDB database, tables, views and the Mongo collections.

    python setup/init_db.py           # create anything missing
    python setup/init_db.py --reset   # drop both databases and recreate (asks for 'yes')
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pymongo import ASCENDING, DESCENDING, TEXT  # noqa: E402
from pymongo.errors import CollectionInvalid  # noqa: E402

from app import db_mongo, db_sql  # noqa: E402
from app.config import Config  # noqa: E402
from app.importer import elo_history, transform  # noqa: E402

SQL_DIR = ROOT / "setup" / "sql"


# Seed structure types: an upper-case name such as RUINED_PORTAL, or null on private and some old matches. A pattern,
# not a list of the known types: a structure added in a future season must still be stored, not rejected.
STRUCTURE_TYPE = {"bsonType": ["string", "null"], "pattern": "^[A-Z][A-Z_]*$"}

# Built from real API responses.
MATCH_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "played_at", "players", "raw", "imported_at"],
        "properties": {
            "_id": {"bsonType": ["int", "long"]},
            "season": {"bsonType": "int", "minimum": 1},
            "match_type": {"bsonType": "int", "enum": [1, 2, 3, 4]},
            "category": {"bsonType": ["string", "null"]},
            "played_at": {"bsonType": "date"},
            "players": {
                "bsonType": "array",
                "minItems": 1,   # a decay "match" has one participant
                "items": {
                    "bsonType": "object",
                    "required": ["uuid", "nickname"],
                    "properties": {
                        "uuid": {"bsonType": "string", "pattern": "^[0-9a-f]{32}$"},
                        "nickname": {"bsonType": "string"},
                    },
                },
            },
            "result": {
                "bsonType": ["object", "null"],  # null on some old decay matches (e.g. 663708)
                "properties": {
                    "uuid": {"bsonType": ["string", "null"]},
                    "time": {"bsonType": ["int", "long", "null"]},
                },
            },
            "forfeited": {"bsonType": "bool"},
            "seed": {
                "bsonType": ["object", "null"],
                "properties": {
                    "id": {"bsonType": ["string", "null"]},
                    "overworld": STRUCTURE_TYPE,
                    "nether": STRUCTURE_TYPE,
                    "endTowers": {"bsonType": ["array", "null"], "items": {"bsonType": "int"}},
                    "variations": {"bsonType": ["array", "null"], "items": {"bsonType": "string"}},
                },
            },
            "videos": {
                "bsonType": "array",
                "items": {
                    "bsonType": "object",
                    "properties": {
                        "uuid": {"bsonType": "string"},
                        "url": {"bsonType": "string"},
                        "startsAt": {"bsonType": ["int", "long", "null"]},
                    },
                },
            },
            "raw": {"bsonType": "object"},
            "imported_at": {"bsonType": "date"},         # the latest import that wrote this match
            "first_imported_at": {"bsonType": "date"},   # set once, by $setOnInsert (absent on older documents)
        },
    }
}


DETAIL_VALIDATOR = {  # match_details: one cached /matches/{id} response + derived End splits
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "timelines", "completions", "end_splits", "raw", "fetched_at"],
        "properties": {
            "_id": {"bsonType": ["int", "long"]},
            "season": {"bsonType": ["int", "null"]},
            "match_type": {"bsonType": ["int", "null"], "enum": [1, 2, 3, 4, None]},
            "decayed": {"bsonType": "bool"},
            "end_towers": {"bsonType": ["array", "null"], "items": {"bsonType": "int"}},
            "variations": {"bsonType": "array", "items": {"bsonType": "string"}},
            "timelines": {"bsonType": "array", "items": {
                "bsonType": "object", "required": ["uuid", "time", "type"],
                "properties": {"uuid": {"bsonType": "string"}, "time": {"bsonType": ["int", "long"]},
                               "type": {"bsonType": "string"}}}},
            "completions": {"bsonType": "array"},
            "end_splits": {"bsonType": "array", "items": {
                "bsonType": "object", "required": ["uuid", "entered_end", "end_deaths", "finished"],
                "properties": {"uuid": {"bsonType": "string", "pattern": "^[0-9a-f]{32}$"},
                               "entered_end": {"bsonType": ["int", "long"]},
                               "dragon_kill": {"bsonType": ["int", "long", "null"]},
                               "dragon_split": {"bsonType": ["int", "long", "null"], "minimum": 0},
                               "end_deaths": {"bsonType": "int", "minimum": 0},
                               "finished": {"bsonType": "bool"}}}},
            "raw": {"bsonType": "object"},
            "fetched_at": {"bsonType": "date"},
        },
    }
}

PLAYER_VALIDATOR = {  # players: raw profile + seasons for fully searched players
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "profile", "seasons", "fetched_at"],
        "properties": {
            "_id": {"bsonType": "string", "pattern": "^[0-9a-f]{32}$"},
            "profile": {"bsonType": "object", "required": ["uuid", "nickname"],
                        "properties": {"uuid": {"bsonType": "string"}, "nickname": {"bsonType": "string"}}},
            "seasons": {"bsonType": "object"},
            "fetched_at": {"bsonType": "date"},
        },
    }
}

NOTE_VALIDATOR = {  # notes: a user's private notes on a match
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["user_id", "match_id", "text", "tags", "created_at", "updated_at"],
        "additionalProperties": False,
        "properties": {
            "_id": {"bsonType": "objectId"},
            "user_id": {"bsonType": ["int", "long"], "minimum": 1},      # MariaDB users.user_id
            "match_id": {"bsonType": ["int", "long"], "minimum": 1},     # MariaDB matches.match_id
            "text": {"bsonType": "string", "minLength": 1, "maxLength": 500},
            "tags": {"bsonType": "array", "maxItems": 8, "uniqueItems": True,
                     "items": {"bsonType": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,23}$"}},
            "created_at": {"bsonType": "date"},
            "updated_at": {"bsonType": "date"},
        },
    }
}


RUN_VALIDATOR = {  # import_runs: one document per player import (progress, outcome, skipped matches)
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["player_uuid", "player_id", "source", "status", "started_at", "current_season",
                     "matches_seen", "matches_inserted", "matches_updated", "matches_failed", "errors"],
        "additionalProperties": False,
        "properties": {
            "_id": {"bsonType": "objectId"},
            "player_uuid": {"bsonType": "string", "pattern": "^[0-9a-f]{32}$"},   # = players.uuid
            "player_id": {"bsonType": ["int", "long"], "minimum": 1},             # = players.player_id
            "source": {"enum": ["full", "refresh"]},
            "status": {"enum": ["running", "success", "partial", "failed"]},
            "started_at": {"bsonType": "date"},
            "finished_at": {"bsonType": ["date", "null"]},
            "current_season": {"bsonType": ["int", "null"]},
            "matches_seen": {"bsonType": "int", "minimum": 0},
            "matches_inserted": {"bsonType": "int", "minimum": 0},
            "matches_updated": {"bsonType": "int", "minimum": 0},
            "matches_failed": {"bsonType": "int", "minimum": 0},
            "errors": {"bsonType": "array", "maxItems": 50, "items": {"bsonType": "string"}},
        },
    }
}


def _apply_validator(db, name, validator, existing):
    if name in existing:
        db.command("collMod", name, validator=validator, validationLevel="strict")
    else:
        try:
            db.create_collection(name, validator=validator, validationLevel="strict")
        except CollectionInvalid:
            db.command("collMod", name, validator=validator, validationLevel="strict")


def _statements(path):
    text = path.read_text(encoding="utf-8")
    for chunk in text.split(";\n"):
        body = "\n".join(line for line in chunk.splitlines() if not line.strip().startswith("--")).strip()
        if body:
            yield body


def _trigger_statements(path):
    """Trigger bodies contain ';', so triggers.sql separates statements with lines holding only '$$'."""
    text = path.read_text(encoding="utf-8")
    for chunk in text.split("\n$$"):
        body = "\n".join(line for line in chunk.splitlines() if not line.strip().startswith("--")).strip()
        if body:
            yield body


def init_mariadb(database, cfg=Config):
    conn = db_sql.connect(database=False, cfg=cfg, admin=True)
    try:
        with conn.cursor() as cur:
            # database is validated against [A-Za-z0-9_]+ in Config, so quoting is safe.
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            cur.execute(f"USE `{database}`")
            for stmt in _statements(SQL_DIR / "schema.sql"):
                cur.execute(stmt)
            for stmt in _trigger_statements(SQL_DIR / "routines.sql"):  # pct() is used by the views
                cur.execute(stmt)
            for stmt in _statements(SQL_DIR / "views.sql"):
                cur.execute(stmt)
            for name in ("summary.sql", "triggers.sql"):
                for stmt in _trigger_statements(SQL_DIR / name):
                    cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def drop_mariadb(database, cfg=Config):
    conn = db_sql.connect(database=False, cfg=cfg, admin=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{database}`")
        conn.commit()
    finally:
        conn.close()


def init_mongo(db):
    existing = set(db.list_collection_names())
    _apply_validator(db, "matches", MATCH_VALIDATOR, existing)
    m = db["matches"]
    m.create_index([("players.uuid", ASCENDING)], name="ix_players_uuid")
    # Top rivals: one player's ranked matches, newest first, read in index order (no in-memory sort)
    m.create_index([("players.uuid", ASCENDING), ("match_type", ASCENDING), ("played_at", DESCENDING)],
                   name="ix_players_type_played")
    m.create_index([("season", ASCENDING), ("match_type", ASCENDING)], name="ix_season_type")  # covered facet counts
    m.create_index([("played_at", DESCENDING)], name="ix_played_at")
    # Seeds page list (match_type filter, newest first): an index walk that stops at the page, not scan + sort
    m.create_index([("match_type", ASCENDING), ("played_at", DESCENDING), ("_id", DESCENDING)], name="ix_type_played")
    m.create_index([("seed.overworld", ASCENDING)], name="ix_seed_overworld")
    m.create_index([("seed.nether", ASCENDING)], name="ix_seed_nether")
    m.create_index([("seed.variations", ASCENDING)], name="ix_seed_variations")
    _apply_validator(db, "players", PLAYER_VALIDATOR, existing)
    # Profiles stored before the importer dropped linked accounts (app/importer/transform.UNSTORED_PROFILE_FIELDS)
    db["players"].update_many({}, {"$unset": {f"profile.{f}": "" for f in transform.UNSTORED_PROFILE_FIELDS}})
    _apply_validator(db, "match_details", DETAIL_VALIDATOR, existing)
    _apply_validator(db, "notes", NOTE_VALIDATOR, existing)
    n = db["notes"]
    n.create_index([("user_id", ASCENDING), ("updated_at", DESCENDING)], name="ix_user_updated")
    n.create_index([("user_id", ASCENDING), ("match_id", ASCENDING)], name="ix_user_match")
    n.create_index([("user_id", ASCENDING), ("tags", ASCENDING)], name="ix_user_tags")
    n.create_index([("text", TEXT), ("tags", TEXT)], name="ix_text", weights={"tags": 3, "text": 1})
    d = db["match_details"]
    d.create_index([("end_splits.uuid", ASCENDING)], name="ix_end_splits_uuid")  # per-player End splits
    d.create_index([("match_type", ASCENDING), ("season", ASCENDING)], name="ix_type_season")
    # Split analysis: one player's timelines (multikey; a timeline holds each participant's uuid many times,
    # but a document is indexed once per distinct value, so about two entries per match)
    d.create_index([("timelines.uuid", ASCENDING), ("match_type", ASCENDING), ("season", ASCENDING)],
                   name="ix_timelines_uuid")
    _apply_validator(db, "import_runs", RUN_VALIDATOR, existing)
    db["import_runs"].create_index([("player_uuid", ASCENDING), ("started_at", DESCENDING)], name="ix_player_started")
    elo_history.ensure_collection(db)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="drop and recreate both databases")
    args = parser.parse_args()
    mongo = db_mongo.client(Config.MONGO_ADMIN_URI)
    if args.reset:
        answer = input(f"This DELETES MariaDB '{Config.MARIADB_DATABASE}' and Mongo '{Config.MONGO_DB}'. Type yes: ")
        if answer.strip() != "yes":
            print("Aborted.")
            return 1
        drop_mariadb(Config.MARIADB_DATABASE)
        mongo.drop_database(Config.MONGO_DB)
    init_mariadb(Config.MARIADB_DATABASE)
    init_mongo(mongo[Config.MONGO_DB])
    print(f"OK: MariaDB '{Config.MARIADB_DATABASE}' and Mongo '{Config.MONGO_DB}' are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
