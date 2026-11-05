"""Players unregister before the start, and edit their bracket registration (skill levels, cells)."""

import random

import pytest

from app import create_app
from chips.engine import Player, Tournament, TournamentError


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("VIEWER_PASSWORD", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


def chip_form(name, n):
    return {"name": name, "agree_terms": "1", "player_name_0": f"Ann {n}", "player_name_1": f"Bob {n}",
            "player_skill_0": "4", "player_skill_1": "4", "player_phone_0": f"90455501{n:02d}", "sms_consent": "1"}


def test_chip_team_unregisters_and_the_waiting_list_moves_up(app):
    ev = app.config["hub"].create("Chips", "chip")
    t = ev.load()
    t.settings.field_cap = 1
    ev.store.save(t)
    sharks, jets = app.test_client(), app.test_client()
    sharks.post("/t/chips/register", data=chip_form("Sharks", 1))
    jets.post("/t/chips/register", data=chip_form("Jets", 2))
    t = ev.load()
    s_id = next(x.id for x in t.teams.values() if x.name == "Sharks")
    assert next(x for x in t.teams.values() if x.name == "Jets").status == "waitlisted"

    page = sharks.get(f"/t/chips/team/{s_id}").get_data(as_text=True)
    assert 'id="unregister"' in page and "Update roster or skill levels" in page
    assert 'id="unregister"' not in app.test_client().get(f"/t/chips/team/{s_id}").get_data(as_text=True)
    assert app.test_client().post(f"/t/chips/team/{s_id}/unregister", data={"confirm": "1"}).status_code == 302
    assert s_id in ev.load().teams  # no PIN: nothing happened
    r = sharks.post(f"/t/chips/team/{s_id}/unregister", follow_redirects=True)
    assert "Tick the box" in r.get_data(as_text=True) and s_id in ev.load().teams
    r = sharks.post(f"/t/chips/team/{s_id}/unregister", data={"confirm": "1"}, follow_redirects=True)
    assert "Sharks is unregistered" in r.get_data(as_text=True)
    t = ev.load()
    assert s_id not in t.teams and [x.status for x in t.teams.values()] == ["registered"]  # Jets moved up
    texts = [m["body"] for m in ev.sms.recent()]
    assert any("Sharks is unregistered" in b for b in texts) and any("a spot opened and Jets" in b for b in texts)


def test_no_unregistering_after_the_start():
    t = Tournament()
    t.settings.require_checkin, t.settings.tables = False, ["1"]
    for i in range(2):
        t.register_team(f"T{i}", [Player(f"A{i}", "9045550100", 4), Player(f"B{i}", "", 4)])
    t.start(random.Random(1))
    with pytest.raises(TournamentError, match="has started"):
        t.unregister(next(iter(t.teams)))


def test_bracket_player_edits_skill_and_unregisters(app):
    ev = app.config["hub"].create("Movin", "bracket", format="modified")
    t = ev.load()
    t.settings.team_size, t.settings.skill_groups, t.settings.race_method = 1, [[1, 4], [5, 9]], "rack_chart"
    t.settings.skill_game = "high"  # placed by the higher of the two levels
    ev.store.save(t)
    me = app.test_client()
    me.post("/t/movin/register", data={"agree_terms": "1", "player_name_0": "Ann Lee", "player_skill8_0": "4",
                                       "player_phone_0": "904-555-0100"})
    entry = next(iter(ev.load().entries.values()))
    assert entry.division == "SL 1–4"
    page = me.get(f"/t/movin/entry/{entry.id}").get_data(as_text=True)
    assert "Edit registration or skill levels" in page and 'id="unregister"' in page
    assert app.test_client().get(f"/t/movin/entry/{entry.id}/edit").status_code == 302  # needs the PIN
    r = me.post(f"/t/movin/entry/{entry.id}/edit", data={"player_name_0": "Ann Lee", "player_skill8_0": "4",
                                                         "player_skill9_0": "6", "player_phone_0": ""},
                follow_redirects=True)
    assert "Skill level changes are noted for the director" in r.get_data(as_text=True)
    entry = ev.load().entries[entry.id]
    assert (entry.players[0].skill_9, entry.players[0].phone, entry.division) == (6, "904-555-0100", "SL 5–9")
    assert "SL 4/- → 4/6" in ev.store.last_undo_label()
    r = me.post(f"/t/movin/entry/{entry.id}/unregister", data={"confirm": "1"}, follow_redirects=True)
    assert "Ann Lee is unregistered" in r.get_data(as_text=True) and not ev.load().entries
