"""Player login pages: member number or email, an emailed code, then 90 days logged in.

The login is its own signed cookie (not the Flask session), so a director who also logs in as
a player on the same phone doesn't stay a director for 90 days.
"""

from __future__ import annotations

import re
from datetime import datetime

from flask import abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer

from chips import backup, eligibility, players, reminders, schedule
from chips import pending as pending_mod
from chips import pool as pool_mod
from chips import series as series_mod
from chips import waitlist as waitlist_mod
from chips import history as history_mod
from chips.bracket import asks_two_levels
from chips.engine import Player, TournamentError
from chips.accounts import LOGIN_DAYS, Accounts, CodeJustSent, LoginError, mask_email
from chips.throttle import Throttle

COOKIE = "ct_player"


def register(app, h) -> None:
    store: players.PlayerStore = app.config["players"]
    accounts = Accounts(app.config["data_dir"], app.secret_key)
    app.config["accounts"] = accounts
    app.config["device_id"] = h["device_id"]
    signer = URLSafeTimedSerializer(app.secret_key, salt="player-login")
    sends_by_ip = Throttle(limit=30, window=3600)
    sends_by_member = Throttle(limit=5, window=3600)
    wrong_by_device = Throttle(limit=20, window=15 * 60)
    wrong_pw_by_member = Throttle(limit=10, window=15 * 60)

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
            return None  # logged out on all devices since (or the password changed)
        rec = store.get(member)
        if rec is not None:
            g.login_via, g.login_at = data.get("via", "code"), data.get("t", 0)
        return rec

    def set_login(resp, member: str, via: str):
        """Log this device in for 90 days; `via` (code or password) and the time are kept for fresh-login checks."""
        import time as _t
        resp.set_cookie(COOKIE, signer.dumps({"m": member, "v": accounts.version(member), "via": via, "t": int(_t.time())}),
                        max_age=LOGIN_DAYS * 86400, httponly=True, samesite="Lax",
                        secure=app.config.get("SESSION_COOKIE_SECURE", False))
        return resp

    def fresh_code_login() -> bool:
        """Logged in with an emailed code in the last 10 minutes (needed to set a password)."""
        import time as _t
        return g.get("login_via") == "code" and _t.time() - (g.get("login_at") or 0) < 600

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
        if request.method == "POST" and request.form.get("action") == "password":
            who, nxt = request.form.get("who", "").strip(), request.form.get("next", "")
            device = h["device_id"]()
            try:
                rec = find(who, need_email=False)
                if wrong_pw_by_member.wait(rec["member"]) or wrong_by_device.wait(device):
                    raise LoginError("Too many wrong passwords. Wait 15 minutes, or use an emailed code.")
                if not accounts.has_password(rec["member"]):
                    raise LoginError("You haven't set a password. Use \"Email me a code\" instead.")
                if not accounts.check_password(rec["member"], request.form.get("password", "")):
                    wrong_pw_by_member.fail(rec["member"])
                    wrong_by_device.fail(device)
                    raise LoginError("That password isn't right. Try again, or use \"Email me a code\".")
                wrong_pw_by_member.clear(rec["member"])
                flash(f"Welcome back, {rec['first']}!", "ok")
                return set_login(redirect(safe_next(nxt)), rec["member"], "password")
            except (LoginError, players.PlayerError) as exc:
                flash(str(exc), "error")
        elif request.method == "POST":
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
            except CodeJustSent:
                # A second tap while the first was still sending (or a quick retry): the code from a moment ago is
                # on its way, so go to where it's entered instead of stopping on an error.
                session["login_member"] = rec["member"]
                session["login_next"] = nxt
                flash("We emailed you a code a moment ago. Enter it below (check spam if it isn't there yet).", "ok")
                return redirect(url_for("player_code"))
            except (LoginError, players.PlayerError) as exc:
                flash(str(exc), "error")
        return render_template("player_login.html", t=None, next=request.args.get("next", request.form.get("next", "")))

    def find(who: str, need_email: bool = True) -> dict:
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
        if need_email and not rec.get("email"):
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
                resp = set_login(redirect(safe_next(session.pop("login_next", "") or "")), member, "code")
                flash(f"Welcome, {rec['first']}! You'll stay logged in on this device for {LOGIN_DAYS} days.", "ok")
                return resp
        return render_template("login_code.html", t=None, email=mask_email(rec["email"]))

    @app.get("/me")
    def my_account():
        if not g.player:
            return redirect(url_for("player_login", next=request.path))
        app.config["record_finished"]()  # catch an event that just finished
        events = app.config["your_events"](g.player)
        past_rows = app.config["history"].for_member(g.player["member"])
        return render_template("me.html", t=None, me=g.player, skill_for=players.skill_for,
                               has_password=accounts.has_password(g.player["member"]), fresh=fresh_code_login(),
                               upcoming=[e for e in events if e["status"] != "finished"],
                               past=past_rows, career=history_mod.summary(past_rows),
                               open_events=[r for r in app.config["public_results"](g.player["member"])
                                            if r["status"] != "registered"])

    @app.post("/me/verify")
    def me_verify():
        """Email a code to the logged-in player, to unlock password settings for 10 minutes."""
        if not g.player:
            return redirect(url_for("player_login"))
        mailer = backup.Mailer.from_env()
        try:
            if mailer is None or not g.player.get("email"):
                raise LoginError("A code can't be sent right now. Contact the league office.")
            code = accounts.new_code(g.player["member"])
            legal = app.jinja_env.globals["legal"]
            mailer.send_to(g.player["email"], f"Your Control Table code: {code}",
                           f"Hi {g.player['first']},\n\nYour code is {code}. It works for 10 minutes.\n\n{legal['brand']}",
                           sender=legal["brand"], reply_to=legal["email"])
        except CodeJustSent:
            flash("We emailed you a code a moment ago. Enter it below (check spam if it isn't there yet).", "ok")
        except (LoginError, backup.BackupError) as exc:
            flash(str(exc) if isinstance(exc, LoginError) else "The code email couldn't be sent. Try again in a minute.",
                  "error")
            return redirect(url_for("my_account") + "#password")
        session["login_member"], session["login_next"] = g.player["member"], url_for("my_account") + "#password"
        return redirect(url_for("player_code"))

    @app.post("/me/password")
    def me_password():
        if not g.player:
            return redirect(url_for("player_login"))
        member, f = g.player["member"], request.form
        action = f.get("action", "set")
        has = accounts.has_password(member)
        try:
            if not (fresh_code_login() or (has and accounts.check_password(member, f.get("current", "")))):
                raise LoginError("For safety, confirm with an emailed code first"
                                 + (" (or enter your current password)." if has else "."))
            if action == "remove":
                accounts.remove_password(member)
                flash("Password removed. You'll log in with emailed codes.", "ok")
                return redirect(url_for("my_account"))
            if f.get("password", "") != f.get("confirm", ""):
                raise LoginError("The two passwords don't match.")
            accounts.set_password(member, f.get("password", ""))
        except LoginError as exc:
            flash(str(exc), "error")
            return redirect(url_for("my_account") + "#password")
        flash("Password saved. You're logged out on your other devices; log in there with your password or a code.",
              "ok")
        return set_login(redirect(url_for("my_account")), member, "code" if fresh_code_login() else "password")

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
    """Does the typed name fit the player list's record for that member number?
    'Ann Lee', 'Annie Lee' (nickname), 'Ann M. Lee' all match Ann Lee; so does just a first name ('Ann'), as
    long as it's their first name, nickname, or a short form of 3+ letters ('Angel' for Angelica). A different
    last name (a married name, a typo, someone else's number) still goes to the director to check."""
    words = (typed or "").lower().replace(".", " ").split()
    if not words:
        return False
    first = ((rec.get("first") or "").lower().split() or [""])[0]
    nickname = (rec.get("nickname") or "").lower().strip()

    def first_fits(word: str) -> bool:
        return word in {first, nickname} - {""} or (len(word) >= 3 and bool(first) and first.startswith(word))

    if len(words) == 1:
        return first_fits(words[0])
    last_words = (rec.get("last") or "").lower().split()
    return bool(last_words) and words[-1] == last_words[-1] and first_fits(words[0])


