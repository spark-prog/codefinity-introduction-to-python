"""Registering with member numbers: one player, one spot; autofill for logged-in players; already registered."""

import random
from datetime import datetime, timedelta

import pytest

from app import create_app
from chips import backup, players
from chips.bracket import BracketTournament
from chips.engine import Player, Tournament, TournamentError
from tests.test_player_login import FakeMailer
from tests.test_players import HEADERS, row


def p(name, member="", skill=4):
    return Player(name, "9045550100", skill, member=member)


def test_chip_one_player_one_spot():
    t = Tournament()
    t.register_team("Sharks", [p("Ann Lee", "32200001"), p("Bo Ray", "32200002")])
    with pytest.raises(TournamentError, match="Ann Lee is already registered for Chip Tournament \\(team Sharks\\)"):
        t.register_team("Jets", [p("Ann Lee", "32200001"), p("Cy Day", "32200003")])
    with pytest.raises(TournamentError, match="listed twice"):
        t.register_team("Jets", [p("Cy Day", "32200003"), p("Cy Day", "32200003")])
    jets = t.register_team("Jets", [p("Cy Day", "32200003"), p("Di Fox")])
    with pytest.raises(TournamentError, match="Bo Ray is already registered"):
        t.update_roster(jets.id, [p("Cy Day"), p("Bo Ray", "32200002")])  # swapping in someone on another team
    t.update_roster(jets.id, [p("Cy Day"), p("Di Fox", "32200004")])
    assert [x.member for x in t.teams[jets.id].players] == ["32200003", "32200004"]  # Cy kept their number
    no_member = t.register_team("Owls", [p("Ann Lee"), p("Ed Kim")])  # typed by hand: no number to check
    assert no_member.id in t.teams


def test_bracket_one_player_one_spot():
    t = BracketTournament()
    t.settings.team_size = 1
    t.register_entry("", [p("Ann Lee", "32200001")])
    with pytest.raises(TournamentError, match="Ann Lee is already registered"):
        t.register_entry("Annie", [p("Ann Lee", "32200001")])
    e = t.register_entry("", [p("Bo Ray", "32200002")])
    t.update_entry(e.id, None, [p("Bo Ray")])
    assert t.entries[e.id].players[0].member == "32200002"


class Clients:
    def __init__(self, app):
        self.app = app

    def logged_in(self, member):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["login_member"] = member
        code = self.app.config["accounts"].new_code(member, datetime.now() - timedelta(minutes=2))
        c.post("/login/code", data={"code": code})
        return c


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: FakeMailer()))
    app = create_app(str(tmp_path))
    app.testing = True
    rows = [HEADERS, row("32200001", first="Ann", last="Lee", sl8="4", sl9="5", email="ann@example.com"),
            row("32200002", first="Bo", last="Ray", sl8="0", sl9="6", mp9="40", email="bo@example.com"),
            row("32200003", first="Cy", last="Day", sl8="3", sl9="3", email="cy@example.com")]
    app.config["players"].apply(*players.parse_rows(rows), "test", "test")
    ev = app.config["hub"].create("Masquerade", "chip")
    t = ev.load()
    t.settings.require_checkin = False
    ev.store.save(t)
    return app


def form(**kw):
    base = {"name": "", "agree_terms": "1", "player_name_0": "", "player_name_1": "", "player_skill_0": "",
            "player_skill_1": "", "player_phone_0": "904-555-0100"}
    base.update(kw)
    return base


