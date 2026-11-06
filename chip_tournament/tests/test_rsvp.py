"""Teams the league office adds: no online registration, and one player answers for the team (RSVP)."""

import random
from datetime import datetime

import pytest

from app import create_app
from chips import backup, players, reminders
from chips.bracket import BracketTournament
from chips.engine import Player, TournamentError
from tests.test_player_login import FakeMailer
from tests.test_register_members import HEADERS, Clients, row


# ------------------------------------------------------------------ the engine

def event():
    t = BracketTournament()
    t.settings.team_size, t.settings.office_adds_teams, t.settings.payouts = 2, True, []
    a = t.register_entry("Sharks", [Player("Ann Lee", member="32200001"), Player("Bo Ray")], by_director=True)
    b = t.register_entry("Jets", [Player("Cy Day"), Player("Di Fox")], by_director=True)
    return t, a, b


def test_a_team_is_answered_for_and_the_counts_follow():
    t, sharks, jets = event()
    assert t.rsvp_counts() == {"attending": 0, "out": 0, "": 2}
    t.rsvp(sharks.id, "attending", "Ann Lee")
    assert (sharks.rsvp, sharks.rsvp_by) == ("attending", "Ann Lee") and sharks.rsvp_at
    t.rsvp(jets.id, "out", "Cy Day")
    assert t.rsvp_counts() == {"attending": 1, "out": 1, "": 0}
    t.rsvp(jets.id, "attending", "Di Fox")  # a different player changes the team's answer
    assert (jets.rsvp, jets.rsvp_by) == ("attending", "Di Fox")
    t.rsvp(jets.id, "", "Di Fox")  # cleared
    assert (jets.rsvp, jets.rsvp_by, jets.rsvp_at) == ("", "", "")
    assert any("Sharks: attending (answered by Ann Lee)" in x for x in t.log)


def test_rsvp_rules():
    t, sharks, _ = event()
    with pytest.raises(TournamentError, match="Pick attending"):
        t.rsvp(sharks.id, "maybe")
    t.settings.office_adds_teams = False
    with pytest.raises(TournamentError, match="doesn't ask teams"):
        t.rsvp(sharks.id, "attending")
    t.settings.office_adds_teams = True
    t.settings.team_size = 2
    t.register_entry("Solo A", [Player("E One"), Player("F Two")], by_director=True)
    t.register_entry("Solo B", [Player("G Three"), Player("H Four")], by_director=True)
    t.start(random.Random(1))
    with pytest.raises(TournamentError, match="too late"):
        t.rsvp(sharks.id, "attending")


def test_nobody_unregisters_from_a_team_the_office_added():
    t, sharks, _ = event()
    with pytest.raises(TournamentError, match="league office adds the teams"):
        t.unregister(sharks.id)
    t.remove_entry(sharks.id)  # the director can still remove it
    assert sharks.id not in t.entries


def test_online_registration_is_shut_for_these_events():
    t, *_ = event()
    assert t.settings.registration_shut
    assert "league office adds the teams" in __import__("chips.schedule", fromlist=["x"]).shut_message(t.settings)


def test_the_reminder_asks_the_team_to_confirm():
    t, sharks, jets = event()
    t.settings.name, t.settings.date, t.settings.start_time = "Vegas Qualifier", "2026-11-21", "10:00 AM"
    t.site_url = "https://controltable.app"
    now = datetime(2026, 11, 20, 10, 0)
    _, text = reminders.email_text(t, "vegas", "bracket", sharks, reminders.DAY_BEFORE, now=now)
    assert "Please confirm your team is coming" in text and "/t/vegas/entry/" in text
    assert "Unregister" not in text and "Can't make it on your team page" in text
    t.rsvp(sharks.id, "attending", "Ann Lee")
    _, text = reminders.email_text(t, "vegas", "bracket", sharks, reminders.DAY_BEFORE, now=now)
    assert "has confirmed it's coming" in text


# ------------------------------------------------------------------ the pages

@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: FakeMailer()))
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["players"].apply(*players.parse_rows([
        HEADERS, row("32200001", first="Ann", last="Lee", email="ann@x.com"),
        row("32200002", first="Bo", last="Ray", email="bo@x.com"),
        row("32200003", first="Cy", last="Day", email="cy@x.com"),
        row("32200009", first="Zed", last="Nobody", email="zed@x.com")]), "t", "t")
    return app


def qualifier(app):
    ev = app.config["hub"].create("Vegas Qualifier", "bracket", format="modified")
    t = ev.load()
    s = t.settings
    s.team_size, s.office_adds_teams, s.date, s.start_time, s.venue = 2, True, "2026-11-21", "10:00 AM", "Park Ave"
    t.register_entry("Sharks", [Player("Ann Lee", member="32200001"), Player("Bo Ray", member="32200002")], by_director=True)
    t.register_entry("Jets", [Player("Cy Day"), Player("Di Fox")], by_director=True)  # no member numbers
    ev.store.save(t)
    return ev, {e.name: e for e in t.entries.values()}


