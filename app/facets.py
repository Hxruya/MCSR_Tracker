"""Live counts next to the match-history filters: one MongoDB aggregation.

Standard faceted-search rule: each group of counts applies every *other* active filter but not its own, so
"Season 12 (1,015)" is how many matches you'd get by switching to season 12 with the rest unchanged.

  $match   filters that are never faceted (player, dates): MongoDB can use an index
  $group   collapse the matches into (season, type, outcome, forfeit) cells with a count: a few dozen
           documents instead of tens of thousands (without a player, ix_season_type covers it: no documents read)
  $facet   per filter, sum the cells that pass the other filters

Outcomes follow app/importer/transform.match_player_rows, so the counts agree with the MariaDB list (tested).
"""
from datetime import timezone

WINNER = {"$ifNull": ["$result.uuid", None]}


def _cell_key(uuid):
    key = {"season": "$season", "type": "$match_type"}
    if uuid:
        key["outcome"] = {"$switch": {"branches": [
            {"case": {"$eq": ["$raw.decayed", True]}, "then": "decay"},   # as transform.match_player_rows
            {"case": {"$eq": [WINNER, None]}, "then": "draw"},
            {"case": {"$eq": [WINNER, uuid]}, "then": "win"}], "default": "loss"}}
        # match_players.forfeited = 1: the player lost a forfeited 1v1 whose winner is listed
        key["forfeit"] = {"$and": [
            "$forfeited", {"$ne": [WINNER, None]}, {"$ne": [WINNER, uuid]},
            {"$eq": [{"$size": {"$setUnion": ["$players.uuid", []]}}, 2]},   # unique players, as transform does
            {"$in": [WINNER, "$players.uuid"]}]}
    return key


def _utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def pipeline(uuid=None, season=None, match_type=None, result=None, lo=None, hi=None):
    fixed = {}
    if uuid:
        fixed["players.uuid"] = uuid
    if lo or hi:
        fixed["played_at"] = {k: _utc(v) for k, v in (("$gte", lo), ("$lt", hi)) if v}
    dims = {}
    if season is not None:
        dims["season"] = {"_id.season": season}
    if match_type is not None:
        dims["type"] = {"_id.type": match_type}
    if result and uuid:
        dims["result"] = {"_id.forfeit": True} if result == "forfeit" else {"_id.outcome": result}

    def others(skip):
        conds = [c for k, c in dims.items() if k != skip]
        return [{"$match": {"$and": conds}}] if conds else []

    facets = {
        "season": others("season") + [{"$group": {"_id": "$_id.season", "n": {"$sum": "$n"}}}],
        "type": others("type") + [{"$group": {"_id": "$_id.type", "n": {"$sum": "$n"}}}],
    }
    if uuid:
        facets["result"] = others("result") + [{"$group": {
            "_id": "$_id.outcome", "n": {"$sum": "$n"},
            "forfeit": {"$sum": {"$cond": ["$_id.forfeit", "$n", 0]}}}}]
    project = ({"season": 1, "match_type": 1, "result.uuid": 1, "forfeited": 1, "players.uuid": 1, "raw.decayed": 1}
               if uuid
               else {"_id": 0, "season": 1, "match_type": 1})   # only indexed fields: covered by ix_season_type
    return [{"$match": fixed}, {"$project": project},
            {"$group": {"_id": _cell_key(uuid), "n": {"$sum": 1}}},
            {"$facet": facets}]


def counts(coll, **filters):
    """{"season": {12: n, ...}, "type": {2: n, ...}, "result": {"win": n, ..., "": all} or None}"""
    covered = not filters.get("uuid") and not filters.get("lo") and not filters.get("hi")
    out = next(coll.aggregate(pipeline(**filters), comment="Filter counts ($facet)",
                          **({"hint": "ix_season_type"} if covered else {})), {})
    result = None
    if filters.get("uuid"):
        groups = out.get("result", [])
        result = {o: sum(g["n"] for g in groups if g["_id"] == o) for o in ("win", "loss", "draw", "decay")}
        result["forfeit"] = sum(g["forfeit"] for g in groups)
        result[""] = sum(g["n"] for g in groups)
    return {"season": {g["_id"]: g["n"] for g in out.get("season", [])},
            "type": {g["_id"]: g["n"] for g in out.get("type", [])},
            "result": result}