def test_logged_in_registration_autofill_and_already_registered(app):
    clients = Clients(app)
    anon = app.test_client()
    page = anon.get("/t/masquerade/register").get_data(as_text=True)
    assert "fill this in for you" in page and "data-lookup" not in page
    assert anon.get("/t/masquerade/player-lookup?q=bo").status_code == 403

    ann = clients.logged_in("32200001")
    page = ann.get("/t/masquerade/register").get_data(as_text=True)
    assert 'value="Ann Lee"' in page and 'value="32200001"' in page and 'value="4"' in page  # 8-Ball SL
    assert 'value="904-555-0100"' in page  # her own cell

    found = ann.get("/t/masquerade/player-lookup?q=bo").get_json()
    assert found[0]["name"] == "Bo Ray" and found[0]["skill"] == 6 and "converted from 9-Ball" in found[0]["how"]
    assert "phone" not in found[0] and "email" not in found[0] and "cell" not in found[0]

    r = ann.post("/t/masquerade/register", data=form(player_name_0="Ann Lee", player_member_0="32200001",
                                                      player_skill_0="4", player_name_1="bo", player_member_1="32200002",
                                                      player_skill_1="6", sms_consent="1"))
    assert r.status_code == 302
    t = app.config["hub"].get("masquerade").load()
    team = next(iter(t.teams.values()))
    assert [(x.name, x.member) for x in team.players] == [("Ann Lee", "32200001"), ("Bo Ray", "32200002")]

    for who in ("32200001", "32200002"):  # both partners see it, and get the team page without the PIN
        c = ann if who == "32200001" else clients.logged_in(who)
        home = c.get("/t/masquerade/").get_data(as_text=True)
        assert "You're already registered" in home and f"/t/masquerade/team/{team.id}" in home
        assert 'class="button big disabled"' in home and "Register a team</a>" not in home
        assert 'class="nav-cta' not in home  # no Register in the menu either
        assert "<form method=\"post\" class=\"card form\">" not in c.get("/t/masquerade/register").get_data(as_text=True)
        assert "Enter your team PIN" not in c.get(f"/t/masquerade/team/{team.id}").get_data(as_text=True)
    assert ann.get("/t/masquerade/player-lookup?q=bo").get_json()[0]["registered"] is True

    cy = clients.logged_in("32200003")
    r = cy.post("/t/masquerade/register", data=form(player_name_0="Cy Day", player_member_0="32200003",
                                                     player_skill_0="3", player_name_1="Bo Ray",
                                                     player_member_1="32200002", player_skill_1="6"),
                follow_redirects=True)
    assert "Bo Ray is already registered" in r.get_data(as_text=True) and len(
        app.config["hub"].get("masquerade").load().teams) == 1


def test_typed_member_numbers_are_checked(app):
    """Anyone can type a member number: a matching name is verified, anything else waits for the director."""
    r = app.test_client().post("/t/masquerade/register", data=form(
        name="Zeds", player_name_0="Zed One", player_skill_0="3", player_member_0="32200001",
        player_name_1="cy  day", player_skill_1="3", player_member_1="322-00003"))
    assert r.status_code == 302
    team = next(iter(app.config["hub"].get("masquerade").load().teams.values()))
    zed, cy = team.players
    assert (zed.member, zed.pending) == ("32200001", "The player list has 32200001 as Ann Lee")
    assert (cy.name, cy.member, cy.pending) == ("Cy Day", "32200003", "")
    r = app.test_client().post("/t/masquerade/register", data=form(
        name="Owls", player_name_0="New Member", player_skill_0="3", player_member_0="32299999",
        player_name_1="Typo Guy", player_skill_1="3", player_member_1="1234"), follow_redirects=True)
    assert "isn&#39;t a member number" in r.get_data(as_text=True)


def test_bracket_prefill_both_skill_levels(app):
    ev = app.config["hub"].create("Movin", "bracket", format="modified")
    t = ev.load()
    t.settings.team_size, t.settings.skill_groups, t.settings.race_method = 1, [[1, 3], [4, 9]], "rack_chart"
    ev.store.save(t)
    page = Clients(app).logged_in("32200001").get("/t/movin/register").get_data(as_text=True)
    assert 'value="Ann Lee"' in page and 'name="player_skill8_0" type="number" min="2" max="7" inputmode="numeric" value="4"' in page
    assert 'value="5"' in page and "data-lookup" in page
