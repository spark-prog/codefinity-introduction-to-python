"""Phase 11: the partner finder."""

import pytest

from app import create_app
from chips import backup, players
from chips.engine import Player
from tests.test_eligibility import COLS, person, rules
from tests.test_player_login import FakeMailer
from tests.test_register_members import Clients


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    mail = FakeMailer()
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: mail))
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["mail"] = mail
    app.config["players"].apply(*players.parse_rows([
        COLS, person("32200001", first="Ann", last="Lee", sl8="6"),
        person("32200002", first="Bo", last="Ray", sl8="4"),
        person("32200003", first="Cy", last="Day", sl8="6"),
        person("32200004", first="Di", last="Fox", sl8="3", status="Past Due"),
        person("32200005", first="Ed", last="Kim", sl8="2")]), "t", "t")
    ev = app.config["hub"].create("Doubles", "chip")
    t = ev.load()
    t.settings.registration_cap = 10
    t.register_team("", [Player("Bo Ray", "9045550100", 4, member="32200002")], list_seeking=True)  # holding a spot
    ev.store.save(t)
    return app


def test_finder_shows_who_fits_and_sends_a_request(app):
    mail, ev = app.config["mail"], app.config["hub"].get("doubles")
    # Cy (SL 6) and Di (Past Due) list themselves without registering.
    for m in ("32200003", "32200004"):
        Clients(app).logged_in(m).post("/t/doubles/partner-list", data={"action": "add"})
    assert len(ev.load().settings.partner_seekers) == 2

    anon = app.test_client().get("/t/doubles/register").get_data(as_text=True)
    assert "Bo Ray" in anon and "Cy Day" in anon and "to see who fits" in anon

    t = ev.load()
    t.settings.eligibility = rules(active=True)
    ev.store.save(t)
    ann = Clients(app).logged_in("32200001")  # SL 6: Bo (4) fits at 10; Cy (6) is 12; Di is Past Due
    page = ann.get("/t/doubles/register").get_data(as_text=True).split('id="looking"')[1]
    assert "Bo Ray" in page and "10 of 10" in page and "Cy Day" not in page and "Di Fox" not in page
    assert "2 other player(s) looking don" in page
    r = ann.post("/t/doubles/partner-ask", data={"member": "32200002"}, follow_redirects=True)
    assert "Sent! Bo Ray can reply" in r.get_data(as_text=True)
    sent = mail.sent[-1]
    assert sent["to"] == "bo@x.com" and sent["reply_to"] == "ann@x.com" and "Ann Lee (skill level 6)" in sent["text"]
    assert "team page" in sent["text"]  # Bo holds a spot: add Ann on the roster

    ed = Clients(app).logged_in("32200005")  # SL 2: Cy (6) fits now
    page = ed.get("/t/doubles/register").get_data(as_text=True).split('id="looking"')[1]
    assert "Cy Day" in page and "8 of 10" in page
    ed.post("/t/doubles/partner-ask", data={"member": "32200003"})
    assert "One of you registers and picks the other" in mail.sent[-1]["text"]
    for _ in range(5):
        ed.post("/t/doubles/partner-ask", data={"member": "32200003"})
    r = ed.post("/t/doubles/partner-ask", data={"member": "32200003"}, follow_redirects=True)
    assert "a lot of requests" in r.get_data(as_text=True)


def test_listed_player_drops_off_when_registered_and_can_unlist(app):
    ev = app.config["hub"].get("doubles")
    cy = Clients(app).logged_in("32200003")
    cy.post("/t/doubles/partner-list", data={"action": "add"})
    assert "You're on the list" in cy.get("/t/doubles/register").get_data(as_text=True)
    cy.post("/t/doubles/partner-list", data={"action": "remove"})
    assert ev.load().settings.partner_seekers == []
    cy.post("/t/doubles/partner-list", data={"action": "add"})
    t = ev.load()
    t.register_team("Owls", [Player("Cy Day", "9045550100", 6, member="32200003"), Player("Ed Kim", "", 2)])
    ev.store.save(t)
    assert "Cy Day" not in app.test_client().get("/t/doubles/register").get_data(as_text=True).split('id="looking"')[1]
