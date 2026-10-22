"""Pure functions: API JSON -> SQL row dicts and Mongo documents.

Missing values become None (NULL), never 0 or ''.
"""
from datetime import datetime, timezone

RANKED = 2


def epoch_to_utc(seconds):
    """Epoch seconds -> aware UTC datetime (None stays None)."""
    if seconds is None:
        return None
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc)


def naive_utc(dt):
    """For MariaDB DATETIME columns (stored as UTC)."""
    return None if dt is None else dt.replace(tzinfo=None)


def player_row(profile):
    """UserProfile (from a match, the leaderboard or /users) -> players row."""
    return {
        "uuid": profile["uuid"],
        "nickname": profile["nickname"],
        "current_elo": profile.get("eloRate"),
        "elo_rank": profile.get("eloRank"),
        "country": profile.get("country"),
    }


# Fields of /users/{id} that are never stored: linked Discord/Twitch/YouTube accounts are personal data and no
# feature uses them (match VOD links come from each match's own `vod` field). Data minimisation: drop on import.
UNSTORED_PROFILE_FIELDS = ("connections",)


def stored_profile(profile):
    """/users/{id} -> the copy kept in Mongo `players` (without UNSTORED_PROFILE_FIELDS)."""
    return {k: v for k, v in profile.items() if k not in UNSTORED_PROFILE_FIELDS}


def season_result_rows(seasons_data):
    """/users/{id}/seasons data -> player_season_results rows (without player_id)."""
    rows = []
    for season, res in (seasons_data.get("seasonResults") or {}).items():
        last = res.get("last") or {}
        rows.append({
            "season_id": int(season),
            "final_elo": last.get("eloRate"),
            "final_rank": last.get("eloRank"),
            "highest_elo": res.get("highest"),
            "lowest_elo": res.get("lowest"),
            "phase_points": last.get("phasePoint"),
        })
    return rows


def result_type(match):
    """'decay' (an inactivity Elo penalty listed as a match), else 'draw' (no winner), 'forfeit' or 'completed'."""
    if match.get("decayed"):
        return "decay"
    winner = (match.get("result") or {}).get("uuid")
    if winner is None:
        return "draw"
    return "forfeit" if match.get("forfeited") else "completed"


def match_row(match):
    """MatchInfo -> matches row. The winner is not stored here: it is the match_players row with outcome 'win'.
    is_decayed is not stored either (a generated column, result_type = 'decay'); it is in the dict for callers."""
    result = match.get("result") or {}
    return {
        "match_id": int(match["id"]),
        "season_id": int(match["season"]),
        "match_type": int(match["type"]),
        "played_at": naive_utc(epoch_to_utc(match["date"])),
        "duration_ms": result.get("time"),
        "result_type": result_type(match),
        "is_decayed": 1 if match.get("decayed") else 0,
    }


def unique_players(match):
    """players[] without repeats (some private matches list a player twice, e.g. 2576635)."""
    seen, out = set(), []
    for p in match.get("players") or []:
        if p["uuid"] not in seen:
            seen.add(p["uuid"])
            out.append(p)
    return out


def outside_winner(match):
    """The winner's uuid when it is not among players[] (seen once: private match 771033), else None."""
    if match.get("decayed"):
        return None
    winner = (match.get("result") or {}).get("uuid")
    if winner is not None and winner not in {p["uuid"] for p in match.get("players") or []}:
        return winner
    return None


