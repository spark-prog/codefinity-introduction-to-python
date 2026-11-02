"""Pending verification: typed member numbers the director checks."""

import pytest

from app import create_app
from chips import players
from tests.test_players import HEADERS, row


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.setenv("VIEWER_PASSWORD", "viewpw123")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["rows"] = [HEADERS, row("32200001", first="Ann", last="Lee", email="a@x.com"),
                          row("32200002", first="Bo", last="Ray", email="b@x.com"),
                          row("32200003", first="Cy", last="Day", email="c@x.com")]
    app.config["players"].apply(*players.parse_rows(app.config["rows"]), "test", "test")
    app.config["hub"].create("Chips", "chip")
    return app


def register(app, name, *people):
    """people: (name, member) pairs."""
    c = app.test_client()
    data = {"name": name, "agree_terms": "1", "player_phone_0": "904-555-0100", "sms_consent": "1"}
    for i, (who, member) in enumerate(people):
        data.update({f"player_name_{i}": who, f"player_skill_{i}": "3", f"player_member_{i}": member})
    assert c.post("/t/chips/register", data=data).status_code == 302
    return c


def team(app, name):
    return next(x for x in app.config["hub"].get("chips").load().teams.values() if x.name == name)


def test_pending_box_verify_correct_remove(app):
    sharks = register(app, "Sharks", ("Zed One", "32200001"), ("New Member", "32299999"))  # wrong name, unknown
    register(app, "Owls", ("Odd Name", "32200003"), ("Cy Day", ""))
    admin, viewer = app.test_client(), app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    viewer.post("/admin/login", data={"password": "viewpw123"})

    page = admin.get("/t/chips/admin").get_data(as_text=True)
    assert "Pending verification (3)" in page and "The player list has 32200001 as Ann Lee" in page
    assert "Not in the player list" in page
    notice = sharks.get(f"/t/chips/team/{team(app, 'Sharks').id}").get_data(as_text=True)
    assert "the league office is checking member number 32299999 for New Member" in notice

    sid = team(app, "Sharks").id
    viewer.post(f"/t/chips/admin/pending/{sid}/1", data={"action": "verify"})
    assert team(app, "Sharks").players[1].pending  # view-only can't
    admin.post(f"/t/chips/admin/pending/{sid}/1", data={"action": "verify"})  # a brand-new member
    assert team(app, "Sharks").players[1].pending == "" and team(app, "Sharks").players[1].member == "32299999"

    r = admin.post(f"/t/chips/admin/pending/{sid}/0", data={"action": "correct", "member": "32200003"},
                   follow_redirects=True)
    assert "already registered" in r.get_data(as_text=True)  # Cy's number is on Owls
    admin.post(f"/t/chips/admin/pending/{sid}/0", data={"action": "correct", "member": "322-00002"})
    zed = team(app, "Sharks").players[0]
    assert (zed.name, zed.member, zed.pending) == ("Bo Ray", "32200002", "")

    oid = team(app, "Owls").id
    r = admin.post(f"/t/chips/admin/pending/{oid}/0", data={"action": "remove"}, follow_redirects=True)
    assert "Removed Owls and texted them why" in r.get_data(as_text=True)
    assert all(x.name != "Owls" for x in app.config["hub"].get("chips").load().teams.values())
    assert any("couldn't verify a member number" in m["body"] for m in app.config["hub"].get("chips").sms.recent())
    assert "Pending verification (" not in admin.get("/t/chips/admin").get_data(as_text=True)


