"""Integrity: triggers, CHECK and foreign-key constraints, unique keys, MongoDB validators, and restore/export
being all or nothing.
"""
from datetime import datetime, timezone

import pymysql
import pytest
from pymongo.errors import WriteError

from app.importer import sync
from tests.conftest import A, B, raw_match, seed_season, uuid_for


def setup_match(conn, mongo_db, match_id=1):
    seed_season(conn)
    sync.store_match(conn, mongo_db, raw_match(match_id, [A, B], A[0]))


def test_triggers_keep_participants_in_step(conn, mongo_db):
    """One winner per match (unique key), and draw/decay on a match <=> draw/decay on its rows (triggers),
    checked in both directions."""
    setup_match(conn, mongo_db)
    with conn.cursor() as cur:
        cur.execute("SELECT player_id FROM players WHERE uuid = %s", (B[0],))
        loser = cur.fetchone()["player_id"]
        with pytest.raises(pymysql.err.IntegrityError, match="uq_mp_one_winner"):   # a second winner
            cur.execute("UPDATE match_players SET outcome = 'win' WHERE match_id = 1 AND player_id = %s", (loser,))
        conn.rollback()
        for result in ("draw", "decay"):
            with pytest.raises(pymysql.err.OperationalError, match="contradicts"):
                cur.execute("UPDATE matches SET result_type = %s WHERE match_id = 1", (result,))
            conn.rollback()
            with pytest.raises(pymysql.err.OperationalError, match="outcome draw/decay"):
                cur.execute("UPDATE match_players SET outcome = %s WHERE match_id = 1 AND player_id = %s",
                            (result, loser))
            conn.rollback()


def test_foreign_and_unique_keys(conn, mongo_db):
    """A match type must exist in match_types, and usernames are unique ignoring case (the collation)."""
    setup_match(conn, mongo_db)
    with conn.cursor() as cur:
        with pytest.raises(pymysql.err.IntegrityError, match="fk_matches_type"):
            cur.execute("UPDATE matches SET match_type = 9 WHERE match_id = 1")
        conn.rollback()
        cur.execute("INSERT INTO users (username, password_hash) VALUES ('alice_1', 'x')")
        with pytest.raises(pymysql.err.IntegrityError, match="uq_users_username"):
            cur.execute("INSERT INTO users (username, password_hash) VALUES ('ALICE_1', 'x')")
        conn.rollback()


def test_every_non_draw_has_exactly_one_winner(conn, mongo_db):
    """The rule the schema can't check row by row (participants are inserted after their match): the importer
    gives every non-draw exactly one win row (outside winners included) and every draw none."""
    class Api:
        def user(self, ident):
            return {"uuid": ident, "nickname": "outsider", "eloRate": None, "eloRank": None, "country": None}
    seed_season(conn)
    for m in [raw_match(1, [A, B], A[0]), raw_match(2, [A, B], B[0], forfeited=True),
              raw_match(3, [A, B], None, forfeited=True, change=None),
              raw_match(4, [A, B], uuid_for(77), match_type=3, forfeited=True)]:
        sync.store_match(conn, mongo_db, m, api=Api())
    with conn.cursor() as cur:
        cur.execute("SELECT m.match_id, m.result_type = 'draw' AS draw, "
                    "(SELECT COUNT(*) FROM match_players mp WHERE mp.match_id = m.match_id AND mp.outcome = 'win') AS wins "
                    "FROM matches m ORDER BY m.match_id")
        assert [(r["draw"], r["wins"]) for r in cur.fetchall()] == [(0, 1), (0, 1), (1, 0), (0, 1)]


def test_decay_is_stored_as_decay_and_flagged(conn, mongo_db):
    seed_season(conn)
    decay = raw_match(9, [A], None, decayed=True)
    decay["changes"][0]["change"] = -7          # a decay is an Elo penalty with no result
    sync.store_match(conn, mongo_db, decay)
    with conn.cursor() as cur:
        cur.execute("SELECT m.result_type, m.is_decayed, m.is_draw, mp.outcome, mp.elo_change FROM matches m "
                    "JOIN match_players mp USING (match_id) WHERE match_id = 9")
        assert cur.fetchone() == {"result_type": "decay", "is_decayed": 1, "is_draw": 0, "outcome": "decay",
                                  "elo_change": -7}
    conn.commit()


