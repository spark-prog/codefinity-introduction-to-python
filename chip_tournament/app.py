"""Control Table: a web app for running billiards tournaments.

Several tournaments can run at once. Each lives at /t/<id>/ with its own data,
texts and undo history: chip tournaments (chips/engine.py) and bracket
tournaments (chips/bracket.py, pages in bracket_web.py). The page at / lists
them all, and /admin is where the director creates and opens them.

Run it with:  python app.py   (then open http://localhost:5000)
In production: gunicorn --workers 1 --threads 8 "app:create_app()"
(one worker only: the tournament files and the check-in clock live in one process)
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import quote_plus

from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from werkzeug.local import LocalProxy
from werkzeug.middleware.proxy_fix import ProxyFix

import bracket_web
from chips import importer, rules as rulebook, themes
from chips.bracket import FINAL_CUTS, FORMATS
from chips.hub import BRACKET, CHIP, Hub
from chips.engine import (
    ACTIVE,
    CALLED,
    DONE,
    FINISHED,
    RUNNING,
    SETUP,
    WAITING,
    TEAM_SIZE,
    Player,
    TournamentError,
    money,
)
from chips.throttle import Throttle


def clock(value) -> str:
    """'2026-11-07T21:42:00' or a datetime -> '9:42 PM'."""
    if not value:
        return ""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return dt.strftime("%I:%M %p").lstrip("0")


def parse_money(text: str) -> int:
    """'$20', '20.00' or '20' -> 2000 cents."""
    cleaned = (text or "0").replace("$", "").replace(",", "").strip() or "0"
    try:
        return round(float(cleaned) * 100)
    except ValueError:
        raise TournamentError(f"'{text}' is not a dollar amount.") from None


def parse_chip_brackets(text: str) -> list[list[int]]:
    """Lines like '8 = 4' (combined skill up to 8 starts with 4 chips)."""
    brackets = []
    for line in (text or "").replace(",", "\n").splitlines():
        if not line.strip():
            continue
        if "=" not in line:
            raise TournamentError(f"Use 'highest combined skill = chips' on each line, not '{line.strip()}'.")
        top, chips = (part.strip() for part in line.split("=", 1))
        if not top.isdigit() or not chips.isdigit() or int(chips) < 1:
            raise TournamentError(f"Bad chip setting: '{line.strip()}'.")
        brackets.append([int(top), int(chips)])
    if not brackets:
        raise TournamentError("Set at least one chip bracket.")
    tops = [top for top, _ in brackets]
    if len(set(tops)) != len(tops):
        raise TournamentError("Each skill level can only appear once in the chip brackets.")
    return sorted(brackets)


def event_when(date: str, start_time: str) -> str:
    """'2026-10-31', '9:00 PM' -> 'October 31, 2026 beginning at 9 PM'."""
    try:
        d = datetime.strptime((date or "").strip(), "%Y-%m-%d")
        day = f"{d.strftime('%B')} {d.day}, {d.year}"
    except ValueError:
        day = (date or "").strip()  # typed in some other way: show it as entered
    when = (start_time or "").strip().replace(":00 ", " ").replace(":00", "")
    if day and when:
        return f"{day} beginning at {when}"
    return day or (f"Beginning at {when}" if when else "")


def last4(phone: str) -> str:
    digits = "".join(c for c in phone or "" if c.isdigit())
    return digits[-4:]


def players_from_form(form, keep_phones_of=None, same_people=()) -> list[Player]:
    """Partners from the form. With keep_phones_of (a team), a blank cell box keeps
    the number on file for that partner, since teams don't get to see the numbers:
    the same name, or a name marked as only corrected (same_people)."""
    players = []
    for i in range(TEAM_SIZE):
        skill = form.get(f"player_skill_{i}", "").strip()
        if skill and not skill.isdigit():
            raise TournamentError(f"Skill level '{skill}' must be a whole number.")
        name = form.get(f"player_name_{i}", "").strip()
        phone = form.get(f"player_phone_{i}", "").strip()
        if not phone and keep_phones_of is not None and i < len(keep_phones_of.players):
            old = keep_phones_of.players[i]
            if i in same_people or " ".join(old.name.lower().split()) == " ".join(name.lower().split()):
                phone = old.phone
        players.append(Player(name=name, phone=phone, skill=int(skill or 0)))
    return players


def create_app(data_dir: str | None = None) -> Flask:
    app = Flask(__name__)
    data_path = Path(data_dir or os.environ.get("CHIPS_DATA_DIR", Path(__file__).parent / "data"))
    hub = Hub(data_path)
    # The tournament this request is about (set from /t/<event>/ in the URL).
    store = LocalProxy(lambda: g.event.store)
    sms = LocalProxy(lambda: g.event.sms)
    rule_book = rulebook.RuleBook(data_path)

    def current_t():
        """The tournament in view, or None on the pages that cover every tournament."""
        ev = g.get("event")
        return ev.store.load() if ev else None

    key_file = data_path / "secret_key"
    if os.environ.get("SECRET_KEY"):
        app.secret_key = os.environ["SECRET_KEY"]
    else:
        if not key_file.exists():
            key_file.write_text(secrets.token_hex(32))
        app.secret_key = key_file.read_text()
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # uploads (CSV import, background photo)
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin")
    # Optional read-only director login (e.g. for the league office to look around).
    viewer_password = os.environ.get("VIEWER_PASSWORD", "")
    if os.environ.get("CHIPS_PRODUCTION") == "1":
        # On the internet: refuse to run with the default password, and only send cookies over HTTPS.
        if admin_password == "admin" or len(admin_password) < 8:
            raise RuntimeError("Set ADMIN_PASSWORD to a password of at least 8 characters before going live.")
        if viewer_password and (len(viewer_password) < 8 or viewer_password == admin_password):
            raise RuntimeError("VIEWER_PASSWORD must be at least 8 characters and different from ADMIN_PASSWORD.")
        app.config["SESSION_COOKIE_SECURE"] = True
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config["hub"] = hub
    app.config["data_dir"] = data_path

    app.jinja_env.filters["money"] = money
    app.jinja_env.filters["urlquote"] = quote_plus
    app.jinja_env.filters["clock"] = clock
    app.jinja_env.filters["last4"] = last4
    app.jinja_env.globals["event_when"] = event_when
    # Who runs the text program, shown on the Terms and Privacy pages (they must match the
    # carrier registration). Override with environment variables if they ever change.
    # The number tournament texts come from, so players can save it as a contact (Twilio setting).
    sender_digits = "".join(c for c in os.environ.get("TWILIO_FROM_NUMBER", "") if c.isdigit())[-10:]
    app.jinja_env.globals["text_sender"] = (
        f"({sender_digits[:3]}) {sender_digits[3:6]}-{sender_digits[6:]}" if len(sender_digits) == 10 else "")
    app.jinja_env.globals["legal"] = {
        "company": os.environ.get("LEGAL_NAME", "Maximum English LLC"),
        "brand": os.environ.get("BRAND_NAME", "First Coast APA"),
        "email": os.environ.get("SUPPORT_EMAIL", "firstcoast@apaleagues.com"),
        "phone": os.environ.get("SUPPORT_PHONE", "904-528-7657"),
        "updated": "October 22, 2026",
    }

    # Background photo: the director can upload their own; otherwise the bundled billiard room.
    photo_types = {b"\xff\xd8\xff": ("jpg", "image/jpeg"), b"\x89PNG": ("png", "image/png"), b"RIFF": ("webp", "image/webp")}

    def custom_background():
        for ext in ("jpg", "png", "webp"):
            path = data_path / f"background.{ext}"
            if path.exists():
                return path
        return None

    @app.get("/background")
    def background():
        path = custom_background()
        if path is None:
            return send_file(Path(app.static_folder) / "img" / "billiard-room.jpg", max_age=86400)
        mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[path.suffix[1:]]
        return send_file(path, mimetype=mime, max_age=86400)

    @app.get("/contact.vcf")
    def save_contact():
        """A contact card for the number tournament texts come from (one tap on a phone adds it)."""
        number = os.environ.get("TWILIO_FROM_NUMBER", "")
        if not number:
            abort(404)
        brand = app.jinja_env.globals["legal"]["brand"]
        card = "\r\n".join([
            "BEGIN:VCARD", "VERSION:3.0", f"FN:{brand} Tournaments", f"N:;{brand} Tournaments;;;", f"ORG:{brand}",
            f"TEL;TYPE=CELL:{number}", "NOTE:Tournament text alerts: table calls, results, cash-in and last call.",
            "END:VCARD", ""])
        return app.response_class(card, mimetype="text/vcard",
                                  headers={"Content-Disposition": f'attachment; filename="{brand.replace(" ", "-")}-Tournaments.vcf"'})

    @app.context_processor
    def globals_():
        bg = custom_background()
        return {
            # Versioned by file time so browsers pick up a new photo (or a new default) right away.
            "background_url": url_for("background", v=int((bg or Path(app.static_folder) / "img" / "billiard-room.jpg").stat().st_mtime)),
            "has_custom_background": bg is not None,
            "THEMES": themes.THEMES,
            "theme_key": themes.theme_key,
            "rule_choices": rule_book.load,
            "is_admin": session.get("admin", False),
            "is_viewer": bool(session.get("viewer")) and not session.get("admin"),
            "ACTIVE": ACTIVE,
            "CALLED": CALLED,
            "WAITING": WAITING,
            "DONE": DONE,
            "SETUP": SETUP,
            "RUNNING": RUNNING,
            "FINISHED": FINISHED,
            "TEAM_SIZE": TEAM_SIZE,
            "event": g.get("event"),
            "CHIP": CHIP,
            "BRACKET": BRACKET,
        }

    def send_texts(t) -> None:
        """Send whatever texts the last change produced, without slowing the page."""
        messages, t.outbox = list(t.outbox), []
        if messages:
            sender = sms._get_current_object()  # this tournament's sender: the thread runs outside the request
            threading.Thread(
                target=lambda: [sender.send(m.to, m.body) for m in messages], daemon=True
            ).start()

    def act(label: str, fn, success: str | None = None):
        """Apply one change to the tournament; show rule errors instead of crashing."""
        try:
            with store.change(label) as t:
                t.site_url = f"{request.url_root.rstrip('/')}/t/{g.event.id}"  # for team-page links in texts
                result = fn(t)
                send_texts(t)
            if success:
                flash(success.format(result=result), "ok")
            return result
        except TournamentError as exc:
            flash(str(exc), "error")
            return None

    def tick(ev) -> None:
        """Start grace periods, record no-show forfeits, and apply the partner deadline when due."""
        if ev.kind != CHIP:
            return
        store, sms = ev.store, ev.sms  # noqa: F841 (runs outside a request, so not the proxies)

        def send_texts(t) -> None:
            messages, t.outbox = list(t.outbox), []
            if messages:
                threading.Thread(target=lambda: [sms.send(m.to, m.body) for m in messages], daemon=True).start()

        probe = store.load()
        solos = [x.name for x in probe.solo_teams_in_field]
        if probe.check_clock() == 0:  # cheap check first, so idle ticks don't fill the undo history
            return
        label = (f"Partner deadline: moved {', '.join(solos)} to the waiting list" if probe.status == SETUP
                 else "Check-in clock (grace / forfeit)")
        with store.change(label) as t:
            t.check_clock()
            send_texts(t)

    def tick_all() -> None:
        for ev in hub.events():
            if ev.kind == CHIP and ev.store.load().status != FINISHED:
                tick(ev)

    def clock_loop() -> None:
        while True:
            for ev in hub.events():
                try:
                    if ev.kind == CHIP and ev.store.load().status != FINISHED:
                        tick(ev)
                except Exception as exc:  # keep the clock running no matter what
                    app.logger.exception("check-in clock failed for %s: %s", ev.id, exc)
            time.sleep(15)

    clock_started = threading.Event()

    @app.before_request
    def start_clock():
        # Started on the first request so it runs in the serving process only.
        if not app.testing and not clock_started.is_set():
            clock_started.set()
            threading.Thread(target=clock_loop, daemon=True).start()

    app.config["tick"] = tick_all

    # ------------------------------------------------- which tournament is in view

    @app.url_value_preprocessor
    def pull_event(endpoint, values):
        if values and "event" in values:
            ev = hub.get(values.pop("event"))
            if ev is None:
                abort(404)
            g.event = ev

    @app.url_defaults
    def add_event(endpoint, values):
        # Links on a tournament's pages stay inside that tournament.
        if "event" not in values and g.get("event") and app.url_map.is_endpoint_expecting(endpoint, "event"):
            values["event"] = g.event.id

    def admin_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("admin"):
                if session.get("viewer"):
                    flash("You're signed in view-only, so changes are turned off.", "error")
                    return redirect(url_for("admin") if g.get("event") else url_for("director"))
                return redirect(url_for("admin_login", next=request.path))
            return view(*args, **kwargs)

        return wrapper

    def director_view(view):
        """Pages the view-only login may look at (it can't change anything)."""
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not (session.get("admin") or session.get("viewer")):
                return redirect(url_for("admin_login", next=request.path))
            return view(*args, **kwargs)

        return wrapper

    # Wrong-guess limits: a 4-digit PIN or the password can't be guessed by trying over and over.
    # Players at the venue share one Wi-Fi address, so per-address limits are generous and the
    # tight limits are per device (a random id kept in the signed session cookie).
    pin_by_ip = Throttle(limit=60, window=15 * 60)
    pin_by_team = Throttle(limit=20, window=15 * 60)
    pin_by_device_team = Throttle(limit=10, window=15 * 60)  # who guessed wrong for which team
    login_by_ip = Throttle(limit=30, window=15 * 60)
    login_by_device = Throttle(limit=5, window=15 * 60)

    def device_id() -> str:
        if "device" not in session:
            session["device"] = secrets.token_hex(8)
        return f"{request.remote_addr or '?'}|{session['device']}"

    def minutes(seconds: int) -> str:
        n = max(1, (seconds + 59) // 60)
        return f"{n} minute{'s' if n != 1 else ''}"

    def check_pin(team) -> bool:
        """Sign the team in if the PIN matches; count wrong guesses."""
        ip, key = request.remote_addr or "?", f"team:{team.id}"
        pair = f"{device_id()}|{team.id}"
        # A team under attack is locked only for devices that have guessed wrong for it, so strangers
        # (or a typo on the venue Wi-Fi) can't lock the real players out of their own team.
        wait = max(pin_by_ip.wait(ip), pin_by_device_team.wait(pair),
                   pin_by_team.wait(key) if pin_by_device_team.failures(pair) else 0)
        if wait:
            flash(f"Too many wrong PINs. Try again in {minutes(wait)}, or ask the director.", "error")
            return False
        if secrets.compare_digest(request.form.get("pin", "").strip(), team.pin):
            pin_by_ip.clear(ip)
            pin_by_device_team.clear(pair)
            session[f"team_{team.id}"] = True
            return True
        pin_by_ip.fail(ip)
        pin_by_team.fail(key)
        pin_by_device_team.fail(pair)
        flash("That PIN doesn't match.", "error")
        return False

    # ------------------------------------------------------------- public pages

    @app.get("/t/<event>/")
    def home():
        return render_template("home.html", t=store.load())

    @app.get("/t/<event>/board")
    def board():
        return render_template("board.html", t=store.load())

    @app.route("/t/<event>/register", methods=["GET", "POST"])
    def register():
        t = store.load()
        if request.method == "POST":
            f = request.form
            solo = f.get("solo") == "1"

            def register_team(t):
                if t.settings.registration_closed:
                    raise TournamentError("Registration is closed for now.")
                if f.get("agree_terms") != "1":
                    raise TournamentError("Please agree to the Terms & Conditions and Privacy Policy to register.")
                players = players_from_form(f)[:1 if solo else TEAM_SIZE]
                if f.get("sms_consent") == "1" and players[0].phone:  # the person registering, for their own number
                    players[0].sms_consent, players[0].sms_consent_at = True, datetime.now().isoformat(timespec="seconds")
                return t.register_team(f.get("name", ""), players, list_seeking=f.get("list_seeking") == "1")

            team = act(f"Registration: {f.get('name', '').strip()}", register_team)
            if team:
                session[f"team_{team.id}"] = True
                flash(f"{team.name} is registered! Your team PIN is {team.pin} — keep it to edit your roster.", "ok")
                return redirect(url_for("team_page", team_id=team.id))
        players = [
            Player(name=request.form.get(f"player_name_{i}", ""), phone=request.form.get(f"player_phone_{i}", ""),
                   skill=int(request.form[f"player_skill_{i}"]) if request.form.get(f"player_skill_{i}", "").isdigit() else 0)
            for i in range(TEAM_SIZE)
        ]
        return render_template("register.html", t=t, form=request.form, players=players)

    @app.get("/favicon.ico")
    def favicon():
        # Browsers and search engines look for the icon here even without a <link> tag.
        return send_file(Path(app.static_folder) / "favicon.ico", mimetype="image/x-icon", max_age=86400)

    @app.get("/t/<event>/privacy")
    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html", t=current_t())

    @app.get("/t/<event>/terms")
    @app.get("/terms")
    def terms():
        return render_template("terms.html", t=current_t())

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/t/<event>/guide")
    @director_view  # the director's guide is for directors only
    def guide():
        return render_template("guide.html", t=store.load())

    @app.get("/t/<event>/team/<team_id>")
    def team_page(team_id):
        t = store.load()
        team = t.teams.get(team_id) or abort(404)
        history = [m for m in t.matches if team_id in (m.team_a, m.team_b) and m.status == DONE]
        return render_template(
            "team.html", t=t, team=team, match=t.current_match_for(team_id), history=history,
            is_team=bool(session.get(f"team_{team_id}") or session.get("admin")),
        )

    def team_match(team_id):
        """The signed-in team's match on a table, and which side it is."""
        if not (session.get(f"team_{team_id}") or session.get("admin")):
            raise TournamentError("Enter your team PIN first.")
        t = store.load()
        m = t.current_match_for(team_id)
        if m is None or m.status != CALLED:
            raise TournamentError("Your team isn't on a table right now.")
        return m, "a" if m.team_a == team_id else "b"

    @app.post("/t/<event>/team/<team_id>/login")
    def team_login(team_id):
        team = store.load().teams.get(team_id) or abort(404)
        check_pin(team)
        return redirect(url_for("team_page", team_id=team_id))

    @app.post("/t/<event>/team/<team_id>/texts")
    def team_texts(team_id):
        # Only someone signed in with the team PIN can opt in, for their own number (not the director).
        if not session.get(f"team_{team_id}"):
            flash("Enter your team PIN first.", "error")
            return redirect(url_for("team_page", team_id=team_id))
        index = request.form.get("player", "")
        if index.isdigit() and request.form.get("sms_consent") == "1":
            player = act(f"Text alerts on: {store.load().name_of(team_id)}", lambda t: t.opt_in(team_id, int(index)))
            if player:
                flash(f"Text alerts are on for {player.name}. A welcome text is on its way.", "ok")
        else:
            flash("Tick the box to agree to text alerts.", "error")
        return redirect(url_for("team_page", team_id=team_id))

    @app.post("/t/<event>/team/<team_id>/submit")
    def team_submit(team_id):
        try:
            m, _ = team_match(team_id)
        except TournamentError as exc:
            flash(str(exc), "error")
        else:
            if act("Submit result (team)", lambda t: t.submit_result(m.id)):
                flash("Result submitted. Only the tournament director can change it now.", "ok")
        return redirect(url_for("team_page", team_id=team_id))

    @app.post("/t/<event>/team/<team_id>/arrived")
    def team_arrived(team_id):
        try:
            m, side = team_match(team_id)
        except TournamentError as exc:
            flash(str(exc), "error")
        else:
            act("Check in (team)", lambda t: t.check_in(m.id, side), "Checked in. Good luck!")
        return redirect(url_for("team_page", team_id=team_id))

    @app.post("/t/<event>/team/<team_id>/score")
    def team_score(team_id):
        side, delta = request.form.get("side"), int(request.form.get("delta", "1"))
        if side not in ("a", "b") or delta not in (1, -1):
            abort(400)
        try:
            m, _ = team_match(team_id)
        except TournamentError as exc:
            flash(str(exc), "error")
        else:
            name = store.load().name_of(team_id)
            act("Score (team)", lambda t: t.score(m.id, side, delta, by=name))
        return redirect(url_for("team_page", team_id=team_id))

    @app.route("/t/<event>/team/<team_id>/roster", methods=["GET", "POST"])
    def roster(team_id):
        t = store.load()
        team = t.teams.get(team_id) or abort(404)
        allowed = session.get(f"team_{team_id}") or session.get("admin")
        if request.method == "POST" and not allowed:
            check_pin(team)
            return redirect(url_for("roster", team_id=team_id))
        if request.method == "POST":
            f = request.form
            solo = f.get("solo") == "1"
            same = {i for i in range(TEAM_SIZE) if f.get(f"same_person_{i}") == "1"}
            if act(
                f"Update roster: {store.load().name_of(team_id)}",
                lambda t: t.update_roster(
                    team_id,
                    players_from_form(f, None if session.get("admin") else t.teams[team_id], same)[:1 if solo else TEAM_SIZE],
                    name=f.get("name"), list_seeking=f.get("list_seeking") == "1", same_people=same),
                "Roster saved.",
            ):
                return redirect(url_for("team_page", team_id=team_id))
            team = store.load().teams[team_id]
        return render_template("roster.html", t=t, team=team, allowed=allowed)

    # -------------------------------------------------------------------- admin

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            ip, password = request.remote_addr or "?", request.form.get("password", "")
            device = device_id()
            wait = max(login_by_ip.wait(ip), login_by_device.wait(device))
            is_director = secrets.compare_digest(password, admin_password)
            is_viewer = bool(viewer_password) and secrets.compare_digest(password, viewer_password)
            if wait:
                flash(f"Too many wrong passwords. Try again in {minutes(wait)}.", "error")
            elif is_director or is_viewer:
                login_by_device.clear(device)
                session.pop("admin", None)
                session.pop("viewer", None)
                session["admin" if is_director else "viewer"] = True
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("director"))
            else:
                login_by_ip.fail(ip)
                login_by_device.fail(device)
                flash("Wrong password.", "error")
        return render_template("login.html", t=None, default_password=admin_password == "admin")

    @app.post("/admin/logout")
    def admin_logout():
        session.pop("admin", None)
        session.pop("viewer", None)
        return redirect(url_for("index"))

    @app.get("/t/<event>/admin")
    @director_view
    def admin():
        if session.get("admin"):  # the view-only login never changes anything (the background clock still runs)
            tick(g.event)
        t = store.load()
        return render_template(
            "admin.html", t=t, undo_label=store.last_undo_label(), undo_step=store.undo_depth(), server_time=datetime.now(),
            tz_name=os.environ.get("TZ", ""), texts=sms.recent(50), sms_enabled=sms.enabled, archives=store.archives(),
            bracket_text="\n".join(f"{top} = {chips}" for top, chips in t.settings.chip_brackets),
        )

    @app.post("/t/<event>/admin/settings")
    @admin_required
    def admin_settings():
        f = request.form

        def update(t):
            s = t.settings
            s.name = f.get("name", "").strip() or "Chip Tournament"
            date = f.get("date", "").strip()
            if date:
                try:
                    if not 2000 <= datetime.strptime(date, "%Y-%m-%d").year <= 2100:
                        raise ValueError
                except ValueError:
                    raise TournamentError("Enter a real tournament date.")
            s.date, s.start_time = date, f.get("start_time", "")
            s.venue, s.address, s.details = f.get("venue", ""), f.get("address", ""), f.get("details", "")
            s.cash_in_time, s.last_call_time = f.get("cash_in_time", ""), f.get("last_call_time", "")
            s.checkin_by = f.get("checkin_by", s.checkin_by).strip()
            if t.status == SETUP:
                s.require_checkin = f.get("require_checkin") == "1"
            s.sms_name = f.get("sms_name", s.sms_name).strip()
            s.theme = themes.theme_key(f.get("theme", s.theme))
            s.contact_phone = f.get("contact_phone", s.contact_phone).strip()
            s.rule_set = event_rule_set(f.get("rule_set", s.rule_set))
            s.event_rules = f.get("event_rules", s.event_rules).replace("\r\n", "\n").strip()
            s.registration_closed = f.get("registration_closed") == "1"
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
            live = [m for m in t.matches if m.status == CALLED and max(m.score_a, m.score_b) > int(race)]
            if live:
                raise TournamentError(f"Table {live[0].table} already has a team past {race} games, so the race can't "
                                      "be lowered that far while it's playing.")
            if not t.entries_locked and t.status != FINISHED:
                cap = f.get("registration_cap", "")
                if not cap.isdigit():
                    raise TournamentError("Registration cap must be a whole number.")
                t.set_money(parse_money(f.get("entry_fee", "0")), parse_money(f.get("added_money", "0")),
                            parse_chip_brackets(f.get("chip_brackets", "")), int(cap))
            s.race_to = int(race)
            for key in ("warmup_minutes", "checkin_minutes", "grace_minutes", "match_minutes",
                        "long_match_minutes", "catchup_matches"):
                value = f.get(key, str(getattr(s, key))).strip()
                if not value.isdigit():
                    raise TournamentError("Times and the catch-up gap must be whole numbers.")
                setattr(s, key, int(value))
            field_cap = f.get("field_cap", "0").strip() or "0"
            if not field_cap.isdigit():
                raise TournamentError("Field cap must be a whole number (0 for no limit).")
            t.set_field_cap(int(field_cap))
            if t.status == RUNNING:
                t._advance()  # new tables may be free now

        if act("Update settings", lambda t: update(t) or True, "Settings saved."):
            t = store.load()
            solos = t.solo_teams_in_field
            if t.status == SETUP and solos and t.partner_deadline_passed() and not t.partner_deadline_paused:
                flash(f"Heads up: with this date and start time, the partner deadline ({t.partner_deadline_text()}) has "
                      f"passed, so {', '.join(x.name for x in solos)} will move to the waiting list. "
                      "Press Undo to keep them in the field.", "error")
        return redirect(url_for("admin"))

    def back_to_settings() -> str:
        return url_for("admin") + "#settings" if g.get("event") else url_for("director")

    @app.post("/t/<event>/admin/background")
    @app.post("/admin/background")
    @admin_required
    def admin_background():
        if request.form.get("reset") == "1":
            for ext in ("jpg", "png", "webp"):
                (data_path / f"background.{ext}").unlink(missing_ok=True)
            flash("Using the default background (the Control Table glow).", "ok")
            return redirect(back_to_settings())
        upload = request.files.get("photo")
        data = upload.read() if upload and upload.filename else b""
        kind = next((v for magic, v in photo_types.items() if data.startswith(magic)), None)
        if not data or kind is None or (kind[0] == "webp" and data[8:12] != b"WEBP"):
            flash("Choose a JPG, PNG or WebP photo.", "error")
            return redirect(back_to_settings())
        for ext in ("jpg", "png", "webp"):
            (data_path / f"background.{ext}").unlink(missing_ok=True)
        (data_path / f"background.{kind[0]}").write_bytes(data)
        flash("Background photo updated.", "ok")
        return redirect(back_to_settings())

    # ------------------------------------------------------------------ rules

    @app.get("/t/<event>/find")
    def find_team():
        """"Find your team": open the picked team's page, where its PIN unlocks scoring."""
        team_id, t = request.args.get("team", ""), current_t()
        if g.event.kind == CHIP:
            return redirect(url_for("team_page", team_id=team_id) if team_id in t.teams else url_for("home"))
        return redirect(url_for("b_entry", entry_id=team_id) if team_id in t.entries else url_for("home"))

    @app.get("/t/<event>/rules")
    @app.get("/rules")
    def rules_page():
        """League-wide: every rule set. Inside an event: only the set that event plays by."""
        t, sets = current_t(), rule_book.load()
        if t is not None:
            sets = [r for r in sets if r["id"] == t.settings.rule_set]
        sets = [{**r, "html": rulebook.render(r["body"])} for r in sets]
        event_rules = rulebook.render(t.settings.event_rules) if t is not None and t.settings.event_rules else ""
        return render_template("rules.html", t=t, rule_sets=sets, event_rules=event_rules)

    def event_rule_set(rule_id: str) -> str:
        """A rule set id an event can use: one that exists, or "" for none chosen yet."""
        return rule_id if any(r["id"] == rule_id for r in rule_book.load()) else ""

    @app.get("/admin/rules")
    @admin_required
    def admin_rules():
        return render_template("admin_rules.html", t=None, rule_sets=rule_book.load())

    @app.post("/admin/rules/new")
    @admin_required
    def admin_rules_new():
        rule = rule_book.add(request.form.get("title", ""))
        flash(f"Added {rule['title']}. Paste the rules in below and save.", "ok")
        return redirect(url_for("admin_rules") + f"#edit-{rule['id']}")

    @app.post("/admin/rules/<rule_id>")
    @admin_required
    def admin_rules_save(rule_id):
        try:
            rule = rule_book.save(rule_id, request.form.get("title", ""), request.form.get("body", ""))
            flash(f"Saved {rule['title']}.", "ok")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_rules") + f"#edit-{rule_id}")

    @app.post("/admin/rules/<rule_id>/move")
    @admin_required
    def admin_rules_move(rule_id):
        rule_book.move(rule_id, -1 if request.form.get("dir") == "up" else 1)
        return redirect(url_for("admin_rules") + f"#edit-{rule_id}")

    @app.post("/admin/rules/<rule_id>/delete")
    @admin_required
    def admin_rules_delete(rule_id):
        rule = rule_book.get(rule_id)
        rule_book.delete(rule_id)
        flash(f"Deleted {rule['title'] if rule else 'the rule set'}.", "ok")
        return redirect(url_for("admin_rules"))

    import_file = LocalProxy(lambda: g.event.dir / "import.csv")

    @app.route("/t/<event>/admin/import", methods=["GET", "POST"])
    @admin_required
    def admin_import():
        t = store.load()
        if t.status != SETUP:
            flash("Teams can only be imported before the tournament starts.", "error")
            return redirect(url_for("admin"))
        if request.method == "GET":
            return render_template("import.html", t=t, fields=importer.FIELDS)

        step = request.form.get("step")
        if step == "upload":
            upload = request.files.get("file")
            text = upload.read().decode("utf-8-sig", errors="replace") if upload and upload.filename else ""
            text = text or request.form.get("csv_text", "")
            if not text.strip():
                flash("Choose a CSV file (or paste the rows) first.", "error")
                return redirect(url_for("admin_import"))
            import_file.write_text(text)
        elif not import_file.exists():
            flash("Upload the file again.", "error")
            return redirect(url_for("admin_import"))

        try:
            headers, raw_rows = importer.read_csv(import_file.read_text())
        except Exception as exc:  # a malformed file should never crash the page
            flash(f"Couldn't read that file: {exc}", "error")
            return redirect(url_for("admin_import"))

        if step == "upload":
            mapping = importer.guess_mapping(headers)
        else:
            mapping = {key: int(request.form[f"map_{key}"]) for key in importer.FIELDS
                       if request.form.get(f"map_{key}", "").isdigit()
                       and int(request.form[f"map_{key}"]) < len(headers)}
        rows = importer.build_rows(raw_rows, mapping)

        if step == "import":
            texts_on = request.form.get("send_texts") == "1"
            with store.change(f"Import {len(rows)} team(s) from CSV") as t:
                added, waitlisted = importer.apply(t, rows, send_texts=texts_on)
                send_texts(t)
            import_file.unlink(missing_ok=True)
            skipped = len(rows) - added - waitlisted
            flash(f"Imported {added} team(s) into the field"
                  f"{f', {waitlisted} onto the waiting list' if waitlisted else ''}"
                  f"{f'; {skipped} skipped (see below)' if skipped else ''}.", "ok" if not skipped else "error")
            return render_template("import.html", t=store.load(), fields=importer.FIELDS, done=True, rows=rows)

        added, waitlisted = importer.preview(t, rows)
        samples = raw_rows[0] if raw_rows else []
        return render_template(
            "import.html", t=t, fields=importer.FIELDS, headers=headers, mapping=mapping, rows=rows,
            samples=samples, added=added, waitlisted=waitlisted,
        )

    @app.post("/t/<event>/admin/start")
    @admin_required
    def admin_start():
        act("Start tournament", lambda t: t.start(), "Tournament started — round 1 is called.")
        return redirect(url_for("admin"))

    @app.post("/t/<event>/admin/match/<match_id>/score")
    @admin_required
    def admin_score(match_id):
        side, delta = request.form.get("side"), int(request.form.get("delta", "1"))
        if side not in ("a", "b") or delta not in (1, -1):
            abort(400)
        act("Score", lambda t: t.score(match_id, side, delta))
        return redirect(url_for("admin") + "#matches")

    @app.post("/t/<event>/admin/match/<match_id>/arrived")
    @admin_required
    def admin_arrived(match_id):
        side = request.form.get("side")
        if side not in ("a", "b"):
            abort(400)
        act("Check in", lambda t: t.check_in(match_id, side))
        return redirect(url_for("admin") + "#matches")

    @app.post("/t/<event>/admin/team/<team_id>/call-next")
    @admin_required
    def admin_call_next(team_id):
        name = store.load().name_of(team_id)
        act("Call next", lambda t: t.call_next(team_id),
            f"{name} is first in line" + " (called to a table if one was free).")
        return redirect(url_for("admin") + "#matches")

    @app.post("/t/<event>/admin/match/<match_id>/winner")
    @admin_required
    def admin_winner(match_id):
        act("Record result", lambda t: t.record_result(match_id, request.form.get("winner", "")), "Result recorded.")
        return redirect(url_for("admin") + "#matches")

    @app.post("/t/<event>/admin/match/<match_id>/submit")
    @admin_required
    def admin_submit(match_id):
        act("Submit result", lambda t: t.submit_result(match_id), "Result recorded.")
        return redirect(url_for("admin") + "#matches")

    @app.post("/t/<event>/admin/match/<match_id>/fix")
    @admin_required
    def admin_fix(match_id):
        act("Fix result", lambda t: t.fix_result(match_id),
            "Result corrected on match #{result.round}. Both teams were texted.")
        return redirect(url_for("admin") + "#matches")

    @app.post("/t/<event>/admin/cash-in/open")
    @admin_required
    def admin_open_cash_in():
        act("Open cash-in", lambda t: t.open_cash_in(), "Cash-in is open and teams were texted.")
        return redirect(url_for("admin"))

    @app.post("/t/<event>/admin/team/<team_id>/cash-in")
    @admin_required
    def admin_cash_in(team_id):
        act("Cash in team", lambda t: money(t.cash_in(team_id)), "Cashed in — pay out {result}.")
        return redirect(url_for("admin") + "#teams")

    @app.post("/t/<event>/admin/last-call")
    @admin_required
    def admin_last_call():
        act("Last call", lambda t: t.call_last_call(),
            "Last call is on and teams were texted. Each team is cashed in automatically after its last match.")
        return redirect(url_for("admin"))

    @app.post("/t/<event>/admin/end-now")
    @admin_required
    def admin_end_now():
        act("Cash everyone out now", lambda t: t.cash_out_everyone(),
            "Tournament ended — every team has been cashed in. See payouts below.")
        return redirect(url_for("admin"))

    @app.post("/t/<event>/admin/team/<team_id>/adjust")
    @admin_required
    def admin_adjust(team_id):
        try:
            change = int(request.form.get("change", "0"))
        except ValueError:
            change = 0
        act("Adjust chips", lambda t: t.adjust_chips(team_id, change, request.form.get("reason", "")), "Chips adjusted.")
        return redirect(url_for("admin") + "#teams")

    @app.post("/t/<event>/admin/team/<team_id>/check-in")
    @admin_required
    def admin_team_check_in(team_id):
        act(f"Check in {store.load().name_of(team_id)}", lambda t: t.check_in_team(team_id))
        return redirect(url_for("admin") + "#teams")

    @app.post("/t/<event>/admin/team/<team_id>/no-show")
    @admin_required
    def admin_no_show(team_id):
        act(f"Mark no-show: {store.load().name_of(team_id)}", lambda t: t.mark_no_show(team_id), "Marked as a no-show.")
        return redirect(url_for("admin") + "#teams")

    @app.post("/t/<event>/admin/team/<team_id>/restore")
    @admin_required
    def admin_restore(team_id):
        act(f"Restore: {store.load().name_of(team_id)}", lambda t: t.restore_team(team_id), "Team restored.")
        return redirect(url_for("admin") + "#teams")

    @app.post("/t/<event>/admin/no-show-rest")
    @admin_required
    def admin_no_show_rest():
        act("Mark remaining teams no-show", lambda t: t.no_show_everyone_not_checked_in(),
            "Marked {result} team(s) as no-shows.")
        return redirect(url_for("admin") + "#teams")

    @app.post("/t/<event>/admin/team/<team_id>/remove")
    @admin_required
    def admin_remove(team_id):
        act(f"Remove: {store.load().name_of(team_id)}", lambda t: t.remove_team(team_id), "Team removed.")
        return redirect(url_for("admin") + "#teams")

    @app.post("/t/<event>/admin/broadcast")
    @admin_required
    def admin_broadcast():
        body = request.form.get("body", "").strip()
        if body:
            act("Broadcast text", lambda t: t.broadcast(body), "Queued {result} text(s).")
        return redirect(url_for("admin") + "#texts")

    @app.post("/t/<event>/admin/text-pins")
    @admin_required
    def admin_text_pins():
        result = act("Resend PINs", lambda t: t.text_pins())
        if result is not None:
            sent, unreachable = result
            flash(f"Queued {sent} PIN text(s)."
                  + (f" {unreachable} team(s) have nobody signed up for texts." if unreachable else ""), "ok")
        return redirect(url_for("admin") + "#texts")

    @app.post("/t/<event>/admin/undo")
    @admin_required
    def admin_undo():
        with store.lock:
            expected = request.form.get("step", "")
            if expected and expected != str(store.undo_depth()):
                latest = store.last_undo_label()
                flash(f"Nothing was undone: something else happened since you looked ({latest or 'nothing to undo'} is "
                      "now the latest action). Check the Undo button and press it again if that's what you want.", "error")
                return redirect(url_for("admin"))
            label = store.undo()
            t = store.load()
            paused = t.pause_overdue_forfeits()
            deadline_paused = t.pause_partner_deadline()
            if paused or deadline_paused:
                store.save(t)
        flash(f"Undid: {label}." if label else "Nothing to undo.", "ok" if label else "error")
        if paused:
            flash(f"Auto-forfeit is paused on {paused} match(es) that are past their deadline. "
                  "Tap Arrived, or pick the Winner to record the forfeit yourself.", "ok")
        if deadline_paused:
            flash(f"The partner deadline is paused: {deadline_paused} team(s) without a partner stay in the field. "
                  "Use \"Move them to the waiting list now\" at the top when you're ready.", "ok")
        return redirect(url_for("admin"))

    @app.post("/t/<event>/admin/partner-deadline")
    @admin_required
    def admin_partner_deadline():
        names = ", ".join(x.name for x in store.load().solo_teams_in_field)
        act(f"Partner deadline: moved {names} to the waiting list",
            lambda t: t.enforce_partner_deadline(force=True), "Moved {result} team(s) to the waiting list.")
        return redirect(url_for("admin"))

    @app.post("/t/<event>/admin/team/<team_id>/pair")
    @admin_required
    def admin_pair(team_id):
        act(f"Pair players: {store.load().name_of(team_id)}", lambda t: t.pair_solo_teams(team_id, request.form.get("other", "")),
            "Paired: {result.partners} are {result.name}.")
        return redirect(url_for("admin") + "#teams")

    @app.post("/t/<event>/admin/reset")
    @admin_required
    def admin_reset():
        if request.form.get("confirm", "").strip().upper() != "RESET":
            flash("Type RESET to confirm.", "error")
        elif request.form.get("mode") == "delete":
            store.delete_and_reset()
            flash("Started a new tournament. The old one, its rosters and its texts were permanently deleted.", "ok")
        else:
            store.archive_and_reset()
            flash("Started a new tournament. The old one is saved under Past tournaments.", "ok")
        return redirect(url_for("admin"))

    @app.post("/t/<event>/admin/archives/delete")
    @admin_required
    def admin_delete_archives():
        stamps = [a["stamp"] for a in store.archives()] if request.form.get("all") == "1" else [request.form.get("stamp", "")]
        deleted = sum(store.delete_archive(stamp) for stamp in stamps)
        flash(f"Permanently deleted {deleted} past tournament(s)." if deleted else "Nothing to delete.",
              "ok" if deleted else "error")
        return redirect(url_for("admin") + "#past")

    # ------------------------------------------------------------- the hub

    @app.get("/")
    def index():
        return render_template("hub.html", t=None, rows=hub.summaries())

    @app.get("/admin")
    @director_view
    def director():
        return render_template("hub_admin.html", t=None, rows=hub.summaries(), formats=FORMATS,
                               final_cuts=FINAL_CUTS, today=datetime.now().strftime("%Y-%m-%d"))

    @app.post("/admin/new")
    @admin_required
    def new_event():
        f = request.form
        kind = f.get("kind", "")
        settings = {"date": f.get("date", "").strip(), "start_time": f.get("start_time", "").strip(),
                    "venue": f.get("venue", "").strip()}
        if kind in dict(FORMATS):
            settings["format"], kind = kind, BRACKET
        elif kind != CHIP:
            flash("Pick a tournament type.", "error")
            return redirect(url_for("director"))
        try:
            ev = hub.create(f.get("name", ""), kind, **settings)
        except TournamentError as exc:
            flash(str(exc), "error")
            return redirect(url_for("director"))
        flash(f"Created {f.get('name', '').strip()}. Finish its settings below, then share the link.", "ok")
        return redirect(url_for("admin", event=ev.id) + "#settings")

    @app.post("/admin/event/<event_id>/delete")
    @admin_required
    def delete_event(event_id):
        ev = hub.get(event_id) or abort(404)
        name = ev.load().settings.name
        if request.form.get("confirm", "").strip().upper() != "DELETE":
            flash("Type DELETE to confirm.", "error")
        elif hub.delete(event_id):
            flash(f"Deleted {name}, its rosters and its texts.", "ok")
        return redirect(url_for("director"))

    # Links from before Control Table hosted several tournaments (old texts, bookmarks, the TV).
    def only_live_event():
        """Where an old /board or /register link should go: the one tournament that's on now."""
        rows = [r for r in hub.summaries() if r["t"].status != FINISHED]
        today = datetime.now().strftime("%Y-%m-%d")
        for pick in (
            [r for r in rows if r["t"].status == RUNNING],                   # under way
            [r for r in rows if r["t"].settings.date == today],              # on today
            [r for r in rows if hub.is_migrated(r["event"].id)],             # the original tournament
            rows,                                                            # the only one there is
        ):
            if len(pick) == 1:
                return pick[0]["event"]
        return None

    @app.get("/board")
    @app.get("/register")
    @app.get("/guide")
    def legacy_page():
        ev = only_live_event()
        if ev is None:
            return redirect(url_for("index"))
        return redirect(f"/t/{ev.id}{request.path}")

    @app.get("/team/<team_id>")
    @app.get("/team/<team_id>/roster")
    def legacy_team(team_id):
        ev = hub.find_team(team_id) or abort(404)
        if ev.kind != CHIP:  # a bracket entry has its own page
            return redirect(f"/t/{ev.id}/entry/{team_id}")
        return redirect(f"/t/{ev.id}{request.path}")

    # Pages every tournament type shares; the rest belong to chip tournaments, and a bracket
    # tournament gets its own version (or a 404) from bracket_web.
    shared = {"rules_page", "terms", "privacy", "admin_background", "admin_reset", "admin_delete_archives",
              "admin_broadcast", "admin_text_pins", "find_team"}
    chip_only = {r.endpoint for r in app.url_map.iter_rules() if "event" in r.arguments} - shared
    bracket_views = bracket_web.register(app, {
        "act": act, "store": store, "sms": sms, "director_view": director_view, "admin_required": admin_required,
        "parse_money": parse_money, "check_pin": check_pin, "minutes": minutes, "event_rule_set": event_rule_set,
    })

    @app.before_request
    def dispatch_by_kind():
        ev = g.get("event")
        endpoint = request.endpoint or ""
        if ev is None:
            return None
        if ev.kind == BRACKET and endpoint in chip_only:
            view = bracket_views.get(endpoint)
            return view() if view else abort(404)
        if ev.kind == CHIP and endpoint.startswith("b_"):
            abort(404)
        return None

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    create_app().run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
