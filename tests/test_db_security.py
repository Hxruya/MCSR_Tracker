"""Database security: least-privilege accounts, ownership enforced inside every query, parameterised SQL (injection
attempts are inert), hashed passwords, data minimisation.
"""
import re
from urllib.parse import quote

from app.importer import sync
from setup import check_privileges as cp, mongo_accounts as ma
from tests.conftest import A, B, TEST_MONGO, as_user, csrf, login, raw_match, register, seed_season


def my_bookmark_ids(client):
    return [int(x) for x in re.findall(r"/bookmarks/(\d+)/edit", client.get("/bookmarks").get_data(as_text=True))]


def setup_players(conn, mongo_db):
    seed_season(conn)
    sync.store_match(conn, mongo_db, raw_match(1, [A, B], A[0]))


def bookmark(client, uuid, note=""):
    return client.post("/bookmarks/new", data={"csrf_token": csrf(client, "/"), "uuid": uuid, "note": note})


def add_note(client, match_id, text, tags=""):
    return client.post(f"/matches/{match_id}/notes",
                       data={"csrf_token": csrf(client, f"/matches/{match_id}"), "text": text, "tags": tags})


def note_ids(client):
    return re.findall(r"/notes/([0-9a-f]{24})/edit", client.get("/notes").get_data(as_text=True))


class FakeApi:
    """Serves a fixed match list through the same interface as ApiClient (newest first, cursor paging)."""

    def __init__(self, matches):
        self.matches = sorted(matches, key=lambda m: m["id"], reverse=True)
        self.calls = []

    def leaderboard(self, season=None):
        self.calls.append(("leaderboard", season))
        return {"season": {"number": 12, "startsAt": 1780000000, "endsAt": 1790000000}, "users": []}

    def user_matches(self, uuid, season, before=None, count=100):
        self.calls.append(("matches", season, before))
        rows = [m for m in self.matches if m["season"] == season and any(p["uuid"] == uuid for p in m["players"])]
        if before is not None:
            rows = [m for m in rows if m["id"] < before]
        return rows[:count]

    def match(self, match_id):
        self.calls.append(("match", match_id))
        m = next(m for m in self.matches if m["id"] == match_id)
        return dict(m, completions=[], timelines=[{"uuid": m["players"][0]["uuid"], "time": 1000,
                                                   "type": "story.enter_the_nether"}])

    def user(self, ident):
        self.calls.append(("user", ident))
        return {"uuid": ident, "nickname": "outsider", "eloRate": None, "eloRank": None, "country": None}


def test_privilege_checker():
    """check_privileges.py reads each database's error code to tell "denied" from "allowed to try", and every
    probe meant to be denied targets an object that doesn't exist (or, to create, one that already does), so
    running the checker can't change data."""
    assert cp.classify_sql(None, (1142,), ()) == "allowed"
    assert cp.classify_sql(1142, (1142,), (1051,)) == "denied"        # command denied to user
    assert cp.classify_sql(1051, (1142,), (1051,)) == "allowed"       # unknown table: it was allowed to try
    assert cp.classify_sql(2013, (1142,), (1051,)) == "error 2013"
    assert cp.classify_mongo(None) == "allowed"
    assert cp.classify_mongo(26) == "allowed"                         # NamespaceNotFound: it was authorised
    assert cp.classify_mongo(13) == "denied"                          # Unauthorized
    assert all(cp.PROBE in stmt or "IF NOT EXISTS" in stmt for _, stmt, *_ in cp.SQL_PROBES[2:])


def test_mongo_app_role_is_data_only():
    privs = ma.app_role_privileges(("mcsr", "mcsr_test"))
    assert {p["resource"]["db"] for p in privs} == {"mcsr", "mcsr_test"}
    for p in privs:
        assert sorted(p["actions"]) == ["find", "insert", "remove", "update"]   # no DDL actions
    assert ma.admin_role_roles(("mcsr",)) == [{"role": "dbOwner", "db": "mcsr"}]


