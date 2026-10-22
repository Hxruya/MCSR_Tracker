"""Import logic behind the Load button (search, leaderboard) and the profile's Refresh button.

Consistency between the stores (they share no transaction): per match, MongoDB is written first (upsert), then
one MariaDB transaction (season, players, matches, match_players). So every match in MariaDB has its MongoDB
document. If the MariaDB write fails, a document that write just created is deleted again (a compensating
action), so a failure leaves no orphan in MongoDB either. Every write is an upsert, so re-running repairs anything
an interruption left behind; setup/check_consistency.py verifies that both stores hold the same matches.
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from app import cache, db_mongo, db_sql
from app.config import Config
from app.importer import elo_history, transform
from app.importer.api_client import ApiClient

log = logging.getLogger(__name__)

PAGE_SIZE = 100
# Refresh re-reads matches from this long before the player's last finished import started. The API's match
# `date` is when the match ended, so the margin only has to cover a clock difference between this server and
# the API, and a delay before a finished match is listed: seconds in practice. Too short would skip matches for
# good; too long only re-reads a few stored matches, which arrive on the first page fetched anyway.
REFRESH_MARGIN = timedelta(hours=1)
# Timelines (splits, End-game stats) only come from /matches/{id}, one call per match: all of edcr's ~8,000
# would take ~2.8 h of API time. So an import loads them for the player's latest ranked matches only, which
# makes End-game stats a consistent sample (the latest 100 per player); the match page's button loads others.
RECENT_TIMELINES = 100
_season_cache = {"number": None, "at": 0.0}
_running = set()
_running_lock = threading.Lock()


# ---------------------------------------------------------------- seasons

def _upsert_season(cur, number, starts_at=None, ends_at=None):
    cur.execute(
        "INSERT INTO seasons (season_id, label, start_at, end_at) VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE start_at = COALESCE(VALUES(start_at), start_at), "
        "end_at = COALESCE(VALUES(end_at), end_at)",
        (number, f"Season {number}",
         transform.naive_utc(transform.epoch_to_utc(starts_at)),
         transform.naive_utc(transform.epoch_to_utc(ends_at))),
    )


def current_season(api, conn):
    """Current season number; creates any missing season rows 1..current (once)."""
    if _season_cache["number"] and time.monotonic() - _season_cache["at"] < 3600:
        return _season_cache["number"]
    meta = api.leaderboard()["season"]
    number = int(meta["number"])
    with conn.cursor() as cur:
        _upsert_season(cur, number, meta.get("startsAt"), meta.get("endsAt"))
        cur.execute("SELECT season_id FROM seasons WHERE start_at IS NOT NULL")
        have = {r["season_id"] for r in cur.fetchall()}
        for n in range(1, number):
            if n not in have:
                m = api.leaderboard(n)["season"]
                _upsert_season(cur, n, m.get("startsAt"), m.get("endsAt"))
    conn.commit()
    _season_cache.update(number=number, at=time.monotonic())
    return number


# ---------------------------------------------------------------- players

def upsert_players(cur, rows, full=False):
    """Insert/update players from transform.player_row dicts. Returns {uuid: player_id}."""
    if not rows:
        return {}
    status = "full" if full else "minimal"
    cur.executemany(
        "INSERT INTO players (uuid, nickname, current_elo, elo_rank, country, profile_status, profile_fetched_at) "
        "VALUES (%(uuid)s, %(nickname)s, %(current_elo)s, %(elo_rank)s, %(country)s, %(status)s, %(fetched)s) "
        "ON DUPLICATE KEY UPDATE nickname = VALUES(nickname), "
        "current_elo = COALESCE(VALUES(current_elo), current_elo), "
        "elo_rank = COALESCE(VALUES(elo_rank), elo_rank), "
        "country = COALESCE(VALUES(country), country), "
        "profile_status = IF(VALUES(profile_status) = 'full', 'full', profile_status), "
        "profile_fetched_at = COALESCE(VALUES(profile_fetched_at), profile_fetched_at)",
        [dict(r, status=status, fetched=transform.naive_utc(datetime.now(timezone.utc)) if full else None)
         for r in rows],
    )
    uuids = [r["uuid"] for r in rows]
    placeholders = ", ".join(["%s"] * len(uuids))
    cur.execute(f"SELECT player_id, uuid FROM players WHERE uuid IN ({placeholders})", uuids)
    return {r["uuid"]: r["player_id"] for r in cur.fetchall()}


def store_profile(api, conn, mongo_db, identifier):
    """Fetch /users/{id} and /users/{id}/seasons; store as a full player. Returns the players row."""
    profile = api.user(identifier)
    seasons = api.user_seasons(profile["uuid"])
    current_season(api, conn)
    now = datetime.now(timezone.utc)
    mongo_db["players"].replace_one(
        {"_id": profile["uuid"]},
        {"_id": profile["uuid"], "profile": transform.stored_profile(profile), "seasons": seasons, "fetched_at": now},
        upsert=True,
    )
    try:
        with conn.cursor() as cur:
            ids = upsert_players(cur, [transform.player_row(profile)], full=True)
            player_id = ids[profile["uuid"]]
            for row in transform.season_result_rows(seasons):
                _upsert_season(cur, row["season_id"])
                cur.execute(
                    "INSERT INTO player_season_results (player_id, season_id, final_elo, final_rank, "
                    "highest_elo, lowest_elo, phase_points) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE final_elo = VALUES(final_elo), final_rank = VALUES(final_rank), "
                    "highest_elo = VALUES(highest_elo), lowest_elo = VALUES(lowest_elo), "
                    "phase_points = VALUES(phase_points)",
                    (player_id, row["season_id"], row["final_elo"], row["final_rank"],
                     row["highest_elo"], row["lowest_elo"], row["phase_points"]),
                )
            cur.execute("SELECT * FROM players WHERE player_id = %s", (player_id,))
            player = cur.fetchone()
        conn.commit()
        return player
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------- matches

def store_match(conn, mongo_db, raw, imported_at=None, api=None):
    """Upsert one MatchInfo into Mongo then MariaDB. Returns 'inserted', 'updated' or 'unchanged'.

    `api` is only used when the winner is not a listed player: one /users call
    fetches their nickname so they can be stored as a minimal player.
    """
    imported_at = imported_at or datetime.now(timezone.utc)
    doc = transform.mongo_match_doc(raw, imported_at)
    row = transform.match_row(raw)
    mp_rows = transform.match_player_rows(raw)
    profiles = [transform.player_row(p) for p in transform.unique_players(raw)]
    outsider = transform.outside_winner(raw)
    if outsider:
        if api is None:
            raise ValueError(f"match {raw.get('id')}: winner {outsider} is not a listed player")
        profiles.append(transform.player_row(api.user(outsider)))
    # Upsert: $set rewrites every field transform produces (as a replace would), and $setOnInsert stamps
    # first_imported_at only when this call creates the document, so re-imports keep the original date.
    written = mongo_db["matches"].update_one(
        {"_id": doc["_id"]},
        {"$set": {k: v for k, v in doc.items() if k != "_id"}, "$setOnInsert": {"first_imported_at": imported_at}},
        upsert=True)
    try:
        with conn.cursor() as cur:
            _upsert_season(cur, row["season_id"])
            ids = upsert_players(cur, profiles)
            cur.execute(
                "INSERT INTO matches (match_id, season_id, match_type, played_at, duration_ms, result_type) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE season_id = VALUES(season_id), match_type = VALUES(match_type), "
                "played_at = VALUES(played_at), duration_ms = VALUES(duration_ms), "
                "result_type = VALUES(result_type)",
                (row["match_id"], row["season_id"], row["match_type"], row["played_at"],
                 row["duration_ms"], row["result_type"]),
            )
            outcome = {1: "inserted", 2: "updated"}.get(cur.rowcount, "unchanged")
            cur.executemany(
                "INSERT INTO match_players (match_id, player_id, elo_before, elo_change, outcome, forfeited, "
                "finish_time_ms) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE elo_before = VALUES(elo_before), elo_change = VALUES(elo_change), "
                "outcome = VALUES(outcome), forfeited = VALUES(forfeited), finish_time_ms = VALUES(finish_time_ms)",
                [(row["match_id"], ids[r["uuid"]], r["elo_before"], r["elo_change"], r["outcome"],
                  r["forfeited"], r["finish_time_ms"]) for r in mp_rows],
            )
            if outcome == "unchanged" and cur.rowcount > 0:   # only a participant row changed (e.g. Elo)
                outcome = "updated"
        conn.commit()
    except Exception:
        conn.rollback()
        # Compensate: a document created just now has no MariaDB row, so remove it. A replaced document belonged
        # to a match MariaDB already holds (its old row survives the rollback), so it stays.
        if written.upserted_id is not None:
            try:
                mongo_db["matches"].delete_one({"_id": doc["_id"]})
            except Exception:   # MongoDB failed too: the orphan stays until a re-run (check_consistency.py)
                log.exception("could not remove the MongoDB document of failed match %s", doc["_id"])
        raise
    elo_history.record_match(mongo_db, row, mp_rows, outcome)  # derived from the committed rows
    return outcome


def store_timeline(api, mongo_db, match_id):
    """Fetch /matches/{id} and cache it in match_details (timelines, completions, End splits). Returns the doc."""
    doc = transform.mongo_detail_doc(api.match(match_id), datetime.now(timezone.utc))
    mongo_db["match_details"].replace_one({"_id": doc["_id"]}, doc, upsert=True)
    return doc


def load_recent_timelines(api, conn, mongo_db, player):
    """Timelines for the player's latest RECENT_TIMELINES ranked, non-decay matches that don't have one yet.
    A failed match is logged and skipped. Returns (loaded, errors)."""
    with conn.cursor() as cur:
        cur.execute("SELECT match_id FROM v_ranked_match_players WHERE player_id = %s "
                    "ORDER BY played_at DESC, match_id DESC LIMIT %s",
                    (player["player_id"], RECENT_TIMELINES))
        ids = [r["match_id"] for r in cur.fetchall()]
    conn.commit()
    have = set(mongo_db["match_details"].distinct("_id", {"_id": {"$in": ids}}))
    loaded, errors = 0, []
    for match_id in ids:
        if match_id in have:
            continue
        try:
            store_timeline(api, mongo_db, match_id)
            loaded += 1
        except Exception as exc:  # log and continue with the next match
            errors.append(f"timeline {match_id}: {type(exc).__name__}: {exc}")
            log.warning("timeline for match %s failed: %s", match_id, exc)
    return loaded, errors


def refresh_stats(conn, player_id=None):
    """Rebuild the materialised player stats: one player, or everyone when None."""
    try:
        with conn.cursor() as cur:
            cur.execute("CALL refresh_player_stats(%s)", (player_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("refresh_player_stats(%s) failed; pages fall back to the live views", player_id)


# ---------------------------------------------------------------- import runs (MongoDB)
MAX_ERRORS = 50


def start_run(mongo_db, player, source):
    """Record a new import in `import_runs`; returns its _id."""
    return mongo_db["import_runs"].insert_one({
        "player_uuid": player["uuid"], "player_id": player["player_id"], "source": source, "status": "running",
        "started_at": datetime.now(timezone.utc), "finished_at": None, "current_season": None,
        "matches_seen": 0, "matches_inserted": 0, "matches_updated": 0, "matches_failed": 0, "errors": [],
    }).inserted_id


def update_run(mongo_db, run_id, stats, season=None, status=None, errors=None):
    """Progress (counts, current season) and, with a status, the final outcome."""
    fields = {"matches_seen": stats["seen"], "matches_inserted": stats["inserted"],
              "matches_updated": stats["updated"], "matches_failed": stats["failed"]}
    if season is not None:
        fields["current_season"] = season
    if errors:
        fields["errors"] = errors[:MAX_ERRORS]
    if status:
        fields.update(status=status, finished_at=datetime.now(timezone.utc))
    mongo_db["import_runs"].update_one({"_id": run_id}, {"$set": fields})


def last_finished_run(mongo_db, uuid):
    """The player's most recent import that stored every match it saw, or None. A partial run doesn't count:
    Refresh stops at matches older than this run, so after a partial one it would never go back for the matches
    that failed. The next import after a partial run is therefore a full one (every write is an upsert)."""
    return mongo_db["import_runs"].find_one({"player_uuid": uuid, "status": "success"},
                                            sort=[("started_at", -1)])


def latest_run(mongo_db, uuid):
    """The player's most recent import, or None."""
    return mongo_db["import_runs"].find_one({"player_uuid": uuid}, sort=[("started_at", -1)])


