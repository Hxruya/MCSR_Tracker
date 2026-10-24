"""Player search, loading new players from MCSR Ranked, and profile pages.

Searching only reads stored players. Loading one from the API is a separate, explicit POST (login and CSRF
token required), so a page view or a link from another site never spends the API budget or starts an import.
"""
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from app import benchmark, elo, endgame, rivalry, seeds, splits, summary, util
from app.auth import login_required
from app.db_mongo import get_mongo, optional
from app.db_sql import get_db, query_all, query_one
from app.importer import sync
from app.importer.api_client import ApiClient, ApiError, ApiNotFound

bp = Blueprint("players", __name__)


def season_ids():
    return [r["season_id"] for r in query_all("SELECT season_id FROM seasons ORDER BY season_id DESC")]


def find_exact(q):
    """uuid or exact nickname -> stored player row, or None. Nicknames aren't unique (players rename), so a
    fully loaded player wins."""
    uuid = util.normalise_uuid(q)
    if uuid:
        return query_one("SELECT * FROM players WHERE uuid = %s", (uuid,))
    return query_one("SELECT * FROM players WHERE nickname = %s ORDER BY profile_status = 'full' DESC LIMIT 1", (q,))


def fetch_and_start(identifier):
    """Populate on demand: profile + first page now, the rest in a background thread."""
    player = sync.fetch_player_on_demand(identifier, get_db(), get_mongo())
    sync.start_background_import(player, "full", current_app.config["MARIADB_DATABASE"],
                                 current_app.config["MONGO_DB"])
    return player


@bp.route("/players")
def search():
    q = request.args.get("q", "").strip()
    try:
        page = util.parse_page(request.args.get("page"))
    except util.BadParam as e:
        flash(str(e), "error")
        return render_template("players/search.html", q=q, rows=[], paging=util.pages(0, 1)), 400

    if q:
        exact = find_exact(q)
        if exact and exact["profile_status"] == "full":
            return redirect(url_for("players.profile", uuid=exact["uuid"]))
        uuid = util.normalise_uuid(q)
        if uuid:
            where, params = "uuid = %s", (uuid,)
        else:
            where, params = "nickname LIKE %s", (f"%{util.like_escape(q)}%",)
    else:
        where, params = "profile_status = 'full'", ()

    total = query_one(f"SELECT COUNT(*) AS n FROM players WHERE {where}", params)["n"]
    rows = query_all(
        f"SELECT uuid, nickname, current_elo, elo_rank, country, profile_status FROM players WHERE {where} "
        "ORDER BY profile_status = 'full' DESC, current_elo IS NULL, current_elo DESC, nickname LIMIT %s OFFSET %s",
        params + (util.PER_PAGE, (page - 1) * util.PER_PAGE))
    return render_template("players/search.html", q=q, rows=rows, paging=util.pages(total, page),
                           loadable=loadable(q))


def loadable(q):
    """Could `q` name an MCSR Ranked player? (Whether one exists is only known by asking the API.)"""
    return bool(q) and bool(util.normalise_uuid(q) or util.NICKNAME_RE.match(q))


@bp.route("/players/load", methods=["POST"])
@login_required
def load():
    """The explicit "Load from MCSR Ranked" button: profile + first page now, the full history in the background."""
    q = request.form.get("q", "").strip()
    if not loadable(q):
        flash("Enter a nickname (letters, digits, underscore) or a UUID to load.", "error")
        return redirect(url_for("players.search", q=q))
    exact = find_exact(q)
    if exact and exact["profile_status"] == "full":   # already loaded: no API call needed
        return redirect(url_for("players.profile", uuid=exact["uuid"]))
    try:
        player = fetch_and_start(q)
    except ApiNotFound:
        flash(f"No MCSR Ranked player called '{q}'.", "info")
        return redirect(url_for("players.search", q=q))
    except ApiError:
        current_app.logger.exception("on-demand fetch failed for %r", q)
        flash("Could not reach the MCSR Ranked API right now. Try again later.", "error")
        return redirect(url_for("players.search", q=q))
    flash(f"Loaded {player['nickname']} from MCSR Ranked. Their full match history is importing.", "success")
    return redirect(url_for("players.profile", uuid=player["uuid"]))


def import_status(uuid):
    """The latest import (MongoDB import_runs) and whether one is running in this process. A run still marked
    'running' that no thread owns was cut short (e.g. the server restarted): shown as interrupted."""
    latest = optional(sync.latest_run, get_mongo(), uuid, what="import status")
    running = sync.is_running(uuid)
    if latest and latest["status"] == "running" and not running:
        latest = dict(latest, status="interrupted")
    return latest, running


