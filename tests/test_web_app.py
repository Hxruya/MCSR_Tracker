"""The web app: accounts and login (CSRF, cookies, redirects), pages and their content, the Load / Refresh / Load
timeline buttons, the official leaderboard, and graceful failure when the API or MongoDB is down.
"""
import json
import re
from datetime import datetime, timezone

import pytest

from app.auth import safe_next
from app.importer import sync
from app.importer.api_client import ApiError, ApiNotFound
from tests.conftest import A, B, C, as_user, csrf, login, raw_match, register, seed_season, uuid_for


def ranked_series(n, start_id=1000, p=A, q=B):
    """n ranked matches between p and q, alternating winners, with a forfeit and a draw mixed in."""
    out = []
    for i in range(n):
        mid = start_id + i
        if i % 10 == 9:
            out.append(raw_match(mid, [p, q], None, forfeited=True, date=1790000000 + i * 600))
        elif i % 7 == 3:
            out.append(raw_match(mid, [p, q], q[0], forfeited=True, time_ms=200000, date=1790000000 + i * 600))
        else:
            out.append(raw_match(mid, [p, q], p[0] if i % 2 else q[0], time_ms=400000 + i * 1000,
                                 date=1790000000 + i * 600))
    return out


def store(conn, mongo_db, matches):
    for m in matches:
        assert sync.store_match(conn, mongo_db, m) == "inserted"


class FakeLeaderboardApi:
    """Stands in for ApiClient on the leaderboard: the shape of a real GET /leaderboard response."""
    calls = []

    def __init__(self, **kwargs):
        pass

    @classmethod
    def for_page(cls):
        return cls()

    def leaderboard(self, season=None):
        FakeLeaderboardApi.calls.append(season)
        users = [(1, "top", 2400, 2450), (2, "tied_a", 2000, 2100), (2, "tied_b", 2000, 2000), (4, "low", 1200, None)]
        return {"season": {"number": season or 12}, "users": [
            {"uuid": uuid_for(i), "nickname": n, "eloRate": 9999, "eloRank": 99, "country": "sg",
             "seasonResult": {"eloRate": elo, "eloRank": rank, "highest": peak}}
            for i, (rank, n, elo, peak) in enumerate(users, 1)]}


BACKEND_WORDS = ("$unwind", "$group", "$match", "pipeline", "MongoDB", "MariaDB", "SELECT ", "find()", "importer")


@pytest.fixture
def timeline_api(monkeypatch):
    monkeypatch.setattr("app.matches.ApiClient", TimelineApi)
    TimelineApi.calls, TimelineApi.down = [], False
    return TimelineApi


class TimelineApi:
    """Stands in for ApiClient on the match page: /matches/{id} with a two-event timeline (or the API down)."""
    calls, down = [], False

    def __init__(self, **kwargs):
        pass

    @classmethod
    def for_page(cls):
        return cls()

    def match(self, match_id):
        TimelineApi.calls.append(match_id)
        if TimelineApi.down:
            raise ApiError("down")
        return dict(raw_match(match_id, [A, B], A[0]), completions=[{"uuid": A[0], "time": 500000}], timelines=[
            {"uuid": A[0], "time": 60000, "type": "story.enter_the_nether"},
            {"uuid": B[0], "time": 70000, "type": "story.enter_the_nether"}])


def rivalry_setup(conn, mongo_db):
    seed_season(conn)
    # alpha vs bravo, oldest first: A A B draw B B B(forfeit) A
    winners = [A, A, B, None, B, B, B, A]
    for i, w in enumerate(winners, 1):
        sync.store_match(conn, mongo_db, raw_match(
            i, [A, B], w[0] if w else None, date=1790000000 + i * 100, forfeited=i == 7,
            time_ms={1: 400000, 2: 500000, 8: 600000}.get(i, 500000),
            overworld="SHIPWRECK" if i <= 2 else "VILLAGE"))
    for i in (20, 21):   # alpha beats charlie twice
        sync.store_match(conn, mongo_db, raw_match(i, [A, C], A[0], date=1790000000 + i * 100))
    sync.store_match(conn, mongo_db, raw_match(30, [A, B], B[0], match_type=3))           # private: excluded
    sync.store_match(conn, mongo_db, raw_match(31, [A], None, decayed=True, change=-5))   # decay: excluded


