"""Match notes: a user's private, tagged notes on matches, stored in MongoDB.

CRUD on the `notes` collection, full-text search ($text on text + tags), tag counts ($unwind/$group).
Every query is scoped to the logged-in user's id; nothing is trusted from the form except text and tags.
"""
import re
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for
from pymongo import DESCENDING
from pymongo.errors import PyMongoError, WriteError

from app import summary, util
from app.auth import login_required, safe_next
from app.db_mongo import get_mongo
from app.db_sql import query_one

bp = Blueprint("notes", __name__)


@bp.errorhandler(PyMongoError)
def mongo_unavailable(_e):
    """Notes live only in MongoDB, so with it down there is nothing to fall back to: say so instead of a 500."""
    current_app.logger.warning("MongoDB unavailable for notes", exc_info=True)
    return render_template("error.html", code=503,
                           message="Notes are unavailable right now. Try again in a minute."), 503

TEXT_MAX = 500
TAGS_MAX = 8
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")


def parse_tags(raw):
    """'Good zero, bastion route' -> ['good-zero', 'bastion-route'] (lowercase, unique, max 8)."""
    tags = []
    for part in (raw or "").split(","):
        tag = re.sub(r"[^a-z0-9-]+", "-", part.strip().lower()).strip("-")[:24].strip("-")
        if tag and tag not in tags:
            tags.append(tag)
    if len(tags) > TAGS_MAX:
        raise util.BadParam(f"At most {TAGS_MAX} tags.")
    if any(not TAG_RE.match(t) for t in tags):
        raise util.BadParam("Tags may only use letters, digits and dashes.")
    return tags


def clean_text(raw):
    text = (raw or "").strip()
    if not text:
        raise util.BadParam("A note can't be empty.")
    if len(text) > TEXT_MAX:
        raise util.BadParam(f"Notes can be at most {TEXT_MAX} characters.")
    return text


def notes_coll():
    return get_mongo()["notes"]


def mine(note_id):
    """Filter for one of the logged-in user's notes; 404 for a malformed id. Ownership is part of every
    query and write, so another user's id simply matches nothing."""
    try:
        return {"_id": ObjectId(note_id), "user_id": g.user["user_id"]}
    except (InvalidId, TypeError):
        abort(404)


def owned(note_id):
    """The note if it belongs to the logged-in user, else 404 (also for malformed ids)."""
    note = notes_coll().find_one(mine(note_id))
    if note is None:
        abort(404)
    return note


def notes_for_match(match_id):
    if not g.user:
        return []
    return list(notes_coll().find({"user_id": g.user["user_id"], "match_id": match_id})
                .sort("updated_at", DESCENDING))


def tag_counts(user_id, limit=30):
    """The user's most used tags: $unwind the tags array, $group, $sort."""
    return list(notes_coll().aggregate([
        {"$match": {"user_id": user_id}},
        {"$unwind": "$tags"},
        {"$group": {"_id": "$tags", "n": {"$sum": 1}}},
        {"$sort": {"n": -1, "_id": 1}},
        {"$limit": limit},
    ]))


@bp.route("/notes")
@login_required
def index():
    q = request.args.get("q", "").strip()[:100]
    tag = request.args.get("tag", "").strip().lower()
    try:
        page = util.parse_page(request.args.get("page"))
        if tag and not TAG_RE.match(tag):
            raise util.BadParam("Unknown tag.")
    except util.BadParam as e:
        flash(str(e), "error")
        return redirect(url_for("notes.index"))

    query = {"user_id": g.user["user_id"]}
    if tag:
        query["tags"] = tag
    if q:
        query["$text"] = {"$search": q}
    coll = notes_coll()
    total = coll.count_documents(query)
    cursor = coll.find(query, {"score": {"$meta": "textScore"}} if q else None,
                       comment="Notes search: $text, ranked by textScore" if q else "Your notes, newest first")
    cursor = cursor.sort([("score", {"$meta": "textScore"})]) if q else cursor.sort("updated_at", DESCENDING)
    notes = list(cursor.skip((page - 1) * util.PER_PAGE).limit(util.PER_PAGE))
    return render_template("notes/index.html", notes=notes, tags=tag_counts(g.user["user_id"]), q=q, tag=tag,
                           summaries=summary.matches_by_id({n["match_id"] for n in notes}),
                           paging=util.pages(total, page))


@bp.route("/matches/<int:match_id>/notes", methods=["POST"])
@login_required
def create(match_id):
    if query_one("SELECT 1 AS ok FROM matches WHERE match_id = %s", (match_id,)) is None:
        abort(404)
    try:
        text, tags = clean_text(request.form.get("text")), parse_tags(request.form.get("tags"))
    except util.BadParam as e:
        flash(str(e), "error")
        return redirect(url_for("matches.detail", match_id=match_id) + "#notes")
    now = datetime.now(timezone.utc)
    try:
        notes_coll().insert_one({"user_id": g.user["user_id"], "match_id": match_id, "text": text, "tags": tags,
                                 "created_at": now, "updated_at": now})
        flash("Note saved.", "success")
    except WriteError:  # schema validator rejected it (should not happen after the checks above)
        flash("That note couldn't be saved.", "error")
    return redirect(url_for("matches.detail", match_id=match_id) + "#notes")


@bp.route("/notes/<note_id>/edit", methods=["GET", "POST"])
@login_required
def edit(note_id):
    if request.method == "POST":
        try:
            text, tags = clean_text(request.form.get("text")), parse_tags(request.form.get("tags"))
        except util.BadParam as e:
            flash(str(e), "error")
            return render_template("notes/edit.html", note=dict(owned(note_id), text=request.form.get("text", ""),
                                                                tags_raw=request.form.get("tags", ""))), 400
        # One atomic find-and-modify: the ownership check and the write can't be separated by another request
        if notes_coll().find_one_and_update(
                mine(note_id), {"$set": {"text": text, "tags": tags, "updated_at": datetime.now(timezone.utc)}},
                projection={"_id": 1}) is None:
            abort(404)
        flash("Note updated.", "success")
        return redirect(safe_next(request.form.get("back")) or url_for("notes.index"))
    note = owned(note_id)
    return render_template("notes/edit.html", note=dict(note, tags_raw=", ".join(note["tags"])))


@bp.route("/notes/<note_id>/delete", methods=["POST"])
@login_required
def delete(note_id):
    if notes_coll().find_one_and_delete(mine(note_id), projection={"_id": 1}) is None:
        abort(404)
    flash("Note deleted.", "success")
    return redirect(safe_next(request.form.get("back")) or url_for("notes.index"))