# ---------------------------------------------------------------- history

def import_history(api, conn, mongo_db, player, mode="full", run_id=None, max_matches=None, max_pages=None):
    """Import a player's matches for every season (newest first).

    mode 'refresh' stops at matches played before this player's own last finished import started (minus
    REFRESH_MARGIN). Not at the newest stored match: other players' imports also store this player's matches,
    so that match can be newer than ones nobody has fetched yet. Without a finished run it imports everything.
    Returns the stats dict. A single bad match is logged and skipped, never fatal.
    """
    stats = {"seen": 0, "inserted": 0, "updated": 0, "unchanged": 0, "failed": 0}
    errors = []
    current = current_season(api, conn)
    since = None
    if mode == "refresh":
        last = last_finished_run(mongo_db, player["uuid"])
        if last:
            since = last["started_at"] - REFRESH_MARGIN

    pages = 0
    done = False
    for season in range(current, 0, -1):   # a refresh ends at the first match before `since`, in any season
        if done:
            break
        if run_id:
            update_run(mongo_db, run_id, stats, season=season)
        before = None
        while not done:
            page = api.user_matches(player["uuid"], season, before=before, count=PAGE_SIZE)
            pages += 1
            for raw in page:
                if since is not None and transform.epoch_to_utc(raw["date"]) < since:
                    done = True
                    break
                stats["seen"] += 1
                try:
                    stats[store_match(conn, mongo_db, raw, api=api)] += 1
                except Exception as exc:  # log and continue with the next match
                    stats["failed"] += 1
                    errors.append(f"match {raw.get('id')}: {type(exc).__name__}: {exc}")
                    log.warning("match %s failed: %s", raw.get("id"), exc)
                if max_matches and stats["seen"] >= max_matches:
                    done = True
                    break
            if run_id:
                update_run(mongo_db, run_id, stats, season=season)
            if max_pages and pages >= max_pages:
                done = True
            if len(page) < PAGE_SIZE:
                break
            before = page[-1]["id"]
    if run_id:
        update_run(mongo_db, run_id, stats, status="success" if not errors else "partial", errors=errors)
    return stats