def seeds_setup(conn, mongo_db):
    seed_season(conn)
    for m in [
        # Village + Bridge: 400 s, 500 s, 900 s -> average 600 s (10:00), median 500 s (08:20)
        raw_match(1, [A, B], A[0], overworld="VILLAGE", nether="BRIDGE", time_ms=400000),
        raw_match(2, [A, B], B[0], overworld="VILLAGE", nether="BRIDGE", time_ms=500000),
        raw_match(3, [A, B], A[0], overworld="VILLAGE", nether="BRIDGE", time_ms=900000),
        # Shipwreck + Bridge: one completed win at 300 s; the 100 s forfeit win must not count
        raw_match(4, [A, B], A[0], overworld="SHIPWRECK", nether="BRIDGE", time_ms=300000),
        raw_match(5, [A, B], A[0], overworld="SHIPWRECK", nether="BRIDGE", forfeited=True, time_ms=100000),
        # Village + Stables: 700 s
        raw_match(6, [A, B], B[0], overworld="VILLAGE", nether="STABLES", time_ms=700000),
        # private match: excluded from the ranked view
        raw_match(7, [A, B], A[0], overworld="DESERT_TEMPLE", nether="HOUSING", match_type=3, time_ms=200000),
    ]:
        sync.store_match(conn, mongo_db, m)


def test_account_lifecycle(client):
    """Register, log in, log out; bad input is explained, and a wrong password and an unknown user get the same
    message (no way to probe which usernames exist)."""
    r = register(client, username="ab", password="short", confirm="other")
    body = r.get_data(as_text=True)
    assert r.status_code == 400 and "3-30 characters" in body and "at least 8" in body and "do not match" in body
    r = register(client)
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    for r in (login(client, password="wrong-password"), login(client, username="nobody_here")):
        assert r.status_code == 401 and "Invalid username or password" in r.get_data(as_text=True)
    assert login(client).status_code == 302
    page = client.get("/").get_data(as_text=True)
    assert "alice_1" in page and "Log Out" in page
    assert client.post("/logout", data={"csrf_token": csrf(client, "/")}).status_code == 302
    assert "Log Out" not in client.get("/").get_data(as_text=True)


def test_login_protects_pages_and_redirects_safely(client, conn, mongo_db):
    """Personal pages and every action that writes need a login; after logging in, ?next= is followed only if it is
    a path on this site (no open redirect)."""
    sync.store_match(conn, mongo_db, raw_match(1, [A, B], A[0]))
    for url in ("/bookmarks", "/account", "/notes"):
        r = client.get(url)
        assert r.status_code == 302 and f"/login?next={url}" in r.headers["Location"], url
    for url in (f"/players/{A[0]}/refresh", "/matches/1/timeline", "/players/load"):
        r = client.post(url, data={"csrf_token": csrf(client), "q": "x"})
        assert r.status_code == 302 and "/login" in r.headers["Location"], url
    assert "Log in</a> to keep private notes" in client.get("/matches/1").get_data(as_text=True)
    register(client)
    assert login(client, next_="/bookmarks").headers["Location"] == "/bookmarks"
    client.post("/logout", data={"csrf_token": csrf(client, "/")})
    assert login(client, next_="https://evil.example/").headers["Location"] == "/"
    assert safe_next("/players?q=x") == "/players?q=x"
    for bad in ("https://evil.com", "//evil.com", "/\\evil.com", "javascript:alert(1)", "", None):
        assert safe_next(bad) is None


