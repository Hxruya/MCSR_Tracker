"""PyMySQL connection helpers. Each request (and each import thread) gets its own connection."""
import pymysql
from flask import current_app, g, has_app_context

from app.config import Config
from app.query_benchmarks.sql import cursor_class


def connect(database=None, cfg=Config, admin=False):
    """Open a new connection. `database=False` connects without selecting a database.
    admin=True uses the schema-owner account; only setup scripts need it."""
    if database is None:
        database = cfg.MARIADB_DATABASE
    return pymysql.connect(
        host=cfg.MARIADB_HOST,
        port=cfg.MARIADB_PORT,
        user=cfg.MARIADB_ADMIN_USER if admin else cfg.MARIADB_USER,
        password=cfg.MARIADB_ADMIN_PASSWORD if admin else cfg.MARIADB_PASSWORD,
        database=database or None,
        charset="utf8mb4",
        cursorclass=cursor_class(pymysql.cursors.DictCursor),   # timed for the query panel
        autocommit=False,
    )


def get_db():
    """Per-request connection, closed in teardown."""
    if "sql" not in g:
        g.sql = connect(current_app.config["MARIADB_DATABASE"])
    return g.sql


def close_db(_exc=None):
    conn = g.pop("sql", None) if has_app_context() else None
    if conn is not None:
        conn.close()


def query_all(sql, params=()):
    with get_db().cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(sql, params=()):
    with get_db().cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()
