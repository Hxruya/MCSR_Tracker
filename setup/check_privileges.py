"""Show what the app's database accounts can and cannot do.

    python setup/check_privileges.py

Connects with the app settings from .env (MARIADB_USER, MONGO_URI). Every "should be denied" probe targets an
object that doesn't exist (or, for CREATE DATABASE IF NOT EXISTS, one that already does), so it changes nothing
either way. The error code tells the two cases apart: a
permission error means the account is restricted; "doesn't exist" means it was allowed to try.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the project root

import pymysql  # noqa: E402
from pymongo.errors import OperationFailure, PyMongoError  # noqa: E402

from app import db_mongo, db_sql  # noqa: E402
from app.config import Config  # noqa: E402

PROBE = "zz_privilege_probe"   # never exists

# (label, statement, expected: "allowed" | "denied", error codes meaning "denied", codes meaning "allowed")
SQL_PROBES = [
    ("Read rows", "SELECT COUNT(*) FROM players", "allowed", (), ()),
    ("Session temporary table", f"CREATE TEMPORARY TABLE {PROBE} (x INT)", "allowed", (), ()),
    ("Drop a table", f"DROP TABLE {PROBE}_t", "denied", (1142,), (1051,)),
    ("Alter a table", f"ALTER TABLE {PROBE}_t ADD COLUMN y INT", "denied", (1142,), (1146,)),
    ("Drop a stored procedure", f"DROP PROCEDURE {PROBE}", "denied", (1370, 1044), (1305,)),
    # the app's own database already exists, so an account allowed to create databases changes nothing here
    ("Create a database", f"CREATE DATABASE IF NOT EXISTS `{Config.MARIADB_DATABASE}`", "denied", (1044,), ()),
]
MONGO_PROBES = [  # MongoDB: 13 = Unauthorized, 26 = NamespaceNotFound
    ("Read documents", lambda db: db["matches"].find_one({}, {"_id": 1}), "allowed"),
    ("Change a collection's validator", lambda db: db.command("collMod", PROBE), "denied"),
    ("Drop an index", lambda db: db.command("dropIndexes", PROBE, index="x_1"), "denied"),
]


def classify_sql(error_code, denied_codes, allowed_codes):
    """None (no error) -> allowed; a permission code -> denied; 'not found' style codes -> allowed."""
    if error_code is None or error_code in allowed_codes:
        return "allowed"
    return "denied" if error_code in denied_codes else f"error {error_code}"


def classify_mongo(error_code):
    if error_code is None or error_code == 26:
        return "allowed"
    return "denied" if error_code == 13 else f"error {error_code}"


def check_mariadb():
    rows = []
    conn = db_sql.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT CURRENT_USER() AS u")
            who = cur.fetchone()
            for label, stmt, expected, denied, allowed in SQL_PROBES:
                code = None
                try:
                    cur.execute(stmt)
                except pymysql.MySQLError as e:
                    code = e.args[0]
                rows.append((label, expected, classify_sql(code, denied, allowed)))
            cur.execute(f"DROP TEMPORARY TABLE IF EXISTS {PROBE}")
            cur.execute(
                "SELECT 'view' AS kind, TABLE_NAME AS name, DEFINER FROM information_schema.VIEWS WHERE TABLE_SCHEMA = DATABASE() "
                "UNION ALL SELECT 'routine', ROUTINE_NAME, DEFINER FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = DATABASE() "
                "UNION ALL SELECT 'trigger', TRIGGER_NAME, DEFINER FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()")
            # NULL where the restricted account may not see an object's definer
            definers = sorted({r["DEFINER"] for r in cur.fetchall() if r["DEFINER"]})
        conn.rollback()
    finally:
        conn.close()
    return who, rows, definers


def check_mongo():
    db = db_mongo.client(Config.MONGO_URI)[Config.MONGO_DB]
    status = db.command("connectionStatus")["authInfo"]
    rows = []
    for label, op, expected in MONGO_PROBES:
        code = None
        try:
            op(db)
        except OperationFailure as e:
            code = e.code
        rows.append((label, expected, classify_mongo(code)))
    return status, rows


def report(title, rows):
    print(f"\n{title}")
    ok = True
    for label, expected, got in rows:
        mark = "OK " if got == expected else "!! "
        ok &= got == expected
        print(f"  {mark}{label:<34} expected {expected:<8} got {got}")
    return ok


def main():
    who, rows, definers = check_mariadb()
    good = report(f"MariaDB as {who['u']}", rows)
    print(f"  Definers of views/routines/triggers: {', '.join(definers)}")
    if who["u"] in definers:
        print("  !! The app account owns schema objects: re-run setup/init_db.py with the admin account.")
        good = False
    try:
        status, mrows = check_mongo()
        users = [u["user"] for u in status.get("authenticatedUsers", [])]
        good &= report(f"MongoDB as {', '.join(users) or 'nobody (access control is off)'}", mrows)
    except PyMongoError as e:
        print(f"\nMongoDB: not checked ({e.__class__.__name__})")
        good = False
    print("\nAll restrictions in place." if good else "\nSome checks differ from the least-privilege setup "
          "(run setup/sql/accounts.sql, then setup/init_db.py, as in README step 3; MongoDB stays open until "
          "setup/mongo_accounts.py is applied).")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
