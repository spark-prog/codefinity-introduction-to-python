"""The league's race charts: rack races (8-Ball and Saratoga) and ball counts (9-Ball), standard and adjusted."""

import copy
import random

import pytest

from app import create_app
from chips import race_charts
from chips.bracket import BracketTournament, CALLED
from chips.engine import Player, TournamentError
from chips.race_charts import ADJUSTED, BALL_COUNT, RACK_RACE, STANDARD, races


def P(name="X", s8=0, s9=0, skill=0):
    return Player(name, phone="9045550100", sms_consent=True, skill_8=s8, skill_9=s9, skill=skill or max(s8, s9))


def test_both_chart_files_load_and_mirror():
    for version in (STANDARD, ADJUSTED):
        race_charts.chart(version)  # checks itself
    bad = copy.deepcopy(race_charts.chart(STANDARD))
    bad["eight_ball"]["singles"]["5"]["3"]["you"] = 5
    with pytest.raises(TournamentError, match="5 vs 3"):
        race_charts.check(bad)


def test_rack_race_singles():
    assert races(RACK_RACE, STANDARD, 1, [P(s8=5)], [P(s8=3)])[:2] == (4, 2)
    assert races(RACK_RACE, STANDARD, 1, [P(s8=3)], [P(s8=5)])[:2] == (2, 4)
    assert races(RACK_RACE, STANDARD, 1, [P(s8=7)], [P(s8=7)])[:2] == (5, 5)
    assert races(RACK_RACE, ADJUSTED, 1, [P(s8=5)], [P(s8=4)])[:2] == (3, 2)
    assert races(RACK_RACE, ADJUSTED, 1, [P(s8=4)], [P(s8=4)])[:2] == (2, 2)


def test_no_8_ball_level_uses_the_9_ball_level_mapped():
    a, b, notes = races(RACK_RACE, STANDARD, 1, [P("Ann", s9=9)], [P("Bo", s8=2)])
    assert (a, b) == (7, 2) and "Ann has no 8-Ball skill level: raced as an 8-Ball 7 from 9-Ball 9" in notes[0]
    assert races(RACK_RACE, STANDARD, 1, [P(s9=1)], [P(s8=2)])[:2] == (2, 2)  # a 9-Ball 1 races as an 8-Ball 2
    # one level asked (the event fills levels from 9-Ball): mapped the same way
    a, b, _ = races(RACK_RACE, STANDARD, 1, [Player("C", skill=8)], [Player("D", skill=5)], skill_game="9")
    assert (a, b) == (5, 3)


def test_adjusted_singles_off_the_chart_races_at_the_nearest_square_with_a_note():
    a, b, notes = races(RACK_RACE, ADJUSTED, 1, [P(s8=2)], [P(s8=5)])
    assert (a, b) == (2, 3) and "SL 2 vs SL 5 isn't on the adjusted chart: raced as SL 4 vs SL 5" in notes[0]
    a, b, _ = races(RACK_RACE, ADJUSTED, 1, [P(s8=7)], [P(s8=3)])
    assert (a, b) == (3, 2)


def test_rack_race_doubles_by_combined_level():
    four_three, three_three = [P(s8=4), P(s8=3)], [P(s8=3), P(s8=3)]
    assert races(RACK_RACE, STANDARD, 2, four_three, three_three)[:2] == (3, 2)  # 7 vs 6 or less
    assert races(RACK_RACE, STANDARD, 2, [P(s8=2), P(s8=2)], [P(s8=6), P(s8=5)])[:2] == (2, 5)  # 4 -> "6 or less" vs 11
    a, b, notes = races(RACK_RACE, STANDARD, 2, [P(s8=7), P(s8=7)], [P(s8=6), P(s8=6)], a_name="Sharks")
    assert (a, b) == (5, 5) and "Sharks: combined skill level 14 is over the chart's 12, raced as 12" in notes[0]
    a, b, notes = races(RACK_RACE, ADJUSTED, 2, [P(s8=6), P(s8=6)], [P(s8=3), P(s8=3)])
    assert (a, b) == (4, 2) and "over the chart's 11" in notes[0]
    assert races(RACK_RACE, ADJUSTED, 2, [P(s8=4), P(s8=4)], [P(s8=4), P(s8=3)])[:2] == (2, 2)  # 8 vs 7


def test_three_player_teams_by_combined_bracket():
    a, b, _ = races(RACK_RACE, ADJUSTED, 3, [P(skill=5)] * 3, [P(skill=7), P(skill=7), P(skill=8)])
    assert (a, b) == (5, 7)  # 15 (14-16) vs 22 (21-23)
    assert races(RACK_RACE, STANDARD, 3, [P(skill=4)] * 3, [P(skill=6)] * 3)[:2] == (4, 6)  # one chart for both
    a, b, notes = races(RACK_RACE, ADJUSTED, 3, [P(skill=9)] * 3, [P(skill=4)] * 3)
    assert (a, b) == (7, 4) and "over the chart's 23" in notes[0]


