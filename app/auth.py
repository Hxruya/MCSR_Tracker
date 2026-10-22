"""Register / login / logout and the login_required decorator."""
import functools
import hashlib
import re
import threading
import time
from urllib.parse import urlsplit

import pymysql
from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from app.db_sql import get_db, query_one

bp = Blueprint("auth", __name__)

USERNAME = re.compile(r"^[A-Za-z0-9_]{3,30}$")
MIN_PASSWORD = 8
LOGIN_ERROR = "Invalid username or password"
# Checked when the username doesn't exist, so an unknown name takes as long as a wrong password.
DUMMY_HASH = generate_password_hash("not-a-real-password")

# After MAX_FAILURES failed logins for one username, that username can't log in for LOCK_SECONDS (counted from
# the last attempt). Unknown usernames are counted the same way, so the lock doesn't reveal which accounts exist.
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
# shortcut: counted in this process's memory, so a restart forgets them and several worker processes would each
# allow 5. Fine for the single-process local app; a deployment would keep the count in the database.
_failures = {}   # lower-case username -> (attempts, time.monotonic() of the latest)
_failures_lock = threading.Lock()


def start_attempt(username):
    """Count a login attempt before its password is checked, in one locked step, so parallel requests can't all
    get past the limit before any failure is recorded (a success clears the count). Returns the seconds left on
    a lock, or 0 to go ahead."""
    key, now = username.lower(), time.monotonic()
    with _failures_lock:
        for name, (_, last) in list(_failures.items()):   # forget expired entries, so the dict can't grow forever
            if now - last >= LOCK_SECONDS:
                del _failures[name]
        count, last = _failures.get(key, (0, now))
        if count >= MAX_FAILURES:
            return int(LOCK_SECONDS - (now - last)) + 1
        _failures[key] = (count + 1, now)
        return 0


def clear_failures(username):
    with _failures_lock:
        _failures.pop(username.lower(), None)


def safe_next(target):
    """Only allow relative paths on this site (no scheme, host or '//' / backslash tricks)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    parts = urlsplit(target)
    return target if not parts.scheme and not parts.netloc else None


def fingerprint(password_hash):
    """Stored in the session at login and checked on every request (app/__init__.py): changing the password
    changes the hash, which ends every other session. A digest, so the cookie never carries the hash itself."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def username_errors(username):
    if not USERNAME.match(username):
        return ["Username must be 3-30 characters: letters, digits or underscore."]
    return []


def password_errors(password, confirm):
    errors = []
    if len(password) < MIN_PASSWORD:
        errors.append(f"Password must be at least {MIN_PASSWORD} characters.")
    if password != confirm:
        errors.append("Passwords do not match.")
    return errors


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            nxt = request.full_path if request.query_string else request.path
            return redirect(url_for("auth.login", next=nxt))
        return view(*args, **kwargs)
    return wrapped


@bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        errors = username_errors(username) + password_errors(password, confirm)
        if not errors:
            db = get_db()
            try:
                with db.cursor() as cur:
                    cur.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                                (username, generate_password_hash(password)))
                db.commit()
            except pymysql.err.IntegrityError:
                db.rollback()
                errors.append("That username is already taken.")
            else:
                flash("Account created. Please log in.", "success")
                return redirect(url_for("auth.login"))
        for e in errors:
            flash(e, "error")
        return render_template("auth/register.html", username=username), 400
    return render_template("auth/register.html", username="")


@bp.route("/login", methods=["GET", "POST"])
def login():
    nxt = safe_next(request.values.get("next"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        wait = start_attempt(username)
        if wait:   # not even a correct password gets in until the lock expires
            flash(f"Too many failed attempts. Try again in {-(-wait // 60)} minute(s).", "error")
            return render_template("auth/login.html", username=username, next=nxt), 429
        # The column compares ignoring case and accents, so 'tárget_1' would find 'target_1' under a separate
        # lock count. Only names that sign-up allows are looked up; anything else can't be an account.
        user = (query_one("SELECT user_id, username, password_hash FROM users WHERE username = %s", (username,))
                if USERNAME.match(username) else None)
        ok = check_password_hash(user["password_hash"] if user else DUMMY_HASH, password)
        if user is None or not ok:
            flash(LOGIN_ERROR, "error")
            return render_template("auth/login.html", username=username, next=nxt), 401
        clear_failures(username)
        session.clear()
        session["user_id"] = user["user_id"]
        session["pw"] = fingerprint(user["password_hash"])
        flash(f"Welcome back, {user['username']}.", "success")
        return redirect(nxt or url_for("index"))
    return render_template("auth/login.html", username="", next=nxt)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))
