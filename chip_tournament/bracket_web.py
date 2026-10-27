"""Pages for bracket tournaments (single, double, modified single elimination, round robin).

They live at the same /t/<event>/... addresses as a chip tournament's pages: app.py sends
home, board, register, admin, settings, start and undo here when the tournament is a
bracket. Everything else a bracket needs has its own route, with an endpoint name that
starts with "b_".
"""

from __future__ import annotations

import os
from datetime import datetime

from flask import abort, flash, g, redirect, render_template, request, session, url_for

from chips import themes
from chips.bracket import (
    CALLED, FINAL_CUTS, FORMATS, MODIFIED, RACK_CHART, READY, ROUND_ROBIN, SETUP, STRAIGHT, place_text,
)
from chips.engine import Player, TournamentError, money
from chips.races import parse_skill_groups, skill_groups_text


def parse_payouts(text: str) -> list[int]:
    """'50, 30, 20' (percent of the pot for 1st, 2nd, 3rd...)."""
    out = []
    for part in (text or "").replace("%", "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise TournamentError(f"Payouts are whole percents, like 50, 30, 20 (not '{part}').")
        out.append(int(part))
    return out


def parse_entry_line(line: str, team_size: int) -> tuple[str, list[Player]]:
    """'Ann Lee' or 'Ann Lee, 555-123-4567' for singles; 'Sharks: Ann & Bob' or 'Ann & Bob' for doubles."""
    line = line.strip()
    name = ""
    if team_size > 1 and ":" in line:
        name, line = (x.strip() for x in line.split(":", 1))
    if team_size == 1:
        parts = [x.strip() for x in line.split(",")]
        return "", [Player(name=parts[0], phone=parts[1] if len(parts) > 1 else "")]
    people = [x.strip() for x in line.replace(" and ", " & ").replace("/", "&").split("&") if x.strip()]
    return name, [Player(name=p) for p in people]


def register(app, h) -> dict:
    """Add the bracket routes. Returns the views for the pages both tournament types share."""
    act, store, sms = h["act"], h["store"], h["sms"]
    director_view, admin_required = h["director_view"], h["admin_required"]
    parse_money = h["parse_money"]

    def players_from_form(f, size: int) -> list[Player]:
        players = []
        for i in range(size):
            levels = {}
            for key in ("skill", "skill8", "skill9"):
                value = f.get(f"player_{key}_{i}", "").strip()
                if value and not value.isdigit():
                    raise TournamentError(f"Skill level '{value}' must be a whole number.")
                levels[key] = int(value or 0)
            players.append(Player(name=f.get(f"player_name_{i}", "").strip(), phone=f.get(f"player_phone_{i}", "").strip(),
                                  skill=levels["skill"], skill_8=levels["skill8"], skill_9=levels["skill9"]))
        return players

    def asks_two_levels(s) -> bool:
        """Singles events that place or race players by skill ask for 8-Ball and 9-Ball levels."""
        return s.team_size == 1 and bool(s.skill_groups or s.race_method == RACK_CHART)

    def back(anchor: str = "") -> str:
        return url_for("admin") + (f"#{anchor}" if anchor else "")

    # ------------------------------------------------------------- shared pages

    def home():
        t = store.load()
        return render_template("b_home.html", t=t)

    def board():
        return render_template("b_board.html", t=store.load())

    def register_page():
        t = store.load()
        size = t.settings.team_size
        if request.method == "POST":
            f = request.form

            def go(t):
                if t.settings.registration_closed:
                    raise TournamentError("Registration is closed for now.")
                if f.get("agree_terms") != "1":
                    raise TournamentError("Please agree to the Terms & Conditions and Privacy Policy to register.")
                players = players_from_form(f, size)
                if asks_two_levels(t.settings) and not (players[0].skill_8 or players[0].skill_9):
                    raise TournamentError("Enter your 8-Ball or 9-Ball skill level (or both).")
                if f.get("sms_consent") == "1" and players[0].phone:
                    players[0].sms_consent, players[0].sms_consent_at = True, datetime.now().isoformat(timespec="seconds")
                return t.register_entry(f.get("name", ""), players)

            entry = act(f"Registration: {f.get('name', '').strip() or f.get('player_name_0', '').strip()}", go)
            if entry:
                session[f"entry_{entry.id}"] = True
                group = f" You're in the {entry.division} group." if entry.division else ""
                flash(f"{entry.name} is registered!{group} Your PIN is {entry.pin}. Keep it to enter scores.", "ok")
                return redirect(url_for("b_entry", entry_id=entry.id))
        return render_template("b_register.html", t=t, form=request.form, size=size)

    def admin():
        t = store.load()
        return render_template(
            "b_admin.html", t=t, undo_label=store.last_undo_label(), undo_step=store.undo_depth(),
            texts=sms.recent(50), sms_enabled=sms.enabled, archives=store.archives(), formats=FORMATS,
            final_cuts=FINAL_CUTS, server_time=datetime.now(), tz_name=os.environ.get("TZ", ""),
        )

    def admin_settings():
        f = request.form

        def update(t):
            s = t.settings
            s.name = f.get("name", "").strip() or s.name
            date = f.get("date", "").strip()
            if date:
                try:
                    datetime.strptime(date, "%Y-%m-%d")
                except ValueError:
                    raise TournamentError("Enter a real tournament date.") from None
            s.date, s.start_time = date, f.get("start_time", "").strip()
            s.venue, s.address, s.details = f.get("venue", ""), f.get("address", ""), f.get("details", "")
            s.checkin_by = f.get("checkin_by", "").strip()
            s.sms_name = f.get("sms_name", "").strip()
            s.contact_phone = f.get("contact_phone", "").strip()
            s.theme = themes.theme_key(f.get("theme", s.theme))
            s.rule_set = h["event_rule_set"](f.get("rule_set", s.rule_set))
            s.event_rules = f.get("event_rules", s.event_rules).replace("\r\n", "\n").strip()
            s.registration_closed = f.get("registration_closed") == "1"
            tables = [x.strip() for x in f.get("tables", "").split(",") if x.strip()]
            if not tables:
                raise TournamentError("Add at least one table.")
            if len(set(tables)) != len(tables):
                raise TournamentError("Table names must be different.")
            if t.busy_tables() - set(tables):
                raise TournamentError("You can't remove a table that has a match on it.")
            s.tables = tables
            s.tables_down = [x for x in s.tables_down if x in tables]
            numbers = {}
            for key in ("race_winners", "race_losers", "race_finals", "match_minutes", "long_match_minutes", "field_cap"):
                value = f.get(key, str(getattr(s, key))).strip() or "0"
                if not value.isdigit():
                    raise TournamentError("Races, times and the field cap must be whole numbers.")
                numbers[key] = int(value)
            if min(numbers["race_winners"], numbers["race_losers"], numbers["race_finals"]) < 1:
                raise TournamentError("Races must be at least 1 game.")
            for key in ("race_winners", "race_losers", "race_finals", "match_minutes", "long_match_minutes"):
                setattr(s, key, numbers[key])
            s.auto_call = f.get("auto_call") == "1"
            if t.status == SETUP:
                fmt = f.get("format", s.format)
                if fmt not in dict(FORMATS):
                    raise TournamentError("Pick a bracket format.")
                size = f.get("team_size", str(s.team_size))
                if size not in ("1", "2"):
                    raise TournamentError("Entries are singles (1) or doubles (2).")
                if int(size) != s.team_size and t.entries:
                    raise TournamentError("Remove the entries before switching between singles and doubles.")
                s.format, s.team_size = fmt, int(size)
                s.seeding = "manual" if f.get("seeding") == "manual" else "random"
                cut = f.get("final_cut", str(s.final_cut))
                s.final_cut = int(cut) if cut.isdigit() and int(cut) in FINAL_CUTS else s.final_cut
                s.true_double = f.get("true_double") == "1"
                for key in ("groups", "advance"):
                    value = f.get(key, str(getattr(s, key))).strip() or "0"
                    if not value.isdigit():
                        raise TournamentError("Groups and advancing entries must be whole numbers.")
                    setattr(s, key, int(value))
                s.groups = max(1, s.groups)
                s.skill_groups = parse_skill_groups(f.get("skill_groups", skill_groups_text(s.skill_groups)))
                method = f.get("race_method", s.race_method)
                if method not in (STRAIGHT, RACK_CHART):
                    raise TournamentError("Pick how races are set.")
                s.race_method = method
                for e in t.entries.values():
                    e.division = t.preview_division(e)
                s.require_checkin = f.get("require_checkin") == "1"
                t.set_money(parse_money(f.get("entry_fee", "0")), parse_money(f.get("added_money", "0")))
                t.set_field_cap(numbers["field_cap"])
            t.set_payouts(parse_payouts(f.get("payouts", "")))
            t.update_races()
            if t.status != SETUP:
                t._call_ready()
            return True

        act("Update settings", update, "Settings saved.")
        return redirect(back("settings"))

    def admin_start():
        act("Draw the bracket", lambda t: t.start(), "The bracket is drawn and the first matches are called.")
        return redirect(back())

    def admin_undo():
        with store.lock:
            expected = request.form.get("step", "")
            if expected and expected != str(store.undo_depth()):
                flash(f"Nothing was undone: something else happened since you looked "
                      f"({store.last_undo_label() or 'nothing to undo'} is now the latest action).", "error")
                return redirect(back())
            label = store.undo()
        flash(f"Undid: {label}." if label else "Nothing to undo.", "ok" if label else "error")
        return redirect(back())

    shared = {
        "home": home, "board": board, "register": register_page, "admin": director_view(admin),
        "admin_settings": admin_required(admin_settings), "admin_start": admin_required(admin_start),
        "admin_undo": admin_required(admin_undo),
    }

    # ----------------------------------------------------------- an entry's page

    def entry_or_404(entry_id):
        t = store.load()
        return t, (t.entries.get(entry_id) or abort(404))

    def signed_in(entry_id) -> bool:
        return bool(session.get(f"entry_{entry_id}") or session.get("admin"))

    @app.get("/t/<event>/entry/<entry_id>")
    def b_entry(entry_id):
        t, entry = entry_or_404(entry_id)
        return render_template("b_entry.html", t=t, entry=entry, match=t.current_match_for(entry_id),
                               history=t.matches_for(entry_id), path=t.next_for(entry_id),
                               place=place_text(t.places().get(entry_id)), is_entry=signed_in(entry_id))

    @app.post("/t/<event>/entry/<entry_id>/login")
    def b_entry_login(entry_id):
        _, entry = entry_or_404(entry_id)
        h["check_pin"](entry)  # signs in as team_<id>; mirror it for the entry
        if session.get(f"team_{entry.id}"):
            session[f"entry_{entry.id}"] = True
        return redirect(url_for("b_entry", entry_id=entry_id))

    @app.post("/t/<event>/entry/<entry_id>/texts")
    def b_entry_texts(entry_id):
        if not session.get(f"entry_{entry_id}"):
            flash("Enter your PIN first.", "error")
        elif request.form.get("player", "").isdigit() and request.form.get("sms_consent") == "1":
            index = int(request.form["player"])
            player = act("Text alerts on", lambda t: t.opt_in(entry_id, index))
            if player:
                flash(f"Text alerts are on for {player.name}.", "ok")
        else:
            flash("Tick the box to agree to text alerts.", "error")
        return redirect(url_for("b_entry", entry_id=entry_id))

    def own_match(entry_id):
        if not signed_in(entry_id):
            raise TournamentError("Enter your PIN first.")
        m = store.load().current_match_for(entry_id)
        if m is None or m.status != CALLED:
            raise TournamentError("You're not on a table right now.")
        return m

    @app.post("/t/<event>/entry/<entry_id>/score")
    def b_entry_score(entry_id):
        side, delta = request.form.get("side"), request.form.get("delta", "1")
        if side not in ("a", "b") or delta not in ("1", "-1"):
            abort(400)
        try:
            m = own_match(entry_id)
        except TournamentError as exc:
            flash(str(exc), "error")
        else:
            name = store.load().name_of(entry_id)
            act("Score (player)", lambda t: t.score(m.id, side, int(delta), by=name))
        return redirect(url_for("b_entry", entry_id=entry_id))

    @app.post("/t/<event>/entry/<entry_id>/submit")
    def b_entry_submit(entry_id):
        try:
            m = own_match(entry_id)
        except TournamentError as exc:
            flash(str(exc), "error")
        else:
            if act("Submit result (player)", lambda t: t.submit_result(m.id)):
                flash("Result submitted. Only the director can change it now.", "ok")
        return redirect(url_for("b_entry", entry_id=entry_id))

    # ------------------------------------------------------------ director: entries

    @app.post("/t/<event>/admin/entries/add")
    @admin_required
    def b_add_entries():
        f = request.form
        bulk = f.get("bulk", "").strip()
        if bulk:
            def add_all(t):
                added, problems = 0, []
                for n, line in enumerate(bulk.splitlines(), 1):
                    if not line.strip():
                        continue
                    try:
                        name, players = parse_entry_line(line, t.settings.team_size)
                        t.register_entry(name, players, by_director=True)
                        added += 1
                    except TournamentError as exc:
                        problems.append(f"line {n}: {exc}")
                if problems:
                    flash("Skipped " + "; ".join(problems[:5]) + ("…" if len(problems) > 5 else ""), "error")
                return added

            act("Add entries", add_all, "Added {result} entries.")
        else:
            act("Add entry", lambda t: t.register_entry(
                f.get("name", ""), players_from_form(f, t.settings.team_size), by_director=True), "Added {result.name}.")
        return redirect(back("entries"))

    @app.post("/t/<event>/admin/entry/<entry_id>/edit")
    @admin_required
    def b_edit_entry(entry_id):
        f = request.form
        act("Edit entry", lambda t: t.update_entry(entry_id, f.get("name"), players_from_form(f, t.settings.team_size)),
            "Saved {result.name}.")
        return redirect(back("entries"))

    actions = {
        "check-in": ("Check in", lambda t, i: t.check_in_entry(i), None),
        "no-show": ("No-show", lambda t, i: t.mark_no_show(i), "Marked as a no-show."),
        "restore": ("Restore", lambda t, i: t.restore_entry(i), "Restored."),
        "remove": ("Remove", lambda t, i: t.remove_entry(i), "Removed."),
        "up": ("Move up", lambda t, i: t.move_seed(i, -1), None),
        "down": ("Move down", lambda t, i: t.move_seed(i, 1), None),
        "withdraw": ("Withdraw", lambda t, i: t.withdraw(i), "Withdrawn: its remaining matches are forfeits."),
    }

    @app.post("/t/<event>/admin/entry/<entry_id>/<action>")
    @admin_required
    def b_entry_action(entry_id, action):
        if action not in actions:
            abort(404)
        label, fn, done = actions[action]
        act(f"{label}: {store.load().name_of(entry_id)}", lambda t: fn(t, entry_id), done)
        return redirect(back("entries"))

    @app.post("/t/<event>/admin/shuffle")
    @admin_required
    def b_shuffle():
        act("Shuffle entries", lambda t: t.shuffle_order(), "Shuffled. With manual seeding, this is the draw.")
        return redirect(back("entries"))

    # ------------------------------------------------------------- director: matches

    def side_and_delta():
        side, delta = request.form.get("side"), request.form.get("delta", "1")
        if side not in ("a", "b") or delta not in ("1", "-1"):
            abort(400)
        return side, int(delta)

    @app.post("/t/<event>/admin/m/<match_id>/score")
    @admin_required
    def b_score(match_id):
        side, delta = side_and_delta()
        act("Score", lambda t: t.score(match_id, side, delta))
        return redirect(back("tables"))

    @app.post("/t/<event>/admin/m/<match_id>/submit")
    @admin_required
    def b_submit(match_id):
        act("Submit result", lambda t: t.submit_result(match_id), "Result recorded.")
        return redirect(back("tables"))

    @app.post("/t/<event>/admin/m/<match_id>/winner")
    @admin_required
    def b_winner(match_id):
        forfeit = request.form.get("forfeit") == "1"
        act("Forfeit" if forfeit else "Record result",
            lambda t: t.record_result(match_id, request.form.get("winner", ""), forfeit=forfeit), "Result recorded.")
        return redirect(back("tables"))

    @app.post("/t/<event>/admin/m/<match_id>/table")
    @admin_required
    def b_call(match_id):
        table = request.form.get("table", "")
        act(f"Table {table}", lambda t: t.call_to_table(match_id, table), f"Sent to table {table}.")
        return redirect(back("tables"))

    @app.post("/t/<event>/admin/m/<match_id>/front")
    @admin_required
    def b_front(match_id):
        act("Move to the front", lambda t: t.prioritize(match_id), "It's next for a free table.")
        return redirect(back("tables"))

    @app.post("/t/<event>/admin/m/<match_id>/fix")
    @admin_required
    def b_fix(match_id):
        act("Fix result", lambda t: t.fix_result(match_id), "Result corrected. Both sides were texted.")
        return redirect(back("results"))

    @app.post("/t/<event>/admin/table/<path:table>")
    @admin_required
    def b_table(table):
        down = request.form.get("down") == "1"
        act(f"Table {table} {'out of service' if down else 'back in service'}",
            lambda t: t.set_table_down(table, down),
            f"Table {table} is {'out of service' if down else 'back in service'}.")
        return redirect(back("tables"))

    @app.post("/t/<event>/admin/call-waiting")
    @admin_required
    def b_call_waiting():
        act("Call waiting matches", lambda t: t.call_waiting(), "Called what fits on the free tables.")
        return redirect(back("tables"))

    app.jinja_env.globals.setdefault("place_text", place_text)
    app.jinja_env.globals.setdefault("asks_two_levels", asks_two_levels)
    app.jinja_env.globals.setdefault("skill_groups_text", skill_groups_text)
    app.jinja_env.globals.setdefault("RACK_CHART", RACK_CHART)
    app.jinja_env.globals.setdefault("READY", READY)
    app.jinja_env.globals.setdefault("MODIFIED", MODIFIED)
    app.jinja_env.globals.setdefault("ROUND_ROBIN", ROUND_ROBIN)
    app.jinja_env.filters.setdefault("money", money)
    return shared
