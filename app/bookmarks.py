"""Bookmarks CRUD. Every query is scoped to the logged-in user; user_id never comes from the form."""
import pymysql
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from app import summary, util
from app.auth import login_required
from app.db_sql import get_db, query_all, query_one

bp = Blueprint("bookmarks", __name__)

NOTE_MAX = 255
DUPLICATE_KEY = 1062


def clean_note(raw):
    note = (raw or "").strip()
    if len(note) > NOTE_MAX:
        raise util.BadParam(f"Notes can be at most {NOTE_MAX} characters.")
    return note or None


def owned(bookmark_id):
    row = query_one(
        "SELECT b.bookmark_id, b.note, b.created_at, b.updated_at, p.uuid, p.nickname "
        "FROM bookmarks b JOIN players p ON p.player_id = b.player_id "
        "WHERE b.bookmark_id = %s AND b.user_id = %s", (bookmark_id, g.user["user_id"]))
    if row is None:
        abort(404)
    return row


@bp.route("/bookmarks")
@login_required
def index():
    rows = query_all(
        "SELECT b.bookmark_id, b.note, b.created_at, b.updated_at, p.player_id, p.uuid, p.nickname, p.current_elo, "
        "p.country FROM bookmarks b JOIN players p ON p.player_id = b.player_id "
        "WHERE b.user_id = %s ORDER BY b.created_at DESC", (g.user["user_id"],))
    summary.with_win_rates(rows)
    return render_template("bookmarks/index.html", rows=rows)


@bp.route("/bookmarks/new", methods=["POST"])
@login_required
def create():
    uuid = util.normalise_uuid(request.form.get("uuid"))
    player = query_one("SELECT player_id, uuid, nickname FROM players WHERE uuid = %s", (uuid,)) if uuid else None
    if player is None:
        abort(404)
    try:
        note = clean_note(request.form.get("note"))
    except util.BadParam as e:
        flash(str(e), "error")
        return redirect(url_for("players.profile", uuid=player["uuid"]))
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO bookmarks (user_id, player_id, note) VALUES (%s, %s, %s)",
                        (g.user["user_id"], player["player_id"], note))
        db.commit()
        flash(f"Bookmarked {player['nickname']}.", "success")
    except pymysql.err.IntegrityError as e:
        db.rollback()
        if e.args[0] != DUPLICATE_KEY:
            raise
        flash(f"You have already bookmarked {player['nickname']}.", "info")
    return redirect(url_for("players.profile", uuid=player["uuid"]))


@bp.route("/bookmarks/<int:bookmark_id>/edit", methods=["GET", "POST"])
@login_required
def edit(bookmark_id):
    bm = owned(bookmark_id)
    if request.method == "POST":
        try:
            note = clean_note(request.form.get("note"))
        except util.BadParam as e:
            flash(str(e), "error")
            return render_template("bookmarks/edit.html", bm=dict(bm, note=request.form.get("note"))), 400
        db = get_db()
        with db.cursor() as cur:
            cur.execute("UPDATE bookmarks SET note = %s WHERE bookmark_id = %s AND user_id = %s",
                        (note, bookmark_id, g.user["user_id"]))
        db.commit()
        flash("Note saved.", "success")
        return redirect(url_for("bookmarks.index"))
    return render_template("bookmarks/edit.html", bm=bm)


@bp.route("/bookmarks/<int:bookmark_id>/delete", methods=["POST"])
@login_required
def delete(bookmark_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM bookmarks WHERE bookmark_id = %s AND user_id = %s",
                    (bookmark_id, g.user["user_id"]))
        deleted = cur.rowcount
    db.commit()
    if not deleted:
        abort(404)
    flash("Bookmark removed.", "success")
    return redirect(url_for("bookmarks.index"))