def match_player_rows(match):
    """MatchInfo -> one row per participant (keyed by uuid; the writer maps uuid -> player_id).

    Forfeit attribution: in a forfeited match with a winner, the non-winner
    forfeited when there are exactly two players; otherwise unknown (None). If the winner is not
    a listed player (private match 771033), every listed player lost, who forfeited is unknown, and the winner
    is added as a participant: the API's result names them, and a winner must be a participant row.
    A decay's participant gets outcome 'decay' (it has an Elo change but was not a game).
    """
    players = unique_players(match)
    result = match.get("result") or {}
    winner = result.get("uuid")
    forfeited_match = bool(match.get("forfeited"))
    changes = {c["uuid"]: c for c in (match.get("changes") or []) if c.get("uuid")}
    winner_listed = outside_winner(match) is None

    rows = []
    for p in players:
        uuid = p["uuid"]
        if match.get("decayed"):
            outcome, forfeited = "decay", 0
        elif winner is None:
            outcome, forfeited = "draw", 0
        elif uuid == winner:
            outcome, forfeited = "win", 0
        else:
            outcome = "loss"
            if not forfeited_match:
                forfeited = 0
            else:
                forfeited = 1 if (len(players) == 2 and winner_listed) else None
        change = changes.get(uuid, {})
        finish = result.get("time") if (outcome == "win" and not forfeited_match) else None
        rows.append({
            "uuid": uuid,
            "elo_before": change.get("eloRate"),
            "elo_change": change.get("change"),
            "outcome": outcome,
            "forfeited": forfeited,
            "finish_time_ms": finish,
        })
    if not winner_listed:
        rows.append({"uuid": winner, "elo_before": None, "elo_change": None, "outcome": "win", "forfeited": 0,
                     "finish_time_ms": None if forfeited_match else result.get("time")})
    return rows


def mongo_match_doc(match, imported_at):
    """MatchInfo -> Mongo `matches` document (`raw` keeps the untouched API JSON)."""
    return {
        "_id": int(match["id"]),
        "season": int(match["season"]),
        "match_type": int(match["type"]),
        "category": match.get("category"),
        "played_at": epoch_to_utc(match["date"]),
        "players": [{"uuid": p["uuid"], "nickname": p["nickname"]} for p in match.get("players") or []],
        "result": match.get("result"),
        "forfeited": bool(match.get("forfeited")),
        "seed": match.get("seed"),
        "videos": match.get("vod") or [],
        "raw": match,
        "imported_at": imported_at,
    }


END_ENTER = "story.enter_the_end"
DRAGON_EVENTS = ("projectelo.timeline.dragon_death", "end.kill_dragon")
DEATH = "projectelo.timeline.death"


def end_splits(detail):
    """Per player who entered the End: entry time, first dragon kill after it, and deaths in between (ms).

    Event names come from real /matches/{id} timelines. `dragon_split` is the time from
    first entering the End to the dragon dying; None when that player never killed the dragon.
    """
    timelines = detail.get("timelines") or []
    finished = {c["uuid"]: c["time"] for c in detail.get("completions") or []}
    out = []
    for p in unique_players(detail):
        uuid = p["uuid"]
        mine = [t for t in timelines if t.get("uuid") == uuid]
        enter = min((t["time"] for t in mine if t["type"] == END_ENTER), default=None)
        if enter is None:
            continue
        dragon = min((t["time"] for t in mine if t["type"] in DRAGON_EVENTS and t["time"] >= enter), default=None)
        stop = dragon if dragon is not None else max((t["time"] for t in mine), default=enter)
        out.append({
            "uuid": uuid,
            "entered_end": enter,
            "dragon_kill": dragon,
            "dragon_split": None if dragon is None else dragon - enter,
            "end_deaths": sum(1 for t in mine if t["type"] == DEATH and enter <= t["time"] <= stop),
            "finished": uuid in finished,
        })
    return out


def mongo_detail_doc(detail, fetched_at):
    """/matches/{id} -> `match_details` document (cached on first view).

    season / match_type / seed are copied from the response so End-split analysis runs inside Mongo.
    """
    seed = detail.get("seed") or {}
    return {
        "_id": int(detail["id"]),
        "season": detail.get("season"),
        "match_type": detail.get("type"),
        "decayed": bool(detail.get("decayed")),
        "end_towers": seed.get("endTowers"),
        "variations": sorted(set(seed.get("variations") or [])),
        "completions": detail.get("completions") or [],
        "timelines": detail.get("timelines") or [],
        "end_splits": end_splits(detail),
        "replay_exists": detail.get("replayExists"),
        "raw": detail,
        "fetched_at": fetched_at,
    }