# ---------------------------------------------------------------- entry points

def mongo_database(cfg=Config, name=None):
    return db_mongo.client(cfg.MONGO_URI)[name or cfg.MONGO_DB]


def is_running(uuid):
    with _running_lock:
        return uuid in _running


def has_completed_full_import(mongo_db, uuid):
    """Whether a full import of this player stored every match (see last_finished_run)."""
    return mongo_db["import_runs"].find_one(
        {"player_uuid": uuid, "source": "full", "status": "success"}, {"_id": 1}) is not None


def run_player_import(player, mode, database=None, mongo_name=None, max_matches=None, api=None):
    """Import (full or refresh) for a stored player, with its own connections. Thread-safe per player."""
    with _running_lock:
        if player["uuid"] in _running:
            return None
        _running.add(player["uuid"])
    conn = db_sql.connect(database)
    mongo_db = mongo_database(name=mongo_name)
    run_id = None
    try:
        api = api or ApiClient()
        if mode == "refresh" and not has_completed_full_import(mongo_db, player["uuid"]):
            mode = "full"  # an interrupted or partial full import is resumed by re-running it (all upserts)
        run_id = start_run(mongo_db, player, mode)
        stats = import_history(api, conn, mongo_db, player, mode, run_id, max_matches=max_matches)
        stats["timelines"], timeline_errors = load_recent_timelines(api, conn, mongo_db, player)
        if timeline_errors:   # recorded with the run; the history import itself still counts as done
            mongo_db["import_runs"].update_one({"_id": run_id}, {
                "$push": {"errors": {"$each": timeline_errors, "$slice": -MAX_ERRORS}}})
        if stats["inserted"] or stats["updated"]:   # timelines feed neither the summary tables nor the cache
            refresh_stats(conn)  # every player: the import also changed its opponents' stats
            cache.clear()        # cached seed and home results now miss these matches
        return stats
    except Exception as exc:
        log.exception("import for %s failed", player["uuid"])
        if run_id:
            try:
                conn.rollback()
                mongo_db["import_runs"].update_one({"_id": run_id}, {
                    "$set": {"status": "failed", "finished_at": datetime.now(timezone.utc)},
                    "$push": {"errors": {"$each": [f"{type(exc).__name__}: {exc}"], "$slice": -MAX_ERRORS}}})
            except Exception:
                log.exception("could not record failure")
            # Matches stored before the failure stay (each was committed on its own), so the summary tables
            # and cached results must catch up with them, as after a successful import.
            refresh_stats(conn)
            cache.clear()
        return None
    finally:
        conn.close()
        with _running_lock:
            _running.discard(player["uuid"])


def start_background_import(player, mode, database=None, mongo_name=None):
    """Run run_player_import in a daemon thread. Returns False if one is already running."""
    if is_running(player["uuid"]):
        return False
    threading.Thread(target=run_player_import, args=(player, mode, database, mongo_name),
                     name=f"import-{player['uuid']}", daemon=True).start()
    return True


def fetch_player_on_demand(identifier, conn, mongo_db, api=None):
    """Web search path: profile + first page of current-season matches synchronously.

    Returns the players row; the caller then starts the background full import.
    """
    api = api or ApiClient.for_page()   # the visitor waits for this; the background import retries patiently
    player = store_profile(api, conn, mongo_db, identifier)
    import_history(api, conn, mongo_db, player, mode="full", max_pages=1)
    refresh_stats(conn, player["player_id"])  # profile shows current stats while the rest imports
    return player