@bp.route("/players/<uuid>")
def profile(uuid):
    uuid = util.normalise_uuid(uuid) or abort(404)
    player = query_one("SELECT * FROM players WHERE uuid = %s", (uuid,)) or abort(404)
    seasons = season_ids()
    error = None
    try:
        season = util.parse_season(request.args.get("season"), seasons)
    except util.BadParam as e:
        error, season = str(e), None

    pid = player["player_id"]
    if season is None:  # materialised stats with live-view fallback
        st = summary.lifetime([pid]).get(pid)
    else:
        st = summary.season(pid, season)
    season_results = query_all("SELECT * FROM player_season_results WHERE player_id = %s ORDER BY season_id DESC",
                               (pid,))
    stored = query_one("SELECT COUNT(*) AS n FROM match_players WHERE player_id = %s", (pid,))["n"]
    if season is None:
        fastest = query_one("/* Fastest win (view: ROW_NUMBER() per player) */ "
                            "SELECT * FROM v_fastest_wins_lifetime WHERE player_id = %s", (pid,))
    else:
        fastest = query_one("SELECT * FROM v_fastest_wins WHERE player_id = %s AND season_id = %s", (pid, season))
    latest_import, running = import_status(uuid)
    season_rows = summary.seasons(pid)
    # MongoDB-backed sections render as "unavailable" (None) if MongoDB is down
    elo_view = optional(elo.history, uuid, season, what="Elo history")
    seed_types = optional(seeds.player_seed_types, uuid, pid, season, what="seed types")
    rivals = optional(rivalry.top_rivals, uuid, season, what="top rivals")
    doc = optional(get_mongo()["players"].find_one, {"_id": uuid},
                   {"profile.statistics": 1, "profile.achievements.total": 1},
                   what="the stored API profile")
    tier_benchmark = benchmark.peer_benchmark(pid, season=season)
    bookmark = None
    if g.user:
        bookmark = query_one("SELECT bookmark_id, note FROM bookmarks WHERE user_id = %s AND player_id = %s",
                             (g.user["user_id"], pid))
    if error:
        flash(error, "error")
    return render_template(
        "players/profile.html", player=player, st=st, season=season, seasons=seasons,
        season_results=season_results, stored=stored, recent=summary.player_matches(pid, limit=10), fastest=fastest,
        latest_import=latest_import, running=running, doc=doc or {}, bookmark=bookmark,
        elo=elo_view, season_rows=season_rows, season_chart=season_win_rate_chart(season_rows),
        end=endgame.profile_view(uuid, season), splits=splits.profile_view(uuid, season), seed_types=seed_types,
        rivals=rivals, benchmark=tier_benchmark,
    ), (400 if error else 200)


def season_win_rate_chart(season_rows):
    if not season_rows:
        return None
    return {
        "kind": "column", "format": "pct", "labels": [f"S{r['season_id']}" for r in season_rows],
        # win_rate is NULL (N/A) for a season of only draws: no bar, as on the Compare page's season chart
        "series": [{"name": "Win rate", "slot": 1, "data": [
            float(r["win_rate"]) if r["win_rate"] is not None else None for r in season_rows]}],
        "extra": {"Win rate": [f"{r['games_played']} games · {int(r['wins'])}W {int(r['losses'])}L "
                               f"{int(r['draws'])}D" for r in season_rows]},
    }


@bp.route("/players/<uuid>/refresh", methods=["POST"])
@login_required
def refresh(uuid):
    uuid = util.normalise_uuid(uuid) or abort(404)
    player = query_one("SELECT * FROM players WHERE uuid = %s", (uuid,)) or abort(404)
    if sync.is_running(uuid):
        flash("An import for this player is already running.", "info")
        return redirect(url_for("players.profile", uuid=uuid))
    try:
        if player["profile_status"] != "full":
            fetch_and_start(uuid)
            flash("Loading the full profile and match history.", "success")
        else:
            player = sync.store_profile(ApiClient.for_page(), get_db(), get_mongo(), uuid)
            sync.start_background_import(player, "refresh", current_app.config["MARIADB_DATABASE"],
                                         current_app.config["MONGO_DB"])
            flash("Profile updated. Fetching new matches.", "success")
    except ApiError:
        current_app.logger.exception("refresh failed for %s", uuid)
        flash("Could not reach the MCSR Ranked API right now. Try again later.", "error")
    return redirect(url_for("players.profile", uuid=uuid))
