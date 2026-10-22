"""Side-by-side player comparison and recent form (latest 20 vs previous 20 ranked matches)."""
from decimal import ROUND_HALF_UP, Decimal

from flask import Blueprint, flash, render_template, request

from app import rivalry, stats, util
from app.db_mongo import optional
from app.db_sql import query_all, query_one
from app.players import find_exact, season_ids

bp = Blueprint("compare", __name__)

FORM_WINDOW = 20


def resolve(value):
    return find_exact(value) if value else None


def filtered_stats(player_id, season, lo, hi):
    """Ranked stats under identical filters; rates via app.stats (same rules as the views). Times come from
    v_completed_wins, the single definition of a timed win: a match that isn't one joins to NULL, which MIN and
    AVG skip, while every count still comes from the player's own rows (one view row per match at most)."""
    where, params = util.sql_filters(season, lo, hi, prefix="r.")
    where, params = ["r.player_id = %s"] + where, [player_id] + params
    row = query_one(
        "/* Ranked stats under the chosen filters */ SELECT COUNT(*) AS games_played, "
        "COALESCE(SUM(r.outcome = 'win'), 0) AS wins, "
        "COALESCE(SUM(r.outcome = 'loss'), 0) AS losses, COALESCE(SUM(r.outcome = 'draw'), 0) AS draws, "
        "COALESCE(SUM(r.forfeited = 1), 0) AS forfeits, SUM(r.elo_change) AS total_elo_change, "
        "AVG(r.elo_change) AS avg_elo_change, MIN(w.finish_time_ms) AS best_time, AVG(w.finish_time_ms) AS avg_time "
        "FROM v_ranked_match_players r "
        "LEFT JOIN v_completed_wins w ON w.match_id = r.match_id AND w.player_id = r.player_id "
        f"WHERE {' AND '.join(where)}", params)
    row = {k: (int(v) if k in ("wins", "losses", "draws", "forfeits") and v is not None else v) for k, v in row.items()}
    row["win_rate"] = stats.win_rate(row["wins"], row["games_played"], row["draws"])
    row["forfeit_rate"] = stats.rate(row["forfeits"], row["games_played"])
    return row


@bp.route("/compare")
def index():
    args = request.args
    errors = []
    a, b = resolve(args.get("a")), resolve(args.get("b"))
    if args.get("a") and a is None:
        errors.append(f"Player A '{args['a']}' is not stored. Search for them first.")
    if args.get("b") and b is None:
        errors.append(f"Player B '{args['b']}' is not stored. Search for them first.")
    if a and b and a["player_id"] == b["player_id"]:
        errors.append("Pick two different players.")
    season = lo = hi = None
    page = 1
    seasons = season_ids()
    try:
        season = util.parse_season(args.get("season"), seasons)
        lo, hi = util.day_bounds(*util.parse_date_range(args))
        page = util.parse_page(args.get("page"))
    except util.BadParam as e:
        errors.append(str(e))

    ctx = dict(a=a, b=b, season=season, seasons=seasons, sa=None, sb=None, h2h=None,
               season_chart=None, season_table=[], one=None, suggest=[])
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("compare/index.html", **ctx), 400
    if a and b:
        ctx["sa"] = filtered_stats(a["player_id"], season, lo, hi)
        ctx["sb"] = filtered_stats(b["player_id"], season, lo, hi)
        if season is None:
            ctx["season_chart"], ctx["season_table"] = season_comparison(a, b, lo, hi)
        ctx["h2h"] = rivalry.head_to_head(a, b, season, lo, hi, page)   # seeds are None if MongoDB is down
    elif a or b:   # one player chosen: suggest the people they've played most as the second
        one = a or b
        ctx["one"] = one
        ctx["suggest"] = optional(rivalry.top_rivals, one["uuid"], limit=6, what="compare suggestions") or []
    return render_template("compare/index.html", **ctx)


