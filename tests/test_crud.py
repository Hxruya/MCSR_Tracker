"""CRUD in both databases: bookmarks in MariaDB, private notes in MongoDB (create, read, update, delete, search),
and the account itself (change username and password, delete it with its bookmarks and notes)."""
import re

from app.importer import sync
from tests.conftest import A, B, as_user, csrf, login, raw_match, seed_season, uuid_for


def bookmark(client, uuid, note=""):
    return client.post("/bookmarks/new", data={"csrf_token": csrf(client, "/"), "uuid": uuid, "note": note})


def setup_players(conn, mongo_db):
    seed_season(conn)
    sync.store_match(conn, mongo_db, raw_match(1, [A, B], A[0]))


def my_bookmark_ids(client):
    return [int(x) for x in re.findall(r"/bookmarks/(\d+)/edit", client.get("/bookmarks").get_data(as_text=True))]


def setup_match(conn, mongo_db, match_id=1):
    seed_season(conn)
    sync.store_match(conn, mongo_db, raw_match(match_id, [A, B], A[0]))


def add_note(client, match_id, text, tags=""):
    return client.post(f"/matches/{match_id}/notes",
                       data={"csrf_token": csrf(client, f"/matches/{match_id}"), "text": text, "tags": tags})


def test_crud_happy_path(client, conn, mongo_db):
    setup_players(conn, mongo_db)
    as_user(client, "user_one")
    r = bookmark(client, A[0], "rival")
    assert r.status_code == 302
    page = client.get("/bookmarks").get_data(as_text=True)
    assert "alpha" in page and "rival" in page
    (bid,) = my_bookmark_ids(client)
    r = client.post(f"/bookmarks/{bid}/edit", data={"csrf_token": csrf(client, f"/bookmarks/{bid}/edit"),
                                                    "note": "<b>new</b> note"})
    assert r.status_code == 302
    page = client.get("/bookmarks").get_data(as_text=True)
    assert "&lt;b&gt;new&lt;/b&gt; note" in page  # autoescaped, never rendered as HTML
    r = client.post(f"/bookmarks/{bid}/delete", data={"csrf_token": csrf(client, "/bookmarks")})
    assert r.status_code == 302 and my_bookmark_ids(client) == []


def test_duplicate_bookmark_friendly_message(client, conn, mongo_db):
    setup_players(conn, mongo_db)
    as_user(client, "user_one")
    bookmark(client, A[0])
    r = bookmark(client, A[0])
    assert r.status_code == 302
    assert "already bookmarked" in client.get(r.headers["Location"]).get_data(as_text=True)
    assert len(my_bookmark_ids(client)) == 1


def test_note_crud_and_search(client, conn, mongo_db):
    setup_match(conn, mongo_db)
    as_user(client, "noter_1")
    assert add_note(client, 1, "Clean bastion route, slow End", "Bastion, End").status_code == 302
    add_note(client, 1, "Great zero cycle on a caged front tower", "zero")
    page = client.get("/matches/1").get_data(as_text=True)
    assert "Clean bastion route" in page and "#bastion" in page and "#zero" in page
    doc = mongo_db["notes"].find_one({"text": {"$regex": "^Clean"}})
    assert doc["tags"] == ["bastion", "end"] and doc["match_id"] == 1
    # full-text search ($text over text + tags) and tag filter
    found = client.get("/notes?q=caged").get_data(as_text=True)
    assert "Great zero cycle" in found and "Clean bastion" not in found
    tagged = client.get("/notes?tag=bastion").get_data(as_text=True)
    assert "Clean bastion" in tagged and "Great zero" not in tagged
    # update
    nid = str(doc["_id"])
    r = client.post(f"/notes/{nid}/edit", data={"csrf_token": csrf(client, f"/notes/{nid}/edit"),
                                                "text": "<b>edited</b>", "tags": "fixed"})
    assert r.status_code == 302 and mongo_db["notes"].find_one({"_id": doc["_id"]})["tags"] == ["fixed"]
    assert "&lt;b&gt;edited&lt;/b&gt;" in client.get("/notes").get_data(as_text=True)  # autoescaped
    # delete
    client.post(f"/notes/{nid}/delete", data={"csrf_token": csrf(client, "/notes")})
    assert mongo_db["notes"].count_documents({}) == 1


def test_note_validation(client, conn, mongo_db):
    setup_match(conn, mongo_db)
    as_user(client, "noter_1")
    add_note(client, 1, "   ")
    add_note(client, 1, "x" * 501)
    add_note(client, 1, "ok", ",".join(f"t{i}" for i in range(9)))
    assert mongo_db["notes"].count_documents({}) == 0
    assert add_note(client, 999, "no such match").status_code == 404


