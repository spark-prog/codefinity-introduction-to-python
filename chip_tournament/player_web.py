"""Player login pages: member number or email, an emailed code, then 90 days logged in.

The login is its own signed cookie (not the Flask session), so a director who also logs in as
a player on the same phone doesn't stay a director for 90 days.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from flask import abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer

from chips import backup, players
from chips import pending as pending_mod
from chips.engine import Player, TournamentError
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
        return render_template("player_login.html", t=None, next=request.args.get("next", request.form.get("next", "")))

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


def full_name(rec: dict) -> str:
    return f"{rec['first']} {rec['last']}".strip()


def name_matches(typed: str, rec: dict) -> bool:
    """'ann lee', 'Annie Lee' (nickname) or 'Ann M. Lee' all match Ann Lee (first or nickname, and last name)."""
    words = (typed or "").lower().replace(".", " ").split()
    last_words = (rec.get("last") or "").lower().split()
    if len(words) < 2 or not last_words:
        return False
    firsts = {w for w in ((rec.get("first") or "").lower().split()[:1] + [(rec.get("nickname") or "").lower()]) if w}
    return words[-1] == last_words[-1] and words[0] in firsts


def fill_for(rec: dict, t) -> dict:
    """What a registration form gets for this player in this event (never their phone or email)."""
    game = getattr(t.settings, "skill_game", "8")
    skill, how = players.skill_for(rec, game)
    return {"member": rec["member"], "name": full_name(rec), "skill": skill or "", "how": how,
            "skill8": rec.get("sl8") or "", "skill9": rec.get("sl9") or ""}


def register_registration(app) -> None:
    """Autofill for registration forms, and knowing who's already registered."""
    store: players.PlayerStore = app.config["players"]

    def my_registration(t):
        """The team or entry the logged-in player is on in this event: (id, name), or None."""
        me = g.get("player")
        if not me or me["member"] in {x["member"] for x in t.settings.member_exceptions}:
            return None  # no player, or the director allowed them more than one spot
        found = t.registered_as(me["member"])
        if found is None:  # registered before member numbers: match their name instead (display only)
            want = full_name(me).lower()
            people = t.teams.values() if hasattr(t, "teams") else t.entries.values()
            found = next((x for x in people if getattr(x, "status", "") != "no_show"
                          and any(" ".join(p.name.lower().split()) == want for p in x.players)), None)
        return found

    def attach_members(form, plist: list[Player]) -> list[Player]:
        """Member numbers on the form, checked against the player list.

        In the list with a matching name (or picked by a logged-in player): verified, named as in the list.
        In the list under another name, or not in the list: kept, but pending the director's check."""
        logged_in = bool(g.get("player"))
        for i, p in enumerate(plist):
            raw = form.get(f"player_member_{i}", "")
            member = re.sub(r"\D", "", raw)
            if not member:
                continue
            if not players.MEMBER_RE.fullmatch(member):
                raise TournamentError(f"'{raw.strip()}' isn't a member number: they're 8 digits starting with 322.")
            rec = store.get(member)
            p.member = member
            if rec is not None and (logged_in or name_matches(p.name, rec)):
                p.name, p.pending = full_name(rec), ""
            elif rec is not None:
                p.pending = f"The player list has {member} as {full_name(rec)}"
            elif store.records():
                p.pending = "Not in the player list"
            else:
                p.pending = "The player list isn't loaded"
        return plist

    def my_prefill(t) -> dict:
        """Partner 1 / the player: the logged-in player's own details, phone included (it's theirs)."""
        me = g.get("player")
        if not me:
            return {}
        fill = fill_for(me, t)
        cell = me.get("cell") or ""
        fill["phone"] = f"{cell[:3]}-{cell[3:6]}-{cell[6:]}" if len(cell) == 10 else ""
        return fill

    @app.before_request
    def roster_member_opens_team_page():
        """A logged-in player on a team's roster (by member number) doesn't need the team PIN."""
        me, ev, args = g.get("player"), g.get("event"), request.view_args or {}
        team_id = args.get("team_id") or args.get("entry_id")
        if not (me and ev and team_id):
            return
        t = ev.store.load()
        found = (getattr(t, "teams", None) or getattr(t, "entries", None) or {}).get(team_id)
        if found is not None and any(p.member == me["member"] for p in found.players):
            session[f"team_{team_id}"] = True
            session[f"entry_{team_id}"] = True

    def pending_rows(t) -> list[dict]:
        """For the director's Pending verification box: what was typed, and what the player list says now."""
        rows = []
        for grp, i, p in pending_mod.pending(t):
            rec = store.get(p.member)
            rows.append({"gid": grp.id, "i": i, "group": grp.name, "name": p.name, "member": p.member,
                         "reason": p.pending, "list_name": full_name(rec) if rec else "",
                         "now_ok": bool(rec) and name_matches(p.name, rec)})
        return rows

    app.jinja_env.globals["pending_rows"] = pending_rows

    @app.post("/t/<event>/admin/pending/<gid>/<int:i>")
    def admin_pending(gid, i):
        if not session.get("admin"):
            flash("Only the director can verify member numbers.", "error")
            return redirect(url_for("admin"))
        action, store_ev = request.form.get("action", ""), g.event.store
        try:
            with store_ev.change(f"Pending verification: {action}") as t:
                if action == "verify":
                    who = pending_mod.verify(t, gid, i)
                    msg = f"Verified {who}."
                elif action == "correct":
                    member = re.sub(r"\D", "", request.form.get("member", ""))
                    rec = store.get(member) if players.MEMBER_RE.fullmatch(member) else None
                    if rec is None:
                        raise TournamentError("Enter a member number that's in the player list (8 digits, 322…).")
                    who = pending_mod.verify(t, gid, i, member=member, name=full_name(rec))
                    msg = f"Corrected to {member} ({who})."
                elif action == "remove":
                    msg = f"Removed {pending_mod.remove(t, gid)} and texted them why."
                    sms = g.event.sms
                    messages, t.outbox = list(t.outbox), []
                    test = t.settings.test_event
                    for m in messages:
                        sms.send(m.to, m.body, test=test)
                else:
                    raise TournamentError("Pick verify, correct or remove.")
            flash(msg, "ok")
        except TournamentError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin") + "#pending")

    def exception_rows(t) -> list[dict]:
        rows = []
        for x in t.settings.member_exceptions:
            rec = store.get(x["member"])
            rows.append({**x, "name": full_name(rec) if rec else "", "teams": pending_mod.on_teams(t, x["member"])})
        return rows

    app.jinja_env.globals["exception_rows"] = exception_rows

    @app.post("/t/<event>/admin/exceptions")
    def admin_exceptions():
        if not session.get("admin"):
            flash("Only the director can add exceptions.", "error")
            return redirect(url_for("admin"))
        f = request.form
        member = re.sub(r"\D", "", f.get("member", ""))
        try:
            if not players.MEMBER_RE.fullmatch(member):
                raise TournamentError("Member numbers are 8 digits and start with 322.")
            with g.event.store.change(f"Exception for {member}: {f.get('action', '')}") as t:
                if f.get("action") == "remove":
                    pending_mod.remove_exception(t, member)
                    msg = f"Removed the exception for {member}. Registrations already made stay."
                else:
                    pending_mod.allow_twice(t, member, f.get("note", ""))
                    rec = store.get(member)
                    msg = f"{full_name(rec) if rec else member} may now be on more than one team in this event."
            flash(msg, "ok")
        except TournamentError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin") + "#exceptions")

    app.config["attach_members"] = attach_members
    app.jinja_env.globals["my_registration"] = my_registration
    app.jinja_env.globals["my_prefill"] = my_prefill

    @app.get("/t/<event>/player-lookup")
    def player_lookup():
        """Partner search for logged-in players: names and skill levels only."""
        if not g.get("player"):
            abort(403)
        q = request.args.get("q", "").strip()
        if len(q) < 2:
            return jsonify([])
        t = g.event.store.load()
        out = []
        for rec in store.search(q, limit=8):
            item = fill_for(rec, t)
            item["registered"] = (t.registered_as(rec["member"]) is not None and rec["member"] not in
                                  {x["member"] for x in t.settings.member_exceptions})
            out.append(item)
        return jsonify(out)
