"""Skill groups (one bracket per skill range) and the rack race chart, for singles bracket events."""

import random

import pytest

from app import create_app
from chips.bracket import BracketTournament
from chips.engine import Player, TournamentError
from chips.races import RACK_RACE_SINGLES, parse_skill_groups, rack_race

MOVIN = [[1, 3], [4, 4], [5, 5], [6, 6], [7, 9]]


def test_chart_mirrors_and_has_the_corrected_cells():
    for mine, row in RACK_RACE_SINGLES.items():
        for theirs, cell in row.items():
            other = RACK_RACE_SINGLES[theirs][mine]
            assert (cell is None) == (other is None)
            if cell:
                assert cell == (other[1], other[0]), (mine, theirs)
    assert rack_race(6, 6) == (5, 5) and rack_race(6, 5) == (6, 5) and rack_race(5, 6) == (5, 6)
    assert rack_race(2, 4) == (2, 4) and rack_race(2, 5) == (2, 5) and rack_race(1, 2) == (1, 2)
    assert rack_race(1, 3) == (2, 2) and rack_race(2, 2) == (3, 3) and rack_race(9, 9) == (6, 6)
    assert rack_race(8, 5) == (5, 2) and rack_race(4, 7) == (3, 6)
    assert rack_race(3, 8) == (2, 5) and rack_race(8, 3) == (5, 2)  # never on the chart: the low side races as an SL 5


def test_parse_skill_groups():
    assert parse_skill_groups("1-3, 4, 5, 6, 7-9") == MOVIN
    assert parse_skill_groups("SL 7–9; SL 1-6") == [[1, 6], [7, 9]]
    assert parse_skill_groups("") == []
    for bad in ("1-3, 3-5", "0-2, 3", "5-3, 6", "abc, 4", "1-9"):
        with pytest.raises(TournamentError):
            parse_skill_groups(bad)


def event(counts, fmt="modified", groups=MOVIN, chart=True):
    """counts: {skill level: how many players}; 8-Ball level when 2-7, else 9-Ball."""
    t = BracketTournament()
    t.settings.format, t.settings.final_cut, t.settings.skill_groups = fmt, 4, groups
    t.settings.race_method = "rack_chart" if chart else "straight"
    t.settings.payouts = []
    n = 0
    for sl, how_many in counts.items():
        for _ in range(how_many):
            n += 1
            levels = {"skill_8": sl} if 2 <= sl <= 7 else {"skill_9": sl}
            t.register_entry(f"P{n}", [Player(f"P{n}", **levels)])
    return t


def test_player_goes_in_the_group_for_their_higher_level():
    t = BracketTournament()
    t.settings.skill_groups = MOVIN
    a = t.register_entry("", [Player("Ann", skill_8=4, skill_9=6)])
    b = t.register_entry("", [Player("Bob", skill_9=2)])
    c = t.register_entry("", [Player("Cy", skill_8=7, skill_9=3)])
    assert (a.skill, a.division) == (6, "SL 6") and b.division == "SL 1–3" and c.division == "SL 7–9"
    with pytest.raises(TournamentError):
        t.register_entry("", [Player("Di", skill_8=8)])  # 8-Ball levels go 2 to 7


def test_small_groups_join_the_neighbor_with_fewer_players():
    t = event({2: 6, 4: 2, 5: 7, 6: 5, 8: 3})
    t.start(random.Random(1))
    assert t.division_names == ["SL 1–4", "SL 5", "SL 6–9"]
    sizes = {d: len(t.division_entries(d)) for d in t.division_names}
    assert sizes == {"SL 1–4": 8, "SL 5": 7, "SL 6–9": 8}
    assert any("SL 4 (2) was too small" in line for line in t.log)


def test_a_tie_joins_the_lower_group_and_merges_repeat():
    t = event({4: 6, 5: 1, 6: 6})
    t.start(random.Random(1))
    assert t.division_names == ["SL 4–5", "SL 6"]
    t = event({2: 2, 4: 2, 5: 2})  # every group small: they keep joining until one can play
    t.start(random.Random(1))
    assert t.division_names == ["SL 1–5"]


