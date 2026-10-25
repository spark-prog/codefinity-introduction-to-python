"""League hub pages, under /league: what goes on between tournaments.

The league office signs in with the director password (/league/office). Host locations
and division reps sign in with a 6-digit PIN the league office gives them; players and
team captains with the phone or email on their listing and the 4-digit PIN they got
when they made it (/league/signin). The rules are in chips/league.py.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta
from functools import wraps

from flask import abort, flash, redirect, render_template, request, session, url_for

from chips.bracket import FORMATS as BRACKET_FORMATS
from chips.engine import TournamentError
from chips.hub import BRACKET, CHIP
from chips.league import (
    ALL, CLAIMED, DAY_NAMES, DAYS, FORMATS, GENERAL, LEAGUE_NIGHT, LEVELS, LOCATION_FIELDS, OPEN, REQUEST_TYPES, SCHEDULED,
    TYPES, League, LeagueError, day_label, label_rows, lines, month_grid,
)
from chips.throttle import Throttle

ROLE_KEYS = {"host": "league_host", "rep": "league_rep", "player": "league_player", "captain": "league_captain"}


def register(app, h) -> League:
    hub, data_path, minutes = h["hub"], h["data_path"], h["minutes"]
    director_view, admin_required = h["director_view"], h["admin_required"]
    league = League(data_path, today=h.get("today"))
    app.config["league"] = league
    signin_by_ip = Throttle(limit=10, window=15 * 60)

    app.jinja_env.filters["day_label"] = day_label
    app.jinja_env.filters["label_rows"] = label_rows
    app.jinja_env.filters["lines"] = lines

    @app.context_processor
    def league_globals():
        return {"L_TYPES": TYPES, "L_DAYS": DAYS, "L_DAY_NAMES": DAY_NAMES, "L_FORMATS": FORMATS, "L_LEVELS": LEVELS, "L_ALL": ALL,
                "L_REQUEST_TYPES": REQUEST_TYPES, "L_GENERAL": GENERAL, "L_LEAGUE_NIGHT": LEAGUE_NIGHT,
                "location_name": League.location_name, "league_roles": {k: bool(session.get(v)) for k, v in ROLE_KEYS.items()}}

    def change(fn, success: str | None = None):
        """Run a league change; show rule errors instead of crashing."""
        try:
            result = fn()
        except LeagueError as exc:
            flash(str(exc), "error")
            return None
        if success:
            flash(success.format(result=result), "ok")
        return True if result is None else result

    def back(endpoint: str, anchor: str = "", **values):
        return redirect(url_for(endpoint, **values) + (f"#{anchor}" if anchor else ""))

    def signed_in(role: str, kind: str):
        """The signed-in host location / rep / player / team, or None (and sign out a removed one)."""
        saved = session.get(ROLE_KEYS[role])
        if not saved:
            return None, None
        s = league.load()
        row_id, pin = saved
        row = next((r for r in s[kind] if r["id"] == row_id), None)
        if row is None or row["pin"] != pin:  # removed, or the league office gave it a new PIN
            session.pop(ROLE_KEYS[role], None)
            row = None
        return s, row

    def role_required(role: str, kind: str):
        def deco(view):
            @wraps(view)
            def wrapper(*args, **kwargs):
                s, row = signed_in(role, kind)
                if row is None:
                    flash("Sign in first.", "error")
                    return redirect(url_for("league_signin", role=role))
                return view(s, row, *args, **kwargs)
            return wrapper
        return deco

    def today() -> str:
        return league.today()

    def plus(days: int) -> str:
        return (date.fromisoformat(today()) + timedelta(days=days)).isoformat()

    # ------------------------------------------------------------- public

    @app.get("/league/")
    def league_home():
        s = league.load()
        upcoming = [e for e in league.events_between(s, today(), plus(60)) if e["type"] != LEAGUE_NIGHT][:12]
        cards = []
        for loc in s["locations"]:
            nxt = next((e for e in league.events_between(s, today(), plus(60), loc["id"])
                        if e["loc"] != ALL and e["type"] != LEAGUE_NIGHT), None)
            cards.append({"loc": loc, "next": nxt, "open": len(league.open_teams(s, loc["id"]))})
        return render_template("league_home.html", t=None, s=s, upcoming=upcoming, cards=cards,
                               teams=league.open_teams(s))

    @app.get("/league/loc/<loc_id>")
    def league_location(loc_id):
        s = league.load()
        loc = league.location(s, loc_id) or abort(404)
        evs = [e for e in league.events_between(s, today(), plus(60), loc_id) if e["type"] != LEAGUE_NIGHT]
        return render_template("league_location.html", t=None, s=s, loc=loc, events=evs,
                               teams=league.open_teams(s, loc_id), form=request.form)

    @app.post("/league/loc/<loc_id>/signup")
    def league_signup(loc_id):
        f = request.form
        data = {**f.to_dict(), "formats": f.getlist("formats"), "nights": f.getlist("nights")}
        if change(lambda: league.sign_up(loc_id, data),
                  "Sent. The league office will reach out to connect you with a team."):
            return back("league_location", loc_id=loc_id)
        return league_location(loc_id)

    def listing_form(f) -> dict:
        return {**f.to_dict(), "formats": f.getlist("formats"), "nights": f.getlist("nights"), "locs": f.getlist("locs"),
                "visible": f.get("visible") == "1"}

    @app.route("/league/players/new", methods=["GET", "POST"])
    def league_player_new():
        s = league.load()
        if request.method == "POST":
            p = change(lambda: league.save_player(None, listing_form(request.form)))
            if p:
                session[ROLE_KEYS["player"]] = [p["id"], p["pin"]]
                flash(f"Your listing is saved. Your PIN is {p['pin']}: use it with your phone or email to sign in "
                      "and change your listing or read team invites.", "ok")
                return redirect(url_for("league_player"))
        player = {"first": "", "last": "", "area": "", "level": LEVELS[1], "formats": [], "nights": [], "locs": [],
                  "about": "", "visible": True, "contact": ""}
        if request.method == "POST":
            player.update(listing_form(request.form))
        return render_template("league_player_new.html", t=None, s=s, player=player)

    @app.route("/league/teams/new", methods=["GET", "POST"])
    def league_team_new():
        s = league.load()
        if request.method == "POST":
            team = change(lambda: league.save_team(None, request.form.to_dict()))
            if team:
                session[ROLE_KEYS["captain"]] = [team["id"], team["pin"]]
                flash(f"{team['name']} is posted. Your PIN is {team['pin']}: use it with the captain phone or email "
                      "to sign in and invite players.", "ok")
                return redirect(url_for("league_captain"))
        return render_template("league_team_new.html", t=None, s=s, team=request.form.to_dict())

    @app.route("/league/signin", methods=["GET", "POST"])
    def league_signin():
        s = league.load()
        role = request.values.get("role", "")
        if request.method == "POST" and role in ROLE_KEYS:
            ip, f = request.remote_addr or "?", request.form
            wait = signin_by_ip.wait(ip)
            if wait:
                flash(f"Too many wrong PINs. Try again in {minutes(wait)}, or ask the league office.", "error")
                return render_template("league_signin.html", t=None, s=s, role=role)
            pin = f.get("pin", "").strip()
            if role in ("host", "rep"):
                kind = "locations" if role == "host" else "reps"
                row = next((r for r in s[kind] if r["id"] == f.get("who")), None)
                row = row if row and secrets.compare_digest(pin, row["pin"]) else None
            else:
                row = league.sign_in("players" if role == "player" else "teams", f.get("contact", ""), pin)
            if row:
                signin_by_ip.clear(ip)
                session[ROLE_KEYS[role]] = [row["id"], row["pin"]]
                return redirect(url_for({"host": "league_host", "rep": "league_rep", "player": "league_player",
                                         "captain": "league_captain"}[role]))
            signin_by_ip.fail(ip)
            flash("That doesn't match. Check the PIN (and the phone or email), or ask the league office.", "error")
        return render_template("league_signin.html", t=None, s=s, role=role)

    @app.post("/league/signout")
    def league_signout():
        for key in ROLE_KEYS.values():
            session.pop(key, None)
        return redirect(url_for("league_home"))

    # ------------------------------------------------------------- host location

    @app.get("/league/host")
    @role_required("host", "locations")
    def league_host(s, loc):
        window = 60 if request.args.get("days") == "60" else 30
        end = plus(window)
        days: dict[str, list] = {}
        for e in league.events_between(s, today(), end, loc["id"]):
            days.setdefault(e["date"], []).append(("event", e, e["loc"] != ALL and league.blackout_on(s, loc["id"], e["date"])))
        for b in s["blackouts"]:
            if b["loc"] != loc["id"]:
                continue
            d = max(date.fromisoformat(b["start"]), date.fromisoformat(today()))
            while d.isoformat() <= min(b["end"], end):
                days.setdefault(d.isoformat(), []).append(("blackout", b, None))
                d += timedelta(days=1)
        for r in s["requests"]:
            if r["loc"] == loc["id"] and r["status"] != SCHEDULED and today() <= r["date"] <= end:
                days.setdefault(r["date"], []).append(("request", r, None))
        evs = [x for items in days.values() for k, x, _ in items if k == "event"]
        stats = {"league": sum(e["type"] == LEAGUE_NIGHT for e in evs),
                 "special": sum(e["type"] not in (LEAGUE_NIGHT, GENERAL) for e in evs),
                 "general": sum(e["loc"] == ALL for e in evs),
                 "blackout": sum(any(k == "blackout" for k, _, _ in items) for items in days.values())}
        blackouts = sorted((b for b in s["blackouts"] if b["loc"] == loc["id"] and b["end"] >= today()),
                           key=lambda b: b["start"])
        requests_ = sorted((r for r in s["requests"] if r["loc"] == loc["id"]), key=lambda r: r["date"])
        reps = {r["id"]: r for r in s["reps"]}
        agenda, last_week = [], ""
        for d, items in sorted(days.items()):
            dt = date.fromisoformat(d)
            week = (dt - timedelta(days=(dt.weekday() + 1) % 7)).isoformat()
            agenda.append({"date": dt, "entries": items, "week": week if week != last_week else "",
                           "blackout": any(k == "blackout" for k, _, _ in items)})
            last_week = week
        return render_template("league_host.html", t=None, s=s, loc=loc, window=window, agenda=agenda,
                               stats=stats, blackouts=blackouts, requests=requests_, reps=reps, today=today())

    @app.post("/league/host/blackout")
    @role_required("host", "locations")
    def league_host_blackout(s, loc):
        f = request.form
        hits = change(lambda: league.add_blackout(loc["id"], f.get("start", ""), f.get("end", ""), f.get("reason", "")))
        if hits is not None:
            flash("Blackout added." + (f" {hits} scheduled event{'s' if hits != 1 else ''} on those days "
                                       "are flagged for the league office." if hits is not True and hits else ""), "ok")
        return back("league_host", "blackouts")

    @app.post("/league/host/blackout/<blackout_id>/delete")
    @role_required("host", "locations")
    def league_host_blackout_delete(s, loc, blackout_id):
        change(lambda: league.delete_blackout(loc["id"], blackout_id), "Blackout removed.")
        return back("league_host", "blackouts")

    @app.post("/league/host/request")
    @role_required("host", "locations")
    def league_host_request(s, loc):
        f = request.form
        change(lambda: league.add_request(loc["id"], f.get("type", ""), f.get("date", ""), f.get("alt", ""),
                                          f.get("notes", "")),
               "Posted to the request board. A division rep can claim it.")
        return back("league_host", "request")

    @app.post("/league/host/page")
    @role_required("host", "locations")
    def league_host_page(s, loc):
        change(lambda: league.save_location(loc["id"], {k: request.form.get(k, loc[k]) for k in LOCATION_FIELDS}),
               "Location page updated.")
        return back("league_host", "page")

    # ------------------------------------------------------------- division rep

    def board(s):
        reps = {r["id"]: r for r in s["reps"]}
        groups = []
        for status, label in ((OPEN, "Open: needs a rep"), (CLAIMED, "Claimed: waiting on the league office"),
                              (SCHEDULED, "On the calendar")):
            rows = sorted((r for r in s["requests"] if r["status"] == status), key=lambda r: r["date"])
            groups.append((status, label, [{"r": r, "loc": league.location(s, r["loc"]), "rep": reps.get(r["rep"]),
                                            "blackout": league.blackout_on(s, r["loc"], r["date"])} for r in rows]))
        return groups

    @app.get("/league/rep")
    @role_required("rep", "reps")
    def league_rep(s, rep):
        return render_template("league_board.html", t=None, s=s, rep=rep, groups=board(s), office=False)

    @app.post("/league/rep/claim/<req_id>")
    @role_required("rep", "reps")
    def league_rep_claim(s, rep, req_id):
        change(lambda: league.claim(req_id, rep["id"]), "Claimed. The league office will confirm the date.")
        return back("league_rep")

    @app.post("/league/rep/release/<req_id>")
    @role_required("rep", "reps")
    def league_rep_release(s, rep, req_id):
        change(lambda: league.release(req_id, rep["id"]), "Released back to the board.")
        return back("league_rep")

    # ------------------------------------------------------------- player

    @app.route("/league/me", methods=["GET", "POST"])
    @role_required("player", "players")
    def league_player(s, player):
        if request.method == "POST":
            if change(lambda: league.save_player(player["id"], listing_form(request.form))):
                flash("Listing saved and shown to team captains." if request.form.get("visible") == "1"
                      else "Listing saved and hidden from team captains.", "ok")
            return back("league_player")
        teams = {t["id"]: t for t in s["teams"]}
        invites = sorted((i for i in s["invites"] if i["player"] == player["id"] and i["team"] in teams),
                         key=lambda i: i["created"], reverse=True)
        return render_template("league_player.html", t=None, s=s, player=player, invites=invites, teams=teams,
                               open_teams=league.open_teams(s))

    @app.post("/league/me/invite/<invite_id>")
    @role_required("player", "players")
    def league_player_invite(s, player, invite_id):
        change(lambda: league.answer_invite(player["id"], invite_id, request.form.get("status", "")))
        return back("league_player", "inbox")

    @app.post("/league/me/delete")
    @role_required("player", "players")
    def league_player_delete(s, player):
        if change(lambda: league.delete_row("players", player["id"]), "Your listing is deleted."):
            session.pop(ROLE_KEYS["player"], None)
        return back("league_home")

    # ------------------------------------------------------------- team captain

    @app.route("/league/captain", methods=["GET", "POST"])
    @role_required("captain", "teams")
    def league_captain(s, team):
        if request.method == "POST":
            change(lambda: league.save_team(team["id"], request.form.to_dict()), "Team saved.")
            return back("league_captain", "team")
        night, fmt = request.args.get("night", "any"), request.args.get("format", "any")
        players = [p for p in s["players"] if p["visible"]
                   and (night == "any" or night in p["nights"]) and (fmt == "any" or fmt in p["formats"])]
        players.sort(key=lambda p: -league.fit(p, team))
        sent = {i["player"]: i for i in s["invites"] if i["team"] == team["id"]}
        names = {p["id"]: p for p in s["players"]}
        return render_template("league_captain.html", t=None, s=s, team=team, players=players, sent=sent, names=names,
                               fit=league.fit, night=night, fmt=fmt)

    @app.post("/league/captain/invite/<player_id>")
    @role_required("captain", "teams")
    def league_captain_invite(s, team, player_id):
        change(lambda: league.invite(team["id"], player_id, request.form.get("message", "")),
               "Team info sent. The player decides whether to reach out.")
        return back("league_captain", "players")

    @app.post("/league/captain/delete")
    @role_required("captain", "teams")
    def league_captain_delete(s, team):
        if change(lambda: league.delete_row("teams", team["id"]), "Your team is taken off the list."):
            session.pop(ROLE_KEYS["captain"], None)
        return back("league_home")

    # ------------------------------------------------------------- league office

    def office_counts(s) -> dict:
        return {"requests": sum(r["status"] in (OPEN, CLAIMED) for r in s["requests"]),
                "signups": sum(x["status"] == "new" for x in s["signups"])}

    @app.get("/league/office")
    @director_view
    def league_office():
        s = league.load()
        month = request.args.get("month") or today()[:7]
        try:
            weeks = month_grid(month)
        except ValueError:
            month, weeks = today()[:7], month_grid(today()[:7])
        loc_filter = request.args.get("loc", "")
        loc_filter = loc_filter if league.location(s, loc_filter) else ""
        evs = league.events_between(s, weeks[0][0], weeks[-1][-1], loc_filter or None)
        by_day: dict[str, list] = {}
        for e in evs:
            by_day.setdefault(e["date"], []).append(e)
        closed: dict[str, list] = {}
        for b in s["blackouts"]:
            if loc_filter and b["loc"] != loc_filter:
                continue
            d = date.fromisoformat(max(b["start"], weeks[0][0]))
            while d.isoformat() <= min(b["end"], weeks[-1][-1]):
                closed.setdefault(d.isoformat(), []).append(b)
                d += timedelta(days=1)
        day = request.args.get("day", "")
        day = day if day in {d for w in weeks for d in w} else ""
        next30 = league.events_between(s, today(), plus(30))
        first = datetime.strptime(month, "%Y-%m").date()
        prev_m = (first - timedelta(days=1)).strftime("%Y-%m")
        next_m = (first + timedelta(days=32)).strftime("%Y-%m")
        prefill = {}
        schedule = request.args.get("schedule", "")
        req = next((r for r in s["requests"] if r["id"] == schedule and r["status"] == CLAIMED), None)
        if req:
            loc = league.location(s, req["loc"])
            rep = next((r for r in s["reps"] if r["id"] == req["rep"]), None)
            prefill = {"type": req["type"], "loc": req["loc"], "date": req["date"], "time": "12:00 PM",
                       "title": f"{loc['name']} {TYPES[req['type']][0]}" if loc else "", "request": req["id"],
                       "director": rep["name"] if rep else "", "notes": req["notes"]}
            day = day or req["date"]
        return render_template(
            "league_office.html", t=None, s=s, weeks=weeks, month=month, month_label=f"{first:%B %Y}",
            prev_m=prev_m, next_m=next_m, by_day=by_day, closed=closed, day=day, loc_filter=loc_filter,
            today=today(), conflicts=league.conflicts(s), counts=office_counts(s), prefill=prefill,
            stats={"league": sum(e["type"] == LEAGUE_NIGHT for e in next30),
                   "special": sum(e["type"] not in (LEAGUE_NIGHT, GENERAL) for e in next30)},
            formats=[*BRACKET_FORMATS, (CHIP, "Chip tournament")], tournaments={ev.id for ev in hub.events()},
        )

    @app.post("/league/office/event")
    @admin_required
    def league_office_event():
        ev = change(lambda: league.add_event(request.form.to_dict()))
        if ev:
            flash("Added to every host's calendar." if ev["loc"] == ALL
                  else f"Added to {League.location_name(league.load(), ev['loc'])}'s calendar.", "ok")
            return redirect(url_for("league_office", month=ev["date"][:7], day=ev["date"]) + "#day")
        return redirect(url_for("league_office", month=request.form.get("date", "")[:7] or None,
                                day=request.form.get("date") or None, schedule=request.form.get("request") or None)
                        + "#day")

    @app.post("/league/office/event/<event_id>/delete")
    @admin_required
    def league_office_event_delete(event_id):
        change(lambda: league.delete_event(event_id), "Event deleted.")
        return redirect(url_for("league_office", month=request.form.get("month") or None,
                                day=request.form.get("day") or None) + "#day")

    @app.post("/league/office/event/<event_id>/tournament")
    @admin_required
    def league_office_tournament(event_id):
        """Set up a tournament in Control Table for a calendar event (its date, time and venue carried over)."""
        s = league.load()
        e = next((x for x in s["events"] if x["id"] == event_id), None) or abort(404)
        kind = request.form.get("kind", "")
        settings = {"date": e["date"], "start_time": e["time"].upper(),
                    "venue": League.location_name(s, e["loc"]) if e["loc"] != ALL else ""}
        if kind in dict(BRACKET_FORMATS):
            settings["format"], kind = kind, BRACKET
        elif kind != CHIP:
            flash("Pick a tournament format.", "error")
            return redirect(url_for("league_office", month=e["date"][:7], day=e["date"]) + "#day")
        try:
            ev = hub.create(e["title"], kind, **settings)
        except TournamentError as exc:
            flash(str(exc), "error")
            return redirect(url_for("league_office", month=e["date"][:7], day=e["date"]) + "#day")
        league.link_tournament(event_id, ev.id)
        flash(f"Created {e['title']} in Control Table. Finish its settings, then share the link.", "ok")
        return redirect(url_for("admin", event=ev.id) + "#settings")

    @app.get("/league/office/requests")
    @director_view
    def league_office_requests():
        s = league.load()
        return render_template("league_board.html", t=None, s=s, rep=None, groups=board(s), office=True,
                               counts=office_counts(s))

    @app.post("/league/office/request/<req_id>/decline")
    @admin_required
    def league_office_decline(req_id):
        change(lambda: league.decline_request(req_id), "Request declined and removed.")
        return back("league_office_requests")

    @app.get("/league/office/signups")
    @director_view
    def league_office_signups():
        s = league.load()
        signups = sorted(s["signups"], key=lambda x: x["created"], reverse=True)
        return render_template("league_signups.html", t=None, s=s, signups=signups, counts=office_counts(s),
                               open_teams=league.open_teams)

    @app.post("/league/office/signup/<signup_id>/connected")
    @admin_required
    def league_office_connected(signup_id):
        change(lambda: league.mark_connected(signup_id), "Marked as connected.")
        return back("league_office_signups")

    @app.post("/league/office/remove/<kind>/<row_id>")
    @admin_required
    def league_office_remove(kind, row_id):
        if kind not in ("players", "teams", "signups"):
            abort(404)
        change(lambda: league.delete_row(kind, row_id), "Removed.")
        return back("league_office_signups" if kind == "signups" else "league_office_setup",
                    "" if kind == "signups" else "people")

    @app.get("/league/office/setup")
    @director_view
    def league_office_setup():
        s = league.load()
        blackouts = sorted((b for b in s["blackouts"] if b["end"] >= today()), key=lambda b: b["start"])
        nights_text = {l["id"]: "\n".join(f"{DAYS[n['dow']]} {n['time']} {n['label']}" for n in l["nights"])
                       for l in s["locations"]}
        return render_template("league_setup.html", t=None, s=s, blackouts=blackouts, nights_text=nights_text,
                               counts=office_counts(s), edit=request.args.get("edit", ""))

    @app.post("/league/office/settings")
    @admin_required
    def league_office_settings():
        f = request.form
        change(lambda: league.save_settings(f.get("name", ""), f.get("season_start", ""), f.get("season_end", "")),
               "Saved.")
        return back("league_office_setup", "season")

    @app.post("/league/office/location")
    @app.post("/league/office/location/<loc_id>")
    @admin_required
    def league_office_location(loc_id=None):
        f = request.form
        loc = change(lambda: league.save_location(loc_id, {k: f[k] for k in LOCATION_FIELDS if k in f},
                                                  League.parse_nights(f.get("nights", ""))))
        if loc and loc_id is None:
            flash(f"Added {loc['name']}. Its host PIN is {loc['pin']}: give it to the location to sign in.", "ok")
        elif loc:
            flash(f"Saved {loc['name']}.", "ok")
        return back("league_office_setup", "locations")

    @app.post("/league/office/location/<loc_id>/delete")
    @admin_required
    def league_office_location_delete(loc_id):
        if request.form.get("confirm", "").strip().upper() != "DELETE":
            flash("Type DELETE to confirm.", "error")
        else:
            change(lambda: league.delete_location(loc_id),
                   "Deleted the location with its events, blackouts, requests, sign-ups and teams.")
        return back("league_office_setup", "locations")

    @app.post("/league/office/rep")
    @admin_required
    def league_office_rep():
        rep = change(lambda: league.add_rep(request.form.get("name", ""), request.form.get("division", "")))
        if rep:
            flash(f"Added {rep['name']}. Their rep PIN is {rep['pin']}.", "ok")
        return back("league_office_setup", "reps")

    @app.post("/league/office/rep/<rep_id>/delete")
    @admin_required
    def league_office_rep_delete(rep_id):
        change(lambda: league.delete_rep(rep_id), "Removed the rep. Requests they had claimed are open again.")
        return back("league_office_setup", "reps")

    @app.post("/league/office/pin/<kind>/<row_id>")
    @admin_required
    def league_office_pin(kind, row_id):
        if kind not in ("locations", "reps", "players", "teams"):
            abort(404)
        pin = change(lambda: league.reset_pin(kind, row_id))
        if pin:
            flash(f"New PIN: {pin}. The old one no longer works.", "ok")
        return back("league_office_setup", {"locations": "locations", "reps": "reps"}.get(kind, "people"))

    return league
