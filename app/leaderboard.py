"""The official MCSR Ranked Elo leaderboard (fetched from the API) and fastest winning times (stored data)."""
import time
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, render_template, request

from app import cache, util
from app.db_sql import query_all, query_one
from app.players import season_ids
from app.importer.api_client import ApiClient, ApiError

bp = Blueprint("leaderboard", __name__)

# The leaderboard changes whenever ranked matches finish and the API is its source of truth, so it is fetched,
# not stored. A 30-second cache means repeated views (and the home page's top 5) cost at most one API call per
# season per 30 s. Local, single-process design: in production a scheduled job would save snapshots instead.
CACHE_SECONDS = 30
PAST_SEASON_SECONDS = 86400   # a finished season's leaderboard never changes
_failed_at = {}   # season -> when its last fetch failed


def _fetch(season):
    data = ApiClient.for_page().leaderboard(season)
    rows = []
    for u in data.get("users") or []:
        res = u.get("seasonResult") or {}   # the season's figures; top-level eloRate is the current Elo
        rows.append({"rank": res.get("eloRank", u.get("eloRank")), "elo": res.get("eloRate", u.get("eloRate")),
                     "peak": res.get("highest"), "uuid": u["uuid"], "nickname": u["nickname"],
                     "country": u.get("country")})
    return {"season": (data.get("season") or {}).get("number", season), "rows": rows,
            "fetched_at": datetime.now(timezone.utc)}


def official(season=None, past=False):
    """{season, rows, fetched_at} from MCSR Ranked (rows ranked, ties share a rank), or None if the API failed.
    past=True: a finished season, cached for a day, so paging through old seasons can't eat the API budget."""
    ttl = PAST_SEASON_SECONDS if past else CACHE_SECONDS
    if time.monotonic() - _failed_at.get(season, -CACHE_SECONDS) < CACHE_SECONDS:
        return None   # failed moments ago: don't make every page view wait for the timeout again
    try:
        board = cache.cached(("leaderboard", season), lambda: _fetch(season), ttl=ttl)
    except ApiError:
        current_app.logger.warning("leaderboard fetch failed (season %s)", season, exc_info=True)
        _failed_at[season] = time.monotonic()
        return None
    stored = {}
    uuids = [r["uuid"] for r in board["rows"]]
    if uuids:
        marks = ", ".join(["%s"] * len(uuids))
        stored = {r["uuid"]: r["profile_status"] for r in query_all(
            f"SELECT uuid, profile_status FROM players WHERE uuid IN ({marks})", uuids)}
    # copies, so the cached rows never carry one request's stored/not-stored flags into another
    return dict(board, rows=[dict(r, stored=stored.get(r["uuid"])) for r in board["rows"]])


@bp.route("/leaderboard")
def index():
    seasons = season_ids()
    try:
        season = util.parse_season(request.args.get("season"), seasons)
        page = util.parse_page(request.args.get("page"))
    except util.BadParam as e:
        flash(str(e), "error")
        return render_template("leaderboard/index.html", board=None, rows=[], seasons=seasons, season=None,
                               paging=util.pages(0, 1), invalid=True), 400
    board = official(season, past=season is not None and season < max(seasons))
    if board is None:
        return render_template("leaderboard/index.html", board=None, rows=[], seasons=seasons, season=season,
                               paging=util.pages(0, 1)), 503
    offset = (page - 1) * util.PER_PAGE
    return render_template("leaderboard/index.html", board=board, rows=board["rows"][offset:offset + util.PER_PAGE],
                           seasons=seasons, season=season, paging=util.pages(len(board["rows"]), page))


@bp.route("/fastest")
def fastest():
    seasons = season_ids()
    try:
        season = util.parse_season(request.args.get("season"), seasons)
        page = util.parse_page(request.args.get("page"))
    except util.BadParam as e:
        flash(str(e), "error")
        return render_template("leaderboard/fastest.html", rows=[], season=None, seasons=seasons,
                               paging=util.pages(0, 1)), 400

    if season is None:
        source, where, params = "v_fastest_wins_lifetime", "1 = 1", ()
    else:
        source, where, params = "v_fastest_wins", "f.season_id = %s", (season,)
    # The view's window functions are the expensive part, so the total comes from the same pass
    # (COUNT(*) OVER ()) instead of a second COUNT query over the view (79 ms -> 40 ms).
    rows = query_all(
        "SELECT f.match_id, f.season_id, f.finish_time_ms, f.played_at, p.uuid, p.nickname, p.country, "
        "COUNT(*) OVER () AS total "
        f"FROM {source} f JOIN players p ON p.player_id = f.player_id WHERE {where} "
        "ORDER BY f.finish_time_ms, f.match_id LIMIT %s OFFSET %s",
        params + (util.PER_PAGE, (page - 1) * util.PER_PAGE))
    if rows:
        total = rows[0]["total"]
    else:  # past the last page: no row carries the total
        total = query_one(f"SELECT COUNT(*) AS n FROM {source} f WHERE {where}", params)["n"]
    return render_template("leaderboard/fastest.html", rows=rows, season=season, seasons=seasons,
                           paging=util.pages(total, page), offset=(page - 1) * util.PER_PAGE)