def director(app):
    c = app.test_client()
    c.post("/admin/login", data={"password": "secretpw1"})
    return c


def test_no_register_button_and_a_message_instead(app):
    ev, _ = qualifier(app)
    c = app.test_client()
    home = c.get(f"/t/{ev.id}/").get_data(as_text=True)
    assert ">Register</a>" not in home and "The league office adds the teams for this event" in home
    assert "Be the first to register" not in home and "Sharks" in home and f'href="/t/{ev.id}/entry/' in home
    reg = c.get(f"/t/{ev.id}/register").get_data(as_text=True)
    assert "league office adds the teams" in reg and 'name="player_name_0"' not in reg
    r = c.post(f"/t/{ev.id}/register", data={"agree_terms": "1", "player_name_0": "X Y", "player_name_1": "Z W",
                                              "player_phone_0": "904-555-0100"}, follow_redirects=True)
    assert "nothing to register for" in r.get_data(as_text=True) and len(ev.load().entries) == 2
    assert "Teams added by the league office" in c.get("/?view=player").get_data(as_text=True)


def test_a_player_with_the_team_pin_answers_for_the_team(app):
    ev, team = qualifier(app)
    sharks = team["Sharks"]
    c = app.test_client()
    page = c.get(f"/t/{ev.id}/entry/{sharks.id}").get_data(as_text=True)
    assert "Is your team coming?" in page and "Enter your team PIN" in page and 'name="answer"' not in page
    c.post(f"/t/{ev.id}/entry/{sharks.id}/rsvp", data={"answer": "attending"}, follow_redirects=True)
    assert ev.load().entries[sharks.id].rsvp == ""  # not signed in: refused
    c.post(f"/t/{ev.id}/entry/{sharks.id}/login", data={"pin": sharks.pin})
    page = c.get(f"/t/{ev.id}/entry/{sharks.id}").get_data(as_text=True)
    assert 'name="answer" value="attending"' in page and "No answer yet" in page and 'id="unregister"' not in page
    r = c.post(f"/t/{ev.id}/entry/{sharks.id}/rsvp", data={"answer": "attending"}, follow_redirects=True)
    assert "Sharks is marked as attending" in r.get_data(as_text=True)
    e = ev.load().entries[sharks.id]
    assert (e.rsvp, e.rsvp_by) == ("attending", "a player on the team")
    r = c.post(f"/t/{ev.id}/entry/{sharks.id}/rsvp", data={"answer": "out"}, follow_redirects=True)
    assert "can't make it" in r.get_data(as_text=True) and ev.load().entries[sharks.id].rsvp == "out"


