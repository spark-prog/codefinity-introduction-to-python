"""The director's exception: a player allowed on more than one team, with a note."""

import pytest

from app import create_app
from chips import pending
from chips.engine import Player, Tournament, TournamentError


def p(name, member):
    return Player(name, "9045550100", 3, member=member)


def test_exception_lets_a_player_on_a_second_team_but_never_twice_on_one():
    t = Tournament()
    t.register_team("Sharks", [p("Ann Lee", "32200001"), p("Bo Ray", "32200002")])
    with pytest.raises(TournamentError, match="ask the director to allow it"):
        t.register_team("Jets", [p("Ann Lee", "32200001"), p("Cy Day", "32200003")])
    with pytest.raises(TournamentError, match="Add a note"):
        pending.allow_twice(t, "32200001", "  ")
    pending.allow_twice(t, "32200001", "Filling in for Jets")
    t.register_team("Jets", [p("Ann Lee", "32200001"), p("Cy Day", "32200003")])
    assert pending.on_teams(t, "32200001") == ["Sharks", "Jets"]
    with pytest.raises(TournamentError, match="listed twice"):
        t.register_team("Owls", [p("Ann Lee", "32200001"), p("Ann Lee", "32200001")])
    assert any("allowed 32200001 on more than one team: Filling in for Jets" in line for line in t.log)
    pending.remove_exception(t, "32200001")
    assert len(t.teams) == 2  # registrations already made stay
    with pytest.raises(TournamentError, match="already registered"):
        t.register_team("Owls", [p("Ann Lee", "32200001"), p("Di Fox", "32200004")])


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.setenv("VIEWER_PASSWORD", "viewpw123")
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["hub"].create("Singles", "bracket", format="single")
    t = app.config["hub"].get("singles").load()
    t.settings.team_size = 1
    t.register_entry("", [p("Ann Lee", "32200001")])
    app.config["hub"].get("singles").store.save(t)
    return app


def test_director_adds_and_removes_an_exception(app):
    admin, viewer = app.test_client(), app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    viewer.post("/admin/login", data={"password": "viewpw123"})
    ev = app.config["hub"].get("singles")
    assert 'id="exceptions"' in admin.get("/t/singles/admin").get_data(as_text=True)
    viewer.post("/t/singles/admin/exceptions", data={"member": "32200001", "note": "x"})
    assert ev.load().settings.member_exceptions == []
    r = admin.post("/t/singles/admin/exceptions", data={"member": "1234", "note": "x"}, follow_redirects=True)
    assert "8 digits" in r.get_data(as_text=True)
    admin.post("/t/singles/admin/exceptions", data={"member": "322-00001", "note": "Second flight allowed"})
    page = admin.get("/t/singles/admin").get_data(as_text=True)
    assert "Second flight allowed" in page and "Exceptions (1)" in page and "on Ann Lee" in page
    r = app.test_client().post("/t/singles/register", data={"agree_terms": "1", "name": "Ann (2)",
                                                            "player_name_0": "Ann Lee", "player_skill_0": "4",
                                                            "player_member_0": "32200001",
                                                            "player_phone_0": "904-555-0100"})
    assert r.status_code == 302 and len(ev.load().entries) == 2
    admin.post("/t/singles/admin/exceptions", data={"member": "32200001", "action": "remove"})
    assert ev.load().settings.member_exceptions == [] and len(ev.load().entries) == 2
