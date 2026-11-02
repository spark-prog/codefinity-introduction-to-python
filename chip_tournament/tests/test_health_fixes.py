"""Fixes from the full health check: rollback-safe saved files, one bad file can't take the site down, shared-table
texts don't hold the locks, input checks, security headers, and the real visitor address behind a CDN."""

import json
import random
import threading
import time

import pytest

from app import create_app
from chips import alerts
from chips.bracket import BracketTournament
from chips.engine import Player, Tournament


def make_app(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("VIEWER_PASSWORD", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
                "CHIPS_PRODUCTION", "TRUSTED_IP_HEADER"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


@pytest.fixture
def app(tmp_path, monkeypatch):
    return make_app(tmp_path, monkeypatch)


def director(app):
    c = app.test_client()
    c.post("/admin/login", data={"password": "secretpw1"})
    return c


def chip_event(app):
    ev = app.config["hub"].create("Masquer", "chip")
    t = ev.load()
    t.register_team("Sharks", [Player("Ann Lee", "9045550100", 4), Player("Cy Day", "", 3)])
    ev.store.save(t)
    return ev


# ------------------------------------------------- saved files from a newer version (rollback, restored backup)

def add_unknown_fields(path):
    d = json.loads(path.read_text())
    d["settings"]["a_setting_from_a_newer_version"] = 1
    people = d.get("teams") or d.get("entries")
    first = next(iter(people.values()))
    first["new_team_field"] = "x"
    first["players"][0]["new_player_field"] = True
    for m in d.get("matches", []):
        m["new_match_field"] = 0
    path.write_text(json.dumps(d))


def test_files_with_newer_fields_still_load(app):
    hub = app.config["hub"]
    chip = chip_event(app)
    br = hub.create("Bracket", "bracket", format="double")
    b = br.load()
    b.settings.team_size = 1
    for i in range(4):
        b.register_entry(f"E{i}", [Player(f"Player {i}", "9045550100", 4)])
    b.start(random.Random(1))
    br.store.save(b)
    for ev, kind in ((chip, Tournament), (br, BracketTournament)):
        add_unknown_fields(ev.store.path)
        t = ev.load()
        assert isinstance(t, kind) and not hasattr(t.settings, "a_setting_from_a_newer_version")
    assert app.test_client().get("/").status_code == 200
    assert app.test_client().get(f"/t/{chip.id}/").status_code == 200
    assert app.test_client().get(f"/t/{br.id}/").status_code == 200


def test_one_unreadable_event_doesnt_take_down_the_lists(app):
    good, bad = chip_event(app), app.config["hub"].create("Broken", "chip")
    bad.store.path.write_text('{"settings": ')  # damaged
    page = app.test_client().get("/")
    assert page.status_code == 200 and "Masquer" in page.get_data(as_text=True)
    health = app.test_client().get("/healthz").get_json()
    assert health["ok"] is True and health["unreadable_events"] == [bad.id]
    hub_page = director(app).get("/admin").get_data(as_text=True)
    assert "1 tournament can't be opened" in hub_page and f"/t/{bad.id}" in hub_page
    assert good.id in hub_page


# ------------------------------------------------- shared-table texts don't hold the locks

def test_shared_table_call_all_doesnt_wait_for_texts(app, monkeypatch):
    sent, release = [], threading.Event()

    def slow(self, to, body):
        release.wait(5)  # a text service that's slow to answer
        sent.append(to)
        return "sent (201)"
    monkeypatch.setattr(alerts.SmsSender, "_send_twilio", slow)
    monkeypatch.setattr(alerts.SmsSender, "enabled", property(lambda self: True))
    hub, evs = app.config["hub"], []
    for n, name in enumerate(("Mini A", "Mini B")):
        ev = hub.create(name, "bracket", format="single")
        t = ev.load()
        t.settings.team_size, t.settings.call_mode = 1, "director"
        for i in range(4):
            p = Player(f"{name} P{i}", f"90455{n}{i:04d}", 4)
            p.sms_consent = True
            t.register_entry(f"{name}{i}", [p])
        ev.store.save(t)
        evs.append(ev)
    c = director(app)
    c.post("/admin/pools", data={"name": "MM", "tables": "1,2,3,4", "events": [e.id for e in evs]})
    for e in evs:
        c.post(f"/t/{e.id}/admin/start")
    start = time.time()
    assert c.post("/admin/pool/mm/call-all").status_code == 302
    assert time.time() - start < 2  # the page and the locks don't wait for the texts
    assert sum(m.status == "called" for e in evs for m in e.load().matches) == 4
    release.set()
    deadline = time.time() + 5
    while len(set(sent)) < 8 and time.time() < deadline:
        time.sleep(0.05)
    assert len(set(sent)) == 8  # and every player still gets texted


# ------------------------------------------------- input checks

def test_a_bad_score_value_is_a_bad_request_not_a_crash(app):
    ev = chip_event(app)
    team = next(iter(ev.load().teams))
    assert app.test_client().post(f"/t/{ev.id}/team/{team}/score", data={"side": "a", "delta": "abc"}).status_code == 400
    assert director(app).post(f"/t/{ev.id}/admin/match/x/score", data={"side": "a", "delta": "abc"}).status_code == 400


@pytest.mark.parametrize("name, phone, ok", [
    ("Dana Audit", "904-555-0123", True),
    ("Dana Audit", "1 (904) 555-0123", True),
    ("Dana Audit", "+44 20 7946 0958", True),
    ("Dana Audit", "", True),
    ("Dana Audit", "not a phone", False),
    ("Dana Audit", "555-0123", False),
    ("D" * 61, "904-555-0123", False),
])
def test_registration_checks_names_and_phones(app, name, phone, ok):
    ev = app.config["hub"].create("Mini", "bracket", format="single")
    t = ev.load()
    t.settings.team_size = 1
    ev.store.save(t)
    page = app.test_client().post(f"/t/{ev.id}/register", data={
        "player_name_0": name, "player_phone_0": phone, "player_skill_0": "4", "agree_terms": "1"},
        follow_redirects=True).get_data(as_text=True)
    assert (len(ev.load().entries) == 1) == ok
    if not ok:
        assert "60 characters" in page or "cell number we can text" in page


def test_chip_team_name_length(app):
    ev = app.config["hub"].create("Chip", "chip")
    r = app.test_client().post(f"/t/{ev.id}/register", data={
        "name": "T" * 61, "player_name_0": "Ann Lee", "player_phone_0": "9045550100", "player_skill_0": "4",
        "player_name_1": "Cy Day", "player_skill_1": "3", "agree_terms": "1"}, follow_redirects=True)
    assert "team name to 60 characters" in r.get_data(as_text=True) and not ev.load().teams


# ------------------------------------------------- headers, undo, real visitor address

def test_security_headers(app):
    ev = chip_event(app)
    c = app.test_client()
    for path in ("/", f"/t/{ev.id}/", f"/t/{ev.id}/board", "/rules"):  # public pages a league site may frame
        r = c.get(path)
        assert r.headers["X-Content-Type-Options"] == "nosniff" and "X-Frame-Options" not in r.headers, path
    for path in ("/admin/login", "/login", f"/t/{ev.id}/register", f"/t/{ev.id}/admin"):
        r = c.get(path)
        assert r.headers["X-Frame-Options"] == "SAMEORIGIN", path
    assert "Strict-Transport-Security" not in c.get("/").headers  # only in production (HTTPS)


def test_production_headers_and_trusted_ip(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, CHIPS_PRODUCTION="1", TRUSTED_IP_HEADER="CF-Connecting-IP")
    c = app.test_client()
    assert "max-age" in c.get("/", base_url="https://localhost").headers["Strict-Transport-Security"]
    assert c.get("/admin/whoami", base_url="https://localhost").status_code == 302  # director only
    c.post("/admin/login", data={"password": "secretpw1"}, base_url="https://localhost")
    seen = c.get("/admin/whoami", base_url="https://localhost",
                 headers={"CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "198.51.100.1"}).get_json()
    assert seen["login_limits_see_you_as"] == "203.0.113.7" and seen["trusted_ip_header"] == "CF-Connecting-IP"


def test_undo_still_works_after_the_safe_write(app):
    ev = chip_event(app)
    with ev.store.change("Add a team") as t:
        t.register_team("Rays", [Player("Bo Kim", "9045550101", 4), Player("Al Fox", "", 3)])
    assert len(ev.load().teams) == 2
    assert ev.store.undo() == "Add a team"
    assert len(ev.load().teams) == 1 and not ev.store.path.with_suffix(".tmp").exists()


def test_live_pages_refresh_without_leaving_the_page(app):
    """A refresh fetches the fresh page first and only then shows it: during a deploy the page stays put
    (saying it's reconnecting) instead of landing on the host's error page, which never reloads."""
    ev = chip_event(app)
    import re
    for path in (f"/t/{ev.id}/board", f"/t/{ev.id}/", "/"):
        html = app.test_client().get(path).get_data(as_text=True)
        assert "window.ctSafeReload = " in html and "Reconnecting…" in html, path
        scripts = "".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        helper = scripts.split("window.ctSafeReload = ", 1)[1].split("})();", 1)[0]
        # the only direct reloads are inside the helper, once the site has answered
        assert scripts.count("location.reload()") == helper.count("location.reload()") == 1, path
    board = app.test_client().get(f"/t/{ev.id}/board").get_data(as_text=True)
    assert 'name="auto-refresh"' in board and "window.ctSafeReload()" in board
