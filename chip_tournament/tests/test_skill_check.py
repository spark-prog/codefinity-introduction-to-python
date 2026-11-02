"""The director's skill level check against the player list."""

import pytest

from app import create_app
from chips import players
from chips.engine import Player
from tests.test_players import HEADERS, row


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.setenv("VIEWER_PASSWORD", "viewpw123")
    app = create_app(str(tmp_path))
    app.testing = True
    rows = [HEADERS,
            row("32200001", first="Ann", last="Lee", sl8="6", sl9="0", email="a@x.com"),
            row("32200002", first="Bo", last="Ray", sl8="4", sl9="5", email="b@x.com"),
            row("32200003", first="Dee", last="Fox", sl8="3", sl9="2", email="d@x.com"),
            row("32200004", first="Ed", last="Kim", sl8="5", sl9="5", email="e1@x.com"),
            row("32200005", first="Ed", last="Kim", sl8="2", sl9="2", email="e2@x.com"),
            row("32200006", first="Fay", last="Orr", sl8="0", sl9="4", mp9="30", email="f@x.com")]
    app.config["players"].apply(*players.parse_rows(rows), "test", "test")
    return app


def logins(app):
    admin, viewer = app.test_client(), app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    viewer.post("/admin/login", data={"password": "viewpw123"})
    return admin, viewer


def test_bracket_skill_groups_check_and_fix(app):
    ev = app.config["hub"].create("Movin", "bracket", format="modified")
    t = ev.load()
    t.settings.team_size, t.settings.skill_groups, t.settings.race_method = 1, [[1, 4], [5, 6], [7, 9]], "rack_chart"
    for name, s8, s9, member in (("Ann Lee", 6, 0, "32200001"), ("bo ray", 4, 4, ""), ("Cy Day", 3, 0, ""),
                                 ("Dee Fox", 0, 0, ""), ("Ed Kim", 5, 5, "")):
        t.register_entry("", [Player(name, "9045550100", 0, skill_8=s8, skill_9=s9, member=member)])
    ev.store.save(t)
    admin, viewer = logins(app)
    assert 'href="/t/movin/admin/skill-check"' in admin.get("/t/movin/admin").get_data(as_text=True)
    page = viewer.get("/t/movin/admin/skill-check").get_data(as_text=True)
    for label in ("Matches: 1", "Differs from the list: 1", "No skill level entered: 1", "Not in the player list: 1",
                  "More than one player with this name: 1"):
        assert label in page, label
    assert "matched by name" in page and "Fix all from the list" not in page  # view-only: no buttons

    viewer.post("/t/movin/admin/skill-check", data={"id": "all"})
    assert next(e for e in ev.load().entries.values() if e.name == "bo ray").players[0].skill_9 == 4  # unchanged

    r = admin.post("/t/movin/admin/skill-check", data={"id": "all"}, follow_redirects=True)
    assert "Updated 2 entries" in r.get_data(as_text=True)
    t = ev.load()
    bo = next(e for e in t.entries.values() if e.name == "bo ray")
    dee = next(e for e in t.entries.values() if e.name == "Dee Fox")
    assert (bo.players[0].skill_8, bo.players[0].skill_9, bo.players[0].member, bo.division) == (4, 5, "32200002", "SL 5–6")
    assert (dee.players[0].skill_8, dee.players[0].skill_9, dee.division) == (3, 2, "SL 1–4")
    assert "Skill check: bo ray" in ev.store.last_undo_label() or "Skill check: Dee Fox" in ev.store.last_undo_label()


def test_chip_check_uses_the_events_format_and_conversion(app):
    ev = app.config["hub"].create("Chips", "chip")
    t = ev.load()
    t.register_team("Sharks", [Player("Ann Lee", "9045550100", 5, member="32200001"),
                               Player("Fay Orr", "", 4)])
    ev.store.save(t)
    admin, _ = logins(app)
    page = admin.get("/t/chips/admin/skill-check").get_data(as_text=True)
    assert "converted from 9-Ball" in page and "Differs from the list: 1" in page and "Matches: 1" in page
    admin.post("/t/chips/admin/skill-check", data={"id": next(iter(ev.load().teams))})
    team = next(iter(ev.load().teams.values()))
    assert [(p.skill, p.member) for p in team.players] == [(6, "32200001"), (4, "32200006")]
