"""Web app for running a chip billiards tournament.

Run it with:  python app.py   (then open http://localhost:5000)
"""

from __future__ import annotations

import os
import secrets
import threading
from functools import wraps
from pathlib import Path
from urllib.parse import quote_plus

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

from chips.alerts import SmsSender
from chips.engine import (
    ACTIVE,
    CALLED,
    DONE,
    FINISHED,
    RUNNING,
    SETUP,
    WAITING,
    Player,
    TournamentError,
    money,
)
from chips.storage import Store

MAX_PLAYERS = 8


def parse_money(text: str) -> int:
    """'$20', '20.00' or '20' -> 2000 cents."""
    cleaned = (text or "0").replace("$", "").replace(",", "").strip() or "0"
    try:
        return round(float(cleaned) * 100)
    except ValueError:
        raise TournamentError(f"'{text}' is not a dollar amount.") from None


def parse_skill_chips(text: str) -> dict[str, int]:
    """Lines like '3 = 4' (skill level 3 starts with 4 chips)."""
    result = {}
    for line in (text or "").replace(",", "\n").splitlines():
        if not line.strip():
            continue
        if "=" not in line:
            raise TournamentError(f"Use 'skill = chips' on each line, not '{line.strip()}'.")
        level, chips = (part.strip() for part in line.split("=", 1))
        if not level or not chips.isdigit() or int(chips) < 1:
            raise TournamentError(f"Bad chip setting: '{line.strip()}'.")
        result[level] = int(chips)
    if not result:
        raise TournamentError("Set at least one skill level.")
    return result


def players_from_form(form) -> list[Player]:
    players = []
    for i in range(MAX_PLAYERS):
        name = form.get(f"player_name_{i}", "").strip()
        if name:
            players.append(Player(name=name, phone=form.get(f"player_phone_{i}", "").strip()))
    return players


