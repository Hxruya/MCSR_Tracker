"""Match history (filtered) and match inspection (MariaDB summary + Mongo seed/videos/timeline)."""
from datetime import timezone
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from app import facets, summary, util
from app.auth import login_required
from app.db_mongo import get_mongo, optional
from app.db_sql import query_all, query_one
from app.notes import notes_for_match
from app.players import find_exact, season_ids
from app.splits import MILESTONES
from app.importer import sync
from app.importer.api_client import ApiClient, ApiError

bp = Blueprint("matches", __name__)

RESULTS = ("win", "loss", "draw", "forfeit", "decay")
TYPES = {"all": None, "ranked": 2, "casual": 1, "private": 3, "event": 4}


def parse_filters(args, seasons):
    """Validate every query parameter. Returns (filters, errors)."""
    errors, f = [], {}
    f["player"] = None
    if args.get("player"):
        player = find_exact(args["player"])
        if player is None:
            errors.append("That player is not stored. Search for them first.")
        f["player"] = player
    try:
        f["season"] = util.parse_season(args.get("season"), seasons)
    except util.BadParam as e:
        errors.append(str(e))
        f["season"] = None
    f["result"] = args.get("result") or None
    if f["result"] is not None and f["result"] not in RESULTS:
        errors.append("Result must be one of: win, loss, draw, forfeit, decay.")
        f["result"] = None
    if f["result"] and not args.get("player"):
        errors.append("Pick a player to filter by result.")
    f["type"] = args.get("type") or "all"
    if f["type"] not in TYPES:
        errors.append("Type must be all, ranked, casual, private or event.")
        f["type"] = "all"
    try:
        f["from"], f["to"] = util.parse_date_range(args)
    except util.BadParam as e:
        errors.append(str(e))
        f["from"] = f["to"] = None
    try:
        f["page"] = util.parse_page(args.get("page"))
    except util.BadParam as e:
        errors.append(str(e))
        f["page"] = 1
    return f, errors


@bp.route("/matches")
def index():
    seasons = season_ids()
    f, errors = parse_filters(request.args, seasons)
    ctx = dict(f=f, seasons=seasons, results=RESULTS, types=TYPES, rows=[], paging=util.pages(0, 1), counts=None)
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("matches/index.html", **ctx), 400

    lo, hi = util.day_bounds(f["from"], f["to"])
    where, params = util.sql_filters(f["season"], lo, hi, prefix="m.")
    if TYPES[f["type"]] is not None:
        where.append("m.match_type = %s")
        params.append(TYPES[f["type"]])

    offset = (f["page"] - 1) * util.PER_PAGE
    if f["player"]:
        if f["result"] == "forfeit":
            where.append("mp.forfeited = 1")
        elif f["result"]:
            where.append("mp.outcome = %s")
            params.append(f["result"])
        pid = f["player"]["player_id"]
        total = query_one("SELECT COUNT(*) AS n FROM match_players mp JOIN matches m ON m.match_id = mp.match_id "
                          f"WHERE {' AND '.join(['mp.player_id = %s'] + where)}", [pid] + params)["n"]
        rows = summary.player_matches(pid, where, params, util.PER_PAGE, offset)
    else:
        sql_where = " AND ".join(where) or "1 = 1"
        total = query_one(f"SELECT COUNT(*) AS n FROM matches m WHERE {sql_where}", params)["n"]
        # Page ids come from the matches indexes; summaries (winner first, player count) from v_match_summary.
        ids = [r["match_id"] for r in query_all(
            f"SELECT m.match_id FROM matches m WHERE {sql_where} "
            "ORDER BY m.played_at DESC, m.match_id DESC LIMIT %s OFFSET %s", params + [util.PER_PAGE, offset])]
        found = summary.matches_by_id(ids)
        rows = [found[i] for i in ids if i in found]
    ctx["counts"] = optional(  # live counts beside each filter option
        facets.counts, get_mongo()["matches"], uuid=f["player"]["uuid"] if f["player"] else None,
        season=f["season"], match_type=TYPES[f["type"]], result=f["result"], lo=lo, hi=hi, what="filter counts")
    ctx.update(rows=rows, paging=util.pages(total, f["page"]))
    return render_template("matches/index.html", **ctx)


def safe_video(video, match_end, duration_ms):
    """Only https links, with a Twitch timestamp at the start of the match. The API docs give
    {date} - {vod.startsAt}, but `date` is when the match ended, so the match's duration is taken off too."""
    url = video.get("url") or ""
    if urlsplit(url).scheme != "https":
        return None
    offset = None
    if video.get("startsAt") and match_end:
        secs = int(match_end) - (duration_ms or 0) // 1000 - int(video["startsAt"])
        if secs > 0:
            offset = f"{secs // 3600}h{(secs % 3600) // 60}m{secs % 60}s"
    return {"url": url, "offset": offset, "uuid": video.get("uuid")}


def load_timeline(match):
    """The cached /matches/{id} detail (MongoDB match_details), or None. Never calls the API: imports load the
    latest ranked timelines of each player, and the Load timeline button (load_timeline_now) any other match."""
    return optional(get_mongo()["match_details"].find_one, {"_id": match["match_id"]}, what="match timeline")


