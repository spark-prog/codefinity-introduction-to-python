"""Phase 10: upcoming events on My account, one-tap Register, partner confirmation."""

import re

import pytest

from app import create_app
from chips import backup, players
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
        COLS, person("32200001", first="Ann", last="Lee", sl8="5", sl9="6"),
        person("32200002", first="Bo", last="Ray", sl8="4", gender="M"),
        person("32200003", first="Cy", last="Day", sl8="3", gender="M")]), "t", "t")
    hub = app.config["hub"]
    singles = hub.create("Singles Night", "bracket", format="single", date="2026-11-14")
    t = singles.load()
    t.settings.team_size, t.settings.skill_groups, t.settings.race_method = 1, [[1, 5], [6, 9]], "rack_chart"
    t.settings.skill_game = "high"  # placed by the higher of the two levels
    singles.store.save(t)
    hub.create("Doubles Chips", "chip", date="2026-11-21")
    masters = hub.create("Masters", "bracket", format="single", date="2026-12-05")
    t = masters.load()
    t.settings.team_size, t.settings.eligibility = 1, rules(min_matches=500)
    masters.store.save(t)
    return app


def test_upcoming_events_and_one_tap_register(app):
    ann = Clients(app).logged_in("32200001")
    page = ann.get("/me").get_data(as_text=True)
    upcoming = page.split('id="upcoming"')[1].split("</section>")[0]
    can, cannot = upcoming.split("Not eligible yet (")
    assert "Singles Night" in can and "Doubles Chips" in can and "Register with a partner" in can
    assert "Masters" in cannot and "Needs 500 lifetime matches" in cannot

    r = ann.post("/t/singles-night/quick-register", data={}, follow_redirects=True)
    assert "Tick the box" in r.get_data(as_text=True)
    r = ann.post("/t/singles-night/quick-register", data={"agree_terms": "1", "sms_consent": "1"}, follow_redirects=True)
    assert "You&#39;re registered for Singles Night" in r.get_data(as_text=True) and "SL 6–9 group" in r.get_data(as_text=True)
    entry = next(iter(app.config["hub"].get("singles-night").load().entries.values()))
    p = entry.players[0]
    assert (p.name, p.member, p.skill_8, p.skill_9, p.sms_consent, entry.division) == (
        "Ann Lee", "32200001", 5, 6, True, "SL 6–9")
    page = ann.get("/me").get_data(as_text=True)
    assert "Singles Night" in page.split('<section class="your-events">')[1].split("</section>")[0]  # now Your events
    assert "Singles Night" not in page.split('id="upcoming"')[1].split("</section>")[0]
    r = ann.post("/t/doubles-chips/quick-register", data={"agree_terms": "1"})
    assert r.headers["Location"].endswith("/t/doubles-chips/register")  # doubles: the filled-in register page


def chip_team(c, name, partner, member):
    return c.post("/t/doubles-chips/register", data={
        "name": name, "agree_terms": "1", "player_name_0": "Ann Lee", "player_skill_0": "5",
        "player_member_0": "32200001", "player_phone_0": "904-555-0100",
        "player_name_1": partner, "player_skill_1": "4", "player_member_1": member})


def test_partner_confirms_or_declines(app):
    mail, ev = app.config["mail"], app.config["hub"].get("doubles-chips")
    ann = Clients(app).logged_in("32200001")
    chip_team(ann, "Sharks", "Bo Ray", "32200002")
    assert ev.store.last_undo_label().startswith("Registration: Sharks")  # the email doesn't take over Undo
    sharks = next(iter(ev.load().teams.values()))
    assert [p.confirm for p in sharks.players] == ["", "waiting"]
    email = mail.sent[-1]
    assert email["to"] == "bo@x.com" and "Ann Lee registered you as their partner on Sharks" in email["text"]
    link = re.search(r"https?://\S+", email["text"]).group(0).replace("http://localhost", "")
    assert "Waiting for Bo Ray to confirm" in ann.get(f"/t/doubles-chips/team/{sharks.id}").get_data(as_text=True)

    bo = app.test_client()
    assert "Are you on Sharks?" in bo.get(link).get_data(as_text=True)
    bo.post(link, data={"action": "confirm"})
    assert next(iter(ev.load().teams.values())).players[1].confirm == "confirmed"
    assert "already confirmed" in bo.get(link).get_data(as_text=True)

    # A second team names Cy, who declines.
    cy_team = Clients(app).logged_in("32200001")
    t = ev.load()
    for team in list(t.teams.values()):
        t.remove_team(team.id)
    ev.store.save(t)
    chip_team(cy_team, "Jets", "Cy Day", "32200003")
    link = re.search(r"https?://\S+", mail.sent[-1]["text"]).group(0).replace("http://localhost", "")
    app.test_client().post(link, data={"action": "decline"})
    cy = next(iter(ev.load().teams.values())).players[1]
    assert cy.confirm == "declined" and cy.pending == "Cy Day declined being on Jets"
    assert mail.sent[-1]["to"] == "ann@x.com" and "Cy Day declined" in mail.sent[-1]["subject"]
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    assert "Cy Day declined being on Jets" in admin.get("/t/doubles-chips/admin").get_data(as_text=True)
    assert "isn&#39;t valid" in app.test_client().get("/t/doubles-chips/partner/forged", follow_redirects=True).get_data(as_text=True)