def test_csrf_and_cookie_flags(client):
    r = client.post("/register", data={"username": "bob_1", "password": "longenough", "confirm": "longenough"})
    assert r.status_code == 400 and "form expired" in r.get_data(as_text=True)
    assert client.post("/logout").status_code == 400
    register(client)
    cookie = login(client).headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


@pytest.mark.parametrize("url", [
    "/matches?season=abc", "/matches?season=99", "/matches?from=2026-13-40", "/matches?from=2026-02-01&to=2026-01-01",
    "/matches?result=maybe&player=alpha", "/matches?result=win", "/matches?page=-3", "/matches?page=x",
    "/matches?type=bogus", "/matches?player=nobody_stored", "/players?q=a&page=0", "/fastest?season=zz",
    "/compare?a=alpha&b=alpha", "/compare?a=alpha&b=bravo&season=x", "/compare?a=alpha&b=bravo&from=nope",
    "/compare/form?a=ghost", "/leaderboard?page=0", "/players/" + uuid_for(1) + "?season=bad",
    "/seeds?overworld=NOT_A_THING", "/seeds?nether=x", "/seeds?variation=nope", "/seeds?type=casualish",
    "/seeds?page=0",
])
def test_invalid_filters_do_not_crash(client, conn, mongo_db, url):
    seed_season(conn)
    store(conn, mongo_db, [raw_match(1, [A, B], A[0])])
    r = client.get(url)
    assert r.status_code == 400, url
    assert 'class="flash error"' in r.get_data(as_text=True)


def test_no_backend_details_on_pages(client, conn, mongo_db):
    seed_season(conn)
    store(conn, mongo_db, ranked_series(5))
    for url in ("/", "/seeds", "/seeds?overworld=VILLAGE", "/matches", f"/players/{A[0]}", "/leaderboard",
                "/fastest", "/compare?a=alpha&b=bravo", "/matches/1000"):
        body = client.get(url).get_data(as_text=True)
        for word in BACKEND_WORDS:
            assert word not in body, f"{word!r} shown on {url}"


def test_pages_survive_mongodb_outage(client, conn, mongo_db, monkeypatch):
    """db_mongo.optional: MongoDB-backed sections fall back, the MariaDB parts still render; a match whose document
    is missing still shows its MariaDB facts; the MongoDB-only Notes pages say they're unavailable (503)."""
    from pymongo.errors import ServerSelectionTimeoutError

    def down(*args, **kwargs):
        raise ServerSelectionTimeoutError("simulated outage")
    seed_season(conn)
    store(conn, mongo_db, ranked_series(3) + [raw_match(77, [A, B], A[0])])
    mongo_db["matches"].delete_one({"_id": 77})
    body = client.get("/matches/77").get_data(as_text=True)
    assert "Seed details unavailable" in body and "alpha" in body
    for target in ("app.elo.history", "app.seeds.player_seed_types", "app.rivalry.top_rivals",
                   "app.facets.counts", "app.rivalry.seed_split", "app.notes.notes_for_match", "app.splits.analyse"):
        monkeypatch.setattr(target, down)
    a = client.get(f"/players/{A[0]}")
    assert a.status_code == 200 and "Rivals are unavailable right now" in a.get_data(as_text=True)
    assert "Split times are unavailable right now" in a.get_data(as_text=True)
    assert client.get("/matches").status_code == 200
    body = client.get("/compare?a=alpha&b=bravo").get_data(as_text=True)
    assert "Seed data is unavailable right now" in body and "4 ranked meetings" in body
    monkeypatch.setattr("app.notes.notes_coll", down)
    as_user(client, "viewer_1")
    for r in (client.get("/notes"), client.post("/matches/1000/notes", data={"csrf_token": csrf(client, "/notes"),
                                                                            "text": "hi"})):
        assert r.status_code == 503 and "Notes are unavailable right now" in r.get_data(as_text=True)