def test_each_group_plays_its_own_bracket_to_its_own_champion():
    t = event({2: 5, 5: 6, 7: 5})
    t.start(random.Random(3))
    rng = random.Random(3)
    for m in t.matches:
        assert all(t.entries[x].division == m.division for x in (m.a, m.b) if x and x != "bye")
    while t.status == "running":
        m = next(m for m in t.matches if m.status == "called")
        while not t.race_winner(m):
            t.score(m.id, rng.choice("ab"), 1)
        t.submit_result(m.id)
    places = t.places()
    for d in t.division_names:
        members = t.division_entries(d)
        assert [e.status for e in members].count("champion") == 1
        assert sorted(places[e.id][0] for e in members)[:2] == [1, 2]  # a champion and a runner-up
    assert all(e.payout_cents == 0 for e in t.entries.values())  # prizes, not cash


def test_rack_chart_sets_each_sides_race():
    t = event({6: 1, 5: 1}, fmt="single", groups=[])
    t.start(random.Random(1))
    m = next(m for m in t.matches if m.status == "called")
    six = "a" if t.entries[m.a].skill == 6 else "b"
    five = "b" if six == "a" else "a"
    assert (t.race_of(m, six), t.race_of(m, five)) == (6, 5)
    for _ in range(5):
        t.score(m.id, five, 1)
    with pytest.raises(TournamentError):
        t.score(m.id, five, 1)  # the SL 5 needs only 5
    assert t.race_winner(m) == getattr(m, five)
    texts = [x.body for x in t.outbox]
    assert any("you race to 6, they race to 5" in b for b in texts) or not any(e.phones for e in t.entries.values())
    t.submit_result(m.id)
    assert t.status == "finished"


def test_draw_needs_skill_levels():
    t = event({}, fmt="single", groups=MOVIN)
    t.register_entry("", [Player("Ann")])  # added by name only
    t.register_entry("", [Player("Bob", skill_8=4)])
    with pytest.raises(TournamentError, match="Add a skill level first"):
        t.start()


# ------------------------------------------------------------------- web


def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "VIEWER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    return app, admin


def test_movin_on_up_setup_and_registration(tmp_path, monkeypatch):
    app, admin = client(tmp_path, monkeypatch)
    ev = app.config["hub"].create("Movin-On-Up", "bracket")
    s = ev.load().settings
    base = f"/t/{ev.id}"
    admin.post(f"{base}/admin/settings", data={
        "name": s.name, "tables": "1, 2, 3", "format": "modified", "team_size": "1", "final_cut": "4",
        "skill_groups": "1-3, 4, 5, 6, 7-9", "race_method": "rack_chart", "payouts": "", "entry_fee": "0",
        "race_winners": "3", "race_losers": "3", "race_finals": "3"})
    s = ev.load().settings
    assert s.skill_groups == MOVIN and s.race_method == "rack_chart" and s.payouts == [] and s.entry_fee_cents == 0
    player = app.test_client()
    page = player.get(f"{base}/register").get_data(as_text=True)
    assert 'name="player_skill8_0"' in page and 'name="player_skill9_0"' in page and "SL 7–9" in page
    r = player.post(f"{base}/register", data={"agree_terms": "1", "player_name_0": "Ann"}, follow_redirects=True)
    assert b"Enter your 8-Ball or 9-Ball skill level" in r.data and not ev.load().entries
    r = player.post(f"{base}/register", data={"agree_terms": "1", "player_name_0": "Ann", "player_skill8_0": "4",
                                              "player_skill9_0": "6", "player_phone_0": "5551234567"},
                    follow_redirects=True)
    assert "You&#39;re in the SL 6 group" in r.get_data(as_text=True)
    ann = next(iter(ev.load().entries.values()))
    assert (ann.players[0].skill_8, ann.players[0].skill_9, ann.skill, ann.division) == (4, 6, 6, "SL 6")
    home = player.get(f"{base}/").get_data(as_text=True)
    assert "Skill groups" in home and "Rack race chart" in home and "SL 6 <span" in home

    # the director adds a player by name, then fills in their skill levels
    admin.post(f"{base}/admin/entries/add", data={"bulk": "Bob"})
    bob = next(e for e in ev.load().entries.values() if e.name == "Bob")
    assert "needs a skill level" in admin.get(f"{base}/admin").get_data(as_text=True)
    admin.post(f"{base}/admin/entry/{bob.id}/edit", data={"name": "Bob", "player_name_0": "Bob", "player_skill9_0": "8"})
    bob = ev.load().entries[bob.id]
    assert (bob.skill, bob.division) == (8, "SL 7–9")
