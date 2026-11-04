"""Seed by skill: the event's game first, then the other game if played in the last 4 months, then first name."""

import random
from datetime import date

import pytest

from app import create_app
from chips import seeding
from chips.bracket import BracketTournament
from chips.engine import Player, TournamentError

DAY = date(2026, 11, 7)


def P(name, s8=0, s9=0, member=""):
    return Player(name, skill_8=s8, skill_9=s9, skill=max(s8, s9), member=member)


RECENT = {"last8": "2026-10-20", "last9": "2026-10-20"}


def event(players, records=None, groups=None, game_name="8-Ball Singles Board"):
    t = BracketTournament()
    t.settings.name, t.settings.date, t.settings.call_mode = game_name, "2026-11-07", "continuous"
    t.settings.format, t.settings.skill_groups, t.settings.payouts = "single", groups or [], []
    for p in players:
        t.register_entry(p.name, [p])
    return t, (lambda m: (records or {}).get(m))


def order(t, record_for, game="8"):
    t.seed_by_skill(game, record_for)
    return [t.entries[i].name for i in t.order]


def test_the_other_games_level_breaks_ties_in_the_event_game():
    """5/6 above 5/5 above 5/4, when the other game was played recently."""
    t, rec = event([P("Dan", 5, 4, "m1"), P("Ann", 5, 5, "m2"), P("Cy", 5, 6, "m3")],
                   {m: RECENT for m in ("m1", "m2", "m3")})
    assert order(t, rec) == ["Cy", "Ann", "Dan"]


def test_the_event_game_comes_first():
    """8-Ball event: an 8-Ball 5 is above an 8-Ball 4 even though the 4 is a 9-Ball 5."""
    t, rec = event([P("Cy", 4, 5, "m1"), P("Bo", 5, 3, "m2")], {"m1": RECENT, "m2": RECENT})
    assert order(t, rec) == ["Bo", "Cy"]
    t, rec = event([P("Cy", 4, 5, "m1"), P("Bo", 5, 3, "m2")], {"m1": RECENT, "m2": RECENT})
    assert order(t, rec, "9") == ["Cy", "Bo"]  # a 9-Ball event looks at the 9-Ball level first


def test_both_4s_in_8_ball_use_the_9_ball_level():
    t, rec = event([P("Bo", 4, 4, "m1"), P("Cy", 4, 5, "m2")], {"m1": RECENT, "m2": RECENT})
    assert order(t, rec) == ["Cy", "Bo"]


def test_the_same_in_both_games_goes_by_first_name():
    t, rec = event([P("Zed Adams", 5, 5, "m1"), P("amy Zane", 5, 5, "m2"), P("Bob Young", 5, 5, "m3"),
                    P("Bob Allen", 5, 5, "m4")], {m: RECENT for m in ("m1", "m2", "m3", "m4")})
    assert order(t, rec) == ["amy Zane", "Bob Allen", "Bob Young", "Zed Adams"]  # first name, then the full name


def test_the_other_game_only_counts_if_played_in_the_four_months_before_the_event():
    old = {"last9": "2026-07-06"}  # 4 months before Nov 7 is Jul 7
    just_in = {"last9": "2026-07-07"}
    t, rec = event([P("Old", 5, 7, "m1"), P("In", 5, 6, "m2"), P("None", 5, 7, "m3"), P("Walk-in", 5, 7)],
                   {"m1": old, "m2": just_in, "m3": {"last9": ""}})
    ranked = dict(seeding.rank(t, [e.id for e in t.entries.values()], "8", rec))
    names = [t.entries[i].name for i, _ in seeding.rank(t, list(t.entries), "8", rec)]
    assert names == ["In", "None", "Old", "Walk-in"]  # In counts as a 5/6; the rest rank as 5/5, by first name
    notes = {t.entries[i].name: n for i, n in ranked.items()}
    assert notes["In"] == "8-Ball 5 · 9-Ball 6 (played Jul 7)"
    assert notes["Old"] == "8-Ball 5 · 9-Ball 7 not counted (last played Jul 6)"
    assert notes["Walk-in"] == "8-Ball 5 · 9-Ball 7 not counted (no play on record)"


def test_four_months_before_handles_short_months():
    assert seeding.months_before(date(2026, 11, 7), 4) == date(2026, 7, 7)
    assert seeding.months_before(date(2026, 12, 31), 4) == date(2026, 8, 31)
    assert seeding.months_before(date(2026, 10, 31), 4) == date(2026, 6, 30)
    assert seeding.months_before(date(2027, 2, 28), 4) == date(2026, 10, 28)
    assert seeding.months_before(date(2027, 3, 31), 1) == date(2027, 2, 28)
    assert seeding.months_before(date(2027, 1, 15), 4) == date(2026, 9, 15)


def test_a_player_with_no_level_in_the_event_game_ranks_by_the_other_one():
    t, rec = event([P("Nine", 0, 9, "m1"), P("Six", 6, 0, "m2"), P("Seven", 7, 0, "m3"), P("One", 0, 1, "m4")],
                   {m: RECENT for m in ("m1", "m2", "m3", "m4")})
    # 8-Ball event: a 9-Ball 9 counts as an 8-Ball 7 (the rack race conversion), a 9-Ball 1 as a 2
    assert order(t, rec) == ["Nine", "Seven", "Six", "One"]  # Nine and Seven tie on 7, so first name decides
    notes = {t.entries[i].name: n for i, n in seeding.rank(t, list(t.entries), "8", rec)}
    assert notes["Nine"] == "No 8-Ball level: ranked by 9-Ball 9"
    t, rec = event([P("Six", 6, 0), P("Nine", 0, 9)])
    assert order(t, rec, "9") == ["Nine", "Six"]  # a 9-Ball event uses the 8-Ball level as it is