def test_users_only_reach_their_own_data(app, conn, mongo_db):
    """Every query on a user's data is scoped to the logged-in user, in both databases: another user can't see,
    search, edit or delete my bookmarks (MariaDB) or notes (MongoDB), and a forged user_id in a form is ignored."""
    setup_players(conn, mongo_db)
    owner, intruder = app.test_client(), app.test_client()
    as_user(owner, "owner_1")
    as_user(intruder, "intruder_1")
    bookmark(owner, A[0], "mine")
    add_note(owner, 1, "secret plan", "private")
    (bid,), (nid,) = my_bookmark_ids(owner), note_ids(owner)
    token = csrf(intruder, "/bookmarks")
    for url in (f"/bookmarks/{bid}/edit", f"/notes/{nid}/edit", "/notes/not-an-object-id/edit"):
        assert intruder.get(url).status_code == 404
    for url, data in ((f"/bookmarks/{bid}/edit", {"note": "hacked"}), (f"/bookmarks/{bid}/delete", {}),
                      (f"/notes/{nid}/edit", {"text": "hacked"}), (f"/notes/{nid}/delete", {})):
        assert intruder.post(url, data={"csrf_token": token, **data}).status_code == 404
    assert "secret plan" not in intruder.get("/matches/1").get_data(as_text=True)
    assert "secret plan" not in intruder.get("/notes?q=secret").get_data(as_text=True)
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM users WHERE username = 'owner_1'")
        owner_id = cur.fetchone()["user_id"]
    conn.commit()
    intruder.post("/bookmarks/new", data={"csrf_token": csrf(intruder, "/"), "uuid": B[0], "user_id": owner_id})
    assert len(my_bookmark_ids(owner)) == 1 and len(my_bookmark_ids(intruder)) == 1   # it went to the intruder
    assert "mine" in owner.get("/bookmarks").get_data(as_text=True)
    assert mongo_db["notes"].count_documents({"text": "secret plan"}) == 1


def test_account_settings_need_login_csrf_and_own_password(app, conn, mongo_db):
    """Changing or deleting an account needs a session and a CSRF token; deleting also needs the account's own
    password, and acts only on the logged-in user: a forged user_id in the form is ignored."""
    anon, owner, intruder = app.test_client(), app.test_client(), app.test_client()
    as_user(owner, "owner_1")
    as_user(intruder, "intruder_1")
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM users WHERE username = 'owner_1'")
        owner_id = cur.fetchone()["user_id"]
    conn.commit()
    for path in ("/account/username", "/account/password", "/account/delete"):
        r = anon.post(path, data={"csrf_token": csrf(anon), "username": "x_user", "password": "correct-horse"})
        assert r.status_code == 302 and "/login" in r.headers["Location"]
        assert intruder.post(path, data={"username": "x_user"}).status_code == 400   # no CSRF token
    def usernames():
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM users ORDER BY username")
            names = [u["username"] for u in cur.fetchall()]
        conn.commit()
        return names

    intruder.post("/account/delete", data={"csrf_token": csrf(intruder, "/account"), "password": "wrong-password"})
    assert usernames() == ["intruder_1", "owner_1"]                     # a wrong password deletes nothing
    intruder.post("/account/delete", data={"csrf_token": csrf(intruder, "/account"), "user_id": owner_id,
                                           "password": "correct-horse"})
    assert usernames() == ["owner_1"]                                   # the intruder deleted only themself


def test_login_locks_after_five_failures(client, conn, mongo_db, monkeypatch):
    """5 wrong passwords lock that username for 15 minutes: even the right password is refused, an unknown
    username locks the same way (so the lock reveals nothing), other users can still log in, and a success
    clears the count."""
    import time

    from app import auth
    register(client, username="target_1", password="correct-horse")
    register(client, username="other_1", password="correct-horse")
    for _ in range(4):
        assert login(client, "target_1", "wrong-guess").status_code == 401
    assert login(client, "target_1", "correct-horse").status_code == 302      # 4 failures: not locked, and reset
    client.post("/logout", data={"csrf_token": csrf(client, "/")})
    for _ in range(5):
        assert login(client, "target_1", "wrong-guess").status_code == 401
    r = login(client, "TARGET_1", "correct-horse")                             # usernames are case-insensitive
    assert r.status_code == 429 and "Too many failed attempts" in r.get_data(as_text=True)
    # the column also ignores accents, but a name sign-up wouldn't allow never reaches the account
    for variant in ("tárget_1", "ｔarget_1"):
        assert login(client, variant, "correct-horse").status_code == 401
    # attempts are counted before the password check, so parallel requests can't all get past the limit
    assert [auth.start_attempt("racer_1") for _ in range(6)][-1] > 0
    for _ in range(5):
        login(client, "nobody_here", "x")
    assert login(client, "nobody_here", "x").status_code == 429
    assert login(client, "other_1", "correct-horse").status_code == 302
    client.post("/logout", data={"csrf_token": csrf(client, "/")})
    later = time.monotonic() + auth.LOCK_SECONDS + 1
    monkeypatch.setattr(auth.time, "monotonic", lambda: later)
    assert login(client, "target_1", "correct-horse").status_code == 302      # the lock has expired