def test_a_logged_in_player_answers_by_member_number_or_by_name(app):
    ev, team = qualifier(app)
    ann = Clients(app).logged_in("32200001")  # on Sharks by member number: straight in, no PIN
    page = ann.get(f"/t/{ev.id}/entry/{team['Sharks'].id}").get_data(as_text=True)
    assert 'name="answer" value="attending"' in page
    ann.post(f"/t/{ev.id}/entry/{team['Sharks'].id}/rsvp", data={"answer": "attending"})
    e = ev.load().entries[team["Sharks"].id]
    assert (e.rsvp, e.rsvp_by) == ("attending", "Ann Lee")
    # My account shows the question, and answering sends them back there
    me = ann.get("/me").get_data(as_text=True)
    assert "Your team is attending" in me and f"/t/{ev.id}/entry/{team['Sharks'].id}/rsvp" in me
    cy = Clients(app).logged_in("32200003")  # Cy is on Jets by name only (no member number was entered)
    me = cy.get("/me").get_data(as_text=True)
    assert "Is your team coming?" in me
    r = cy.post(f"/t/{ev.id}/entry/{team['Jets'].id}/rsvp", data={"answer": "out", "next": "/me#upcoming"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/me#upcoming")
    assert ev.load().entries[team["Jets"].id].rsvp_by == "Cy Day"
    page = cy.get(f"/t/{ev.id}/entry/{team['Jets'].id}").get_data(as_text=True)
    assert 'name="answer" value="attending"' in page  # a name match sees the buttons too
    # someone who isn't on either team can't answer
    zed = Clients(app).logged_in("32200009")
    r = zed.post(f"/t/{ev.id}/entry/{team['Jets'].id}/rsvp", data={"answer": "attending"}, follow_redirects=True)
    assert "Log in as a player on this team" in r.get_data(as_text=True)
    assert ev.load().entries[team["Jets"].id].rsvp == "out"
    r = ann.post(f"/t/{ev.id}/entry/{team['Jets'].id}/rsvp", data={"answer": "attending"}, follow_redirects=True)
    assert "Log in as a player on this team" in r.get_data(as_text=True)  # Ann is on Sharks, not Jets
    assert "//evil" not in ann.post(f"/t/{ev.id}/entry/{team['Sharks'].id}/rsvp",
                                    data={"answer": "attending", "next": "//evil.example"}).headers["Location"]


def test_the_director_sees_and_sets_rsvps(app):
    ev, team = qualifier(app)
    d = director(app)
    page = d.get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert "RSVPs:" in page and "0 attending" in page and "2 no reply" in page and "No reply yet" in page
    d.post(f"/t/{ev.id}/admin/entry/{team['Sharks'].id}/rsvp", data={"answer": "attending"})
    d.post(f"/t/{ev.id}/admin/entry/{team['Jets'].id}/rsvp", data={"answer": "out"})
    page = d.get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert "1 attending" in page and "1 can't make it" in page and "0 no reply" in page
    assert "✓ Attending" in page and "✗ Can't make it" in page and "the league office" in page
    assert ev.load().entries[team["Sharks"].id].rsvp_by == "the league office"
    csv = d.get(f"/t/{ev.id}/admin/export.csv").get_data(as_text=True)
    assert "RSVP" in csv.splitlines()[0] and "attending" in csv and "can't make it" in csv
    # nothing public
    public = app.test_client().get(f"/t/{ev.id}/").get_data(as_text=True)
    assert "can't make it" not in public.lower() and "attending" not in public.lower()
    nobody = app.test_client().post(f"/t/{ev.id}/admin/entry/{team['Jets'].id}/rsvp", data={"answer": ""})
    assert nobody.status_code in (302, 401, 403) and ev.load().entries[team["Jets"].id].rsvp == "out"


def test_the_director_adds_a_team_with_member_numbers_one_at_a_time(app):
    ev, _ = qualifier(app)
    d = director(app)
    d.post(f"/t/{ev.id}/admin/entries/add", data={"name": "Kings", "player_name_0": "Zed Nobody", "player_member_0": "32200009",
                                                  "player_name_1": "Walk In", "player_member_1": ""})
    t = ev.load()
    kings = next(e for e in t.entries.values() if e.name == "Kings")
    assert kings.players[0].member == "32200009" and kings.players[1].member == "" and len(t.entries) == 3
    page = d.get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert 'name="player_member_0"' in page and "APA member number" in page
    # a member number can be added to a team that was added without one
    jets = next(e for e in t.entries.values() if e.name == "Jets")
    d.post(f"/t/{ev.id}/admin/entry/{jets.id}/edit", data={"name": "Jets", "player_name_0": "Cy Day", "player_member_0": "32200003",
                                                           "player_name_1": "Di Fox", "player_member_1": ""})
    assert ev.load().entries[jets.id].players[0].member == "32200003"
    bad = d.post(f"/t/{ev.id}/admin/entries/add", data={"name": "Bad", "player_name_0": "A B", "player_member_0": "12",
                                                        "player_name_1": "C D"}, follow_redirects=True)
    assert "isn&#39;t a member number" in bad.get_data(as_text=True) and len(ev.load().entries) == 3


def test_the_setting_turns_it_on_and_off(app):
    ev = app.config["hub"].create("Vegas Qualifier", "bracket", format="single")
    d = director(app)
    s = ev.load().settings
    form = {"name": s.name, "format": s.format, "team_size": "2", "seeding": s.seeding, "final_cut": str(s.final_cut),
            "groups": "1", "advance": "0", "race_method": s.race_method, "race_chart": s.race_chart, "race_winners": "5",
            "race_losers": "4", "race_finals": "5", "entry_fee": "0", "added_money": "0", "field_cap": "0",
            "tables": ", ".join(s.tables), "call_mode": s.call_mode, "match_minutes": "30", "long_match_minutes": "45",
            "no_rack_minutes": "15"}
    page = d.get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert 'name="office_adds_teams"' in page and "The league office adds the teams" in page
    d.post(f"/t/{ev.id}/admin/settings", data={**form, "office_adds_shown": "1", "office_adds_teams": "1"})
    assert ev.load().settings.office_adds_teams is True
    d.post(f"/t/{ev.id}/admin/settings", data={**form, "office_adds_shown": "1"})
    assert ev.load().settings.office_adds_teams is False
    d.post(f"/t/{ev.id}/admin/settings", data={**form, "office_adds_shown": "1", "office_adds_teams": "1"})
    d.post(f"/t/{ev.id}/admin/settings", data=form)  # a form without the marker leaves it alone
    assert ev.load().settings.office_adds_teams is True