def test_home_invites_a_first_step(client, conn, mongo_db):
    """Home: search first, record run as a timer, and question cards built from real data."""
    seed_season(conn)
    store(conn, mongo_db, ranked_series(4))
    with conn.cursor() as cur:   # make alpha a fully loaded, top-rated player so the cards pick them
        cur.execute("UPDATE players SET profile_status = 'full', current_elo = 2000 WHERE uuid = %s", (A[0],))
    conn.commit()
    body = client.get("/").get_data(as_text=True)
    assert "Look up any MCSR Ranked player" in body and 'name="q"' in body
    assert "Fastest stored ranked win" in body
    assert "Who leads a rivalry?" in body and "alpha and bravo have met 4 times" in body


def test_search_never_calls_the_api(client, conn, monkeypatch):
    """Searching only reads stored players, even logged in (a partial name such as 'edc' would otherwise cost a
    call, or import the wrong player): it offers an explicit Load button instead."""
    called = []
    monkeypatch.setattr(sync, "fetch_player_on_demand", lambda *a, **k: called.append(a))
    body = client.get("/players?q=SomeNewPlayer").get_data(as_text=True)
    assert "to load “SomeNewPlayer” from MCSR Ranked" in body and "/players/load" not in body
    register(client)
    login(client)
    assert "Load “SomeNewPlayer” from MCSR Ranked" in client.get("/players?q=SomeNewPlayer").get_data(as_text=True)
    assert "from MCSR Ranked</button>" not in client.get("/players?q=not a name!").get_data(as_text=True)
    assert called == []


def test_refresh_player(client, conn, mongo_db, monkeypatch):
    """Refresh on a fully loaded player updates the profile and starts a 'refresh' import; on a basic player
    (only seen as an opponent) it loads the full profile and history instead."""
    started, loaded = [], []
    monkeypatch.setattr(sync, "store_profile", lambda api, c, m, uuid: {"uuid": uuid, "player_id": 1})
    monkeypatch.setattr(sync, "start_background_import", lambda p, mode, *a: started.append((p["uuid"], mode)))
    monkeypatch.setattr("app.players.fetch_and_start", lambda ident: loaded.append(ident))
    seed_season(conn)
    store(conn, mongo_db, [raw_match(1, [A, B], A[0])])
    with conn.cursor() as cur:
        cur.execute("UPDATE players SET profile_status = 'full' WHERE uuid = %s", (A[0],))
    conn.commit()
    as_user(client, "viewer_1")
    for uuid in (A[0], B[0]):
        r = client.post(f"/players/{uuid}/refresh", data={"csrf_token": csrf(client, f"/players/{uuid}")})
        assert r.status_code == 302 and r.headers["Location"].endswith(f"/players/{uuid}")
    assert started == [(A[0], "refresh")] and loaded == [B[0]]