INJECTIONS = ("x' OR '1'='1", "'; DROP TABLE players; --", "' UNION SELECT password_hash FROM users -- ", "%_%")


def test_sql_injection_attempts_are_inert(client, conn, mongo_db):
    """Every value reaches MariaDB as a bound parameter, never as SQL text: injection payloads in a search, a filter,
    a login form or a stored note are matched or stored literally, and the tables are untouched."""
    setup_players(conn, mongo_db)
    register(client, username="victim_1", password="victim-password")
    for payload in INJECTIONS:
        search = client.get(f"/players?q={quote(payload)}").get_data(as_text=True)
        assert "No stored players match" in search and ">alpha</a>" not in search
        r = client.get(f"/matches?player={quote(payload)}")
        assert r.status_code == 400 and "not stored" in r.get_data(as_text=True)
        assert login(client, username=payload, password=payload).status_code == 401
    as_user(client, "user_one")
    bookmark(client, A[0], INJECTIONS[1])
    with conn.cursor() as cur:
        cur.execute("SELECT note FROM bookmarks")
        assert cur.fetchone()["note"] == INJECTIONS[1]                # stored as text, not run
        cur.execute("SELECT (SELECT COUNT(*) FROM players) AS players, (SELECT COUNT(*) FROM users) AS users")
        assert cur.fetchone() == {"players": 2, "users": 2}
    conn.commit()


def test_passwords_are_stored_only_as_salted_hashes(client, conn, monkeypatch):
    """The users table never holds a password: each is a salted hash, so equal passwords give different hashes.
    A login for an unknown username still checks a hash, so its response time doesn't reveal that the name is free."""
    for name in ("first_user", "second_user"):
        register(client, username=name, password="the-same-password")
    with conn.cursor() as cur:
        cur.execute("SELECT password_hash FROM users ORDER BY username")
        hashes = [r["password_hash"] for r in cur.fetchall()]
    conn.commit()
    assert len(hashes) == 2 and hashes[0] != hashes[1]
    assert all("the-same-password" not in h and h.startswith(("scrypt:", "pbkdf2:")) for h in hashes)

    from app import auth
    checked = []
    real_check = auth.check_password_hash
    monkeypatch.setattr(auth, "check_password_hash", lambda h, pw: checked.append(h) or real_check(h, pw))
    assert login(client, username="nobody_here", password="whatever-pw").status_code == 401
    assert checked == [auth.DUMMY_HASH]


def test_linked_accounts_are_never_stored_or_shown(client, conn, mongo_db):
    """Data minimisation: linked Discord/Twitch/YouTube accounts are personal data no feature uses, so the importer
    drops them, init_db removes them from profiles stored earlier, and the profile page never reads them."""
    from app.config import Config
    from app import db_mongo
    from setup import init_db

    class ProfileApi(FakeApi):
        def user(self, ident):
            return {"uuid": A[0], "nickname": "alpha", "eloRate": 1500, "eloRank": 9, "country": "sg",
                    "connections": {"twitch": {"id": "1", "name": "alpha_on_twitch"}},
                    "achievements": {"total": [{"id": "x"}]}, "statistics": {}}

        def user_seasons(self, ident):
            return {"seasonResults": {}}
    sync.store_profile(ProfileApi([]), conn, mongo_db, A[0])
    stored = mongo_db["players"].find_one({"_id": A[0]})["profile"]
    assert "connections" not in stored and stored["achievements"] == {"total": [{"id": "x"}]}

    mongo_db["players"].update_one({"_id": A[0]}, {"$set": {"profile.connections": {"twitch": {"name": "old_copy"}}}})
    body = client.get(f"/players/{A[0]}").get_data(as_text=True)
    assert "old_copy" not in body and "1 achievements earned" in body
    init_db.init_mongo(db_mongo.client(Config.MONGO_ADMIN_URI)[TEST_MONGO])   # cleans profiles stored earlier
    assert "connections" not in mongo_db["players"].find_one({"_id": A[0]})["profile"]