def test_ball_count():
    assert races(BALL_COUNT, STANDARD, 1, [P(s9=5)], [P(s9=3)])[:2] == (38, 25)
    assert races(BALL_COUNT, ADJUSTED, 1, [P(s9=5)], [P(s9=9)])[:2] == (27, 55)
    assert races(BALL_COUNT, STANDARD, 2, [P(s9=3), P(s9=1)], [P(s9=6), P(s9=4)])[:2] == (19, 38)
    a, b, notes = races(BALL_COUNT, STANDARD, 2, [P(s9=7), P(s9=7)], [P(s9=2), P(s9=2)])
    assert (a, b) == (46, 19) and "over the chart's 12" in notes[0]
    assert race_charts.missing_levels(BALL_COUNT, [P("Ann", s8=5), P("Bo", s9=4)]) == ["Ann"]


def test_which_team_sizes_each_chart_covers():
    assert not race_charts.supports(RACK_RACE, 3) and race_charts.supports(RACK_RACE, 4)
    assert not race_charts.supports(BALL_COUNT, 2) and race_charts.supports(BALL_COUNT, 3)


# ------------------------------------------------------------------ in a bracket

def bracket(method, version=STANDARD, size=1, players=None, cap=0):
    t = BracketTournament()
    s = t.settings
    s.call_mode, s.format, s.payouts, s.team_size = "continuous", "single", [], size
    s.race_method, s.race_chart, s.tables = method, version, ["1", "2"]
    for n, ps in enumerate(players, 1):
        t.register_entry(f"E{n}", ps)
    s.team_skill_cap = cap  # set after registering: the team went over when its levels went up
    s.seeding = "manual"
    t.start(random.Random(1))
    return t


def first(t):
    return next(m for m in t.matches if m.status == CALLED)


def test_bracket_matches_get_chart_races_and_the_call_text_says_them():
    t = bracket(RACK_RACE, players=[[P("Ann", s8=5)], [P("Bo", s8=3)]])
    m = first(t)
    assert (m.race_a, m.race_b, m.race_to) == (4, 2, 4)
    assert t.race_text(m) == "E1 races to 4 · E2 to 2"
    assert any("you race to 4, they race to 2" in x.body for x in t.outbox)


def test_ball_count_scoring_counts_points_with_the_9_worth_2():
    t = bracket(BALL_COUNT, ADJUSTED, players=[[P("Ann", s9=1)], [P("Bo", s9=2)]])
    m = first(t)
    assert (m.race_a, m.race_b) == (9, 13) and t.ball_count
    assert t.race_text(m) == "E1 9 points · E2 13 points"
    assert any("you need 9 points, they need 13" in x.body for x in t.outbox)
    for _ in range(4):
        t.score(m.id, "a", 2)
    assert m.score_a == 8 and not t.race_winner(m)
    t.score(m.id, "a", 2)  # the 9 with one point to go: capped at the target
    assert m.score_a == 9 and t.race_winner(m) == m.a
    with pytest.raises(TournamentError):
        t.score(m.id, "a", 1)


def test_draw_needs_the_level_the_chart_uses():
    with pytest.raises(TournamentError, match="needs a 9-Ball skill level for: Ann"):
        bracket(BALL_COUNT, players=[[P("Ann", s8=5)], [P("Bo", s9=4)]])


def test_a_team_over_the_cap_races_one_rack_or_four_points_more():
    t = bracket(RACK_RACE, size=2, cap=10, players=[[P("A", s8=6), P("B", s8=5)], [P("C", s8=3), P("D", s8=3)]])
    m = first(t)
    assert (m.race_a, m.race_b) == (6, 2)  # chart 11 vs 6 or less = 5/2, +1 rack
    assert "over the skill cap (11 of 10): 1 more rack" in m.race_note
    t = bracket(BALL_COUNT, size=2, cap=10, players=[[P("A", s9=6), P("B", s9=5)], [P("C", s9=3), P("D", s9=3)]])
    m = first(t)
    assert (m.race_a, m.race_b) == (46, 25)  # 42 + 4 points
    t = bracket("straight", size=2, cap=10, players=[[P("A", skill=6), P("B", skill=5)], [P("C", skill=3), P("D", skill=3)]])
    m = first(t)
    assert (m.race_a, m.race_b) == (6, 5)  # race to 5, the over-cap team to 6


def test_the_director_changes_one_matchs_race_with_a_note():
    t = bracket(RACK_RACE, players=[[P("Ann", s8=5)], [P("Bo", s8=3)], [P("Cy", s8=4)], [P("Di", s8=4)]])
    t.settings.tables = ["1"]
    m = first(t)
    usual = (m.race_a, m.race_b)
    with pytest.raises(TournamentError, match="note"):
        t.set_race(m.id, 3, 2, "  ")
    t.set_race(m.id, 3, 2, "agreed shorter")
    assert (m.race_a, m.race_b, m.race_set) == (3, 2, True) and "agreed shorter" in m.race_note
    assert any("race changed: you need 3, they need 2 racks" in x.body for x in t.outbox)
    t.update_races()
    assert (m.race_a, m.race_b) == (3, 2)
    t.score(m.id, "a", 1)
    t.score(m.id, "a", 1)
    with pytest.raises(TournamentError, match="2-0"):
        t.set_race(m.id, 1, 2, "oops")
    t.clear_race(m.id)
    assert (m.race_a, m.race_b, m.race_set, m.race_note) == (*usual, False, "")