def create_app(data_dir: str | None = None) -> Flask:
    app = Flask(__name__)
    data_path = Path(data_dir or os.environ.get("CHIPS_DATA_DIR", Path(__file__).parent / "data"))
    store = Store(data_path)
    sms = SmsSender(data_path / "texts.log")

    key_file = data_path / "secret_key"
    if os.environ.get("SECRET_KEY"):
        app.secret_key = os.environ["SECRET_KEY"]
    else:
        if not key_file.exists():
            key_file.write_text(secrets.token_hex(32))
        app.secret_key = key_file.read_text()
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin")
    app.config["store"] = store
    app.config["sms"] = sms

    app.jinja_env.filters["money"] = money
    app.jinja_env.filters["urlquote"] = quote_plus

    @app.context_processor
    def globals_():
        return {
            "is_admin": session.get("admin", False),
            "ACTIVE": ACTIVE,
            "CALLED": CALLED,
            "WAITING": WAITING,
            "DONE": DONE,
            "SETUP": SETUP,
            "RUNNING": RUNNING,
            "FINISHED": FINISHED,
            "MAX_PLAYERS": MAX_PLAYERS,
        }

    def send_texts(t) -> None:
        """Send whatever texts the last change produced, without slowing the page."""
        messages, t.outbox = list(t.outbox), []
        if messages:
            threading.Thread(
                target=lambda: [sms.send(m.to, m.body) for m in messages], daemon=True
            ).start()

    def act(label: str, fn, success: str | None = None):
        """Apply one change to the tournament; show rule errors instead of crashing."""
        try:
            with store.change(label) as t:
                result = fn(t)
                send_texts(t)
            if success:
                flash(success.format(result=result), "ok")
            return result
        except TournamentError as exc:
            flash(str(exc), "error")
            return None

    def admin_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("admin"):
                return redirect(url_for("admin_login", next=request.path))
            return view(*args, **kwargs)

        return wrapper

    # ------------------------------------------------------------- public pages

    @app.get("/")
    def home():
        return render_template("home.html", t=store.load())

    @app.get("/board")
    def board():
        return render_template("board.html", t=store.load())

    @app.route("/register", methods=["GET", "POST"])
    def register():
        t = store.load()
        if request.method == "POST":
            f = request.form
            team = act(
                "Register team",
                lambda t: t.register_team(
                    f.get("name", ""), f.get("skill_level", ""), f.get("captain", ""), f.get("phone", ""),
                    players_from_form(f),
                ),
            )
            if team:
                session[f"team_{team.id}"] = True
                flash(f"{team.name} is registered! Your team PIN is {team.pin} — keep it to edit your roster.", "ok")
                return redirect(url_for("team_page", team_id=team.id))
        return render_template("register.html", t=t, form=request.form)

    @app.get("/team/<team_id>")
    def team_page(team_id):
        t = store.load()
        team = t.teams.get(team_id) or abort(404)
        history = [m for m in t.matches if team_id in (m.team_a, m.team_b) and m.status == DONE]
        return render_template(
            "team.html", t=t, team=team, match=t.current_match_for(team_id), history=history,
            can_edit=session.get(f"team_{team_id}") or session.get("admin"),
        )

    @app.route("/team/<team_id>/roster", methods=["GET", "POST"])
    def roster(team_id):
        t = store.load()
        team = t.teams.get(team_id) or abort(404)
        allowed = session.get(f"team_{team_id}") or session.get("admin")
        if request.method == "POST" and not allowed:
            if request.form.get("pin", "").strip() == team.pin:
                session[f"team_{team_id}"] = True
            else:
                flash("That PIN doesn't match.", "error")
            return redirect(url_for("roster", team_id=team_id))
        if request.method == "POST":
            f = request.form
            if act(
                "Update roster",
                lambda t: t.update_roster(team_id, f.get("captain", ""), f.get("phone", ""), players_from_form(f),
                                          name=f.get("name")),
                "Roster saved.",
            ):
                return redirect(url_for("team_page", team_id=team_id))
            team = store.load().teams[team_id]
        return render_template("roster.html", t=t, team=team, allowed=allowed)

    # -------------------------------------------------------------------- admin

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            if secrets.compare_digest(request.form.get("password", ""), admin_password):
                session["admin"] = True
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("admin"))
            flash("Wrong password.", "error")
        return render_template("login.html", default_password=admin_password == "admin")

    @app.post("/admin/logout")
    def admin_logout():
        session.pop("admin", None)
        return redirect(url_for("home"))

    @app.get("/admin")
    @admin_required
    def admin():
        t = store.load()
        return render_template(
            "admin.html", t=t, undo_label=store.last_undo_label(), texts=sms.recent(50), sms_enabled=sms.enabled,
            skill_text="\n".join(f"{k} = {v}" for k, v in t.settings.skill_chips.items()),
        )

    @app.post("/admin/settings")
    @admin_required
    def admin_settings():
        f = request.form

        def update(t):
            s = t.settings
            s.name = f.get("name", "").strip() or "Chip Tournament"
            s.date, s.start_time = f.get("date", ""), f.get("start_time", "")
            s.venue, s.address, s.details = f.get("venue", ""), f.get("address", ""), f.get("details", "")
            s.cash_in_time, s.last_call_time = f.get("cash_in_time", ""), f.get("last_call_time", "")
            tables = [x.strip() for x in f.get("tables", "").split(",") if x.strip()]
            if not tables:
                raise TournamentError("Add at least one table.")
            if len(set(tables)) != len(tables):
                raise TournamentError("Table names must be different.")
            busy = {m.table for m in t.matches if m.status == CALLED}
            if busy - set(tables):
                raise TournamentError("You can't remove a table that has a match on it.")
            s.tables = tables
            race = f.get("race_to", "3")
            if not race.isdigit() or int(race) < 1:
                raise TournamentError("Race must be a whole number of games.")
            if t.status == SETUP:
                s.entry_fee_cents = parse_money(f.get("entry_fee", "0"))
                s.added_money_cents = parse_money(f.get("added_money", "0"))
                s.skill_chips = parse_skill_chips(f.get("skill_chips", ""))
                missing = {tm.skill_level for tm in t.teams.values()} - set(s.skill_chips)
                if missing:
                    raise TournamentError(f"Registered teams use skill level(s) {', '.join(sorted(missing))}.")
            s.race_to = int(race)
            if t.status == RUNNING:
                t._call_matches()  # new tables may be free now

        act("Update settings", update, "Settings saved.")
        return redirect(url_for("admin"))

    @app.post("/admin/start")
    @admin_required
    def admin_start():
        act("Start tournament", lambda t: t.start(), "Tournament started — round 1 is called.")
        return redirect(url_for("admin"))

    @app.post("/admin/match/<match_id>/score")
    @admin_required
    def admin_score(match_id):
        side, delta = request.form.get("side"), int(request.form.get("delta", "1"))
        if side not in ("a", "b") or delta not in (1, -1):
            abort(400)
        act("Score", lambda t: t.score(match_id, side, delta))
        return redirect(url_for("admin") + "#matches")

    @app.post("/admin/match/<match_id>/winner")
    @admin_required
    def admin_winner(match_id):
        act("Record result", lambda t: t.record_result(match_id, request.form.get("winner", "")), "Result recorded.")
        return redirect(url_for("admin") + "#matches")

    @app.post("/admin/cash-in/open")
    @admin_required
    def admin_open_cash_in():
        act("Open cash-in", lambda t: t.open_cash_in(), "Cash-in is open and teams were texted.")
        return redirect(url_for("admin"))

    @app.post("/admin/team/<team_id>/cash-in")
    @admin_required
    def admin_cash_in(team_id):
        act("Cash in team", lambda t: money(t.cash_in(team_id)), "Cashed in — pay out {result}.")
        return redirect(url_for("admin") + "#teams")

    @app.post("/admin/last-call")
    @admin_required
    def admin_last_call():
        act("Last call", lambda t: t.call_last_call(), "Last call — every team has been cashed in. See payouts below.")
        return redirect(url_for("admin"))

    @app.post("/admin/team/<team_id>/adjust")
    @admin_required
    def admin_adjust(team_id):
        try:
            change = int(request.form.get("change", "0"))
        except ValueError:
            change = 0
        act("Adjust chips", lambda t: t.adjust_chips(team_id, change, request.form.get("reason", "")), "Chips adjusted.")
        return redirect(url_for("admin") + "#teams")

    @app.post("/admin/team/<team_id>/skill")
    @admin_required
    def admin_skill(team_id):
        act("Change skill level", lambda t: t.set_skill_level(team_id, request.form.get("skill_level", "")),
            "Skill level updated.")
        return redirect(url_for("admin") + "#teams")

    @app.post("/admin/team/<team_id>/remove")
    @admin_required
    def admin_remove(team_id):
        act("Remove team", lambda t: t.remove_team(team_id), "Team removed.")
        return redirect(url_for("admin") + "#teams")

    @app.post("/admin/broadcast")
    @admin_required
    def admin_broadcast():
        body = request.form.get("body", "").strip()
        if body:
            act("Broadcast text", lambda t: t.broadcast(body), "Queued {result} text(s).")
        return redirect(url_for("admin") + "#texts")

    @app.post("/admin/undo")
    @admin_required
    def admin_undo():
        label = store.undo()
        flash(f"Undid: {label}." if label else "Nothing to undo.", "ok" if label else "error")
        return redirect(url_for("admin"))

    @app.post("/admin/reset")
    @admin_required
    def admin_reset():
        if request.form.get("confirm") != "RESET":
            flash("Type RESET to confirm.", "error")
        else:
            store.archive_and_reset()
            flash("Started a new tournament. The old one was archived in the data folder.", "ok")
        return redirect(url_for("admin"))

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    create_app().run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
