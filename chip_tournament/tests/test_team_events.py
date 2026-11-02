"""Team tournaments (3 to 10 players per entry) next to singles and doubles, optional skill-level limits, and
Save buttons at the top and bottom of the settings."""

import copy
import json
import random
from pathlib import Path

import pytest

from app import create_app
from bracket_web import parse_entry_line
from chips import bonus_event
from chips.bracket import MAX_TEAM_SIZE, entries_label
from chips.engine import Player, TournamentError

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "bonus_event_halloween.json").read_text())


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("VIEWER_PASSWORD", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


def director(app):
    c = app.test_client()
    c.post("/admin/login", data={"password": "secretpw1"})
    return c


def save(c, ev, **fields):
    t = ev.load()
    s = t.settings
    data = {"name": s.name, "format": s.format, "team_size": str(s.team_size), "seeding": s.seeding,
            "final_cut": str(s.final_cut), "groups": str(s.groups), "advance": str(s.advance),
            "race_method": s.race_method, "race_winners": str(s.race_winners), "race_losers": str(s.race_losers),
            "race_finals": str(s.race_finals), "entry_fee": "0", "added_money": "0", "field_cap": "0",
            "tables": ", ".join(s.tables), "call_mode": s.call_mode, "match_minutes": str(s.match_minutes),
            "long_match_minutes": str(s.long_match_minutes), "no_rack_minutes": str(s.no_rack_minutes),
            "max_player_skill": str(s.max_player_skill or ""), "team_skill_cap": str(s.team_skill_cap or "")}
    data.update({k: str(v) for k, v in fields.items()})
    return c.post(f"/t/{ev.id}/admin/settings", data=data, follow_redirects=True).get_data(as_text=True)


def team_form(name, players, **extra):
    data = {"name": name, "agree_terms": "1", **extra}
    for i, (who, sl) in enumerate(players):
        data[f"player_name_{i}"] = who
        data[f"player_skill_{i}"] = str(sl) if sl else ""
        data[f"player_phone_{i}"] = "9045550100" if i == 0 else ""
    return data


def test_entries_can_be_singles_doubles_or_teams_of_3_to_10(app):
    ev = app.config["hub"].create("Team Night", "bracket", format="double")
    c = director(app)
    page = c.get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert "Team of 3 players" in page and f"Team of {MAX_TEAM_SIZE} players" in page
    form = page.split('<section id="settings"', 1)[1].split("</form>", 1)[0]
    assert form.count(">Save settings</button>") == 2  # top and bottom of the settings
    assert 'id="unsaved-save">Save settings</button>' in page  # and on the unsaved-changes bar
    save(c, ev, team_size=4)
    assert ev.load().settings.team_size == 4 and ev.load().entries_label == "teams of 4"
    assert "teams of 4" in app.test_client().get(f"/t/{ev.id}/").get_data(as_text=True)
    page = save(c, ev, team_size=MAX_TEAM_SIZE + 1)
    assert "teams of 3 to 10" in page and ev.load().settings.team_size == 4
    assert [entries_label(n) for n in (1, 2, 5)] == ["singles", "doubles", "teams of 5"]


def test_a_team_registers_with_a_name_and_every_player(app):
    ev = app.config["hub"].create("Team Night", "bracket", format="double")
    t = ev.load()
    t.settings.team_size = 4
    ev.store.save(t)
    page = app.test_client().get(f"/t/{ev.id}/register").get_data(as_text=True)
    assert page.count('name="player_name_') == 4 and 'name="name" value="" required' in page
    players = [("Ann Lee", 5), ("Bo Ray", 4), ("Cy Day", 3), ("Di Fox", 6)]
    r = app.test_client().post(f"/t/{ev.id}/register", data=team_form("", players), follow_redirects=True)
    assert "Enter a team name" in r.get_data(as_text=True) and not ev.load().entries
    r = app.test_client().post(f"/t/{ev.id}/register", data=team_form("Sharks", players[:3]), follow_redirects=True)
    assert "Enter all 4 player names" in r.get_data(as_text=True)
    app.test_client().post(f"/t/{ev.id}/register", data=team_form("Sharks", players))
    entry = next(iter(ev.load().entries.values()))
    assert entry.name == "Sharks" and [p.name for p in entry.players] == [p for p, _ in players]


def test_optional_skill_limits(app):
    ev = app.config["hub"].create("Team Night", "bracket", format="double")
    c = director(app)
    save(c, ev, team_size=3, max_player_skill=6, team_skill_cap=15)
    s = ev.load().settings
    assert (s.max_player_skill, s.team_skill_cap) == (6, 15)
    page = app.test_client().get(f"/t/{ev.id}/register").get_data(as_text=True)
    assert "every player SL 6 or lower" in page and "combined skill levels 15 or lower" in page

    def register(name, players):
        return app.test_client().post(f"/t/{ev.id}/register", data=team_form(name, players),
                                      follow_redirects=True).get_data(as_text=True)
    assert "SL 6 at most" not in register("Ok", [("A One", 5), ("B One", 5), ("C One", 5)])  # 15: right at the cap
    assert "skill level 6 at most: D Two (SL 7)" in register("High", [("D Two", 7), ("E Two", 3), ("F Two", 3)])
    assert "combined skill level is 16, over this event&#39;s cap of 15" in register(
        "Over", [("G Three", 6), ("H Three", 5), ("I Three", 5)])
    assert "Enter a skill level for K Four" in register("Blank", [("J Four", 4), ("K Four", 0), ("L Four", 4)])
    assert [e.name for e in ev.load().entries.values()] == ["Ok"]
    # A lower limit can't leave a team already in over it
    page = save(c, ev, team_skill_cap=14)
    assert "Ok&#39;s combined skill level is 15, over this event&#39;s cap of 14" in page
    assert ev.load().settings.team_skill_cap == 15
    save(c, ev, team_skill_cap="", max_player_skill="")  # blank = no limit
    assert (ev.load().settings.max_player_skill, ev.load().settings.team_skill_cap) == (0, 0)


def test_singles_can_have_a_max_skill_level_too(app):
    ev = app.config["hub"].create("Lower SL Singles", "bracket", format="single")
    c = director(app)
    save(c, ev, team_size=1, max_player_skill=4)
    t = ev.load()
    t.register_entry("", [Player("Ann Lee", "9045550100", 4)])
    with pytest.raises(TournamentError, match="skill level 4 at most: Bo Ray \\(SL 5\\)"):
        t.register_entry("", [Player("Bo Ray", "9045550100", 5)])


def test_the_director_adds_teams_with_skill_levels(app):
    ev = app.config["hub"].create("Team Night", "bracket", format="double")
    c = director(app)
    save(c, ev, team_size=3, team_skill_cap=15)
    assert parse_entry_line("Sharks: Ann Lee (5) & Bo Ray (4) and Cy Day (6)", 3)[1][2].skill == 6
    c.post(f"/t/{ev.id}/admin/entries/add", data={"bulk": "Sharks: Ann Lee (5) & Bo Ray (4) & Cy Day (6)\n"
                                                     "Jets: Di Fox (6) & Ed Kim (6) & Flo Ng (6)"})
    names = [e.name for e in ev.load().entries.values()]
    assert names == ["Sharks"]  # Jets are 18, over 15
    page = c.get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert 'name="player_skill_0" type="number" min="1" max="9" required' in page  # the one-entry form asks too


def test_a_team_bracket_plays_to_the_end(app):
    ev = app.config["hub"].create("Team Night", "bracket", format="double")
    t = ev.load()
    t.settings.team_size, t.settings.tables = 5, ["1", "2", "3"]
    for k in range(6):
        t.register_entry(f"Team {k}", [Player(f"P{k}-{i}", "9045550100", 4) for i in range(5)])
    t.start(random.Random(1))
    rng = random.Random(2)
    for _ in range(100):
        if t.status == "finished":
            break
        t.call_all() if t.assigned_matches() else None
        live = [m for m in t.matches if m.status == "called"]
        if live:
            m = live[0]
            t.record_result(m.id, rng.choice([m.a, m.b]))
    assert t.status == "finished"


def test_the_bonus_event_form_maps_teams_and_the_combined_cap():
    doc = copy.deepcopy(SAMPLE)
    doc["format"].update(tournament_structure="Double Elimination", players_per_entry=5)
    doc["eligibility"]["skill_level_limit"] = "Combined skill level 23 or lower"
    p = bonus_event.plan(doc)
    assert p.kind == "bracket" and p.settings["team_size"] == 5 and p.settings["team_skill_cap"] == 23
    assert ("Entries", "Teams of 5") in p.summary
