import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

_DB_NAME = re.compile(r"^[A-Za-z0-9_]+$")


def _db_name(key, default):
    value = os.environ.get(key, default)
    if not _DB_NAME.match(value):
        raise ValueError(f"{key} must match [A-Za-z0-9_]+")
    return value


class Config:
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    TEMPLATES_AUTO_RELOAD = True  # pick up template edits without debug mode (no tracebacks exposed)

    MARIADB_HOST = os.environ.get("MARIADB_HOST", "localhost")
    MARIADB_PORT = int(os.environ.get("MARIADB_PORT", "3306"))
    MARIADB_USER = os.environ.get("MARIADB_USER", "mcsr_app")
    MARIADB_PASSWORD = os.environ.get("MARIADB_PASSWORD", "")
    # Least privilege (setup/sql/accounts.sql): the app account only reads and writes data; schema changes
    # (tables, views, routines, triggers) use the admin account.
    MARIADB_ADMIN_USER = os.environ.get("MARIADB_ADMIN_USER") or "mcsr_admin"
    MARIADB_ADMIN_PASSWORD = os.environ.get("MARIADB_ADMIN_PASSWORD", "")
    MARIADB_DATABASE = _db_name("MARIADB_DATABASE", "mcsr")
    MARIADB_TEST_DATABASE = _db_name("MARIADB_TEST_DATABASE", "mcsr_test")

    MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
    MONGO_ADMIN_URI = os.environ.get("MONGO_ADMIN_URI") or MONGO_URI   # collections, validators, indexes
    MONGO_DB = _db_name("MONGO_DB", "mcsr")
    MONGO_TEST_DB = _db_name("MONGO_TEST_DB", "mcsr_test")

    MCSR_API_BASE = os.environ.get("MCSR_API_BASE", "https://api.mcsrranked.com")

    # "Behind this page" query panel (the standalone query_benchmarks package). On unless QUERY_PANEL=0.
    QUERY_BENCHMARKS_ENABLED = os.environ.get("QUERY_PANEL", "1") != "0"
    QUERY_BENCHMARKS_SQL_LABEL = "MariaDB"
    QUERY_BENCHMARKS_HIDE = ("users",)   # account lookups: counted and timed, but the statement isn't shown
