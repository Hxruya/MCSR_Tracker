"""Flask app factory."""
import logging
import os

from flask import Flask, g, render_template, session, url_for
from flask_wtf.csrf import CSRFError, CSRFProtect

from app import db_sql, stats
from app.config import Config
from app.query_benchmarks import QueryBenchmarks

csrf = CSRFProtect()


def create_app(config_overrides=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if config_overrides:
        app.config.update(config_overrides)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("FLASK_SECRET_KEY is not set (see .env.example)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    csrf.init_app(app)
    QueryBenchmarks(app)   # the "Behind this page" panel; see app/query_benchmarks/README.md
    app.teardown_appcontext(db_sql.close_db)

    from app import account, auth, bookmarks, compare, leaderboard, matches, notes, players, seeds
    for bp in (auth.bp, players.bp, matches.bp, compare.bp, seeds.bp, leaderboard.bp, bookmarks.bp, notes.bp,
               account.bp):
        app.register_blueprint(bp)

    app.jinja_env.filters["rate"] = stats.format_rate
    app.jinja_env.filters["ms"] = stats.format_ms
    app.jinja_env.filters["gap"] = stats.format_gap
    app.jinja_env.globals["nice"] = seeds.nice   # "RUINED_PORTAL" -> "Ruined Portal", in every template

    def asset(filename):
        """Static URL with ?v=<mtime> so browsers pick up CSS/JS changes (a global, so macros see it)."""
        path = os.path.join(app.static_folder, filename)
        version = int(os.path.getmtime(path)) if os.path.exists(path) else 0
        return url_for("static", filename=filename, v=version)
    app.jinja_env.globals["asset"] = asset

    @app.before_request
    def load_user():
        g.user = None
        user_id = session.get("user_id")
        if user_id is not None:
            row = db_sql.query_one("SELECT user_id, username, password_hash FROM users WHERE user_id = %s",
                                   (user_id,))
            # A deleted account, or a password changed since this session logged in: log it out
            if row is None or session.get("pw") != auth.fingerprint(row.pop("password_hash")):
                session.clear()
            else:
                g.user = row

    @app.route("/")
    def index():
        from app.home import landing_data
        return render_template("index.html", **landing_data())

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404, message="That page does not exist."), 404

    @app.errorhandler(CSRFError)
    def csrf_error(_e):
        return render_template("error.html", code=400,
                               message="Your form expired or was invalid. Go back, reload and try again."), 400

    @app.errorhandler(500)
    def server_error(_e):
        return render_template("error.html", code=500, message="Something went wrong on our side."), 500

    return app
