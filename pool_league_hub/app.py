"""Pool League Hub: the league calendar, host locations and player match-ups.

With GOOGLE_CALENDAR_ID and a service account key set, the calendar syncs both ways with
the league's Google Calendar (league/gcal.py).

Run it with:  python app.py   (then open http://localhost:5000)
In production: gunicorn --workers 1 --threads 8 "app:create_app()"
(one worker only: league.json is written by one process)
"""

from __future__ import annotations

import os
import secrets
import threading
from functools import wraps
from pathlib import Path
from urllib.parse import quote_plus

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import views
from league import gcal
from league.core import League
from league.throttle import Throttle


def create_app(data_dir: str | None = None, gcal_client=None) -> Flask:
    app = Flask(__name__)
    data_path = Path(data_dir or os.environ.get("LEAGUE_DATA_DIR", Path(__file__).parent / "data"))
    data_path.mkdir(parents=True, exist_ok=True)

    key_file = data_path / "secret_key"
    if os.environ.get("SECRET_KEY"):
        app.secret_key = os.environ["SECRET_KEY"]
    else:
        if not key_file.exists():
            key_file.write_text(secrets.token_hex(32))
        app.secret_key = key_file.read_text()
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    office_password = os.environ.get("OFFICE_PASSWORD", "office")
    # Optional read-only office login (e.g. for a board member to look around).
    viewer_password = os.environ.get("VIEWER_PASSWORD", "")
    if os.environ.get("LEAGUE_PRODUCTION") == "1":
        # On the internet: refuse to run with the default password, and only send cookies over HTTPS.
        if office_password == "office" or len(office_password) < 8:
            raise RuntimeError("Set OFFICE_PASSWORD to a password of at least 8 characters before going live.")
        if viewer_password and (len(viewer_password) < 8 or viewer_password == office_password):
            raise RuntimeError("VIEWER_PASSWORD must be at least 8 characters and different from OFFICE_PASSWORD.")
        app.config["SESSION_COOKIE_SECURE"] = True
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.jinja_env.filters["urlquote"] = quote_plus

    @app.context_processor
    def globals_():
        return {"is_admin": session.get("admin", False),
                "is_viewer": bool(session.get("viewer")) and not session.get("admin")}

    def admin_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("admin"):
                if session.get("viewer"):
                    flash("You're signed in view-only, so changes are turned off.", "error")
                    return redirect(url_for("league_office"))
                return redirect(url_for("office_login", next=request.path))
            return view(*args, **kwargs)

        return wrapper

    def office_view(view):
        """Pages the view-only login may look at (it can't change anything)."""
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not (session.get("admin") or session.get("viewer")):
                return redirect(url_for("office_login", next=request.path))
            return view(*args, **kwargs)

        return wrapper

    login_by_ip = Throttle(limit=5, window=15 * 60)

    def minutes(seconds: int) -> str:
        n = max(1, (seconds + 59) // 60)
        return f"{n} minute{'s' if n != 1 else ''}"

    @app.route("/office/login", methods=["GET", "POST"])
    def office_login():
        if request.method == "POST":
            ip, password = request.remote_addr or "?", request.form.get("password", "")
            wait = login_by_ip.wait(ip)
            is_office = secrets.compare_digest(password, office_password)
            is_viewer = bool(viewer_password) and secrets.compare_digest(password, viewer_password)
            if wait:
                flash(f"Too many wrong passwords. Try again in {minutes(wait)}.", "error")
            elif is_office or is_viewer:
                login_by_ip.clear(ip)
                session.pop("admin", None)
                session.pop("viewer", None)
                session["admin" if is_office else "viewer"] = True
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("league_office"))
            else:
                login_by_ip.fail(ip)
                flash("Wrong password.", "error")
        return render_template("login.html", default_password=office_password == "office")

    @app.post("/office/logout")
    def office_logout():
        session.pop("admin", None)
        session.pop("viewer", None)
        return redirect(url_for("league_home"))

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # Google Calendar two-way sync (league/gcal.py), on when GOOGLE_CALENDAR_ID and a service account key are set.
    league = League(data_path)
    app.config["league"] = league
    tz = os.environ.get("LEAGUE_TIMEZONE", "America/New_York")
    client = gcal_client or gcal.client_from_env(os.environ)
    sync = gcal.Sync(league, client, tz) if client else None
    app.config["sync"] = sync
    if sync:
        league.on_change = sync.wake.set
        sync_started = threading.Event()

        @app.before_request
        def start_sync():
            # Started on the first request so it runs in the serving process only.
            if not app.testing and not sync_started.is_set():
                sync_started.set()
                every = int(os.environ.get("GOOGLE_SYNC_MINUTES", "5"))
                threading.Thread(target=sync.loop, args=(every, app.logger.warning), daemon=True).start()

    views.register(app, {"data_path": data_path, "minutes": minutes, "office_view": office_view,
                         "admin_required": admin_required, "league": league, "sync": sync, "tz": tz})
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    create_app().run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
