"""Member numbers are required for online registration (per-event switch, on by default)."""

import pytest

from app import create_app
from chips import engine, players
from chips.engine import Player
from tests.test_players import HEADERS, row


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "REQUIRE_MEMBER_DEFAULT", True)
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["players"].apply(*players.parse_rows([HEADERS, row("32200001", first="Ann", last="Lee"),
                                                     row("32200002", first="Bo", last="Ray")]), "t", "t")
    app.config["hub"].create("Chips", "chip")
    return app


def chip(**kw):
    data = {"name": "Sharks", "agree_terms": "1", "player_name_0": "Ann Lee", "player_skill_0": "3",
            "player_name_1": "Bo Ray", "player_skill_1": "3", "player_phone_0": "904-555-0100"}
    data.update(kw)
    return data


def teams(app):
    return app.config["hub"].get("chips").load().teams


def test_every_player_needs_a_number(app):
    c = app.test_client()
    page = c.get("/t/chips/register").get_data(as_text=True)
    assert 'name="player_member_0"' in page and "(optional)" not in page.split('name="player_member_0"')[0][-200:]
    r = c.post("/t/chips/register", data=chip(player_member_0="32200001"), follow_redirects=True)
    assert "Enter Bo Ray&#39;s APA member number" in r.get_data(as_text=True) and not teams(app)
    c.post("/t/chips/register", data=chip(player_member_0="32200001", player_member_1="32200002"))
    assert [p.member for p in next(iter(teams(app).values())).players] == ["32200001", "32200002"]


def test_switch_off_old_rosters_and_the_director(app):
    ev = app.config["hub"].get("chips")
    t = ev.load()
    old = t.register_team("Owls", [Player("Cy Day", "9045550100", 3), Player("Di Fox", "", 3)])  # from before
    ev.store.save(t)
    owls = app.test_client()
    with owls.session_transaction() as s:
        s[f"team_{old.id}"] = True
    roster = {"name": "Owls", "player_name_0": "Cy Day", "player_skill_0": "4", "player_name_1": "Di Fox",
              "player_skill_1": "3", "player_phone_0": ""}
    owls.post(f"/t/chips/team/{old.id}/roster", data=roster)
    assert teams(app)[old.id].players[0].skill == 4  # old players without numbers can still save
    r = owls.post(f"/t/chips/team/{old.id}/roster", data={**roster, "player_name_1": "Ed Kim"}, follow_redirects=True)
    assert "Enter Ed Kim&#39;s APA member number" in r.get_data(as_text=True)  # a new partner needs one

    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    admin.post("/t/chips/register", data=chip(name="Director Team", player_name_0="Gus Hay", player_name_1="Hal Ito"))
    assert any(x.name == "Director Team" for x in teams(app).values())  # the director isn't held to it

    t = ev.load()
    t.settings.require_member = False
    ev.store.save(t)
    app.test_client().post("/t/chips/register", data=chip(name="Open", player_name_0="Ivy Joy", player_name_1="Jo Kay"))
    assert any(x.name == "Open" for x in teams(app).values())