# ------------------------------------------------------------------ pages

@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("VIEWER_PASSWORD", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


def settings_form(s, **fields):
    data = {"name": s.name, "format": s.format, "team_size": str(s.team_size), "seeding": s.seeding,
            "final_cut": str(s.final_cut), "groups": str(s.groups), "advance": str(s.advance),
            "race_method": s.race_method, "race_chart": s.race_chart, "race_winners": str(s.race_winners),
            "race_losers": str(s.race_losers), "race_finals": str(s.race_finals), "entry_fee": "0", "added_money": "0",
            "field_cap": "0", "tables": ", ".join(s.tables), "call_mode": s.call_mode,
            "match_minutes": str(s.match_minutes), "long_match_minutes": str(s.long_match_minutes),
            "no_rack_minutes": str(s.no_rack_minutes)}
    data.update({k: str(v) for k, v in fields.items()})
    return data


def test_settings_pick_the_race_type_and_chart(app):
    ev = app.config["hub"].create("9-Ball Singles Board", "bracket", format="double")
    c = app.test_client()
    c.post("/admin/login", data={"password": "secretpw1"})
    page = c.get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert 'value="ball_count"' in page and 'name="race_chart"' in page and "Original rack race chart" not in page
    html = c.post(f"/t/{ev.id}/admin/settings", data=settings_form(ev.load().settings, race_method="ball_count",
                  race_chart="adjusted"), follow_redirects=True).get_data(as_text=True)
    s = ev.load().settings
    assert (s.race_method, s.race_chart) == (BALL_COUNT, ADJUSTED)
    assert "Ball count chart (adjusted)" in html
    assert "Ball count chart (adjusted)" in c.get(f"/t/{ev.id}/details").get_data(as_text=True)
    assert "9-Ball skill level" in c.get(f"/t/{ev.id}/register").get_data(as_text=True)  # asks both levels
    html = c.post(f"/t/{ev.id}/admin/settings", data=settings_form(s, team_size="3"),
                  follow_redirects=True).get_data(as_text=True)
    assert "ball count charts cover singles and doubles" in html and ev.load().settings.team_size == 1
    html = c.post(f"/t/{ev.id}/admin/settings", data=settings_form(s, team_size="2"),
                  follow_redirects=True).get_data(as_text=True)
    assert "set Skill levels from the player list to 9-Ball" in html and ev.load().settings.team_size == 1
    c.post(f"/t/{ev.id}/admin/settings", data=settings_form(s, team_size="2", skill_game="9"))
    assert ev.load().settings.team_size == 2


def test_director_and_scorekeeper_pages_for_a_ball_count_match(app):
    ev = app.config["hub"].create("9-Ball Singles Board", "bracket", format="single")
    t = ev.load()
    s = t.settings
    s.race_method, s.call_mode, s.payouts, s.seeding = BALL_COUNT, "continuous", [], "manual"
    for name, sl in (("Ann Lee", 5), ("Bo Ray", 3)):
        t.register_entry(name, [Player(name, skill_9=sl, skill=sl)])
    t.start(random.Random(1))
    ev.store.save(t)
    m = next(x for x in t.matches if x.status == CALLED)
    c = app.test_client()
    c.post("/admin/login", data={"password": "secretpw1"})
    page = c.get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert "Ann Lee 38 points · Bo Ray 25 points" in page and "of 38" in page and ">+2<" in page
    assert "Change race" in page
    c.post(f"/t/{ev.id}/admin/m/{m.id}/score", data={"side": "a", "delta": "2"})
    assert ev.load().match(m.id).score_a == 2
    html = c.post(f"/t/{ev.id}/admin/m/{m.id}/race", data={"race_a": "30", "race_b": "20", "note": "short on time"},
                  follow_redirects=True).get_data(as_text=True)
    m2 = ev.load().match(m.id)
    assert (m2.race_a, m2.race_b) == (30, 20) and "Set by the director: short on time" in html
    html = c.post(f"/t/{ev.id}/admin/m/{m.id}/race", data={"usual": "1"}, follow_redirects=True).get_data(as_text=True)
    assert (ev.load().match(m.id).race_a, ev.load().match(m.id).race_b) == (38, 25)
    entry = c.get(f"/t/{ev.id}/entry/{m.a}").get_data(as_text=True)
    assert "Ann Lee 38 points · Bo Ray 25 points" in entry
    pin = ev.load().entries[m.a].pin  # the scorekeeper's message talks about balls, not racks
    c.post(f"/t/{ev.id}/entry/{m.a}/login", data={"pin": pin})
    html = c.post(f"/t/{ev.id}/entry/{m.a}/keep-score", data={"player": "Ann Lee"}, follow_redirects=True).get_data(as_text=True)
    assert "Tap + for each ball made, +2 for the 9." in html and "each time a rack is won" not in html
