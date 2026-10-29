"""Director button: resend each team its own PIN and team-page link."""

import time

from app import create_app
from chips.alerts import SmsSender
from chips.bracket import BracketTournament
from chips.engine import Player, Tournament


def test_each_team_gets_its_own_pin_only_on_opted_in_numbers():
    t = Tournament()
    t.site_url = "https://example.com/t/chips"
    sharks = t.register_team("Sharks", [Player("A", "5551110000", 4, True), Player("B", "5551112222", 4)])
    jets = t.register_team("Jets", [Player("C", "5552220000", 4, True), Player("D", "5552223333", 4, True)])
    t.register_team("Owls", [Player("E", "5553330000", 4), Player("F", "", 4)])
    t.outbox = []
    sent, unreachable = t.text_pins()
    assert (sent, unreachable) == (3, 1)
    by_number = {m.to: m.body for m in t.outbox}
    assert set(by_number) == {"5551110000", "5552220000", "5552223333"}  # B and Owls never opted in
    assert f"PIN for {t.settings.name} is {sharks.pin}" in by_number["5551110000"]
    assert f"/team/{sharks.id}" in by_number["5551110000"]
    assert jets.pin in by_number["5552223333"] and sharks.pin not in by_number["5552223333"]
    assert "STOP" in by_number["5551110000"]


def test_no_shows_are_skipped():
    t = Tournament()
    team = t.register_team("Sharks", [Player("A", "5551110000", 4, True), Player("B", "", 4)])
    team.status = "no_show"
    t.outbox = []
    assert t.text_pins() == (0, 0)


def test_bracket_entries_get_their_own_pin():
    t = BracketTournament()
    t.site_url = "https://example.com/t/b"
    ann = t.register_entry("Ann", [Player("Ann", "5551110000", 0, True)])
    t.register_entry("Bob", [Player("Bob", "5552220000", 0)])
    t.outbox = []
    assert t.text_pins() == (1, 1)
    assert ann.pin in t.outbox[0].body and f"/entry/{ann.id}" in t.outbox[0].body


def test_director_button_sends_the_texts(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "VIEWER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    sent = []
    monkeypatch.setattr(SmsSender, "send", lambda self, to, body: sent.append((to, body)) or "logged")
    app = create_app(str(tmp_path))
    app.testing = True
    ev = app.config["hub"].create("Chips", "chip")
    with ev.store.change("teams") as t:
        team = t.register_team("Sharks", [Player("A", "5551110000", 4, True), Player("B", "", 4)])
        t.register_team("Owls", [Player("E", "5553330000", 4), Player("F", "", 4)])
        t.outbox = []
    assert app.test_client().post(f"/t/{ev.id}/admin/text-pins").status_code in (302, 403)  # not logged in
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    assert b"Resend PIN and team link" in admin.get(f"/t/{ev.id}/admin").data
    r = admin.post(f"/t/{ev.id}/admin/text-pins", follow_redirects=True)
    assert b"Queued 1 PIN text(s). 1 team(s) have nobody signed up for texts." in r.data
    for _ in range(100):
        if sent:
            break
        time.sleep(0.02)
    assert sent == [("5551110000", f"Sharks, your team PIN for Chips is {team.pin}."
                    f" Team page: http://localhost/t/{ev.id}/team/{team.id} Reply STOP to opt out.")]


def test_site_url_setting_sets_the_link_in_texts(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.setenv("SITE_URL", "https://controltable.app/")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "VIEWER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    sent = []
    monkeypatch.setattr(SmsSender, "send", lambda self, to, body: sent.append(body) or "logged")
    app = create_app(str(tmp_path))
    app.testing = True
    ev = app.config["hub"].create("Chips", "chip")
    with ev.store.change("teams") as t:
        team = t.register_team("Sharks", [Player("A", "5551110000", 4, True), Player("B", "", 4)])
        t.outbox = []
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    admin.post(f"/t/{ev.id}/admin/text-pins")
    for _ in range(100):
        if sent:
            break
        time.sleep(0.02)
    assert f"https://controltable.app/t/{ev.id}/team/{team.id}" in sent[0]
