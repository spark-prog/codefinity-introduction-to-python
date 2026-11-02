"""Copy a tournament: same setup, a new name and date, none of the old event's players, results or history."""

import random

import pytest

from app import create_app
from chips.engine import Player, TournamentError


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.setenv("VIEWER_PASSWORD", "viewerpw1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


def director(app, password="secretpw1"):
    c = app.test_client()
    c.post("/admin/login", data={"password": password})
    return c


def movin_on_up(app):
    """A played bracket with plenty of setup and plenty of history."""
    ev = app.config["hub"].create("Movin-On-Up", "bracket", format="modified")
    t = ev.load()
    s = t.settings
    s.date, s.start_time, s.venue, s.details = "2026-11-21", "11:00 AM", "Park Ave", "Free, yearly."
    s.team_size, s.final_cut, s.skill_groups = 1, 4, [(1, 3), (4, 4), (5, 5), (6, 6), (7, 9)]
    s.race_method, s.series, s.entry_fee_cents, s.tables = "rack_chart", "Movin-On-Up", 0, ["1", "2", "3"]
    s.registration_opens, s.registration_closes = "2026-11-01T09:00", "2026-11-20T17:00"
    s.eligibility = {"active": True, "list_column": "MOU"}
    s.member_exceptions = [{"member": "32200001", "note": "ok"}]
    s.review_requests = [{"id": "r1", "member": "32200002", "status": "open"}]
    s.elig_overrides = [{"member": "32200003", "allow": True}]
    s.elig_locked_at, s.bonus_event_id, s.registration_closed = "2026-11-20T17:00", "be-123", True
    s.tables_down = ["2"]
    for i in range(10):
        t.register_entry("", [Player(f"Player {i}", "9045550100", 2 + i % 7)])
    t.start(random.Random(1))
    ev.store.save(t)
    return ev


def test_a_copy_has_the_setup_but_none_of_the_history(app):
    src = movin_on_up(app)
    new, notes = app.config["hub"].copy(src.id, "Movin-On-Up 2027", "2027-11-20")
    t, s = new.load(), new.load().settings
    assert new.kind == "bracket" and new.id != src.id and notes == []
    assert (s.name, s.date, s.start_time, s.venue, s.details) == ("Movin-On-Up 2027", "2027-11-20", "11:00 AM",
                                                                  "Park Ave", "Free, yearly.")
    assert (s.format, s.final_cut, s.race_method, s.series, s.tables) == ("modified", 4, "rack_chart", "Movin-On-Up",
                                                                          ["1", "2", "3"])
    assert [tuple(g) for g in s.skill_groups] == [(1, 3), (4, 4), (5, 5), (6, 6), (7, 9)]
    assert s.eligibility == {"active": True, "list_column": "MOU"}
    # 364 days later: the registration window moves with it, same times of day
    assert (s.registration_opens, s.registration_closes) == ("2027-10-31T09:00", "2027-11-19T17:00")
    assert t.status == "setup" and not t.entries and not t.matches and not t.waitlist
    assert (s.member_exceptions, s.review_requests, s.elig_overrides, s.elig_locked_at, s.bonus_event_id,
            s.registration_closed, s.tables_down) == ([], [], [], "", "", False, [])
    s.skill_groups.append((9, 9))  # a real copy: changing it doesn't touch the original
    assert len(src.load().settings.skill_groups) == 5
    assert src.load().status == "running" and len(src.load().entries) == 10  # the original is untouched


def test_copy_a_chip_tournament_and_a_team_bracket(app):
    hub = app.config["hub"]
    chip = hub.create("Masquer 8-Ball", "chip", date="2027-01-02")
    t = chip.load()
    t.settings.registration_cap, t.settings.race_to, t.settings.start_time = 11, 3, "5:00 PM"
    t.register_team("Sharks", [Player("Ann Lee", "9045550100", 5), Player("Cy Day", "", 4)])
    chip.store.save(t)
    copy, _ = hub.copy(chip.id, "Masquer 2028", "2028-01-01")
    s = copy.load().settings
    assert copy.kind == "chip" and (s.registration_cap, s.race_to, s.start_time) == (11, 3, "5:00 PM")
    assert not copy.load().teams
    team = hub.create("Team Night", "bracket", format="double")
    t = team.load()
    t.settings.team_size, t.settings.max_player_skill, t.settings.team_skill_cap = 5, 6, 23
    team.store.save(t)
    s = hub.copy(team.id, "Team Night 2", "")[0].load().settings
    assert (s.team_size, s.max_player_skill, s.team_skill_cap, s.date) == (5, 6, 23, "")


def test_without_a_date_the_registration_window_is_cleared(app):
    src = movin_on_up(app)
    new, notes = app.config["hub"].copy(src.id, "Next one", "")
    s = new.load().settings
    assert (s.date, s.registration_opens, s.registration_closes) == ("", "", "")
    assert notes == ["Registration open/close times were cleared: set them in Settings."]
    with pytest.raises(TournamentError, match="calendar"):
        app.config["hub"].copy(src.id, "Bad date", "21/11/2027")
    with pytest.raises(TournamentError, match="name"):
        app.config["hub"].copy(src.id, "  ", "2027-11-20")


def test_the_copy_button_and_page(app):
    src = movin_on_up(app)
    c = director(app)
    assert f"/admin/event/{src.id}/copy" in c.get("/admin").get_data(as_text=True)
    page = c.get(f"/admin/event/{src.id}/copy").get_data(as_text=True)
    assert 'value="Movin-On-Up (copy)"' in page and "move by as many days as the date does" in page
    r = c.post(f"/admin/event/{src.id}/copy", data={"name": "Movin-On-Up 2027", "date": "2027-11-20"})
    new = next(e for e in app.config["hub"].events() if e.id != src.id)
    assert r.headers["Location"].endswith(f"/t/{new.id}/admin#settings")
    assert "Copied Movin-On-Up to Movin-On-Up 2027" in c.get(r.headers["Location"]).get_data(as_text=True)
    r = c.post(f"/admin/event/{src.id}/copy", data={"name": "", "date": "2027-11-20"}, follow_redirects=True)
    assert "Give the tournament a name" in r.get_data(as_text=True) and len(app.config["hub"].events()) == 2


def test_test_events_stay_test_events_and_viewers_cant_copy(app):
    src = movin_on_up(app)
    t = src.load()
    t.settings.test_event = True
    src.store.save(t)
    c = director(app)
    r = c.post(f"/admin/event/{src.id}/copy", data={"name": "Practice 2", "date": "2027-11-20"}, follow_redirects=True)
    assert "untick Test event" in r.get_data(as_text=True)
    assert all(e.load().settings.test_event for e in app.config["hub"].events())
    viewer = director(app, "viewerpw1")
    assert "/copy" not in viewer.get("/admin").get_data(as_text=True)
    viewer.post(f"/admin/event/{src.id}/copy", data={"name": "Sneaky", "date": "2027-11-20"})
    assert len(app.config["hub"].events()) == 2
    assert app.test_client().get(f"/admin/event/{src.id}/copy").status_code == 302  # logged out: to the login
