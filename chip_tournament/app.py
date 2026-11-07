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

import contextlib
import json
import copy
import os
import re
import secrets
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import quote_plus

from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from markupsafe import Markup, escape
from werkzeug.local import LocalProxy
from werkzeug.middleware.proxy_fix import ProxyFix

import bracket_web
import guest_web
import photos_web
import qr_web
import cal_web
import event_pages
import player_web
from chips import alerts, apa_rules, backup, bonus_event, guest_td, eligibility, event_info, export, importer, players, pool as pool_mod, rules as rulebook, schedule, skill_check, themes
from chips.bracket import FINAL_CUTS, FORMATS, asks_two_levels
from chips.hub import BRACKET, CHIP, Hub
from chips.engine import (
    ACTIVE,
    CALLED,
    CONTINUOUS,
    check_form_entry,
    DIRECTOR_CALLS,
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


def day(value) -> str:
    """'2026-11-01T03:02:00' -> 'Sun, Nov 1'."""
    if not value:
        return ""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return f"{dt:%a, %b} {dt.day}"


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
    check_form_entry(form, TEAM_SIZE)
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
    search_settings = rulebook.SearchSettings(data_path)
    app.config["search_settings"] = search_settings
    app.config["apa_link_fetch"] = None  # tests put a stand-in here; None means the real website
    link_check_running = threading.Lock()
    guests = guest_td.GuestTDs(data_path)  # division reps who run one event of their own
    app.config["guests"] = guests
    player_store = players.PlayerStore(data_path)  # every APA player, kept apart from the tournaments
    pools = pool_mod.Pools(data_path)  # shared tables (Mini-Mania)
    app.config["pools"] = pools
    app.config["players"] = player_store

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
        # Behind a CDN (Render's edge, or Cloudflare in front of it), the address ProxyFix finds can be the CDN's,
        # which would make every visitor share one set of login limits. TRUSTED_IP_HEADER names the header the
        # CDN sets to the real visitor's address (Cloudflare: CF-Connecting-IP). Only set it when every request
        # really comes through that CDN, since anyone can send the header to a server reached directly.
        ip_header = os.environ.get("TRUSTED_IP_HEADER", "").strip()
        if ip_header:
            inner, key = app.wsgi_app, "HTTP_" + ip_header.upper().replace("-", "_")

            def real_ip(environ, start_response):
                value = environ.get(key, "").split(",")[0].strip()
                if value:
                    environ["REMOTE_ADDR"] = value
                return inner(environ, start_response)
            app.wsgi_app = real_ip  # runs after ProxyFix (wrapped inside it), so it has the last word
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config["hub"] = hub
    app.config["data_dir"] = data_path

    app.jinja_env.filters["money"] = money

    def with_links(text) -> Markup:
        """A message with its web addresses as links: APA's join page as a JOIN button, others by name.
        The message itself keeps the plain address, so it still reads right in a text or the log."""
        from chips.eligibility import JOIN_URL
        out, last = [], 0
        s = str(text or "")
        for m in re.finditer(r"https?://[^\s<>\"')]+[^\s<>\"').,;:!?]", s):
            out.append(escape(s[last:m.start()]))
            url = m.group(0)
            if url.rstrip("/") == JOIN_URL.rstrip("/"):
                out.append(Markup('<a class="button small warning join-link" href="{}" target="_blank" rel="noopener">JOIN</a>').format(url))
            else:
                label = re.sub(r"^https?://", "", url).rstrip("/").removesuffix("/home")
                out.append(Markup('<a href="{}" target="_blank" rel="noopener">{}</a>').format(url, label))
            last = m.end()
        out.append(escape(s[last:]))
        return Markup("").join(out)

    app.jinja_env.filters["with_links"] = with_links
    app.jinja_env.filters["urlquote"] = quote_plus
    app.jinja_env.filters["clock"] = clock
    app.jinja_env.filters["day"] = day
    app.jinja_env.filters["last4"] = last4
    app.jinja_env.globals["event_when"] = event_when
    app.jinja_env.globals["reg_window"] = schedule.reg_window
    app.jinja_env.globals.update(event_status=event_info.status, event_fee=event_info.fee, event_spots=event_info.spots,
                                 skill_rule=event_info.skill_rule, skill_short=event_info.skill_short,
                                 event_can_register=event_info.can_register, event_register_label=event_info.register_label,
                                 event_tabs=event_info.tabs, event_alertable=event_info.can_alert, event_tag_line=event_info.tag_line, event_tone=event_info.calendar_tone, event_weekday=event_info.weekday, event_filter_data=event_info.filter_data, event_fill=event_info.fill, event_play=event_info.play_phrase,
                                 event_access=event_info.access_phrase, event_skill=event_info.skill_phrase,
                                 event_wants_partner=event_info.partner_link_wanted, event_categories=event_info.categories, event_games=lambda: event_info.GAMES, short_when=lambda t: event_info.short_when(t.settings))
    app.jinja_env.globals["reg_when"] = schedule.when_text
    app.jinja_env.globals["reg_shut_message"] = schedule.shut_message
    app.jinja_env.globals["now"] = datetime.now
    app.jinja_env.globals["skill_for"] = players.skill_for
    app.jinja_env.globals["skill_bases"] = players.SKILL_BASES
    app.jinja_env.globals["level_rule"] = players.level_rule

    def public_url(path: str) -> str:
        """A link to share outside the app: on the SITE_URL domain (e.g. https://controltable.app) when set."""
        root = os.environ.get("SITE_URL", "").strip().rstrip("/") or request.url_root.rstrip("/")
        return root + path
    app.jinja_env.globals["public_url"] = public_url
    app.jinja_env.globals["elig_rules"] = eligibility.rules_of
    app.jinja_env.globals["elig_describe"] = eligibility.describe
    app.jinja_env.globals["elig_genders"] = eligibility.GENDERS
    app.jinja_env.globals["session_start"] = lambda: player_store.config()["session_start"]
    app.jinja_env.globals["player_columns"] = lambda: player_store.extra_columns()
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
        "updated": "November 2, 2026",
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
            "is_guest_td": guest_here(),
            "guest_listed": guest_member() is not None,
            "guest_pending": guest_here() and guest_pending(),
            "hide_phones": (bool(session.get("viewer")) and not session.get("admin")) or guest_here(),
            "ACTIVE": ACTIVE,
            "CALLED": CALLED,
            "WAITING": WAITING,
            "CONTINUOUS": CONTINUOUS,
            "DIRECTOR_CALLS": DIRECTOR_CALLS,
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
            test = t.settings.test_event  # a test event never texts anyone
            threading.Thread(
                target=lambda: [sender.send(m.to, m.body, test=test) for m in messages], daemon=True
            ).start()

    def act(label: str, fn, success: str | None = None):
        """Apply one change to the tournament; show rule errors instead of crashing."""
        pool = pools.of_event(g.event.id)
        try:
            # Events sharing tables change one at a time, so two can never take the same table.
            with pool_mod.lock_for(pool["id"]) if pool else contextlib.nullcontext():
                with store.change(label) as t:
                    # For team-page links in texts: the SITE_URL setting (e.g. https://controltable.app) when set,
                    # else whatever address the director is using.
                    root = os.environ.get("SITE_URL", "").strip().rstrip("/") or request.url_root.rstrip("/")
                    t.site_url = f"{root}/t/{g.event.id}"
                    pool_mod.inform(t, hub, pool, g.event.id)
                    result = fn(t)
                    send_texts(t)
                if pool:
                    share_freed_tables(pool, g.event.id)
            if success:
                flash(success.format(result=result), "ok")
            return result
        except TournamentError as exc:
            flash(str(exc), "error")
            return None

    def text_in_background(ev, t) -> None:
        """Send the texts a change produced on a background thread, so a slow text service never holds up the
        page or the locks (shared tables lock every event in the group while one changes)."""
        messages, t.outbox = list(t.outbox), []
        if messages:
            sender, test = ev.sms, t.settings.test_event
            threading.Thread(target=lambda: [sender.send(m.to, m.body, test=test) for m in messages],
                             daemon=True).start()

    def share_freed_tables(pool: dict, done_event: str) -> None:
        """After a change in one pooled event, the others get a turn at any table that's free now."""
        for other in pool["events"]:
            ev = hub.get(other)
            if other == done_event or ev is None:
                continue
            probe = ev.load()
            pool_mod.inform(probe, hub, pool, other)
            if not (pool_mod.waiting_for_table(probe) and probe.free_tables()):
                continue
            with ev.store.change("Shared tables: a table freed up") as t:
                pool_mod.inform(t, hub, pool, other)
                pool_mod.give_tables(t)
                text_in_background(ev, t)

    app.config["share_freed_tables"] = share_freed_tables

    def tick(ev) -> None:
        """Apply the partner deadline when it's due (nothing runs on its own once play starts)."""
        if ev.kind != CHIP:
            return
        store, sms = ev.store, ev.sms  # noqa: F841 (runs outside a request, so not the proxies)

        def send_texts(t) -> None:
            messages, t.outbox = list(t.outbox), []
            if messages:
                test = t.settings.test_event
                threading.Thread(target=lambda: [sms.send(m.to, m.body, test=test) for m in messages],
                                 daemon=True).start()

        probe = store.load()
        solos = [x.name for x in probe.solo_teams_in_field]
        if probe.check_clock() == 0:  # cheap check first, so idle ticks don't fill the undo history
            return
        label = f"Partner deadline: moved {', '.join(solos)} to the waiting list"
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

    # Public, read-only pages a league website may show in a frame: the lists, boards and rules. Every other page
    # (logins, registration, PIN pages, the director's) refuses to be framed by another site.
    embeddable = re.compile(r"^/(?:$|board$|rules$|pool/[^/]+$|static/|t/[^/]+/(?:$|board$|rules$))")
    production = os.environ.get("CHIPS_PRODUCTION") == "1"

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if not embeddable.match(request.path):
            resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
            resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
        if production:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=15552000")  # 180 days: always HTTPS
        return resp

    @app.before_request
    def start_clock():
        # Started on the first request so it runs in the serving process only.
        if not app.testing and not clock_started.is_set():
            clock_started.set()
            threading.Thread(target=clock_loop, daemon=True).start()
            threading.Thread(target=backup.nightly_loop, args=(data_path, app.logger.error), daemon=True).start()
            threading.Thread(target=players.sync_loop, args=(player_store, app.logger.error), daemon=True).start()
            threading.Thread(target=app.config["eligibility_lock_loop"], args=(app.logger.error,), daemon=True).start()

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

    # ---- guest TDs: a division rep running their own event (see chips/guest_td.py)

    def guest_member() -> str | None:
        """The signed-in player's member number if they're on the guest TD list."""
        p = g.get("player")
        return p["member"] if p and guests.guest(p["member"]) else None

    def guest_fresh() -> bool:
        return time.time() - (g.get("login_at") or 0) < guest_td.FRESH_HOURS * 3600

    def guest_here() -> bool:
        """A guest TD may run the event in view: theirs, agreement accepted, signed in recently, not finished."""
        if "guest_here" not in g:
            member, ev = guest_member(), g.get("event")
            g.guest_here = bool(member and ev and not session.get("admin") and guest_fresh()
                                and guests.can_run(member, ev.id) and ev.load().status != FINISHED)
        return g.guest_here

    def director_here() -> bool:
        """The league director, or the guest TD of the event in view."""
        return bool(session.get("admin")) or guest_here()

    def guest_pending() -> bool:
        """A guest TD's event that the league office hasn't approved yet."""
        ev = g.get("event")
        rec = guests.event(ev.id) if ev else None
        return bool(rec and rec["status"] == guest_td.PENDING)

    def guest_locked(s) -> dict:
        """The settings a guest TD can't change, as they were before a settings save."""
        return {"push_to": s.push_to, "require_member": s.require_member,
                "eligibility": copy.deepcopy(getattr(s, "eligibility", None))}

    def guest_lock_settings(s, before: dict) -> None:
        """What a guest TD can't change in their event's settings: who can play (eligibility and member
        numbers) and pushing players into another event are the league office's call."""
        if not guest_here():
            return
        s.push_to, s.require_member, s.eligibility = before["push_to"], before["require_member"], before["eligibility"]
        if guest_pending():  # hidden, no texts, registration closed until approved
            s.test_event, s.registration_closed = True, True

    def broadcast_reach() -> int:
        """How many phones "text everyone" reaches now: opted-in players, minus anyone who replied STOP."""
        ev = g.get("event")
        if ev is None:
            return 0
        t = ev.load()
        if ev.kind == CHIP:
            groups = t.active_teams if t.status == RUNNING else list(t.teams.values())
        else:
            groups = [e for e in t.entries.values() if e.status != "no_show"]
        blocked = hub.optouts.all()
        return len({alerts.normalize_phone(n) for grp in groups for n in grp.phones} - set(blocked) - {""})

    app.jinja_env.globals["broadcast_reach"] = broadcast_reach
    app.jinja_env.globals["sms_optouts"] = lambda: sorted(hub.optouts.all().items(), key=lambda kv: kv[1]["at"], reverse=True)
    app.jinja_env.globals["guest_limits"] = {"chars": guest_td.GUEST_BROADCAST_CHARS, "per_day": guest_td.GUEST_BROADCASTS_PER_DAY}
    app.config["director_here"] = director_here
    app.jinja_env.globals["broadcasts"] = lambda: guest_td.BroadcastLog(g.event.dir).all() if g.get("event") else []
    app.config["guest_here"] = guest_here

    def guest_refused():
        """A guest TD on a page they can't use: say why, and send them somewhere useful."""
        if g.get("event") and guest_member() and request.endpoint in guest_td.GUEST_BLOCKED and guest_here():
            flash("That's for the league office only.", "error")
            return redirect(url_for("admin"))
        if guest_member():
            if not guest_fresh():
                flash("For safety, sign in again to keep running your event.", "error")
            else:
                flash("Guest TD access covers only your own event, while it's running.", "error")
            return redirect(url_for("guest_home"))
        return None

    def admin_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("admin") and guest_here() and request.endpoint not in guest_td.GUEST_BLOCKED:
                return view(*args, **kwargs)
            if not session.get("admin"):
                if (stop := guest_refused()) is not None:
                    return stop
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
                if guest_here() and request.endpoint not in guest_td.GUEST_BLOCKED:
                    return view(*args, **kwargs)
                if (stop := guest_refused()) is not None:
                    return stop
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

    def scorer_device() -> str:
        """This phone, for scorekeeping: the browser's own token (not its network, which can change)."""
        if "device" not in session:
            session["device"] = secrets.token_hex(8)
        return session["device"]

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
        return app.config["event_tab_page"]()

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
                if t.settings.registration_shut:
                    raise TournamentError(schedule.shut_message(t.settings))
                if f.get("agree_terms") != "1":
                    raise TournamentError("Please agree to the Terms & Conditions and Privacy Policy to register.")
                players = app.config["attach_members"](f, players_from_form(f)[:1 if solo else TEAM_SIZE], t)
                if f.get("sms_consent") == "1" and players[0].phone:  # the person registering, for their own number
                    players[0].sms_consent, players[0].sms_consent_at = True, datetime.now().isoformat(timespec="seconds")
                return t.register_team(f.get("name", ""), players, list_seeking=f.get("list_seeking") == "1")

            team = act(f"Registration: {f.get('name', '').strip()}", register_team)
            if team:
                app.config["ask_partners"](g.event, team.id)
                session[f"team_{team.id}"] = True
                flash(f"{team.name} is registered! Your team PIN is {team.pin} — keep it to edit your roster.", "ok")
                return redirect(url_for("team_page", team_id=team.id))
        players = [
            Player(name=request.form.get(f"player_name_{i}", ""), phone=request.form.get(f"player_phone_{i}", ""),
                   skill=int(request.form[f"player_skill_{i}"]) if request.form.get(f"player_skill_{i}", "").isdigit() else 0,
                   member=request.form.get(f"player_member_{i}", ""))
            for i in range(TEAM_SIZE)
        ]
        if request.method == "GET" and g.get("player"):  # logged in: Partner 1 is them
            me = app.jinja_env.globals["my_prefill"](t)
            players[0] = Player(name=me["name"], phone=me["phone"], skill=me["skill"] or 0, member=me["member"])
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

    @app.get("/admin/whoami")
    @admin_required
    def admin_whoami():
        """For the director: the address the login limits see for you, and what the host sent, to check the
        TRUSTED_IP_HEADER setting. Only your own request's details."""
        heads = ("X-Forwarded-For", "CF-Connecting-IP", "True-Client-IP", "X-Real-IP")
        return {"login_limits_see_you_as": request.remote_addr,
                "trusted_ip_header": os.environ.get("TRUSTED_IP_HEADER", "") or "(not set)",
                "headers": {h: request.headers.get(h, "") for h in heads}}

    @app.post("/sms/inbound")
    def sms_inbound():
        """Twilio posts every text a player sends to our number here. STOP words put the number on the
        league-wide opt-out list; START words take it off. Twilio sends the standard confirmation reply."""
        token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        # Twilio signs the exact address it was given, which may be the onrender.com one or the site's own
        # domain (SITE_URL). Try each address this request could have been sent to; a forged request still
        # fails every one, since only Twilio has the auth token.
        path = request.full_path.rstrip("?")
        site = os.environ.get("SITE_URL", "").strip().rstrip("/")
        here = request.url_root.rstrip("/")
        candidates = {here + path, here.replace("http://", "https://", 1) + path}
        if site:
            candidates.add(site + path)
        params, signature = request.form.to_dict(), request.headers.get("X-Twilio-Signature", "")
        if not any(alerts.valid_twilio_signature(token, url, params, signature) for url in candidates):
            app.logger.warning("SMS reply rejected: Twilio signature didn't match %s%s", sorted(candidates),
                               "" if token else " (TWILIO_AUTH_TOKEN isn't set)")
            return "Forbidden", 403
        hub.optouts.handle_reply(request.form.get("From", ""), request.form.get("Body", ""))
        return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>', 200, {"Content-Type": "text/xml"}

    @app.get("/healthz")
    def healthz():
        """Up and answering. Lists any event file that couldn't be read at the last scan (every minute), but still
        answers ok: one bad file shouldn't make the host restart the whole site over and over."""
        out = {"ok": True}
        if hub.unreadable:
            out["unreadable_events"] = sorted(hub.unreadable)
        return out

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
            is_team=bool(session.get(f"team_{team_id}") or director_here()), my_device=scorer_device(),
        )

    def team_match(team_id):
        """The signed-in team's match on a table, and which side it is."""
        if not (session.get(f"team_{team_id}") or director_here()):
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

    # ------------------------------------------------- skill level check (chips and brackets)

    def two_levels(t) -> bool:
        s = t.settings
        return g.event.kind == BRACKET and asks_two_levels(s)

    @app.get("/t/<event>/admin/skill-check")
    @director_view
    def admin_skill_check():
        t = store.load()
        groups = skill_check.check_event(t, player_store, two_levels(t),
                                         lambda m: app.config["pushed_in"](g.event.id, t, m))
        counts = {}
        for grp in groups:
            for r in grp["players"]:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
        return render_template("skill_check.html", t=t, groups=groups, counts=counts, labels=skill_check.LABELS,
                               two=two_levels(t), has_list=bool(player_store.records()),
                               can_fix=(t.status == SETUP))

    @app.post("/t/<event>/admin/skill-check")
    @admin_required
    def admin_skill_check_fix():
        t = store.load()
        two = two_levels(t)
        want = request.form.get("id", "")
        fixed, failed = 0, []
        for grp in skill_check.check_event(t, player_store, two):
            if not grp["fixable"] or (want != "all" and grp["id"] != want):
                continue
            people = t.teams if g.event.kind == CHIP else t.entries
            new = skill_check.fixed_players(people[grp["id"]], grp["players"], two, getattr(t.settings, "skill_game", "8"))
            label = f"Skill check: {grp['name']} set to the player list's levels"

            def apply(tt, gid=grp["id"], new=new):
                return tt.update_roster(gid, new) if g.event.kind == CHIP else tt.update_entry(gid, None, new)

            try:
                with store.change(label) as tt:
                    apply(tt)
                fixed += 1
            except TournamentError as exc:
                failed.append(f"{grp['name']}: {exc}")
        if fixed:
            noun = ("team" if fixed == 1 else "teams") if g.event.kind == CHIP else ("entry" if fixed == 1 else "entries")
            flash(f"Updated {fixed} {noun} to the player list's skill levels.", "ok")
        for msg in failed:
            flash(f"Couldn't update {msg}", "error")
        if not fixed and not failed:
            flash("Nothing to update.", "ok")
        return redirect(url_for("admin_skill_check"))

    @app.post("/t/<event>/team/<team_id>/unregister")
    def team_unregister(team_id):
        """The team takes itself out before the start (signed in with the PIN, or a logged-in teammate)."""
        if not session.get(f"team_{team_id}"):
            flash("Enter your team PIN first.", "error")
            return redirect(url_for("team_page", team_id=team_id))
        if request.form.get("confirm") != "1":
            flash("Tick the box to confirm you want to unregister.", "error")
            return redirect(url_for("team_page", team_id=team_id) + "#unregister")
        name = act(f"Unregistered: {store.load().name_of(team_id)}", lambda t: t.unregister(team_id))
        if not name:
            return redirect(url_for("team_page", team_id=team_id))
        session.pop(f"team_{team_id}", None)
        flash(f"{name} is unregistered. If you change your mind, register again while there's room.", "ok")
        return redirect(url_for("home"))

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
            if act("Send result (scorekeeper)", lambda t: t.send_result(m.id, scorer_device())):
                flash("Result sent to the director. It counts once the director approves it.", "ok")
        return redirect(url_for("team_page", team_id=team_id))

    @app.post("/t/<event>/team/<team_id>/keep-score")
    def team_keep_score(team_id):
        try:
            m, _ = team_match(team_id)
        except TournamentError as exc:
            flash(str(exc), "error")
        else:
            player = request.form.get("player", "")
            label = "Take over scoring" if m.scorekeeper_device else "Keep score"
            if act(f"{label} (team)", lambda t: t.claim_scorekeeper(m.id, team_id, player, scorer_device())):
                flash("You're keeping score for this match. Tap + each time a team wins a rack.", "ok")
        return redirect(url_for("team_page", team_id=team_id))

    @app.post("/t/<event>/team/<team_id>/score")
    def team_score(team_id):
        side, delta = request.form.get("side"), request.form.get("delta", "1")
        if side not in ("a", "b") or delta not in ("1", "-1"):
            abort(400)
        delta = int(delta)
        try:
            m, _ = team_match(team_id)
        except TournamentError as exc:
            flash(str(exc), "error")
        else:
            name = store.load().name_of(team_id)
            act("Score (scorekeeper)", lambda t: t.score(m.id, side, delta, by=name, device=scorer_device()))
        return redirect(url_for("team_page", team_id=team_id))

    @app.route("/t/<event>/team/<team_id>/roster", methods=["GET", "POST"])
    def roster(team_id):
        t = store.load()
        team = t.teams.get(team_id) or abort(404)
        allowed = session.get(f"team_{team_id}") or director_here()
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
                    app.config["attach_members"](f, players_from_form(
                        f, None if session.get("admin") else t.teams[team_id], same)[:1 if solo else TEAM_SIZE],
                        t, t.teams[team_id].players),
                    name=f.get("name"), list_seeking=f.get("list_seeking") == "1", same_people=same),
                "Roster saved.",
            ):
                app.config["ask_partners"](g.event, team_id)
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
            if f.get("reminders_shown"):
                s.reminders, s.reminder_texts = f.get("reminders") == "1", f.get("reminder_texts") == "1"
            s.theme = themes.theme_key(f.get("theme", s.theme))
            s.contact_phone = f.get("contact_phone", s.contact_phone).strip()
            s.rule_set = event_rule_set(f.get("rule_set", s.rule_set))
            s.event_rules = f.get("event_rules", s.event_rules).replace("\r\n", "\n").strip()
            try:
                rulebook.check_apa_links(s.event_rules)
            except ValueError as exc:
                raise TournamentError(str(exc).replace("Not saved: ", "Settings not saved: ")) from None
            s.registration_closed = f.get("registration_closed") == "1"
            push_before = guest_locked(s)
            if f.get("require_member_shown"):
                s.require_member = f.get("require_member") == "1"
            if f.get("elig_shown"):
                s.eligibility = eligibility.parse_form(f, s.eligibility)
            if f.get("push_to") is not None:  # the event players can push to ("none" = no pushing)
                pick = f.get("push_to", "")
                s.push_to = pick if pick != g.event.id and app.config["hub"].get(pick) is not None else "none"
            guest_lock_settings(s, push_before)
            hours = (f.get("waitlist_offer_hours", "") or "").strip()
            if hours:
                if not hours.isdigit() or int(hours) > 72:
                    raise TournamentError("Waiting-list offer hours must be 0 to 72.")
                s.waitlist_offer_hours = int(hours)
            if f.get("skill_game") in ("8", "9", "high", "high7"):
                s.skill_game = f["skill_game"]
            if f.get("categories_shown") and f.get("game"):  # blank = not chosen yet: leave it unconfirmed
                for key, value in event_info.parse_categories(f).items():
                    setattr(s, key, value)
            if "registration_opens" in f:
                s.registration_opens, s.registration_closes = schedule.check_times(
                    f.get("registration_opens", ""), f.get("registration_closes", ""))
            tables = [x.strip() for x in f.get("tables", "").split(",") if x.strip()]
            if not tables:
                raise TournamentError("Add at least one table.")
            if len(set(tables)) != len(tables):
                raise TournamentError("Table names must be different.")
            busy = {m.table for m in t.matches if m.status in (CALLED, WAITING) and m.table}
            if busy - set(tables):
                raise TournamentError("You can't remove a table that has a match on it (or set for it).")
            s.tables = tables
            s.tables_down = [x for x in s.tables_down if x in tables]
            s.tables_closing = [x for x in s.tables_closing if x in tables]
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
            for key in ("no_rack_minutes", "match_minutes", "long_match_minutes", "catchup_matches"):
                value = f.get(key, str(getattr(s, key))).strip()
                if not value.isdigit():
                    raise TournamentError("Times and the catch-up gap must be whole numbers.")
                setattr(s, key, int(value))
            call_mode = f.get("call_mode", s.call_mode)
            if call_mode not in (DIRECTOR_CALLS, CONTINUOUS):
                raise TournamentError("Pick how matches are called.")
            s.call_mode = call_mode
            if f.get("test_event_shown"):
                s.test_event = f.get("test_event") == "1"
            guest_lock_settings(s, push_before)  # again, after the test-event box: a waiting event stays hidden
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
        sets = [{**r, "html": rulebook.render(r["body"], prefix=r["id"])} for r in sets]
        event_rules = (rulebook.render(t.settings.event_rules, prefix="tournament")
                       if t is not None and t.settings.event_rules else "")
        settings = search_settings.load()
        if os.environ.get("CHIPS_PRODUCTION") == "1" and search_settings.check_is_stale():
            threading.Thread(target=run_link_check, daemon=True).start()  # weekly, in the background
        check = settings["link_check"]
        return render_template("rules.html", t=t, active_tab="rules" if t is not None else None, rule_sets=sets,
                               event_rules=event_rules,
                               apa=apa_rules.load(), apa_sections=apa_rules.sections(), link_check=check,
                               moved={b["url"] for b in check["bad"]} if check else set(),
                               shorthand=rulebook.parse_shorthand(settings["shorthand"]),
                               popular=rulebook.parse_popular(settings["popular"]))

    def run_link_check() -> dict | None:
        """Open every official APA page and save the result. Skips if a check is already running."""
        if not link_check_running.acquire(blocking=False):
            return None
        try:
            fetch = app.config["apa_link_fetch"]
            result = apa_rules.check_links(fetch) if fetch else apa_rules.check_links()
            if apa_rules.looks_like_outage(result):  # couldn't reach APA: keep the last good result, retry soon
                search_settings.record_failed_check(result)
                return {**result, "outage": True}
            search_settings.record_check(result)
            return result
        finally:
            link_check_running.release()

    def event_rule_set(rule_id: str) -> str:
        """A rule set id an event can use: one that exists, or "" for none chosen yet."""
        return rule_id if any(r["id"] == rule_id for r in rule_book.load()) else ""

    def render_admin_rules(edited: dict | None = None, search_form: dict | None = None):
        """The rules editor. After a refused save, `edited` / `search_form` keep what was typed."""
        sets = [{**r, **(edited or {})} if edited and r["id"] == edited["id"] else r for r in rule_book.load()]
        return render_template("admin_rules.html", t=None, rule_sets=sets, search=search_form or search_settings.load(),
                               check=search_settings.load()["link_check"], apa_count=len(apa_rules.pages()),
                               check_failed=search_settings.load().get("link_check_failed"),
                               apa_sections=apa_rules.sections)

    @app.get("/admin/rules")
    @admin_required
    def admin_rules():
        return render_admin_rules()

    @app.post("/admin/rules/search")
    @admin_required
    def admin_rules_search():
        f = request.form
        try:
            search_settings.save_text(f.get("shorthand", ""), f.get("popular", ""))
        except ValueError as exc:
            flash(str(exc), "error")
            return render_admin_rules(search_form={"shorthand": f.get("shorthand", ""), "popular": f.get("popular", "")})
        flash("Saved the shorthand list and popular searches.", "ok")
        return redirect(url_for("admin_rules") + "#search-settings")

    @app.post("/admin/rules/check-links")
    @admin_required
    def admin_rules_check_links():
        result = run_link_check()
        if result is None:
            flash("A link check is already running. Give it a minute, then reload this page.", "error")
        elif result.get("outage"):
            flash(f"Couldn't reach rules.poolplayers.com ({len(result['bad'])} of {result['total']} pages didn't open), "
                  "so nothing was marked. That's usually a short outage. It tries again by itself within the hour, "
                  "or press the button later.", "error")
        elif result["bad"]:
            flash(f"{len(result['bad'])} of {result['total']} APA links need a look. They're listed below.", "error")
        else:
            flash(f"All {result['total']} APA links open.", "ok")
        return redirect(url_for("admin_rules") + "#link-check")

    @app.post("/admin/rules/new")
    @admin_required
    def admin_rules_new():
        rule = rule_book.add(request.form.get("title", ""))
        flash(f"Added {rule['title']}. Paste the rules in below and save.", "ok")
        return redirect(url_for("admin_rules") + f"#edit-{rule['id']}")

    @app.post("/admin/rules/<rule_id>")
    @admin_required
    def admin_rules_save(rule_id):
        title, body = request.form.get("title", ""), request.form.get("body", "")
        try:
            rule = rule_book.save(rule_id, title, body)
            flash(f"Saved {rule['title']}.", "ok")
        except ValueError as exc:
            flash(str(exc), "error")
            if rule_book.get(rule_id) is not None:  # keep what was typed so nothing is lost
                return render_admin_rules(edited={"id": rule_id, "title": title, "body": body})
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
        side, delta = request.form.get("side"), request.form.get("delta", "1")
        if side not in ("a", "b") or delta not in ("1", "-1"):
            abort(400)
        delta = int(delta)
        act("Score", lambda t: t.score(match_id, side, delta))
        return redirect(url_for("admin") + "#matches")

    @app.post("/t/<event>/admin/match/<match_id>/call")
    @admin_required
    def admin_call_match(match_id):
        m = act("Call match", lambda t: t.call_match(match_id))
        if m:
            flash(f"Table {m.table} called. Both teams were texted.", "ok")
        return redirect(url_for("admin") + "#matches")

    @app.post("/t/<event>/admin/call-all")
    @admin_required
    def admin_call_all():
        act("Call all matches", lambda t: t.call_all(), "Called {result} match(es). Everyone was texted their table.")
        return redirect(url_for("admin") + ("#matches" if g.event.kind == CHIP else "#tables"))

    @app.post("/t/<event>/admin/match/<match_id>/forfeit")
    @admin_required
    def admin_forfeit(match_id):
        winner = request.form.get("winner", "")
        if winner == "both":
            act("Forfeit (both teams)", lambda t: t.forfeit_both(match_id), "Both teams forfeited a chip.")
        else:
            act("Forfeit", lambda t: t.record_result(match_id, winner, forfeit=True), "Forfeit recorded.")
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

    @app.post("/t/<event>/admin/match/<match_id>/approve")
    @admin_required
    def admin_approve(match_id):
        act("Approve result", lambda t: t.approve_result(match_id), "Result approved. Chips and lines are updated.")
        return redirect(url_for("admin") + "#matches")

    @app.post("/t/<event>/admin/match/<match_id>/send-back")
    @admin_required
    def admin_send_back(match_id):
        act("Send result back", lambda t: t.send_back(match_id), "Sent back: both teams were texted to finish the match.")
        return redirect(url_for("admin") + "#matches")

    @app.post("/t/<event>/admin/match/<match_id>/scorekeeper")
    @admin_required
    def admin_scorekeeper(match_id):
        action = request.form.get("action")
        if action == "clear":
            act("Change scorekeeper", lambda t: t.clear_scorekeeper(match_id),
                "Scorekeeper cleared: the next player to tap I'll keep score takes over.")
        elif action in ("lock", "unlock"):
            act("Lock scorekeeper" if action == "lock" else "Unlock scorekeeper",
                lambda t: t.lock_scorekeeper(match_id, action == "lock"))
        else:
            abort(400)
        return redirect(url_for("admin") + "#matches")

    @app.post("/t/<event>/admin/table-state/<path:table>")
    @admin_required
    def admin_table_state(table):
        action = request.form.get("action")
        actions = {"close": ("Close table after match", lambda t: t.close_table_after_match(table)),
                   "down": ("Table out of service", lambda t: t.table_out_now(table)),
                   "open": ("Table back in service", lambda t: t.reopen_table(table))}
        if action not in actions:
            abort(400)
        label, fn = actions[action]
        act(f"{label}: {table}", fn, f"{label}: table {table}.")
        return redirect(url_for("admin") + "#matches")

    @app.post("/t/<event>/admin/match/<match_id>/move")
    @admin_required
    def admin_move_match(match_id):
        table = request.form.get("table", "")
        act(f"Move to table {table}", lambda t: t.move_match(match_id, table),
            f"Moved to table {table}. Both teams were texted if the match had been called.")
        return redirect(url_for("admin") + "#matches")

    @app.post("/t/<event>/admin/team/<team_id>/restore-out")
    @admin_required
    def admin_restore_out(team_id):
        chips = request.form.get("chips", "1").strip()
        if not chips.isdigit():
            flash("Enter a number of chips.", "error")
        else:
            act(f"Restore: {store.load().name_of(team_id)}", lambda t: t.restore_eliminated(team_id, int(chips)),
                "Restored: the team is back in line.")
        return redirect(url_for("admin") + "#teams")

    @app.get("/t/<event>/admin/export.csv")
    @admin_required
    def admin_export():
        """Every team (or entry) with PINs, cells and text consent, as a spreadsheet. Directors only."""
        t = store.load()
        rows = export.chip_teams(t) if g.event.kind == CHIP else export.bracket_entries(t)
        if guest_here():  # a guest TD never gets full phone numbers, not even in a download
            rows = export.mask_phones(rows, guest_td.mask_phone)
        name = f"{g.event.id}-teams-{datetime.now():%Y-%m-%d}.csv"
        return app.response_class(export.to_csv(rows), mimetype="text/csv",
                                  headers={"Content-Disposition": f'attachment; filename="{name}"',
                                           "Cache-Control": "no-store"})

    @app.post("/t/<event>/admin/match/<match_id>/undo-forfeit")
    @admin_required
    def admin_undo_forfeit(match_id):
        act("Undo forfeit", lambda t: t.undo_forfeit(match_id),
            "Forfeit undone: chips are back and the two teams replay the match next.")
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
        if body and guest_here():  # a guest TD texts players in the League's name: keep it short and occasional
            log = guest_td.BroadcastLog(g.event.dir)
            today = datetime.now().date().isoformat()
            mine = [x for x in log.all() if x["at"].startswith(today) and x["by"].endswith("(guest TD)")]
            if len(body) > guest_td.GUEST_BROADCAST_CHARS:
                flash(f"Keep texts to {guest_td.GUEST_BROADCAST_CHARS} characters or fewer "
                      f"(this one is {len(body)}).", "error")
                return redirect(url_for("admin") + "#texts")
            if len(mine) >= guest_td.GUEST_BROADCASTS_PER_DAY:
                flash(f"Guest TDs can send {guest_td.GUEST_BROADCASTS_PER_DAY} texts to everyone per day, and "
                      "you've sent them all today. Ask the league office if something urgent needs to go out.", "error")
                return redirect(url_for("admin") + "#texts")
        if body:
            sent = act("Broadcast text", lambda t: t.broadcast(body), "Queued {result} text(s).")
            if sent is not None:
                who = f"{g.player['first']} {g.player['last']} (guest TD)" if guest_here() else "League office"
                guest_td.BroadcastLog(g.event.dir).add(who, body, sent)
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
            deadline_paused = t.pause_partner_deadline()
            if deadline_paused:
                store.save(t)
        flash(f"Undid: {label}." if label else "Nothing to undo.", "ok" if label else "error")
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
        if (session.get("admin") or session.get("viewer")) and request.args.get("view") != "player":
            return redirect(url_for("director"))  # directors land on their own page; View as player shows this one
        rows = hub.summaries()
        if not (session.get("admin") or session.get("viewer")):  # practice events stay off the public list
            rows = [r for r in rows if not r["t"].settings.test_event]
        mine, reasons = {}, {}
        if g.get("player"):  # what each event means for the logged-in player: registered, can register, not eligible yet
            results = app.config["public_results"](g.player["member"])
            mine = {r["id"]: r["status"] for r in results}
            reasons = {r["id"]: r["reasons"][0] for r in results if r["reasons"]}
        return render_template("hub.html", t=None, rows=rows, mine=mine, reasons=reasons)

    @app.get("/calendar")
    def calendar_page():
        """Every tournament by date: a month grid and a list, with the same filters as the home page."""
        director = bool(session.get("admin") or session.get("viewer"))
        rows = [r for r in hub.summaries() if r["t"].status != FINISHED and (director or not r["t"].settings.test_event)]
        mine = {}
        if g.get("player"):
            mine = {r["id"]: r["status"] for r in app.config["public_results"](g.player["member"])}
        today = datetime.now().strftime("%Y-%m-%d")
        dated = sorted((r for r in rows if r["t"].settings.date), key=lambda r: (r["t"].settings.date, r["t"].settings.start_time or ""))
        months = []
        for r in dated:
            key = r["t"].settings.date[:7]
            if not months or months[-1]["key"] != key:
                try:
                    label = datetime.strptime(key, "%Y-%m").strftime("%B %Y")
                except ValueError:
                    label = key
                months.append({"key": key, "label": label, "rows": []})
            months[-1]["rows"].append(r)
        undated = [r for r in rows if not r["t"].settings.date]
        return render_template("calendar.html", t=None, months=months, undated=undated, mine=mine, today=today,
                               total_events=len(dated) + len(undated))

    @app.get("/admin")
    @director_view
    def director():
        rows = hub.summaries()
        return render_template("hub_admin.html", t=None, rows=rows, unreadable=hub.unreadable, formats=FORMATS,
                               final_cuts=FINAL_CUTS, today=datetime.now().strftime("%Y-%m-%d"),
                               backup_status=backup.load_status(data_path), backup_setup=backup.configured(),
                               pools=pools.all())

    # ------------------------------------------------------- shared tables (Mini-Mania)

    def pool_view(pool: dict) -> dict:
        """Every table in the pool and what's on it, and each event's state."""
        tables = {name: None for name in pool["tables"]}
        events = []
        for eid in pool["events"]:
            ev = hub.get(eid)
            if ev is None:
                continue
            t = ev.load()
            chip = ev.kind == CHIP
            for m in t.matches:
                if m.status in ("called", "waiting", "ready") and m.table and m.table in tables:
                    a, b = (m.team_a, m.team_b) if chip else (m.a, m.b)
                    tables[m.table] = {"event": t.settings.name, "event_id": eid, "names": f"{t.name_of(a)} vs {t.name_of(b)}",
                                       "state": "sent" if m.sent_at else ("playing" if m.status == "called" else "set")}
            waiting = len(t.table_queue() if chip else t.ready_queue()) if t.status == RUNNING else 0
            events.append({"id": eid, "name": t.settings.name, "status": t.status, "waiting": waiting,
                           "set": len(t.assigned_matches()) if t.status == RUNNING else 0,
                           "approve": len(t.results_to_approve()) if t.status == RUNNING else 0,
                           "kind": "Chip tournament" if chip else t.format_name})
        return {"pool": pool, "tables": tables, "events": events}

    def sync_pool_tables(pool: dict) -> None:
        """Every event in the pool uses the pool's tables."""
        for eid in pool["events"]:
            ev = hub.get(eid)
            if ev is not None:
                with ev.store.lock:
                    t = ev.load()
                    if t.settings.tables != pool["tables"]:
                        t.settings.tables = list(pool["tables"])
                        ev.store.save(t)

    @app.post("/admin/pools")
    @admin_required
    def admin_pool_save():
        f = request.form
        try:
            tables = [x.strip() for x in f.get("tables", "").split(",") if x.strip()]
            events = [e for e in request.form.getlist("events") if hub.get(e)]
            pool_mod.check_no_clash(hub, events)
            try:
                hours = max(0.0, float(f.get("hours") or pool_mod.DEFAULT_HOURS))
            except ValueError:
                raise TournamentError("Hours per tournament should be a number, like 4.")
            pool = pools.save(f.get("pool_id") or None, f.get("name", ""), tables, events, hours)
            sync_pool_tables(pool)
            flash(f"Saved {pool['name']}: {len(events)} event(s) share tables {', '.join(tables)}.", "ok")
            return redirect(url_for("admin_pool", pool_id=pool["id"]))
        except TournamentError as exc:
            flash(str(exc), "error")
            return redirect(url_for("director") + "#pools")

    @app.post("/admin/pool/<pool_id>/delete")
    @admin_required
    def admin_pool_delete(pool_id):
        pools.delete(pool_id)
        flash("The events no longer share tables. Each keeps the table list it has now.", "ok")
        return redirect(url_for("director") + "#pools")

    @app.get("/admin/pool/<pool_id>")
    @director_view
    def admin_pool(pool_id):
        pool = pools.get(pool_id) or abort(404)
        return render_template("pool_admin.html", t=None, view=pool_view(pool), rows=hub.summaries())

    @app.post("/admin/pool/<pool_id>/call-all")
    @admin_required
    def admin_pool_call_all(pool_id):
        """Call every set match in every event sharing these tables."""
        pool = pools.get(pool_id) or abort(404)
        called = 0
        for eid in pool["events"]:
            ev = hub.get(eid)
            if ev is None or not ev.load().assigned_matches():
                continue
            with pool_mod.lock_for(pool_id):
                with ev.store.change("Call all (shared tables)") as t:
                    pool_mod.inform(t, hub, pool, eid)
                    root = os.environ.get("SITE_URL", "").strip().rstrip("/") or request.url_root.rstrip("/")
                    t.site_url = f"{root}/t/{eid}"
                    try:
                        called += t.call_all()
                    except TournamentError:
                        pass
                    text_in_background(ev, t)
        flash(f"Called {called} match(es) across the events." if called else "No matches are waiting to be called.", "ok")
        return redirect(url_for("admin_pool", pool_id=pool_id))

    @app.get("/pool/<pool_id>")
    def pool_board(pool_id):
        pool = pools.get(pool_id) or abort(404)
        return render_template("pool_board.html", t=None, view=pool_view(pool))

    # ----------------------------------------------------------------- players

    def sync_message(entry: dict) -> tuple[str, str]:
        if entry["status"] == "held":
            return (f"Sync held: it would remove {entry['removed']} players at once. Check the list below, then approve "
                    "it or fix the sheet. The previous list is still in use.", "error")
        extra = f" {entry['problems']} row(s) need a look (listed below)." if entry["problems"] else ""
        if entry["status"] == "no changes":
            return f"Checked the sheet: no changes ({entry['players']} players).{extra}", "ok"
        return (f"Synced {entry['players']} players: {entry['added']} added, {entry['updated']} updated, "
                f"{entry['removed']} removed.{extra}", "ok")

    @app.get("/admin/players")
    @director_view
    def admin_players():
        q = request.args.get("q", "")
        return render_template("players.html", t=None, st=player_store.status(), log=player_store.log()[:20],
                               q=q, results=player_store.search(q), sa_email=configured_sa_email())

    def configured_sa_email() -> str:
        try:
            reader = players.SheetReader.from_env()
        except players.PlayerError:
            return ""
        return reader.drive.email if reader else ""

    @app.post("/admin/players/sheet")
    @admin_required
    def admin_players_sheet():
        try:
            sid = player_store.set_sheet(request.form.get("sheet", ""))
            if sid:
                flash(*sync_message(player_store.sync_sheet("director")))
            else:
                flash("Unlinked the player sheet. The last list stays until a new one is synced.", "ok")
        except players.PlayerError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_players"))

    @app.post("/admin/players/sync")
    @admin_required
    def admin_players_sync():
        try:
            flash(*sync_message(player_store.sync_sheet("director")))
        except players.PlayerError as exc:
            flash(f"Sync didn't run: {exc} The previous list is still in use.", "error")
        return redirect(url_for("admin_players"))

    @app.post("/admin/players/session")
    @admin_required
    def admin_players_session():
        try:
            day = player_store.set_session_start(request.form.get("session_start", ""),
                                                 request.form.get("session_name", ""))
            flash(f"The {player_store.config()['session_name']} session started {day}.", "ok")
        except players.PlayerError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_players"))

    @app.post("/admin/players/upload")
    @admin_required
    def admin_players_upload():
        upload = request.files.get("file")
        try:
            if not upload or not upload.filename:
                raise players.PlayerError("Pick the CSV file first.")
            flash(*sync_message(player_store.sync_csv(upload.read(), "director")))
        except players.PlayerError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_players"))

    @app.post("/admin/players/held")
    @admin_required
    def admin_players_held():
        try:
            if request.form.get("action") == "approve":
                flash(*sync_message(player_store.approve_held("director")))
            else:
                player_store.discard_held()
                flash("Discarded the held sync. Fix the sheet, then sync again.", "ok")
        except players.PlayerError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_players"))

    # ----------------------------------------------------------------- backups

    @app.get("/admin/backups")
    @director_view
    def admin_backups():
        files, drive_error = [], ""
        try:
            drive = backup.Drive.from_env()
            files = drive.list() if drive else []
        except backup.BackupError as exc:
            drive_error = str(exc)
        safety = sorted((data_path / backup.SAFETY_DIR).glob("before-restore-*.zip"), reverse=True)
        return render_template("backups.html", t=None, backup_status=backup.load_status(data_path),
                               backup_setup=backup.configured(), files=files, drive_error=drive_error,
                               safety=[p.name for p in safety])

    @app.post("/admin/backup")
    @admin_required
    def admin_backup_now():
        status = backup.run(data_path, reason="by hand")
        if status.get("error"):
            flash(f"Backup didn't save: {status['error']}", "error")
        else:
            flash(f"Backed up to {status['last_where']} ({status['last_name']})."
                  + (f" Note: {status['warning']}" if status.get("warning") else ""), "ok")
        back = request.form.get("back", "")
        return redirect(back if back in (url_for("director"), url_for("admin_backups")) else url_for("admin_backups"))

    @app.get("/admin/backup.zip")
    @admin_required
    def admin_backup_download():
        """A copy of everything, to keep on this device. It holds names, phone numbers and PINs."""
        return app.response_class(backup.make_zip(data_path), mimetype="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{backup.backup_name()}"', "Cache-Control": "no-store"})

    @app.post("/admin/backups/restore")
    @admin_required
    def admin_restore_backup():
        request.max_content_length = 200 * 1024 * 1024  # a backup can be bigger than a CSV or photo
        f = request.form
        if f.get("confirm", "").strip().upper() != "RESTORE":
            flash("Type RESTORE to confirm. Nothing was changed.", "error")
            return redirect(url_for("admin_backups"))
        try:
            if f.get("file_id"):
                drive = backup.Drive.from_env()
                if drive is None:
                    raise backup.BackupError("Google Drive isn't set up.")
                data, source = drive.download(f["file_id"]), f.get("file_name", "the Drive backup")
            elif f.get("safety"):
                name = f["safety"]
                path = data_path / backup.SAFETY_DIR / name
                if "/" in name or "\\" in name or not name.startswith("before-restore-") or not path.is_file():
                    raise backup.BackupError("Unknown backup.")
                data, source = path.read_bytes(), name
            else:
                upload = request.files.get("file")
                if not upload or not upload.filename:
                    raise backup.BackupError("Pick a backup file to restore.")
                data, source = upload.read(), upload.filename
            info = backup.restore_zip(data_path, data, hub)
        except backup.BackupError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin_backups"))
        flash(f"Restored {source}: {info['events']} tournament(s) from {info['made'].replace('T', ' ')}. "
              "Your data from just before the restore is kept under Undo a restore.", "ok")
        return redirect(url_for("director"))

    @app.post("/admin/new")
    @admin_required
    def new_event():
        f = request.form
        kind = f.get("kind", "")
        settings = {"date": f.get("date", "").strip(), "start_time": f.get("start_time", "").strip(),
                    "venue": f.get("venue", "").strip(), "test_event": f.get("test_event") == "1" or None}
        if kind in dict(FORMATS):
            settings["format"], kind = kind, BRACKET
        elif kind != CHIP:
            flash("Pick a tournament type.", "error")
            return redirect(url_for("director"))
        try:
            settings.update(event_info.parse_categories(f, need_entry=kind == BRACKET))  # chips are always teams of two
            ev = hub.create(f.get("name", ""), kind, **settings)
        except TournamentError as exc:
            flash(str(exc), "error")
            return redirect(url_for("director"))
        flash(f"Created {f.get('name', '').strip()}. Finish its settings below, then share the link.", "ok")
        return redirect(url_for("admin", event=ev.id) + "#settings")

    @app.post("/admin/categorize/<event_id>")
    @admin_required
    def categorize_event(event_id):
        """The director chooses an older event's game and type (Qualifier / Bonus) so cards and filters are right."""
        ev = hub.get(event_id)
        if ev is None:
            abort(404)
        try:
            chosen = event_info.parse_categories(request.form)
            with ev.store.lock:  # bookkeeping about the event, not a tournament action: no undo step
                t = ev.load()
                for key, value in chosen.items():
                    setattr(t.settings, key, value)
                ev.store.save(t)
            flash(f"Saved the categories for {t.settings.name}.", "ok")
        except TournamentError as exc:
            flash(str(exc), "error")
        return redirect(url_for("director") + "#categorize")

    # ------------------------------------------------- an event from the Bonus Event form

    def bonus_plan(raw):
        doc = bonus_event.read(raw)
        p = bonus_event.plan(doc, rule_book.load())
        if p.external_id:
            for ev in hub.events():
                if ev.load().settings.bonus_event_id == p.external_id:
                    raise TournamentError(f"This form entry was already made into '{ev.load().settings.name}'. "
                                          "Open it from the list, or delete it first to start over.")
        return doc, p

    @app.post("/admin/new/bonus-event")
    @admin_required
    def new_from_bonus_event():
        """Step 1: read the export and show what will be created."""
        upload = request.files.get("file")
        try:
            if not upload or not upload.filename:
                raise TournamentError("Pick the Bonus Event export (.json) first.")
            raw = upload.read()
            doc, p = bonus_plan(raw)
        except TournamentError as exc:
            flash(str(exc), "error")
            return redirect(url_for("director") + "#from-form")
        return render_template("bonus_preview.html", t=None, plan=p, doc=doc, raw=raw.decode("utf-8"),
                               test_event=request.form.get("test_event") == "1")

    @app.post("/admin/new/bonus-event/create")
    @admin_required
    def create_from_bonus_event():
        """Step 2: the director confirmed; create the event (registration stays closed until they open it)."""
        raw = request.form.get("raw", "")
        try:
            doc, p = bonus_plan(raw)
            extra = {"format": p.settings["format"]} if p.kind == BRACKET else {}
            ev = hub.create(p.name, p.kind, **extra)
            with ev.store.change("Created from the Bonus Event form") as t:
                bonus_event.apply(t, p)
                t.settings.test_event = request.form.get("test_event") == "1"
            (ev.dir / bonus_event.SAVED_AS).write_text(json.dumps(doc, indent=1))
        except TournamentError as exc:
            flash(str(exc), "error")
            return redirect(url_for("director") + "#from-form")
        flash(f"Created {p.name} from the Bonus Event form. Online registration is closed until you check the settings "
              "below and untick Close registration" + (f". Still to do: {' '.join(p.warnings)}" if p.warnings else "."),
              "ok")
        return redirect(url_for("admin", event=ev.id) + "#settings")

    @app.post("/admin/event/<event_id>/delete")
    @admin_required
    def delete_event(event_id):
        ev = hub.get(event_id) or abort(404)
        name = ev.load().settings.name
        if request.form.get("confirm", "").strip().upper() != "DELETE":
            flash("Type DELETE to confirm.", "error")
        elif hub.delete(event_id):
            guests.forget_event(event_id)
            flash(f"Deleted {name}, its rosters and its texts.", "ok")
        return redirect(url_for("director"))

    @app.route("/admin/event/<event_id>/copy", methods=["GET", "POST"])
    @admin_required
    def copy_event(event_id):
        """Copy a tournament's setup to a new one with a new name and date (no players, results or history)."""
        source = hub.get(event_id) or abort(404)
        t = source.load()
        if request.method == "POST":
            try:
                ev, notes = hub.copy(event_id, request.form.get("name", ""), request.form.get("date", ""))
            except TournamentError as exc:
                flash(str(exc), "error")
            else:
                flash(f"Copied {t.settings.name} to {ev.load().settings.name}. Check the settings below, make your "
                      "changes, and press Save settings.", "ok")
                for note in notes:
                    flash(note, "error")
                if t.settings.test_event:
                    flash("The original is a test event, so this copy is too: untick Test event in Settings to make "
                          "it a real one (players see it and get texts).", "error")
                return redirect(url_for("admin", event=ev.id) + "#settings")
        return render_template("copy_event.html", t=None, source=source, src=t,
                               form=request.form if request.method == "POST" else {})

    # Links from before Control Table hosted several tournaments (old texts, bookmarks, the TV).
    def only_live_event():
        """Where an old /board or /register link should go: the one tournament that's on now."""
        rows = [r for r in hub.summaries() if r["t"].status != FINISHED and not r["t"].settings.test_event]
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
              "admin_broadcast", "admin_text_pins", "find_team", "admin_call_all",
              "admin_export", "admin_skill_check", "admin_skill_check_fix"}
    chip_only = {r.endpoint for r in app.url_map.iter_rules() if "event" in r.arguments} - shared
    bracket_views = bracket_web.register(app, {
        "act": act, "store": store, "sms": sms, "director_view": director_view, "admin_required": admin_required,
        "parse_money": parse_money, "check_pin": check_pin, "minutes": minutes, "event_rule_set": event_rule_set,
        "scorer_device": scorer_device, "guest_lock_settings": guest_lock_settings, "guest_locked": guest_locked,
    })
    player_web.register(app, {"device_id": device_id})
    guest_web.register(app, {"hub": hub, "guests": guests, "players": player_store, "admin_required": admin_required,
                             "guest_fresh": guest_fresh, "FORMATS": FORMATS, "CHIP": CHIP, "BRACKET": BRACKET})
    player_web.register_registration(app)
    photos_web.register(app, admin_required)  # header photo and flyer: every tournament type
    qr_web.register(app, director_view)  # table QR stickers: every tournament type
    cal_web.register(app)  # Add to calendar: every tournament type
    event_pages.register(app)  # the event's tabs (after chip_only is worked out: these pages exist for both types)

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