def test_bookmark_validation(client, conn, mongo_db):
    """A note over 255 characters is refused, and so is a bookmark on a player who isn't stored."""
    setup_players(conn, mongo_db)
    as_user(client, "user_one")
    bookmark(client, A[0], "x" * 256)
    assert my_bookmark_ids(client) == []
    assert bookmark(client, uuid_for(99)).status_code == 404


# ---------------------------------------------------------------- account settings

def settings(client, path, **data):
    """POST an account form and return the page it redirects to (with its flash messages)."""
    r = client.post(path, data={"csrf_token": csrf(client, "/account"), **data})
    assert r.status_code == 302
    return client.get(r.headers["Location"]).get_data(as_text=True)


def user_row(conn, username):
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, username FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
    conn.commit()
    return row


def test_change_username_and_password(app, conn, mongo_db):
    client, other = app.test_client(), app.test_client()
    as_user(other, "taken_1")
    as_user(client, "renamer_1")   # password: correct-horse
    assert "already taken" in settings(client, "/account/username", username="taken_1")
    assert "letters, digits or underscore" in settings(client, "/account/username", username="no spaces")
    assert "already your username" in settings(client, "/account/username", username="renamer_1")
    assert "now renamer_2" in settings(client, "/account/username", username="renamer_2")
    assert user_row(conn, "renamer_1") is None and user_row(conn, "renamer_2") is not None
    new = {"password": "battery-staple", "confirm": "battery-staple"}
    assert "current password is wrong" in settings(client, "/account/password", current="nope", **new)
    assert "do not match" in settings(client, "/account/password", current="correct-horse",
                                      password="battery-staple", confirm="battery-stapler")
    assert "at least 8" in settings(client, "/account/password", current="correct-horse", password="short",
                                    confirm="short")
    second = app.test_client()                                               # the same user on another device
    login(second, "renamer_2", "correct-horse")
    assert second.get("/account").status_code == 200
    assert "Password changed" in settings(client, "/account/password", current="correct-horse", **new)
    assert client.get("/account").status_code == 200                       # this browser stays logged in
    assert second.get("/account").status_code == 302                       # the other one is logged out
    client.post("/logout", data={"csrf_token": csrf(client, "/")})
    assert login(client, "renamer_2", "correct-horse").status_code == 401   # the old password no longer works
    assert login(client, "renamer_2", "battery-staple").status_code == 302  # new name, new password


def test_delete_account(app, conn, mongo_db, monkeypatch):
    """Deleting an account removes its MariaDB row, its bookmarks (ON DELETE CASCADE) and its MongoDB notes, and
    nobody else's. Notes go first, so with MongoDB down nothing is deleted at all."""
    from pymongo.errors import ServerSelectionTimeoutError

    from app import notes
    setup_players(conn, mongo_db)
    client, other = app.test_client(), app.test_client()
    for c, name in ((client, "leaver_1"), (other, "stayer_1")):
        as_user(c, name)
        bookmark(c, A[0], "followed")
        add_note(c, 1, f"note by {name}")
    leaver = user_row(conn, "leaver_1")["user_id"]

    def unchanged():
        return (user_row(conn, "leaver_1") is not None and len(my_bookmark_ids(client)) == 1
                and mongo_db["notes"].count_documents({"user_id": leaver}) == 1)

    class Down:   # every collection method fails, as with the server unreachable
        def __getattr__(self, _name):
            def fail(*_args, **_kwargs):
                raise ServerSelectionTimeoutError("down")
            return fail
    monkeypatch.setattr(notes, "notes_coll", lambda: Down())
    assert "be deleted right now" in settings(client, "/account/delete", password="correct-horse")
    assert unchanged()
    monkeypatch.undo()
    assert "password is wrong" in settings(client, "/account/delete", password="nope")
    assert unchanged()

    assert "have been deleted" in settings(client, "/account/delete", password="correct-horse")
    assert user_row(conn, "leaver_1") is None
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM bookmarks WHERE user_id = %s", (leaver,))
        assert cur.fetchone()["n"] == 0
    conn.commit()
    assert mongo_db["notes"].count_documents({"user_id": leaver}) == 0
    assert client.get("/account").status_code == 302                       # logged out
    assert len(my_bookmark_ids(other)) == 1 and mongo_db["notes"].count_documents({"text": "note by stayer_1"}) == 1