@bp.route("/matches/<int:match_id>/timeline", methods=["POST"])
@login_required
def load_timeline_now(match_id):
    """The explicit Load timeline button: one API call (the visitor waits, so a short timeout and no retries)."""
    match = query_one("SELECT match_id, is_decayed FROM matches WHERE match_id = %s", (match_id,)) or abort(404)
    if match["is_decayed"]:
        flash("Decay matches have no events.", "info")
    else:
        try:
            sync.store_timeline(ApiClient.for_page(), get_mongo(), match_id)
        except ApiError:
            current_app.logger.warning("timeline fetch failed for match %s", match_id, exc_info=True)
            flash("Could not reach MCSR Ranked right now. Try again in a minute.", "error")
        except Exception:   # bad data or a Mongo error: say so, the page still works
            current_app.logger.exception("timeline for match %s could not be stored", match_id)
            flash("This match's timeline couldn't be stored.", "error")
    return redirect(url_for("matches.detail", match_id=match_id) + "#timeline")


EVENT_NAMES = dict(MILESTONES) | {
    "projectelo.timeline.death": "Died", "projectelo.timeline.reset": "Reset", "projectelo.timeline.forfeit": "Forfeited",
    "projectelo.timeline.death_spawnpoint": "Died (spawn point set)", "end.kill_dragon": "Killed the dragon",
    "story.smelt_iron": "Smelt iron", "story.iron_tools": "Iron pickaxe", "story.lava_bucket": "Lava bucket",
    "story.form_obsidian": "Make obsidian", "nether.loot_bastion": "Loot bastion chest",
    "nether.distract_piglin": "Barter with piglins", "nether.obtain_crying_obsidian": "Crying obsidian",
    "story.obtain_armor": "Iron armour", "adventure.kill_a_mob": "Kill a mob", "adventure.shoot_arrow": "Shoot an arrow",
}


def pretty_event(kind):
    if kind in EVENT_NAMES:
        return EVENT_NAMES[kind]
    parts = kind.split(".")
    if parts[-1] == "root" and len(parts) > 1:
        return f"{parts[-2].capitalize()} started"
    return parts[-1].replace("_", " ").capitalize()


def split_table(events, completions, players):
    """Per-player first time for each milestone (+ finish, deaths, resets), fastest marked per row."""
    first, counts = {}, {}
    for t in events:
        first.setdefault((t["type"], t["uuid"]), t["time"])
        counts[(t["type"], t["uuid"])] = counts.get((t["type"], t["uuid"]), 0) + 1
    rows = []
    for key, label in MILESTONES + [("__finish", "Finish")]:
        if key == "__finish":
            times = [next((c["time"] for c in completions if c["uuid"] == p["uuid"]), None) for p in players]
        else:
            times = [first.get((key, p["uuid"])) for p in players]
        if any(v is not None for v in times):
            best = min(v for v in times if v is not None)
            rows.append({"label": label, "cells": [{"ms": v, "best": v == best and len(players) > 1} for v in times]})
    for key, label in (("projectelo.timeline.death", "Deaths"), ("projectelo.timeline.reset", "Resets")):
        rows.append({"label": label, "count": True,
                     "cells": [{"n": counts.get((key, p["uuid"]), 0)} for p in players]})
    return rows


@bp.route("/matches/<int:match_id>")
def detail(match_id):
    match = query_one(
        "SELECT m.*, s.label AS season_label, w.nickname AS winner, w.uuid AS winner_uuid "
        "FROM matches m JOIN seasons s ON s.season_id = m.season_id "
        "LEFT JOIN match_players wx ON wx.winner_match_id = m.match_id "
        "LEFT JOIN players w ON w.player_id = wx.player_id WHERE m.match_id = %s", (match_id,))
    if match is None:
        abort(404)
    participants = query_all(
        "SELECT p.uuid, p.nickname, p.country, mp.outcome, mp.forfeited, mp.elo_before, mp.elo_change, "
        "mp.elo_after, mp.finish_time_ms FROM match_players mp JOIN players p ON p.player_id = mp.player_id "
        "WHERE mp.match_id = %s ORDER BY FIELD(mp.outcome, 'win', 'draw', 'loss', 'decay'), p.nickname", (match_id,))

    doc = optional(get_mongo()["matches"].find_one, {"_id": match_id}, {"raw": 0}, what=f"match {match_id}")
    videos = []
    if doc:
        end = int(match["played_at"].replace(tzinfo=timezone.utc).timestamp())
        videos = [v for v in (safe_video(v, end, match["duration_ms"]) for v in doc.get("videos") or []) if v]

    timeline, splits, names = None, None, {p["uuid"]: p["nickname"] for p in participants}
    if doc is not None:
        detail_doc = load_timeline(match)
        if detail_doc:
            events = sorted(detail_doc.get("timelines") or [], key=lambda t: t["time"])
            timeline = [{"time": t["time"], "who": names.get(t["uuid"], t["uuid"][:8]), "event": pretty_event(t["type"])}
                        for t in events]
            if 1 < len(participants) <= 4:
                splits = split_table(events, detail_doc.get("completions") or [], participants)
    my_notes = optional(notes_for_match, match_id, default=[], what="match notes")
    return render_template("matches/detail.html", match=match, participants=participants, doc=doc,
                           videos=videos, timeline=timeline, splits=splits, my_notes=my_notes)