def season_comparison(a, b, lo, hi):
    """Ranked win rate per season for both players under the same date filter (chart + table view)."""
    per = {}
    dates, date_params = util.sql_filters(lo=lo, hi=hi)
    for key, player in (("a", a), ("b", b)):
        where, params = ["player_id = %s"] + dates, [player["player_id"]] + date_params
        for r in query_all("/* Win rate per season */ SELECT season_id, COUNT(*) AS games, SUM(outcome = 'win') AS wins, "
                           "SUM(outcome = 'draw') AS draws "
                           f"FROM v_ranked_match_players WHERE {' AND '.join(where)} GROUP BY season_id", params):
            per.setdefault(r["season_id"], {})[key] = (int(r["games"]), int(r["wins"]), int(r["draws"]))
    seasons = sorted(per)
    if len(seasons) < 2:
        return None, []
    table = []
    for s in seasons:
        row = {"season": s}
        for key in ("a", "b"):
            games, wins, draws = per[s].get(key, (0, 0, 0))
            row[key] = {"games": games, "rate": stats.win_rate(wins, games, draws)}
        table.append(row)
    rate = lambda r, k: float(r[k]["rate"]) if r[k]["rate"] is not None else None
    chart = {
        "kind": "grouped", "format": "pct", "labels": [f"S{r['season']}" for r in table],
        "series": [{"name": a["nickname"], "slot": 1, "data": [rate(r, "a") for r in table]},
                   {"name": b["nickname"], "slot": 2, "data": [rate(r, "b") for r in table]}],
        "extra": {a["nickname"]: [f"{r['a']['games']} games" for r in table],
                  b["nickname"]: [f"{r['b']['games']} games" for r in table]},
    }
    return chart, table


def _avg(value):
    return None if value is None else Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def recent_form(player_id):
    """Latest 20 vs previous 20 ranked matches via ROW_NUMBER(). Returns (total, buckets or None, strip): the
    strip is the last 40 results, oldest first (empty with fewer than 40 matches)."""
    total = query_one("SELECT COUNT(*) AS n FROM v_ranked_match_players WHERE player_id = %s", (player_id,))["n"]
    if total < 2 * FORM_WINDOW:
        return total, None, []
    rows = query_all(
        "/* Latest 20 vs previous 20: ROW_NUMBER() buckets */ WITH numbered AS ("
        "  SELECT r.*, ROW_NUMBER() OVER (PARTITION BY r.player_id ORDER BY r.played_at DESC, r.match_id DESC) AS rn"
        "  FROM v_ranked_match_players r WHERE r.player_id = %s"
        ") SELECT CASE WHEN rn <= %s THEN 'latest' ELSE 'previous' END AS bucket, "
        "COUNT(*) AS games, SUM(outcome = 'win') AS wins, SUM(outcome = 'loss') AS losses, "
        "SUM(outcome = 'draw') AS draws, COALESCE(SUM(forfeited = 1), 0) AS forfeits, AVG(elo_change) AS avg_elo, "
        "MIN(played_at) AS first_at, MAX(played_at) AS last_at "
        "FROM numbered WHERE rn <= %s GROUP BY bucket",
        (player_id, FORM_WINDOW, 2 * FORM_WINDOW))
    buckets = {}
    for r in rows:
        games = int(r["games"])
        buckets[r["bucket"]] = {
            "games": games, "wins": int(r["wins"]), "losses": int(r["losses"]), "draws": int(r["draws"]),
            "forfeits": int(r["forfeits"]), "win_rate": stats.win_rate(int(r["wins"]), games, int(r["draws"])),
            "forfeit_rate": stats.rate(int(r["forfeits"]), games), "avg_elo": _avg(r["avg_elo"]),
            "first_at": r["first_at"], "last_at": r["last_at"],
        }
    strip = query_all(
        "/* Form strip: the last 40 results */ SELECT match_id, played_at, outcome, forfeited, elo_change FROM v_ranked_match_players "
        "WHERE player_id = %s ORDER BY played_at DESC, match_id DESC LIMIT %s", (player_id, 2 * FORM_WINDOW))
    return total, buckets, strip[::-1]  # oldest first: previous 20, then latest 20


@bp.route("/compare/form")
def form():
    value = request.args.get("a", "")
    player = resolve(value)
    if value and player is None:
        flash(f"Player '{value}' is not stored. Search for them first.", "error")
        return render_template("compare/form.html", player=None, total=None, buckets=None, strip=[], value=value,
                               window=FORM_WINDOW), 400
    total = buckets = None
    strip = []
    if player:
        total, buckets, strip = recent_form(player["player_id"])
    return render_template("compare/form.html", player=player, total=total, buckets=buckets, strip=strip,
                           value=value, window=FORM_WINDOW)
