"""The logged-in user's own page: their notes and bookmarks in one place, and account settings.

Personal pages live under the username menu rather than the main navigation, which is for the public data.
Settings: change username, change password, delete the account (MariaDB row + bookmarks + MongoDB notes).
"""
import pymysql
from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for
from pymongo import DESCENDING
from pymongo.errors import PyMongoError
from werkzeug.security import check_password_hash, generate_password_hash

from app import notes, summary
from app.auth import fingerprint, login_required, password_errors, username_errors
from app.db_mongo import optional
from app.db_sql import get_db, query_all, query_one

bp = Blueprint("account", __name__)

RECENT = 5


@bp.route("/account")
@login_required
def index():
    uid = g.user["user_id"]
    user = query_one("SELECT username, created_at FROM users WHERE user_id = %s", (uid,))
    n_bookmarks = query_one("SELECT COUNT(*) AS n FROM bookmarks WHERE user_id = %s", (uid,))["n"]
    bookmarks = query_all(
        "SELECT b.note, p.player_id, p.uuid, p.nickname, p.current_elo FROM bookmarks b "
        "JOIN players p ON p.player_id = b.player_id WHERE b.user_id = %s ORDER BY b.created_at DESC LIMIT %s",
        (uid, RECENT))
    summary.with_win_rates(bookmarks)
    # None when MongoDB is unavailable: the bookmark half of the page still renders
    mine = optional(notes_overview, uid, what="the account page")
    return render_template("account/index.html", user=user, n_bookmarks=n_bookmarks, bookmarks=bookmarks,
                           notes=mine)


def notes_overview(uid):
    coll = notes.notes_coll()
    recent = list(coll.find({"user_id": uid}).sort("updated_at", DESCENDING).limit(RECENT))
    return {"count": coll.count_documents({"user_id": uid}), "recent": recent,
            "tags": notes.tag_counts(uid, limit=12), "n_tags": len(coll.distinct("tags", {"user_id": uid})),
            "summaries": summary.matches_by_id({n["match_id"] for n in recent})}


# ---------------------------------------------------------------- settings

def _settings():
    return redirect(url_for("account.index") + "#settings")


def _flash_all(errors):
    for e in errors:
        flash(e, "error")


def password_ok(password):
    """Re-check the logged-in user's password before a sensitive change."""
    row = query_one("SELECT password_hash FROM users WHERE user_id = %s", (g.user["user_id"],))
    return row is not None and check_password_hash(row["password_hash"], password)


@bp.route("/account/username", methods=["POST"])
@login_required
def change_username():
    username = request.form.get("username", "").strip()
    errors = username_errors(username)
    if errors:
        _flash_all(errors)
        return _settings()
    if username == g.user["username"]:
        flash("That is already your username.", "info")
        return _settings()
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("UPDATE users SET username = %s WHERE user_id = %s", (username, g.user["user_id"]))
        db.commit()
    except pymysql.err.IntegrityError:   # uq_users_username
        db.rollback()
        flash("That username is already taken.", "error")
        return _settings()
    flash(f"Your username is now {username}.", "success")
    return _settings()


@bp.route("/account/password", methods=["POST"])
@login_required
def change_password():
    password, confirm = request.form.get("password", ""), request.form.get("confirm", "")
    errors = [] if password_ok(request.form.get("current", "")) else ["Your current password is wrong."]
    errors += password_errors(password, confirm)
    if errors:
        _flash_all(errors)
        return _settings()
    new_hash = generate_password_hash(password)
    db = get_db()
    with db.cursor() as cur:
        cur.execute("UPDATE users SET password_hash = %s WHERE user_id = %s", (new_hash, g.user["user_id"]))
    db.commit()
    session["pw"] = fingerprint(new_hash)   # this browser stays logged in; every other session is logged out
    flash("Password changed. Any other device that was logged in has been logged out.", "success")
    return _settings()


@bp.route("/account/delete", methods=["POST"])
@login_required
def delete():
    """Delete the account: MongoDB notes first, then the MariaDB row (bookmarks go with it: ON DELETE CASCADE).

    The two stores share no transaction, so the order decides what a failure leaves behind. Notes first: if
    MongoDB fails, nothing has changed and the user can try again; if MariaDB then fails, the account remains
    without its notes, and deleting again finishes the job. The other order could leave notes owned by no one.
    """
    if not password_ok(request.form.get("password", "")):
        flash("Your password is wrong, so the account was not deleted.", "error")
        return _settings()
    uid = g.user["user_id"]
    try:
        notes.notes_coll().delete_many({"user_id": uid})
    except PyMongoError:
        current_app.logger.warning("MongoDB unavailable: account %s not deleted", uid, exc_info=True)
        flash("Your account couldn't be deleted right now (notes are unavailable). Nothing was removed; "
              "try again in a minute.", "error")
        return _settings()
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM users WHERE user_id = %s", (uid,))
    db.commit()
    session.clear()
    flash("Your account, bookmarks and notes have been deleted.", "success")
    return redirect(url_for("index"))