def test_now_in_the_list_and_roster_saves_keep_the_verdict(app):
    sharks = register(app, "Sharks", ("Dee Fox", "32200004"), ("Ann Lee", "32200001"))
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    assert "Verify as Dee Fox" not in admin.get("/t/chips/admin").get_data(as_text=True)
    app.config["players"].apply(*players.parse_rows(app.config["rows"] + [row("32200004", first="Dee", last="Fox")]),
                                "test", "test")
    page = admin.get("/t/chips/admin").get_data(as_text=True)
    assert "Verify as Dee Fox" in page and "Now in the player list as Dee Fox" in page
    sid = team(app, "Sharks").id
    admin.post(f"/t/chips/admin/pending/{sid}/0", data={"action": "verify"})
    # The team saves its roster: the verified numbers stay verified.
    sharks.post(f"/t/chips/team/{sid}/roster", data={"name": "Sharks", "player_name_0": "Dee Fox", "player_skill_0": "3",
                                                     "player_member_0": "32200004", "player_name_1": "Ann Lee",
                                                     "player_skill_1": "4", "player_member_1": "32200001",
                                                     "player_phone_0": ""})
    assert [(p.member, p.pending, p.skill) for p in team(app, "Sharks").players] == [("32200004", "", 3),
                                                                                    ("32200001", "", 4)]


def test_bracket_pending(app):
    ev = app.config["hub"].create("Singles", "bracket", format="single")
    t = ev.load()
    t.settings.team_size = 1
    ev.store.save(t)
    me = app.test_client()
    me.post("/t/singles/register", data={"agree_terms": "1", "player_name_0": "Ann Lee", "player_skill_0": "4",
                                         "player_member_0": "32211111", "player_phone_0": "904-555-0100"})
    entry = next(iter(ev.load().entries.values()))
    assert entry.players[0].pending == "Not in the player list"
    assert "the league office is checking member number 32211111" in me.get(f"/t/singles/entry/{entry.id}").get_data(as_text=True)
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    assert "Pending verification (1)" in admin.get("/t/singles/admin").get_data(as_text=True)
    admin.post(f"/t/singles/admin/pending/{entry.id}/0", data={"action": "correct", "member": "32200001"})
    p = ev.load().entries[entry.id].players[0]
    assert (p.member, p.pending) == ("32200001", "")


def test_a_first_name_alone_matches_the_player_list():
    from player_web import name_matches
    angelica = {"first": "Angelica", "last": "McCain", "nickname": ""}
    for typed, ok in (("Angel", True), ("angelica", True), ("Angel McCain", True), ("Angelica McCain", True),
                      ("An", False),  # too short to be sure
                      ("Angela", False), ("Angelica Smith", False), ("McCain", False), ("", False)):
        assert name_matches(typed, angelica) is ok, typed
    assert name_matches("Gabby", {"first": "Gabrielle", "last": "Dionysius", "nickname": "Gabby"})  # nickname
    assert name_matches("Solen", {"first": "Solen", "last": "Hills", "nickname": ""})


def test_first_name_only_registrations_go_straight_through_with_the_full_name(app):
    register(app, "Sharks", ("Ann", "32200001"), ("Bo", "32200002"))
    sharks = team(app, "Sharks")
    assert [(p.name, p.member, p.pending) for p in sharks.players] == [("Ann Lee", "32200001", ""), ("Bo Ray", "32200002", "")]
    page = app.test_client().get("/t/chips/register").get_data(as_text=True)
    assert "Full name (first and last)" in page


def test_verify_uses_the_player_lists_name_or_keeps_the_typed_one(app):
    register(app, "Sharks", ("Ann Jones", "32200001"), ("Bob Rays", "32200002"))  # married name; a typo
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    page = admin.get("/t/chips/admin").get_data(as_text=True)
    assert "Verify as Ann Lee" in page and "Keep &ldquo;Ann Jones&rdquo;" in page and "Verify as Bo Ray" in page
    sid = team(app, "Sharks").id
    admin.post(f"/t/chips/admin/pending/{sid}/0", data={"action": "verify", "keep": "1"})  # keeps her married name
    admin.post(f"/t/chips/admin/pending/{sid}/1", data={"action": "verify"})  # fixes the typo
    sharks = team(app, "Sharks")
    assert [(p.name, p.pending) for p in sharks.players] == [("Ann Jones", ""), ("Bo Ray", "")]
    log = app.config["hub"].get("chips").load().log
    assert any("Director verified Bo Ray (32200002)" in line and "(registered as Bob Rays)" in line for line in log)