def test_load_player(client, conn, monkeypatch):
    """Load needs a CSRF token, explains an unknown name, and otherwise starts a background full import."""
    started, found = [], [True]

    def fetch(ident, *_a, **_k):
        if not found[0]:
            raise ApiNotFound("no such user")
        return {"uuid": uuid_for(5), "nickname": ident, "player_id": 1}
    monkeypatch.setattr(sync, "fetch_player_on_demand", fetch)
    monkeypatch.setattr(sync, "start_background_import", lambda p, mode, *a: started.append((p["uuid"], mode)))
    register(client)
    login(client)
    assert client.post("/players/load", data={"q": "SomeNewPlayer"}).status_code == 400   # no token
    assert started == []
    r = client.post("/players/load", data={"csrf_token": csrf(client, "/players?q=SomeNewPlayer"),
                                           "q": "SomeNewPlayer"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/players/{uuid_for(5)}")
    assert started == [(uuid_for(5), "full")]
    found[0] = False
    r = client.post("/players/load", data={"csrf_token": csrf(client, "/players"), "q": "Nobody_123"},
                    follow_redirects=True)
    assert "No MCSR Ranked player called &#39;Nobody_123&#39;" in r.get_data(as_text=True)


def test_profile_season_of_only_draws(client, conn, mongo_db):
    """A season whose ranked games were all draws has no win rate (N/A): the profile must still render."""
    seed_season(conn, 11)
    seed_season(conn)
    store(conn, mongo_db, [raw_match(1, [A, B], None, season=11), raw_match(2, [A, B], A[0], season=12)])
    resp = client.get(f"/players/{A[0]}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    spec = json.loads(re.search(r'<script type="application/json" id="chart-seasons">(.*?)</script>', body, re.S)
                      .group(1))
    assert spec["labels"] == ["S11", "S12"] and spec["series"][0]["data"] == [None, 100.0]
    assert re.search(r"<td>S11</td>.*?N/A", body, re.S)   # the data table says N/A for that season


def test_profile_shows_interrupted_import(client, conn, mongo_db):
    """A run still 'running' with no live import thread (e.g. after a restart) is shown as interrupted."""
    seed_season(conn)
    store(conn, mongo_db, ranked_series(2))
    with conn.cursor() as cur:
        cur.execute("SELECT player_id FROM players WHERE uuid = %s", (A[0],))
        pid = cur.fetchone()["player_id"]
    mongo_db["import_runs"].insert_one({
        "player_uuid": A[0], "player_id": pid, "source": "full", "status": "running",
        "started_at": datetime.now(timezone.utc), "finished_at": None, "current_season": 9,
        "matches_seen": 120, "matches_inserted": 120, "matches_updated": 0, "matches_failed": 0, "errors": []})
    body = client.get(f"/players/{A[0]}").get_data(as_text=True)
    assert "The last import was interrupted (120 matches done)" in body


@pytest.mark.parametrize("n,enough", [(39, False), (40, True)])
def test_recent_form_needs_40(client, conn, mongo_db, n, enough):
    seed_season(conn)
    store(conn, mongo_db, ranked_series(n))
    body = client.get(f"/compare/form?a={A[0]}").get_data(as_text=True)
    if enough:
        assert "Insufficient history" not in body and "Latest 20" in body and "Previous 20" in body
    else:
        assert f"Insufficient history (need 40, have {n})" in body
        assert "Avg Elo change" not in body  # no partial comparison


def test_profile_split_analysis(client, conn, mongo_db):
    """The Splits section: medians per milestone against the opponent, and an empty state without timelines."""
    from datetime import datetime, timezone

    from app.importer.transform import mongo_detail_doc
    seed_season(conn)
    store(conn, mongo_db, ranked_series(1))
    body = client.get(f"/players/{A[0]}").get_data(as_text=True).split('id="splits"')[1].split("</section>")[0]
    assert "no split times" in body
    events = [(A[0], 90000, "story.enter_the_nether"), (B[0], 100000, "story.enter_the_nether"),
              (A[0], 120000, "nether.find_bastion"), (B[0], 150000, "nether.find_bastion")]
    detail = dict(raw_match(1000, [A, B], B[0]), completions=[{"uuid": B[0], "time": 500000}],
                  timelines=[{"uuid": u, "time": t, "type": k} for u, t, k in events])
    mongo_db["match_details"].insert_one(mongo_detail_doc(detail, datetime.now(timezone.utc)))
    body = client.get(f"/players/{A[0]}").get_data(as_text=True).split('id="splits"')[1].split("</section>")[0]
    assert "1 ranked matches with timelines" in body
    assert "01:30.000" in body and "01:40.000" in body          # Nether: alpha's and bravo's times
    assert "−10.0s" in body and "−30.0s" in body                # alpha ahead by 10 s, then 30 s at the bastion
    assert "Find bastion" in body and "20.0s</span> gained" in body


def test_compare_and_profile_pages(client, conn, mongo_db):
    rivalry_setup(conn, mongo_db)
    body = client.get("/compare?a=alpha&b=bravo").get_data(as_text=True)
    assert "8 ranked meetings" in body and "Running Score" in body and "Every Meeting" in body
    assert 'class="won">alpha</span><span class="vs">vs</span><span class="lost">bravo' in body   # match 8
    assert 'class="lost">alpha</span><span class="vs">vs</span><span class="won">bravo' in body   # match 7
    assert "alpha · 1 win" in body                                            # current streak
    assert "2 <span class=\"muted\">vs</span> 3" in body                      # longest streaks: alpha 2, bravo 3
    assert "42.86%" in body and "57.14%" in body                              # head-to-head win rates (draw excluded)
    assert "Shipwreck" in body
    profile = client.get(f"/players/{A[0]}").get_data(as_text=True)
    rivals = profile.split("Top Rivals")[1].split("Recent Matches")[0]
    assert rivals.index("bravo") < rivals.index("charlie") and "3–4–1" in rivals
    assert f"/compare?a={A[0]}&amp;b={B[0]}#h2h" in rivals
    assert client.get("/compare?a=alpha&b=bravo&page=0").status_code == 400
    assert "No ranked meetings" in client.get("/compare?a=bravo&b=charlie").get_data(as_text=True)
    body = client.get(f"/compare?a={A[0]}").get_data(as_text=True)                 # one player: suggest rivals
    assert "Who should alpha be compared with?" in body and f"a={A[0]}&amp;b={B[0]}" in body
    assert f'href="/compare?a={A[0]}"' in profile


def test_page_shows_ranking_and_links(client, conn, mongo_db):
    seeds_setup(conn, mongo_db)
    body = client.get("/seeds").get_data(as_text=True)
    table = body.split("Combinations Ranked by Average Winning Time")[1].split("End Game")[0]
    assert table.index("Shipwreck + Bridge") < table.index("Village + Bridge") < table.index("Village + Stables")
    assert "10:00.000" in table and "08:20.000" in table          # Village + Bridge average and median
    assert "small sample" in table and "+40.0s" in table and "-260.0s" in table
    assert "overworld=VILLAGE&amp;nether=BRIDGE" in table        # click-through to that combination's matches
    filtered = client.get("/seeds?overworld=SHIPWRECK&nether=BRIDGE").get_data(as_text=True)
    assert "2 found" in filtered and "/matches/4\"" in filtered and "/matches/5\"" in filtered
    assert "/matches/1\"" not in filtered


def test_load_timeline(client, conn, mongo_db, timeline_api):
    """Viewing a match never spends the API budget: an uncached timeline offers a button, which needs a login and
    a CSRF token, stores the timeline once, and explains an API outage instead of failing."""
    seed_season(conn)
    store(conn, mongo_db, [raw_match(78, [A, B], A[0])])
    body = client.get("/matches/78").get_data(as_text=True)
    assert "Village" in body and "Log in</a> to load it from MCSR Ranked" in body and "Load timeline" not in body
    as_user(client, "viewer_1")
    assert "Load timeline</button>" in client.get("/matches/78").get_data(as_text=True)
    assert client.post("/matches/78/timeline").status_code == 400   # no CSRF token
    assert timeline_api.calls == []
    timeline_api.down = True
    r = client.post("/matches/78/timeline", data={"csrf_token": csrf(client, "/matches/78")}, follow_redirects=True)
    assert r.status_code == 200 and "Could not reach MCSR Ranked right now" in r.get_data(as_text=True)
    timeline_api.down = False
    r = client.post("/matches/78/timeline", data={"csrf_token": csrf(client, "/matches/78")})
    assert r.status_code == 302 and r.headers["Location"].endswith("/matches/78#timeline")
    assert timeline_api.calls == [78, 78] and mongo_db["match_details"].count_documents({"_id": 78}) == 1
    body = client.get("/matches/78").get_data(as_text=True)
    assert "2 events" in body and "Enter the Nether" in body and "Load timeline</button>" not in body


def test_split_table_marks_faster_player():
    from app.matches import split_table
    p1, p2 = {"uuid": "a"}, {"uuid": "b"}
    events = [{"type": "story.enter_the_nether", "uuid": "a", "time": 90000},
              {"type": "story.enter_the_nether", "uuid": "b", "time": 120000},
              {"type": "story.enter_the_nether", "uuid": "a", "time": 300000},   # re-entry ignored (first only)
              {"type": "projectelo.timeline.death", "uuid": "b", "time": 150000}]
    rows = {r["label"]: r for r in split_table(events, [{"uuid": "a", "time": 480000}], [p1, p2])}
    nether = rows["Enter the Nether"]["cells"]
    assert nether[0] == {"ms": 90000, "best": True} and nether[1] == {"ms": 120000, "best": False}
    assert rows["Finish"]["cells"][0]["ms"] == 480000 and rows["Finish"]["cells"][1]["ms"] is None
    assert [c["n"] for c in rows["Deaths"]["cells"]] == [0, 1]
    assert "Find fortress" not in rows  # milestone nobody reached is left out


def test_vod_link_starts_at_the_match_start():
    """The API's `date` is when the match ended: the Twitch timestamp also takes off the match's duration."""
    from app.matches import safe_video
    video = {"url": "https://www.twitch.tv/videos/1", "startsAt": 1_000}
    assert safe_video(video, 1_000 + 3_600 + 600, 600_000)["offset"] == "1h0m0s"   # 1 h in, after a 10 min match
    assert safe_video({"url": "http://insecure.example/v"}, 2_000, None) is None


def test_leaderboard_is_the_official_one(client, conn, mongo_db, monkeypatch):
    """The API's ranking as given (ties share a rank, the season's Elo not the top-level one); stored players link
    to their profile, others don't; ?season= is passed through."""
    monkeypatch.setattr("app.leaderboard.ApiClient", FakeLeaderboardApi)
    FakeLeaderboardApi.calls = []
    seed_season(conn, 11)
    seed_season(conn)
    with conn.cursor() as cur:   # only "top" is stored
        sync.upsert_players(cur, [{"uuid": uuid_for(1), "nickname": "top", "current_elo": 1, "elo_rank": None,
                                   "country": None}])
    conn.commit()
    body = client.get("/leaderboard").get_data(as_text=True)
    assert [n for n in ("top", "tied_a", "tied_b", "low") if n in body] == ["top", "tied_a", "tied_b", "low"]
    assert body.count('<td class="rank">#2</td>') == 2 and '<td class="rank">#4</td>' in body   # 1, 2, 2, 4
    assert "<strong>2400</strong>" in body and "9999" not in body
    assert f'href="/players/{uuid_for(1)}"' in body and f'href="/players/{uuid_for(2)}"' not in body
    assert client.get("/leaderboard?season=11").status_code == 200 and FakeLeaderboardApi.calls == [None, 11]
    home = client.get("/").get_data(as_text=True)
    assert "Top of the Leaderboard" in home and "2400" in home


def test_leaderboard_cache_and_outage(client, conn, monkeypatch):
    """Finished seasons can't change, so they use the long cache. With the API down the page explains instead of
    failing, the home page still renders, and for 30 s nothing asks the API again (no repeated 5 s timeouts)."""
    calls = []

    def down(season):
        calls.append(season)
        raise ApiError("down")
    monkeypatch.setattr("app.leaderboard._fetch", down)
    resp = client.get("/leaderboard")
    assert resp.status_code == 503 and "can't be reached right now" in resp.get_data(as_text=True)
    home = client.get("/")
    assert home.status_code == 200 and "can't be reached right now" in home.get_data(as_text=True)
    assert calls == [None]                    # the home page reused the failure instead of waiting again
    seen = []
    monkeypatch.setattr("app.leaderboard.official", lambda season=None, past=False: seen.append((season, past)))
    seed_season(conn, 11)
    seed_season(conn)
    for url in ("/leaderboard", "/leaderboard?season=12", "/leaderboard?season=11"):
        client.get(url)
    assert seen == [(None, False), (12, False), (11, True)]