def fill_for(rec: dict, t) -> dict:
    """What a registration form gets for this player in this event (never their phone or email)."""
    game = getattr(t.settings, "skill_game", "8")
    skill, how = players.event_skill(rec, game)
    return {"member": rec["member"], "name": full_name(rec), "skill": skill or "", "how": how,
            "skill8": rec.get("sl8") or "", "skill9": rec.get("sl9") or ""}


def register_registration(app) -> None:
    """Autofill for registration forms, and knowing who's already registered."""
    store: players.PlayerStore = app.config["players"]
    pushes = series_mod.Pushes(app.config["data_dir"])
    app.config["pushes"] = pushes

    def push_target(event_id: str, t) -> tuple[str, str] | None:
        """(event id, name) this event's players can push their eligibility to, or None."""
        return series_mod.target(app.config["hub"], event_id, t.settings)

    def pushed_out(event_id: str, t, member: str, said_to_them: bool = True) -> str:
        """Why a player who pushed their eligibility away from this event can't enter it ("" if they didn't).
        said_to_them: "You pushed your …" (the player's own pages) or "They pushed their …" (a registration form)."""
        mine = [p for p in pushes.from_event(event_id) if p["member"] == member] if member else []
        if mine:
            where = mine[-1].get("to_name") or f"the next {mine[-1].get('series') or 'event'}"
            you, your = ("You", "your") if said_to_them else ("They", "their")
            return (f"{you} pushed {your} eligibility to {where}, so {you.lower()} can't enter this one. "
                    "If that was a mistake, ask the league office.")
        return ""

    def pushed_in(event_id: str, t, member: str):
        """The push that makes this member eligible for this edition (from the series' previous one)."""
        return series_mod.pushed_into(app.config["hub"], pushes, event_id, getattr(t.settings, "series", ""), member)

    app.config["pushed_in"] = pushed_in

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

    def attach_members(form, plist: list[Player], t=None, existing=()) -> list[Player]:
        """Member numbers on the form, checked against the player list.

        In the list with a matching name (or picked by a logged-in player): verified, named as in the list.
        In the list under another name, or not in the list: kept, but pending the director's check.
        When the event requires member numbers (players, not the director), every player needs one, except
        someone already on the roster from before numbers were asked for (`existing`)."""
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
                if logged_in and member != g.player["member"]:
                    p.confirm = "ask"  # a partner picked by someone else confirms by email
            elif rec is not None:
                p.pending = f"The player list has {member} as {full_name(rec)}"
            elif store.records():
                p.pending = "Not in the player list"
            else:
                p.pending = "The player list isn't loaded"
        if t is not None and getattr(t.settings, "require_member", False) and not session.get("admin"):
            def key(name):
                return " ".join((name or "").lower().split())
            grandfathered = {key(o.name) for o in existing if not o.member}
            for p in plist:
                if not p.member and key(p.name) not in grandfathered:
                    raise TournamentError(f"Enter {p.name or 'each player'}'s APA member number (8 digits starting "
                                          "with 322). It's on their APA card and in the APA app.")
        if t is not None and not session.get("admin"):
            check_eligibility(t, plist, existing)
            already = {o.member for o in existing if o.member}
            pool_mod.check_times(app.config["hub"], app.config["pools"], g.event.id, t,
                                 [p for p in plist if p.member not in already])
        return plist

    def check_eligibility(t, plist: list[Player], existing=()) -> None:
        """Refuse a player who doesn't qualify (saying why and how to fix it); send what the list
        can't answer to Pending verification. Players already on the roster aren't re-checked here."""
        rules = eligibility.rules_of(t.settings)
        if not eligibility.any_rules(rules):
            return
        start, game = store.config()["session_start"], getattr(t.settings, "skill_game", "8")
        already = {o.member for o in existing if o.member}
        recs = []
        for p in plist:
            rec = store.get(p.member) if p.member else None
            if p.member and p.member in already:
                recs.append(rec)
                continue
            if p.pending:  # the number itself is waiting for the director
                recs.append(None)
                continue
            if p.member and eligibility.override_for(t, p.member):  # the director already said yes
                recs.append(rec)
                continue
            if p.member and pushed_out(g.event.id, t, p.member):  # they gave this one up for next time
                raise TournamentError(f"{p.name} can't register for {t.settings.name}: "
                                      + pushed_out(g.event.id, t, p.member, said_to_them=False))
            no, review = eligibility.check_player(rec, rules, start, game,
                                                  bool(p.member and pushed_in(g.event.id, t, p.member)))
            if no:
                raise TournamentError(f"{p.name} can't register for {t.settings.name}: " + " ".join(no))
            if review:
                p.pending = "Eligibility: " + "; ".join(review)
            recs.append(rec)
        team = eligibility.check_team(recs, rules)
        if team:
            raise TournamentError(team[0])

    def my_eligibility(t):
        """For a logged-in player looking at an event: are they eligible, and why not?"""
        me = g.get("player")
        rules = eligibility.rules_of(t.settings)
        if not me or not eligibility.any_rules(rules):
            return None
        if eligibility.override_for(t, me["member"]):
            return {"ok": True, "no": [], "review": [], "override": True}
        asked = next((r for r in reversed(t.settings.review_requests) if r["member"] == me["member"]), None)
        gone = pushed_out(g.event.id, t, me["member"])
        if gone:
            return {"ok": False, "no": [gone], "review": [], "asked": asked, "grace": False, "can_push": False}
        via = pushed_in(g.event.id, t, me["member"])
        no, review = eligibility.check_player(me, rules, store.config()["session_start"],
                                              getattr(t.settings, "skill_game", "8"), bool(via))
        grace = bool(via) or eligibility.list_value(me, rules) == "push"
        return {"ok": not no, "no": no, "review": review, "asked": asked, "grace": grace,
                "can_push": can_push(g.event.id, t, me["member"])[0]}

    app.jinja_env.globals["my_eligibility"] = my_eligibility

    # ------------------------------------------------ Phase 8: the public eligibility check

    lookups_by_ip = Throttle(limit=40, window=3600)
    lookups_by_device = Throttle(limit=20, window=3600)

    def public_results(member: str) -> list[dict]:
        """Upcoming events and whether this member can play: a status only, never the reasons (anyone can type
        anyone's number). The reasons are for the player themself, logged in."""
        rec, start = store.get(member), store.config()["session_start"]
        mine = bool(g.get("player")) and g.player["member"] == member
        out = []
        for ev, t in app.config["hub"].loaded():
            s = t.settings
            if t.status == "finished" or (s.test_event and not session.get("admin")):
                continue  # test events only show to a phone that's also signed in as director (practice runs)
            rules = eligibility.rules_of(s)
            row = {"name": s.name, "date": s.date, "time": s.start_time, "home": url_for("home", event=ev.id),
                   "register": url_for("register", event=ev.id), "open": t.status == "setup" and not s.registration_shut,
                   "reasons": [], "id": ev.id, "venue": s.venue,
                   "singles": ev.kind == "bracket" and getattr(s, "team_size", 2) == 1,
                   "quick": url_for("quick_register", event=ev.id), "shut_note": schedule.shut_message(s)
                   if t.status == "setup" and s.registration_shut else ""}
            if t.registered_as(member) is not None:
                row["status"] = "registered"
            elif not eligibility.any_rules(rules) or eligibility.override_for(t, member):
                row["status"] = "eligible"
            elif pushed_out(ev.id, t, member):
                row["status"] = "no"
                if mine:
                    row["reasons"] = [pushed_out(ev.id, t, member)]
            else:
                no, review = eligibility.check_player(rec, rules, start, getattr(s, "skill_game", "8"),
                                                      bool(pushed_in(ev.id, t, member)))
                row["status"] = "no" if no else ("check" if review else "eligible")
                if mine:
                    row["reasons"] = no or review
            out.append(row)
        return sorted(out, key=lambda r: r["date"] or "9999")

    app.config["public_results"] = public_results

    # ------------------------------------------------ Phase 11: the partner finder

    asks_by_member = Throttle(limit=5, window=3600)

    def doubles(ev, t) -> bool:
        return ev.kind == "chip" or getattr(t.settings, "team_size", 1) == 2

    def seekers(ev, t) -> list[dict]:
        """Everyone looking for a partner in an event: chip players who registered alone and asked to be listed,
        and logged-in players who listed themselves without registering."""
        out = []
        if ev.kind == "chip":
            for team in t.seeking_partner:
                p = team.players[0]
                out.append({"member": p.member, "name": p.name, "skill": p.skill, "team": team.id,
                            "source": "registered"})
        game = getattr(t.settings, "skill_game", "8")
        for x in t.settings.partner_seekers:
            if t.registered_as(x["member"]) is not None:
                continue  # they found a team
            rec = store.get(x["member"])
            if rec is None:
                continue
            skill, _ = players.event_skill(rec, game)
            out.append({"member": x["member"], "name": full_name(rec), "skill": skill, "team": "", "source": "listed"})
        return out

    def partner_finder(t) -> dict:
        """For the register page: who's looking, filtered to who fits the logged-in player."""
        ev, me = g.event, g.get("player")
        everyone = seekers(ev, t)
        result = {"everyone": everyone, "fits": None, "hidden": 0, "me_listed": False, "my_skill": None,
                  "cap": t.settings.registration_cap if ev.kind == "chip" else None, "doubles": doubles(ev, t)}
        if not me:
            return result
        game = getattr(t.settings, "skill_game", "8")
        mine, _ = players.event_skill(me, game)
        rules, start = eligibility.rules_of(t.settings), store.config()["session_start"]
        fits = []
        for s_ in everyone:
            if s_["member"] == me["member"]:
                continue
            if s_["member"] and eligibility.any_rules(rules) and not eligibility.override_for(t, s_["member"]):
                no, _ = eligibility.check_player(store.get(s_["member"]), rules, start, game)
                if no:
                    continue
            if result["cap"] and mine and s_["skill"] and mine + s_["skill"] > result["cap"]:
                continue
            fits.append({**s_, "combined": (mine or 0) + (s_["skill"] or 0)})
        result.update(fits=fits, hidden=len([x for x in everyone if x["member"] != me["member"]]) - len(fits),
                      my_skill=mine, me_listed=any(x["member"] == me["member"] for x in t.settings.partner_seekers))
        return result

    app.jinja_env.globals["partner_finder"] = partner_finder

    @app.post("/t/<event>/partner-list")
    def partner_list():
        """A logged-in player adds or removes themselves from the looking-for-a-partner list."""
        me, ev = g.get("player"), g.event
        if not me:
            return redirect(url_for("player_login", next=url_for("register")))
        with ev.store.lock:  # bookkeeping: no undo step
            t = ev.store.load()
            t.settings.partner_seekers = [x for x in t.settings.partner_seekers if x["member"] != me["member"]]
            if request.form.get("action") == "add":
                if t.registered_as(me["member"]) is not None:
                    flash("You're already on a team for this event.", "error")
                    return redirect(url_for("register"))
                t.settings.partner_seekers.append({"member": me["member"], "at": datetime.now().isoformat(timespec="seconds")})
                flash("You're on the looking-for-a-partner list (your name and skill level show; nothing else).", "ok")
            else:
                flash("You're off the looking-for-a-partner list.", "ok")
            ev.store.save(t)
        return redirect(url_for("register") + "#looking")

    @app.post("/t/<event>/partner-ask")
    def partner_ask():
        """Email a player who's looking: the asker's name, skill level and (as the reply-to) email."""
        me, ev = g.get("player"), g.event
        if not me:
            return redirect(url_for("player_login", next=url_for("register")))
        member = re.sub(r"\D", "", request.form.get("member", ""))
        t = ev.store.load()
        target = next((x for x in seekers(ev, t) if x["member"] == member), None)
        rec = store.get(member) if member else None
        if target is None or rec is None or not rec.get("email"):
            flash("That player can't be emailed from here. Ask the tournament director to put you in touch.", "error")
        elif asks_by_member.wait(me["member"]):
            flash("You've sent a lot of requests. Try again in an hour.", "error")
        elif not me.get("email"):
            flash("There's no email on file for you, so they couldn't reply. Ask the league office to add one.", "error")
        else:
            mailer = backup.Mailer.from_env()
            legal = app.jinja_env.globals["legal"]
            game = getattr(t.settings, "skill_game", "8")
            my_sl, _ = players.event_skill(me, game)
            how = (f"Add them on your team page: {request.url_root.rstrip('/')}{url_for('team_page', team_id=target['team'])}"
                   if target["team"] else
                   f"One of you registers and picks the other as partner: {request.url_root.rstrip('/')}{url_for('register')}")
            try:
                if mailer is None:
                    raise backup.BackupError("no mailer")
                mailer.send_to(rec["email"], f"{t.settings.name}: {full_name(me)} would like to be your partner",
                               f"Hi {rec['first']},\n\n{full_name(me)} (skill level {my_sl or '?'}) saw you're looking for a "
                               f"partner for {t.settings.name} and would like to team up. Reply to this email to reach "
                               f"them.\n\n{how}\n\n{legal['brand']}",
                               sender=legal["brand"], reply_to=me["email"])
                asks_by_member.fail(me["member"])
                flash(f"Sent! {target['name']} can reply straight to your email.", "ok")
            except backup.BackupError:
                flash("The email couldn't be sent. Try again in a minute.", "error")
        return redirect(url_for("register") + "#looking")

    # ------------------------------------------------ partner confirmation

    confirm_signer = URLSafeTimedSerializer(app.secret_key, salt="partner-confirm")

    def ask_partners(ev, gid: str) -> None:
        """Email each newly added partner a link to confirm (or decline) being on the team."""
        probe = (ev.store.load().teams if ev.kind == "chip" else ev.store.load().entries).get(gid)
        if probe is None or not any(p.confirm == "ask" for p in probe.players):
            return  # nobody to ask: don't add an undo step
        with ev.store.lock:  # a bookkeeping save (no undo step: Undo should still take back the registration)
            t = ev.store.load()
            grp = (t.teams if ev.kind == "chip" else t.entries).get(gid)
            if grp is not None:
                asker = next((p for p in grp.players if p.confirm != "ask"), None)
                for p in grp.players:
                    if p.confirm != "ask":
                        continue
                    token = confirm_signer.dumps({"e": ev.id, "g": gid, "m": p.member})
                    link = request.url_root.rstrip("/") + url_for("partner_confirm", event=ev.id, token=token)
                    who = asker.name if asker else "Someone"
                    when = ""
                    if t.settings.date:
                        d = datetime.strptime(t.settings.date, "%Y-%m-%d")
                        when = f" on {d:%a, %b} {d.day}"
                    sent = email_player(p.member, f"{t.settings.name}: are you on {grp.name}?",
                                        f"{who} registered you as their partner on {grp.name} for {t.settings.name}{when}."
                                        f"\n\nConfirm (or decline) here: {link}")
                    p.confirm = "waiting" if sent else ""
                ev.store.save(t)

    app.config["ask_partners"] = ask_partners

    history = history_mod.History(app.config["data_dir"])
    app.config["history"] = history

    def record_finished() -> None:
        """Copy every finished event's results into players' histories (once each)."""
        for ev, t in app.config["hub"].loaded():
            if not history.recorded(ev.id):
                if t.status == "finished" and not t.settings.test_event:
                    history.record(ev.id, ev.kind, t)

    app.config["record_finished"] = record_finished
    app.jinja_env.globals["offer_when"] = waitlist_mod.when_text

    @app.route("/t/<event>/partner/<token>", methods=["GET", "POST"])
    def partner_confirm(token):
        try:
            data = confirm_signer.loads(token, max_age=60 * 86400)
        except BadSignature:
            data = None
        ev = g.event
        if not data or data.get("e") != ev.id:
            flash("That link isn't valid any more. Contact the league office if you need help.", "error")
            return redirect(url_for("home"))
        t = ev.store.load()
        grp = (t.teams if ev.kind == "chip" else t.entries).get(data["g"])
        me = next((p for p in grp.players if p.member == data["m"]), None) if grp else None
        if me is None:
            flash("That registration has changed: you're not on it any more.", "error")
            return redirect(url_for("home"))
        if request.method == "POST":
            yes = request.form.get("action") == "confirm"
            with ev.store.change(f"Partner {'confirmed' if yes else 'declined'}: {me.name}") as t:
                grp = (t.teams if ev.kind == "chip" else t.entries)[data["g"]]
                p = next(x for x in grp.players if x.member == data["m"])
                p.confirm = "confirmed" if yes else "declined"
                if not yes:
                    p.pending = f"{p.name} declined being on {grp.name}"
                t._say(f"{p.name} {'confirmed' if yes else 'declined'} being on {grp.name}.")
                others = [x for x in grp.players if x.member and x.member != p.member]
            if not yes:
                for o in others:
                    email_player(o.member, f"{t.settings.name}: {p.name} declined",
                                 f"{p.name} declined being your partner on {grp.name} for {t.settings.name}. "
                                 "Update your roster with a new partner, or contact the league office.")
            flash(f"Thanks! You're confirmed on {grp.name}." if yes else
                  f"Got it. You're not on {grp.name}; the league office and your partner have been told.", "ok")
            return redirect(url_for("home"))
        return render_template("partner_confirm.html", t=t, grp=grp, me=me, token=token)

    @app.post("/t/<event>/quick-register")
    def quick_register():
        """One-tap registration for a logged-in player in a singles event, from their player-list details."""
        me, ev = g.get("player"), g.event
        if not me:
            return redirect(url_for("player_login", next=url_for("my_account")))
        if request.form.get("agree_terms") != "1":
            flash("Tick the box to agree to the Terms & Conditions and Privacy Policy.", "error")
            return redirect(url_for("my_account") + "#upcoming")
        t0 = ev.store.load()
        if ev.kind != "bracket" or t0.settings.team_size != 1:
            return redirect(url_for("register"))  # doubles: the register page, filled in, with the partner search
        fill = my_prefill(t0)
        form = {"player_member_0": me["member"]}
        two = asks_two_levels(t0.settings)
        player = Player(name=fill["name"], phone=fill["phone"], skill=0 if two else int(fill["skill"] or 0),
                        skill_8=int(fill["skill8"] or 0) if two else 0, skill_9=int(fill["skill9"] or 0) if two else 0)
        if request.form.get("sms_consent") == "1" and player.phone:
            player.sms_consent, player.sms_consent_at = True, datetime.now().isoformat(timespec="seconds")
        try:
            with ev.store.change(f"Registration (one tap): {fill['name']}") as t:
                if t.settings.registration_shut:
                    raise TournamentError(schedule.shut_message(t.settings))
                if two and not (player.skill_8 or player.skill_9) or not two and not player.skill:
                    raise TournamentError("Your skill level isn't in the player list yet: use the register page.")
                plist = attach_members(form, [player], t)
                entry = t.register_entry("", plist)
                messages, t.outbox = list(t.outbox), []
                for m in messages:
                    ev.sms.send(m.to, m.body, test=t.settings.test_event)
        except TournamentError as exc:
            flash(str(exc), "error")
            return redirect(url_for("my_account") + "#upcoming")
        session[f"entry_{entry.id}"] = True
        group = f" You're in the {entry.division} group." if entry.division else ""
        flash(f"You're registered for {t.settings.name}!{group} Your PIN is {entry.pin}.", "ok")
        return redirect(url_for("b_entry", entry_id=entry.id))

    @app.route("/eligibility", methods=["GET", "POST"])
    def eligibility_check():
        results, member, known = None, "", True
        if request.method == "POST":
            member = re.sub(r"\D", "", request.form.get("member", ""))
            ip, device = request.remote_addr or "?", app.config["device_id"]()
            if lookups_by_ip.wait(ip) or lookups_by_device.wait(device):
                flash("That's a lot of lookups. Try again in an hour.", "error")
            elif not players.MEMBER_RE.fullmatch(member):
                flash("Member numbers are 8 digits and start with 322.", "error")
            elif not store.records():
                flash("Eligibility checks aren't available yet.", "error")
            else:
                lookups_by_ip.fail(ip)
                lookups_by_device.fail(device)
                known = store.get(member) is not None
                results = public_results(member)
        elif g.get("player"):  # logged in: their own results straight away
            member = g.player["member"]
            results = public_results(member)
        return render_template("eligibility_check.html", t=None, results=results, member=member, known=known,
                               mine=bool(g.get("player")) and g.player["member"] == member)

    # ------------------------------------------------ Phase 7: reviews, overrides, the lock

    def admin_only():
        if not session.get("admin"):
            flash("Only the director can do that.", "error")
            return redirect(url_for("admin"))
        return None

    def email_player(member: str, subject: str, text: str) -> bool:
        rec, mailer = store.get(member), backup.Mailer.from_env()
        if not rec or not rec.get("email") or mailer is None:
            return False
        legal = app.jinja_env.globals["legal"]
        try:
            mailer.send_to(rec["email"], subject, f"Hi {rec['first']},\n\n{text}\n\n{legal['brand']}",
                           sender=legal["brand"], reply_to=legal["email"])
            return True
        except backup.BackupError:
            return False

    @app.post("/t/<event>/review-request")
    def review_request():
        me = g.get("player")
        if not me:
            flash("Log in first, so the league office knows who's asking.", "error")
            return redirect(url_for("player_login", next=url_for("register")))
        try:
            with g.event.store.change(f"Review requested: {full_name(me)}") as t:
                fit = my_eligibility(t)
                if not fit or fit["ok"]:
                    raise TournamentError("You're eligible already: go ahead and register.")
                eligibility.request_review(t, me["member"], full_name(me), fit["no"], request.form.get("message", ""))
            flash("Sent to the league office. You'll get an email when they decide.", "ok")
        except TournamentError as exc:
            flash(str(exc), "error")
        return redirect(url_for("register"))

    @app.post("/t/<event>/admin/review/<rid>")
    def admin_review(rid):
        if (stop := admin_only()) is not None:
            return stop
        approve, note = request.form.get("action") == "approve", request.form.get("note", "")
        try:
            with g.event.store.change(f"Review {'approved' if approve else 'declined'}") as t:
                req = eligibility.decide_review(t, rid, approve, note)
                name = t.settings.name
            link = request.url_root.rstrip("/") + url_for("register")
            body = (f"Good news: the league office approved you for {name}. Register here: {link}"
                    + (f"\n\nNote: {req['note']}" if req["note"] else "") if approve else
                    f"The league office reviewed your request for {name} and couldn't approve it.\n\n{req['note']}")
            sent = email_player(req["member"], f"{name}: your eligibility review", body)
            flash(f"{'Approved' if approve else 'Declined'} {req['name']}."
                  + (" They've been emailed." if sent else " (No email on file, so let them know yourself.)"), "ok")
        except TournamentError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin") + "#eligibility")

    @app.post("/t/<event>/admin/overrides")
    def admin_overrides():
        if (stop := admin_only()) is not None:
            return stop
        member = re.sub(r"\D", "", request.form.get("member", ""))
        try:
            if not players.MEMBER_RE.fullmatch(member):
                raise TournamentError("Member numbers are 8 digits and start with 322.")
            rec = store.get(member)
            name = full_name(rec) if rec else member
            with g.event.store.change(f"Eligibility override: {name}") as t:
                if request.form.get("action") == "remove":
                    eligibility.remove_override(t, member)
                    msg = f"Removed the override for {name}."
                else:
                    eligibility.add_override(t, member, name, request.form.get("note", ""))
                    msg = f"{name} is eligible for this event (override)."
            flash(msg, "ok")
        except TournamentError as exc:
            flash(str(exc), "error")
        back = request.form.get("back", "")
        return redirect(back if back in (url_for("admin_skill_check"),) else url_for("admin") + "#eligibility")

    @app.post("/t/<event>/admin/eligibility-lock")
    def admin_eligibility_lock():
        if (stop := admin_only()) is not None:
            return stop
        with g.event.store.change("Eligibility " + ("unlocked" if request.form.get("action") == "unlock" else "locked")) as t:
            if request.form.get("action") == "unlock":
                eligibility.unlock(t)
                msg = "Eligibility unlocked: the check uses the latest player list again."
            else:
                flagged = eligibility.lock(t, store, store.config()["session_start"],
                                           lambda m, t=t, eid=g.event.id: bool(pushed_in(eid, t, m)))
                msg = f"Eligibility checked and locked. {flagged} player(s) sent to Pending verification."
        flash(msg, "ok")
        return redirect(url_for("admin") + "#eligibility")

    def can_push(event_id: str, t, member: str) -> tuple[bool, str]:
        """Whether a player may push their eligibility from this event to the one the director picked, and why not."""
        s = t.settings
        rules = eligibility.rules_of(s)
        rec = store.get(member)
        if pushed_in(event_id, t, member) or eligibility.list_value(rec, rules) == "push":
            return False, "This is your one-time grace entry, so it can't be pushed again."
        if push_target(event_id, t) is None or not (rules["list_column"] or rules["list_members"]):
            return False, "Pushing to another event isn't turned on for this one."
        if t.status != "setup":
            return False, "The event has started, so it's too late to push."
        if eligibility.list_value(rec, rules) != "yes":
            return False, f"You're not on the {rules['list_label'] or 'eligible'} list for this event, so there's nothing to push."
        if any(p["member"] == member for p in pushes.from_event(event_id)):
            return False, "You've already pushed this one."
        return True, ""

    @app.post("/t/<event>/push")
    def push_eligibility():
        """Push eligibility to the next edition: the player (logged in) or the director for a member number."""
        ev, admin = g.event, bool(session.get("admin"))
        member = re.sub(r"\D", "", request.form.get("member", "")) if admin else (g.player or {}).get("member", "")
        back = url_for("admin") + "#eligibility" if admin else url_for("my_account")
        came_from = request.form.get("back", "")
        if not admin and came_from.startswith("/") and not came_from.startswith("//"):  # the event page's button
            back = came_from
        if not member:
            flash("Log in first.", "error")
            return redirect(url_for("player_login", next=url_for("register")))
        if request.form.get("confirm") != "1":
            flash("Tick the box to confirm.", "error")
            return redirect(back)
        rec = store.get(member)
        name = full_name(rec) if rec else member
        try:
            with ev.store.change(f"Eligibility pushed to next time: {name}") as t:
                ok, why = can_push(ev.id, t, member)
                if not ok:
                    raise TournamentError(why)
                to_event, to_name = push_target(ev.id, t)
                found = t.registered_as(member)
                if found is not None:  # their spot opens for the waiting list
                    body = (f"{found.name}: your eligibility for {t.settings.name} is pushed to {to_name}, "
                            "so you're unregistered from this one. See you there!")
                    if hasattr(t, "teams"):
                        t._text_team(found, body)
                        t.remove_team(found.id)
                    else:
                        t._text(found, body)
                        t.remove_entry(found.id)
                eligibility.audit(t, "pushed to the next edition", member, name, "by the director" if admin else "by the player")
                pushes.add(t.settings.series, member, name, ev.id, t.settings.name, t.settings.date,
                           "director" if admin else "player", to_event, to_name)
                messages, t.outbox = list(t.outbox), []
                for m in messages:
                    ev.sms.send(m.to, m.body, test=t.settings.test_event)
            flash(f"{name}'s eligibility is pushed to {to_name}"
                  + (" and they're unregistered from this one." if found is not None else ".")
                  + " This is a one-time grace: it can't be pushed again.", "ok")
        except TournamentError as exc:
            flash(str(exc), "error")
        return redirect(back)

    app.jinja_env.globals["can_push"] = lambda t, member: can_push(g.event.id, t, member)

    def pushes_into(event_id: str) -> list[dict]:
        """Players on their one-time grace in this edition (pushed from the previous one in the app)."""
        hub = app.config["hub"]
        return [p for p in pushes.all() if series_mod.lands_in(hub, p) == event_id]

    app.jinja_env.globals["pushes_from"] = lambda: pushes.from_event(g.event.id)
    app.jinja_env.globals["push_target"] = lambda t: push_target(g.event.id, t)

    def push_choices(t) -> tuple[str, list]:
        """For the settings: (what's picked now, [(event id, name, date)]) of the events players can be pushed to:
        not this one, not finished, soonest first. An older event without a pick shows its series' next event."""
        here, hub = g.event.id, app.config["hub"]
        rows = sorted((x.settings.date or "9999-99-99", ev.id, x.settings.name, x.settings.date)
                      for ev, x in hub.loaded() if ev.id != here and x.status != "finished")
        picked = t.settings.push_to or (push_target(here, t) or (series_mod.NO_PUSHING,))[0]
        return picked, [(i, n, d) for _, i, n, d in rows]

    app.jinja_env.globals["push_choices"] = push_choices
    app.jinja_env.globals["pushes_into"] = lambda: pushes_into(g.event.id)

    @app.post("/t/<event>/team/<team_id>/offer")
    @app.post("/t/<event>/entry/<entry_id>/offer")
    def waitlist_offer(team_id=None, entry_id=None):
        """A waiting team accepts or declines the open spot it was offered."""
        gid, ev = team_id or entry_id, g.event
        page = url_for("team_page", team_id=gid) if team_id else url_for("b_entry", entry_id=gid)
        if not (session.get(f"team_{gid}") or session.get(f"entry_{gid}") or session.get("admin")):
            flash("Enter your PIN first.", "error")
            return redirect(page)
        yes = request.form.get("action") == "accept"
        try:
            with ev.store.change(f"Waiting list offer {'accepted' if yes else 'declined'}") as t:
                name = waitlist_mod.accept(t, gid) if yes else waitlist_mod.decline(t, gid)
                messages, t.outbox = list(t.outbox), []
                for m in messages:
                    ev.sms.send(m.to, m.body, test=t.settings.test_event)
            flash(f"{name} is in the field. See you there!" if yes else
                  f"{name} is off the waiting list. The spot goes to the next team.", "ok")
        except TournamentError as exc:
            flash(str(exc), "error")
        return redirect(page if yes else url_for("home"))

    def email_offers(ev, t) -> bool:
        """Email (as well as text) players who have an offer they haven't been emailed about."""
        changed = False
        for grp in waitlist_mod.offered(t):
            if grp.offer_emailed == grp.offer_until:
                continue
            for p in grp.players:
                if p.member:
                    email_player(p.member, f"{t.settings.name}: a spot opened for {grp.name}",
                                 f"A spot opened in {t.settings.name}! {grp.name} has until "
                                 f"{waitlist_mod.when_text(grp.offer_until)} to take it. Accept or decline on your "
                                 f"team page: {(t.site_url or '') + ('/team/' if ev.kind == 'chip' else '/entry/') + grp.id}")
            grp.offer_emailed, changed = grp.offer_until, True
        return changed

    def send_reminders(ev) -> dict | None:
        """Email (and text, if the event says so) the field the day before and 2 hours before. Returns the counts."""
        if ev.load().status != "setup" or not reminders.due(ev.load().settings):
            return None
        with ev.store.lock:  # bookkeeping: no undo step. Marked before sending, so a restart never sends it twice.
            t = ev.load()
            which = reminders.due(t.settings)
            if t.status != "setup" or not which:
                return None
            t.settings.reminders_sent[which] = {"at": datetime.now().isoformat(timespec="seconds"), "emails": 0, "texts": 0}
            ev.store.save(t)
        s, emailed, texted = t.settings, set(), set()
        body = reminders.text_body(t, which)
        if s.sms_name:
            body = f"{s.sms_name}: {body}"
        for grp, p in reminders.recipients(t):
            if p.member and p.member not in emailed:
                subject, text = reminders.email_text(t, ev.id, ev.kind, grp, which)
                if email_player(p.member, subject, text):
                    emailed.add(p.member)
            if s.reminder_texts and p.phone and p.sms_consent and p.phone not in texted:
                ev.sms.send(p.phone, body, test=s.test_event)
                texted.add(p.phone)
        counts = {"at": t.settings.reminders_sent[which]["at"], "emails": len(emailed), "texts": len(texted)}
        with ev.store.lock:
            t = ev.load()
            t.settings.reminders_sent[which] = counts
            ev.store.save(t)
        return counts

    def lock_loop(log=None, sleep: float = 60) -> None:
        """Background: lock an event's eligibility once its registration closes or it starts."""
        import time as _time

        while True:
            for ev in app.config["hub"].events():
                try:
                    probe = ev.load()
                    now_ = datetime.now()
                    if any(datetime.fromisoformat(x.offer_until) <= now_ for x in waitlist_mod.offered(probe)):
                        with ev.store.change("Waiting list offer ran out") as t:
                            waitlist_mod.expire(t)
                            messages, t.outbox = list(t.outbox), []
                            for m in messages:
                                ev.sms.send(m.to, m.body, test=t.settings.test_event)
                    with ev.store.lock:  # bookkeeping: no undo step
                        t = ev.load()
                        if email_offers(ev, t):
                            ev.store.save(t)
                    send_reminders(ev)
                    if probe.status == "finished" and not history.recorded(ev.id):
                        history.record(ev.id, ev.kind, probe)
                    if eligibility.lock_due(ev.load()):
                        with ev.store.change("Eligibility locked (registration closed)") as t:
                            if eligibility.lock_due(t):
                                eligibility.lock(t, store, store.config()["session_start"],
                                                 lambda m, t=t, eid=ev.id: bool(pushed_in(eid, t, m)))
                except Exception as exc:  # never stop the loop
                    if log:
                        log("eligibility lock failed for %s: %s", ev.id, exc)
            _time.sleep(sleep)

    app.config["eligibility_lock_loop"] = lock_loop
    app.config["send_reminders"] = send_reminders

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
                    # The player list's name for that number, unless the director keeps what was typed.
                    p = next((p for grp, j, p in pending_mod.pending(t) if grp.id == gid and j == i), None)
                    rec = store.get(p.member) if p is not None and request.form.get("keep") != "1" else None
                    who = pending_mod.verify(t, gid, i, name=full_name(rec) if rec else None)
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

    def find_me(t, me):
        """The team or entry this player is on in a tournament, and their place on it (or None, None)."""
        want = full_name(me).lower()
        for grp in (t.teams if hasattr(t, "teams") else t.entries).values():
            if grp.status == "no_show":
                continue
            for p in grp.players:
                if p.member == me["member"] or (not p.member and " ".join(p.name.lower().split()) == want):
                    return grp, p
        return None, None

    def your_events(me) -> list[dict]:
        """Every tournament the player is registered in: live and upcoming first (soonest first), then past."""
        out = []
        for ev, t in app.config["hub"].loaded():
            grp, mine = find_me(t, me)
            if grp is None:
                continue
            s, chip = t.settings, ev.kind == "chip"
            skill = {"mine": mine.skill or max(mine.skill_8, mine.skill_9), "sl8": mine.skill_8, "sl9": mine.skill_9}
            if chip:
                skill["team"] = grp.combined_skill if len(grp.players) > 1 else None
                skill["chips"] = t.chips_preview(grp) if t.status == "setup" else grp.starting_chips or None
            else:
                skill["group"] = grp.division
            where = t.where(grp)
            if any(p.pending for p in grp.players):
                where += " · member number pending verification"
            partners = [p.name for p in grp.players if p is not mine]
            pushable = can_push(ev.id, t, me["member"])[0]
            out.append({"can_push": pushable, "push_url": url_for("push_eligibility", event=ev.id),
                        "push_name": (push_target(ev.id, t) or ("", ""))[1],
                        "id": ev.id, "name": s.name, "date": s.date, "time": s.start_time, "venue": s.venue,
                        "status": t.status, "test": s.test_event, "group_name": grp.name, "partners": partners,
                        "where": where, "skill": skill, "chip": chip,
                        "page": url_for("team_page", event=ev.id, team_id=grp.id) if chip
                        else url_for("b_entry", event=ev.id, entry_id=grp.id),
                        "rules": url_for("rules_page", event=ev.id), "home": url_for("home", event=ev.id)})
        live = sorted((e for e in out if e["status"] != "finished"),
                      key=lambda e: (e["status"] != "running", e["date"] or "9999"))
        past = sorted((e for e in out if e["status"] == "finished"), key=lambda e: e["date"] or "", reverse=True)
        return live + past

    app.config["your_events"] = your_events
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