def test_each_skill_group_seeds_its_own_players_best_first():
    players = [P("Ann", 2, 2), P("Bo", 3, 3), P("Cy", 3, 2), P("Di", 2, 3), P("Ed", 5, 5), P("Flo", 4, 4)]
    t, rec = event(players, {}, groups=[[1, 3], [4, 4], [5, 5]])
    names = order(t, rec)
    assert [n for n in names if n in ("Ann", "Bo", "Cy", "Di")] == ["Bo", "Cy", "Ann", "Di"]  # the 3s, then the 2s
    t.start(random.Random(1))
    one = [e.name for e in sorted(t.division_entries("SL 1–3"), key=lambda e: e.seed)]
    assert one == ["Bo", "Cy", "Ann", "Di"]


def test_seed_by_skill_keeps_the_order_through_the_draw():
    t, rec = event([P("Dan", 3, 3), P("Ann", 7, 7), P("Cy", 5, 5), P("Bo", 6, 6)], {})
    t.settings.seeding = "random"
    assert t.seed_by_skill("8", rec) == 4
    assert t.settings.seeding == "manual" and t.entries[t.order[0]].seed_note.startswith("8-Ball 7")
    t.start(random.Random(9))
    assert [t.entries[i].name for i in t.order][:4] == ["Ann", "Bo", "Cy", "Dan"]
    assert [e.seed for e in (t.entries[i] for i in t.order)] == [1, 2, 3, 4]


def test_only_the_field_is_seeded_and_only_before_the_draw_for_singles():
    t, rec = event([P("Zed", 5, 5), P("Ann", 5, 5), P("Late", 7, 7)], {})
    late = next(e for e in t.entries.values() if e.name == "Late")
    t.mark_no_show(late.id)
    t.seed_by_skill("8", rec)
    assert [t.entries[i].name for i in t.order if t.entries[i].status != "no_show"] == ["Ann", "Zed"]
    assert late.seed_note == ""
    with pytest.raises(TournamentError, match="Pick 8-Ball or 9-Ball"):
        t.seed_by_skill("x", rec)
    t.restore_entry(late.id)
    t.start(random.Random(1))
    with pytest.raises(TournamentError, match="once the bracket is drawn"):
        t.seed_by_skill("8", rec)
    doubles = BracketTournament()
    doubles.settings.team_size = 2
    with pytest.raises(TournamentError, match="singles"):
        doubles.seed_by_skill("8", rec)
    one = BracketTournament()
    one.register_entry("Solo", [P("Solo", 5, 5)])
    with pytest.raises(TournamentError, match="at least 2"):
        one.seed_by_skill("8", rec)


def test_shuffling_clears_the_seeding_notes():
    t, rec = event([P("Ann", 5, 5), P("Bo", 4, 4)], {})
    t.seed_by_skill("8", rec)
    t.shuffle_order()
    assert all(e.seed_note == "" for e in t.entries.values())


def test_the_game_defaults_from_the_event():
    t = BracketTournament()
    s = t.settings
    s.name = "9-Ball Singles Board"
    assert seeding.game_for(s) == "9"
    s.name = "8-Ball Singles Board"
    s.skill_game = "9"
    assert seeding.game_for(s) == "8"  # the name wins over the default skill-level setting
    s.name = "Singles Board"
    assert seeding.game_for(s) == "9"  # then the skill-level setting
    s.skill_game = "8"
    assert seeding.game_for(s) == "8"
    s.race_method = "ball_count"
    assert seeding.game_for(s) == "9"  # a ball count is a 9-Ball event


# ------------------------------------------------------------------ the page

@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


def test_the_director_seeds_from_the_entries_list(app):
    ev = app.config["hub"].create("9-Ball Singles Board", "bracket", format="single")
    t = ev.load()
    t.settings.date, t.settings.skill_groups = "2026-11-07", [[1, 4], [5, 9]]
    for p in (P("Ann", 4, 4), P("Bo", 5, 6), P("Cy", 3, 3), P("Di", 6, 7)):
        t.register_entry(p.name, [p])
    ev.store.save(t)
    c = app.test_client()
    c.post("/admin/login", data={"password": "secretpw1"})
    page = c.get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert 'action="/t/%s/admin/seed-by-skill"' % ev.id in page and '<option value="9" selected>9-Ball</option>' in page
    assert "SL 1–4 · 2" in page and "SL 5–9 · 2" in page  # the groups at the draw
    r = c.post(f"/t/{ev.id}/admin/seed-by-skill", data={"game": "9"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Seeded 4 entries by 9-Ball skill level" in html
    t = ev.load()
    assert [t.entries[i].name for i in t.order] == ["Di", "Bo", "Ann", "Cy"] and t.settings.seeding == "manual"
    assert "9-Ball 7 · 8-Ball 6 not counted (no play on record)" in html
    assert "Seeded" in html and c.post(f"/t/{ev.id}/admin/seed-by-skill", data={"game": "7"},
                                       follow_redirects=True).get_data(as_text=True).count("Pick 8-Ball or 9-Ball") == 1
    nobody = app.test_client()
    nobody.post(f"/t/{ev.id}/admin/seed-by-skill", data={"game": "8"})
    assert ev.load().entries[ev.load().order[0]].seed_note.startswith("9-Ball")  # not changed without logging in


def test_no_seed_button_for_doubles(app):
    ev = app.config["hub"].create("Doubles", "bracket", format="single")
    t = ev.load()
    t.settings.team_size = 2
    ev.store.save(t)
    c = app.test_client()
    c.post("/admin/login", data={"password": "secretpw1"})
    assert "seed-by-skill" not in c.get(f"/t/{ev.id}/admin").get_data(as_text=True)