def test_validators_reject_bad_documents(conn, mongo_db):
    """MongoDB itself refuses bad notes and matches, not only the app: notes are strict, and a match needs
    upper-case seed structure names (or null, as on private matches), a participant and a season from 1."""
    from app.importer import transform
    now = datetime.now(timezone.utc)
    good = {"user_id": 1, "match_id": 1, "text": "ok", "tags": ["a"], "created_at": now, "updated_at": now}
    mongo_db["notes"].insert_one(dict(good))
    for bad in ({**good, "tags": ["Has Space"]}, {**good, "text": ""}, {**good, "extra": 1}, {**good, "user_id": "1"}):
        with pytest.raises(WriteError):
            mongo_db["notes"].insert_one(bad)
    good = transform.mongo_match_doc(raw_match(1, [A, B], A[0]), now)
    mongo_db["matches"].insert_one(dict(good))
    for i, seed in enumerate(({"overworld": "TRAIL_RUINS", "nether": "STABLES"},     # a type added later
                              {"overworld": None, "nether": None}), start=2):     # private match
        mongo_db["matches"].insert_one({**good, "_id": i, "seed": {**good["seed"], **seed}})
    for bad in ({**good, "seed": {**good["seed"], "overworld": "Village 2"}},
                {**good, "seed": {**good["seed"], "nether": "bridge"}},
                {**good, "seed": {**good["seed"], "endTowers": [76, "79"]}},
                {**good, "players": []}, {**good, "season": 0}):
        with pytest.raises(WriteError):
            mongo_db["matches"].insert_one({**bad, "_id": 99})


def test_export_restore_roundtrip(tmp_path, conn, mongo_db):
    from setup import export_data, restore_data
    from tests.conftest import TEST_SQL
    seed_season(conn)
    for m in [raw_match(1, [A, B], A[0]), raw_match(2, [A, B], None, forfeited=True, change=None),
              raw_match(3, [A, B], B[0], match_type=3)]:
        sync.store_match(conn, mongo_db, m)
    mongo_db["players"].insert_one({"_id": A[0], "profile": {"uuid": A[0], "nickname": "alpha",
                                    "connections": {"discord": {"id": "1", "name": "x"}}}, "seasons": {},
                                    "fetched_at": datetime.now(timezone.utc)})
    manifest = export_data.export(TEST_SQL, mongo_db, tmp_path)
    assert manifest["mariadb"]["match_players"] == 6 and manifest["mongodb"]["matches"] == 3
    with pytest.raises(restore_data.RestoreError):           # data already there and no --replace
        restore_data.restore(TEST_SQL, mongo_db, tmp_path, log=lambda *_: None)
    sync.start_run(mongo_db, {"uuid": A[0], "player_id": 1}, "full")   # an import of the data being replaced
    restore_data.restore(TEST_SQL, mongo_db, tmp_path, replace=True, log=lambda *_: None)
    assert mongo_db["import_runs"].count_documents({}) == 0   # else Refresh would skip what the restore removed
    with conn.cursor() as cur:
        cur.execute("SELECT m.result_type, m.is_draw, s.winner FROM matches m JOIN v_match_summary s USING (match_id) "
                    "WHERE match_id = 2")
        assert cur.fetchone() == {"result_type": "draw", "is_draw": 1, "winner": None}   # generated column recomputed
        cur.execute("SELECT COUNT(*) AS n FROM match_players")
        assert cur.fetchone()["n"] == 6
    conn.commit()
    assert mongo_db["matches"].find_one({"_id": 1})["played_at"].tzinfo is not None
    assert "connections" not in mongo_db["players"].find_one({"_id": A[0]})["profile"]   # personal data stripped


def test_failed_replace_keeps_the_old_data(tmp_path, conn, mongo_db):
    """restore --replace wipes and loads MariaDB in one transaction and only touches MongoDB after it commits,
    so a failed load leaves both databases as they were."""
    import gzip
    import json

    from setup import export_data, restore_data
    from tests.conftest import TEST_SQL
    seed_season(conn)
    for m in [raw_match(1, [A, B], A[0]), raw_match(2, [A, B], B[0])]:
        sync.store_match(conn, mongo_db, m)
    export_data.export(TEST_SQL, mongo_db, tmp_path)
    dump = tmp_path / "mariadb_match_players.jsonl.gz"           # break the dump: a row for a missing player
    with gzip.open(dump, "rt", encoding="utf-8") as fh:
        lines = fh.readlines()
    bad = json.loads(lines[1])
    bad[json.loads(lines[0])["columns"].index("player_id")] = 999999
    with gzip.open(dump, "wt", encoding="utf-8") as fh:
        fh.writelines(lines + [json.dumps(bad) + "\n"])
    with pytest.raises(Exception):
        restore_data.restore(TEST_SQL, mongo_db, tmp_path, replace=True, log=lambda *_: None)
    conn.commit()   # a fresh snapshot
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM match_players")
        assert cur.fetchone()["n"] == 4                            # the old rows came back
    conn.commit()
    assert mongo_db["matches"].count_documents({}) == 2           # MongoDB was never touched
