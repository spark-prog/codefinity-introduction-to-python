"""Player login pages: member number or email, an emailed code, then 90 days logged in.

The login is its own signed cookie (not the Flask session), so a director who also logs in as
a player on the same phone doesn't stay a director for 90 days.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from flask import flash, g, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer

from chips import backup, players
from chips.accounts import LOGIN_DAYS, Accounts, LoginError, mask_email
from chips.throttle import Throttle

COOKIE = "ct_player"


def register(app, h) -> None:
    store: players.PlayerStore = app.config["players"]
    accounts = Accounts(app.config["data_dir"], app.secret_key)
    app.config["accounts"] = accounts
    signer = URLSafeTimedSerializer(app.secret_key, salt="player-login")
    sends_by_ip = Throttle(limit=30, window=3600)
    sends_by_member = Throttle(limit=5, window=3600)
    wrong_by_device = Throttle(limit=20, window=15 * 60)

    def current_player() -> dict | None:
        raw = request.cookies.get(COOKIE)
        if not raw:
            return None
        try:
            data = signer.loads(raw, max_age=LOGIN_DAYS * 86400)
        except BadSignature:
            return None
        member = data.get("m", "")
        if data.get("v") != accounts.version(member):
            return None  # logged out on all devices since
        return store.get(member)

    @app.before_request
    def load_player():
        g.player = current_player()

    @app.context_processor
    def player_globals():
        return {"player": g.get("player")}

    def safe_next(value: str) -> str:
        return value if value.startswith("/") and not value.startswith("//") else url_for("my_account")

    @app.route("/login", methods=["GET", "POST"])
    def player_login():
        if g.player:
            return redirect(safe_next(request.args.get("next", "")))
        if request.method == "POST":
            who = request.form.get("who", "").strip()
            nxt = request.form.get("next", "")
            try:
                rec = find(who)
                ip = request.remote_addr or "?"
                if sends_by_ip.wait(ip) or sends_by_member.wait(rec["member"]):
                    raise LoginError("Too many codes were asked for. Try again in an hour, or contact the league office.")
                mailer = backup.Mailer.from_env()
                if mailer is None:
                    raise LoginError("Player login isn't set up yet. Contact the league office.")
                code = accounts.new_code(rec["member"])
                legal = app.jinja_env.globals["legal"]
                try:
                    mailer.send_to(rec["email"], f"Your Control Table code: {code}",
                                   f"Hi {rec['first']},\n\nYour code to log in to Control Table is {code}.\n"
                                   "It works for 10 minutes. If you didn't ask for it, you can ignore this email.\n\n"
                                   f"{legal['brand']}", sender=legal["brand"], reply_to=legal["email"])
                except backup.BackupError:
                    raise LoginError("The code email couldn't be sent. Try again in a minute, or contact the league office.")
                sends_by_ip.fail(ip)
                sends_by_member.fail(rec["member"])
                session["login_member"] = rec["member"]
                session["login_next"] = nxt
                return redirect(url_for("player_code"))
            except (LoginError, players.PlayerError) as exc:
                flash(str(exc), "error")
        return render_template("login.html", t=None, next=request.args.get("next", request.form.get("next", "")))

    def find(who: str) -> dict:
        if not store.records():
            raise LoginError("Player login isn't available yet. Contact the league office.")
        if "@" in who:
            rec = store.by_email(who)
            if rec is None:
                raise LoginError("That email isn't in the player list. Try your member number, or contact the league office.")
        else:
            member = re.sub(r"\D", "", who)
            if not players.MEMBER_RE.fullmatch(member):
                raise LoginError("Member numbers are 8 digits and start with 322.")
            rec = store.get(member)
            if rec is None:
                raise LoginError("That member number isn't in the player list. Check it, or contact the league office.")
        if not rec.get("email"):
            raise LoginError("There's no email on file for you, so a code can't be sent. Contact the league office to add one.")
        return rec

    @app.route("/login/code", methods=["GET", "POST"])
    def player_code():
        member = session.get("login_member")
        rec = store.get(member) if member else None
        if rec is None:
            return redirect(url_for("player_login"))
        if request.method == "POST":
            device = h["device_id"]()
            try:
                if wrong_by_device.wait(device):
                    raise LoginError("Too many wrong codes from this phone. Wait 15 minutes and ask for a new code.")
                accounts.check_code(member, request.form.get("code", ""))
            except LoginError as exc:
                wrong_by_device.fail(device)
                flash(str(exc), "error")
            else:
                wrong_by_device.clear(device)
                session.pop("login_member", None)
                resp = redirect(safe_next(session.pop("login_next", "") or ""))
                resp.set_cookie(COOKIE, signer.dumps({"m": member, "v": accounts.version(member)}),
                                max_age=LOGIN_DAYS * 86400, httponly=True, samesite="Lax",
                                secure=app.config.get("SESSION_COOKIE_SECURE", False))
                flash(f"Welcome, {rec['first']}! You'll stay logged in on this device for {LOGIN_DAYS} days.", "ok")
                return resp
        return render_template("login_code.html", t=None, email=mask_email(rec["email"]))

    @app.get("/me")
    def my_account():
        if not g.player:
            return redirect(url_for("player_login", next=request.path))
        return render_template("me.html", t=None, me=g.player, skill_for=players.skill_for)

    @app.post("/logout")
    def player_logout():
        resp = redirect(url_for("index"))
        resp.delete_cookie(COOKIE)
        flash("You're logged out on this device.", "ok")
        return resp

    @app.post("/logout/everywhere")
    def player_logout_everywhere():
        if g.player:
            accounts.log_out_everywhere(g.player["member"])
        resp = redirect(url_for("index"))
        resp.delete_cookie(COOKIE)
        flash("You're logged out on every device.", "ok")
        return resp
