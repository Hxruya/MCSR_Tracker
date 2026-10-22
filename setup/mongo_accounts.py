"""Create least-privilege MongoDB roles and users. Run it yourself; it asks for the passwords.

    python setup/mongo_accounts.py                         # local server without access control yet
    python setup/mongo_accounts.py --uri mongodb://root:...@localhost:27017/?authSource=admin

Roles (defined in the `admin` database so one role covers both databases):
    mcsrApp    find / insert / update / remove on every collection of mcsr and mcsr_test: what the web app and
               importer need. It cannot create or drop collections, change validators or build indexes.
    mcsrAdmin  dbOwner on mcsr and mcsr_test: init_db (collections, $jsonSchema validators, indexes), restore and
               the test suite (which creates and drops mcsr_test).

Afterwards: set MONGO_URI (app user) and MONGO_ADMIN_URI (admin user) in .env, e.g.
    mongodb://mcsr_app:<password>@localhost:27017/?authSource=admin
(URL-encode special characters in the password), then turn on access control in mongod.cfg:
    security:
      authorization: enabled
and restart the MongoDB service. Until then the users exist but MongoDB doesn't ask for them.
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the project root

from pymongo import MongoClient  # noqa: E402
from pymongo.errors import OperationFailure  # noqa: E402

from app.config import Config  # noqa: E402

DATABASES = (Config.MONGO_DB, Config.MONGO_TEST_DB)
APP_ACTIONS = ["find", "insert", "update", "remove"]


def app_role_privileges(databases=DATABASES):
    return [{"resource": {"db": db, "collection": ""}, "actions": APP_ACTIONS} for db in databases]


def admin_role_roles(databases=DATABASES):
    return [{"role": "dbOwner", "db": db} for db in databases]


def upsert_role(admin, name, privileges, roles):
    try:
        admin.command("createRole", name, privileges=privileges, roles=roles)
        return "created"
    except OperationFailure as e:
        if e.code != 51002:  # role already exists
            raise
        admin.command("updateRole", name, privileges=privileges, roles=roles)
        return "updated"


def upsert_user(admin, name, password, role):
    try:
        admin.command("createUser", name, pwd=password, roles=[{"role": role, "db": "admin"}])
        return "created"
    except OperationFailure as e:
        if e.code != 51003:  # user already exists
            raise
        admin.command("updateUser", name, pwd=password, roles=[{"role": role, "db": "admin"}])
        return "updated"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uri", default="mongodb://localhost:27017", help="a URI allowed to manage users")
    args = parser.parse_args()
    admin = MongoClient(args.uri, serverSelectionTimeoutMS=3000)["admin"]
    print("mcsrApp role:", upsert_role(admin, "mcsrApp", app_role_privileges(), []))
    print("mcsrAdmin role:", upsert_role(admin, "mcsrAdmin", [], admin_role_roles()))
    for user, role in (("mcsr_app", "mcsrApp"), ("mcsr_admin", "mcsrAdmin")):
        pw = getpass.getpass(f"Password for MongoDB user {user}: ")
        if len(pw) < 12:
            print("Use at least 12 characters.")
            return 1
        print(f"user {user}:", upsert_user(admin, user, pw, role))
    print("Done. Next: set MONGO_URI / MONGO_ADMIN_URI in .env and enable authorization (see this file's header).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
