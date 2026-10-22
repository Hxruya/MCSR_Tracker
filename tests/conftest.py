"""Fixtures: separate MariaDB/Mongo test databases, created per session and dropped at the end.

The real databases (MARIADB_DATABASE / MONGO_DB) are never touched.
"""
import re

import pytest

from app import create_app, db_mongo, db_sql
from app.config import Config
from setup import init_db

TEST_SQL = Config.MARIADB_TEST_DATABASE
TEST_MONGO = Config.MONGO_TEST_DB
assert TEST_SQL != Config.MARIADB_DATABASE and TEST_MONGO != Config.MONGO_DB, "test DB names must differ"

TABLES = ["mv_player_stats_season", "mv_player_stats_lifetime", "bookmarks", "match_players",
          "matches", "player_season_results", "players", "seasons", "users"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never reach the real MCSR Ranked API: any real HTTP request fails at once as an ApiError
    (pages then show their "unavailable" state). Tests that need API data pass a fake client or session."""
    import requests

    from app.importer.api_client import ApiError

    def refuse(self, url, *args, **kwargs):
        raise ApiError(f"network disabled in tests: {url}")
    monkeypatch.setattr(requests.Session, "get", refuse)


@pytest.fixture(autouse=True)
def fresh_season_cache(monkeypatch):
    """The importer remembers the current season for an hour, and the leaderboard a failed fetch for 30 s;
    every test starts without either."""
    from app import leaderboard
    from app.importer import sync
    monkeypatch.setattr(sync, "_season_cache", {"number": None, "at": 0.0})
    monkeypatch.setattr(leaderboard, "_failed_at", {})


@pytest.fixture(autouse=True)
def fresh_login_failures():
    """Failed logins are counted per process (app/auth.py); every test starts with none."""
    from app import auth
    auth._failures.clear()
    yield
    auth._failures.clear()


@pytest.fixture(scope="session")
def databases():
    init_db.drop_mariadb(TEST_SQL)
    mongo = db_mongo.client(Config.MONGO_ADMIN_URI)   # creates and drops the test database
    mongo.drop_database(TEST_MONGO)
    init_db.init_mariadb(TEST_SQL)
    init_db.init_mongo(mongo[TEST_MONGO])
    yield
    init_db.drop_mariadb(TEST_SQL)
    mongo.drop_database(TEST_MONGO)


@pytest.fixture
def conn(databases):
    c = db_sql.connect(TEST_SQL)
    with c.cursor() as cur:
        for t in TABLES:
            cur.execute(f"DELETE FROM {t}")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def mongo_db(databases):
    db = db_mongo.client(Config.MONGO_URI)[TEST_MONGO]
    for name in ("matches", "players", "match_details", "notes", "elo_history", "import_runs"):
        db[name].delete_many({})
    return db


@pytest.fixture
def app(conn, mongo_db):
    return create_app({"TESTING": True, "MARIADB_DATABASE": TEST_SQL, "MONGO_DB": TEST_MONGO,
                       "QUERY_BENCHMARKS_ENABLED": False})   # the panel has its own tests


@pytest.fixture
def client(app):
    return app.test_client()


def uuid_for(n):
    return f"{n:032x}"


# The players most tests use: (uuid, nickname)
A, B, C = (uuid_for(1), "alpha"), (uuid_for(2), "bravo"), (uuid_for(3), "charlie")


def raw_match(match_id, players, winner=None, *, season=12, match_type=2, forfeited=False, time_ms=500000,
              date=1790000000, elo=1500, change=10, decayed=False, overworld="VILLAGE", nether="BRIDGE"):
    """An API-shaped MatchInfo (fields as in tests/fixtures). players: list of (uuid, nickname)."""
    changes = []
    if match_type == 2:
        for uuid, _ in players:
            delta = None if change is None else (0 if winner is None else (change if uuid == winner else -change))
            changes.append({"uuid": uuid, "change": delta, "eloRate": elo})
    return {
        "id": match_id, "type": match_type, "season": season, "category": "ANY", "gameMode": "default",
        "date": date,
        "players": [{"uuid": u, "nickname": n, "roleType": 0, "eloRate": elo, "eloRank": None, "country": "sg"}
                    for u, n in players],
        "spectators": [], "result": {"uuid": winner, "time": time_ms}, "forfeited": forfeited, "decayed": decayed,
        "rank": {"season": None, "allTime": None}, "changes": changes, "beginner": False, "botSource": None,
        "seed": {"id": f"seed{match_id}", "overworld": overworld, "nether": nether, "endTowers": [76, 79, 82, 85],
                 "variations": ["biome:structure:plains", "bastion:triple:1"]},
        "vod": [], "tag": None, "replayExists": 0, "seedType": overworld, "bastionType": nether,
    }


def seed_season(conn, n=12):
    with conn.cursor() as cur:
        cur.execute("INSERT IGNORE INTO seasons (season_id, label) VALUES (%s, %s)", (n, f"Season {n}"))
    conn.commit()


_TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf(client, path="/login"):
    """Fetch a page and return its CSRF token."""
    html = client.get(path).get_data(as_text=True)
    return _TOKEN.search(html).group(1)


def register(client, username="alice_1", password="correct-horse", confirm=None):
    return client.post("/register", data={"csrf_token": csrf(client, "/register"), "username": username,
                                          "password": password, "confirm": confirm or password})


def login(client, username="alice_1", password="correct-horse", next_=None):
    data = {"csrf_token": csrf(client), "username": username, "password": password}
    if next_:
        data["next"] = next_
    return client.post("/login", data=data)


def as_user(client, name):
    """Register and log in a test user."""
    register(client, username=name)
    login(client, username=name)
