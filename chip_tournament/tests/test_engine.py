import random

import pytest

from chips.engine import (
    ACTIVE,
    CALLED,
    CASHED_OUT,
    ELIMINATED,
    FINISHED,
    Tournament,
    TournamentError,
)


def make(n_teams=4, skills=None, tables=None, fee=2000):
    t = Tournament()
    t.settings.entry_fee_cents = fee
    t.settings.race_to = 2
    if tables:
        t.settings.tables = tables
    skills = skills or ["3"] * n_teams
    for i, skill in enumerate(skills):
        t.register_team(f"Team {i}", skill, f"Captain {i}", f"555000000{i}")
    return t


def play(t, match, winner_side="a"):
    return t.record_result(match.id, match.team_a if winner_side == "a" else match.team_b)


def called(t):
    return [m for m in t.matches if m.status == CALLED]


def test_starting_chips_follow_skill_level():
    t = make(skills=["1", "3", "5"])
    t.start(random.Random(1))
    chips = sorted(team.chips for team in t.teams.values())
    assert chips == [1, 3, 5]
    assert t.pot_cents == 3 * 2000
    assert t.chip_value_cents == pytest.approx(6000 / 9)


def test_round_one_pairs_everyone_and_calls_tables():
    t = make(n_teams=6)
    t.start(random.Random(2))
    first = [m for m in t.matches if m.round == 1]
    assert len(first) == 3
    seen = [x for m in first for x in (m.team_a, m.team_b)]
    assert sorted(seen) == sorted(t.teams)
    assert {m.table for m in first} == {"1", "2", "3"}


def test_more_matches_than_tables_wait_in_order():
    t = make(n_teams=6, tables=["A"])
    t.start(random.Random(3))
    assert len(called(t)) == 1
    play(t, called(t)[0])
    assert len(called(t)) == 1 and called(t)[0].table == "A"


def test_loser_gives_chip_back_and_value_rises():
    t = make(n_teams=4)
    t.start(random.Random(4))
    before = t.chip_value_cents
    m = called(t)[0]
    loser = t.teams[m.team_b]
    play(t, m)
    assert loser.chips == 2
    assert t.teams[m.team_a].chips == 3
    assert t.pot_cents == 8000
    assert t.chip_value_cents == pytest.approx(8000 / 11)
    assert t.chip_value_cents > before


def test_next_round_pairs_winners_and_losers_in_finish_order():
    t = make(n_teams=4)
    t.start(random.Random(5))
    m1, m2 = called(t)
    play(t, m2, "a")  # finishes first
    play(t, m1, "b")
    r2 = [m for m in t.matches if m.round == 2]
    assert [(m.bracket, m.team_a, m.team_b) for m in r2] == [
        ("winners", m2.team_a, m1.team_b),
        ("losers", m2.team_b, m1.team_a),
    ]


def test_odd_lists_cross_over():
    t = make(n_teams=6)
    t.start(random.Random(6))
    for m in called(t):
        play(t, m)
    r2 = [m for m in t.matches if m.round == 2]
    assert sorted(m.bracket for m in r2) == ["cross", "losers", "winners"]


def test_odd_field_gives_a_bye():
    t = make(n_teams=3)
    t.start(random.Random(7))
    byes = [m for m in t.matches if m.team_b is None]
    assert len(byes) == 1
    assert byes[0].team_a in t.winners_list


def test_team_out_of_chips_is_eliminated_and_removed_from_rotation():
    t = make(skills=["5", "1"])  # 1 chip vs 5 chips
    t.start(random.Random(8))
    weak = next(team for team in t.teams.values() if team.chips == 1)
    m = called(t)[0]
    t.record_result(m.id, m.team_a if m.team_b == weak.id else m.team_b)
    assert weak.status == ELIMINATED
    assert weak.id not in t.losers_list
    # Only one team left, so it wins the whole pot.
    assert t.status == FINISHED
    champ = next(team for team in t.teams.values() if team.id != weak.id)
    assert champ.payout_cents == 4000


def test_score_to_race_declares_winner():
    t = make(n_teams=2)
    t.start(random.Random(9))
    m = called(t)[0]
    t.score(m.id, "b", 1)
    assert m.status == CALLED
    t.score(m.id, "b", 1)
    assert m.winner == m.team_b


def test_cash_in_requires_window_and_pays_chips_times_value():
    t = make(n_teams=4)
    t.start(random.Random(10))
    for m in called(t):
        play(t, m)
    team = next(x for x in t.teams.values() if x.chips == 3 and t.current_match_for(x.id))
    m = t.current_match_for(team.id)
    with pytest.raises(TournamentError):
        t.cash_in(team.id)
    t.open_cash_in()
    value = t.chip_value_cents
    payout = t.cash_in(team.id)
    assert payout == round(3 * value)
    assert team.status == CASHED_OUT and team.chips == 0
    # Other chips keep the same value after a cash-in.
    assert t.chip_value_cents == pytest.approx(value, abs=0.01)
    assert m.status == "cancelled"


def test_cannot_cash_in_mid_match():
    t = make(n_teams=2)
    t.start(random.Random(11))
    t.open_cash_in()
    m = called(t)[0]
    t.score(m.id, "a", 1)
    with pytest.raises(TournamentError):
        t.cash_in(m.team_a)


def test_last_call_pays_out_entire_pot_exactly():
    t = make(n_teams=7, skills=["1", "2", "3", "4", "5", "3", "2"], fee=1500)
    t.settings.added_money_cents = 1000
    t.start(random.Random(12))
    for _ in range(6):
        for m in called(t):
            if t.status == FINISHED:
                break
            play(t, m, random.Random(len(t.matches)).choice("ab"))
    if t.status != FINISHED:
        t.call_last_call()
    assert t.status == FINISHED
    assert t.pot_cents == 0
    assert sum(team.payout_cents for team in t.teams.values()) == 7 * 1500 + 1000
    assert all(team.chips == 0 for team in t.teams.values())


def test_full_tournament_runs_to_a_single_winner():
    rng = random.Random(13)
    t = make(n_teams=9, skills=[rng.choice("12345") for _ in range(9)])
    t.start(rng)
    for _ in range(500):
        if t.status == FINISHED:
            break
        m = called(t)[0]
        play(t, m, rng.choice("ab"))
        # Chip accounting never drifts.
        losses = sum(1 for x in t.matches if x.loser)
        assert t.chips_in_play + losses + sum(
            -e.change for e in t.ledger if "cashed in" in e.reason
        ) == t.total_chips_issued
    assert t.status == FINISHED
    assert sum(team.payout_cents for team in t.teams.values()) == 9 * 2000
    assert sum(1 for team in t.teams.values() if team.payout_cents) == 1


def test_adjust_chips_to_zero_eliminates():
    t = make(n_teams=4)
    t.start(random.Random(14))
    for m in called(t):
        play(t, m)
    team = t.teams[t.matches[0].loser]
    m = t.current_match_for(team.id)
    t.adjust_chips(team.id, -team.chips, "scoring mistake")
    assert team.status == ELIMINATED
    assert m.status == "cancelled"


def test_late_registration_joins_winners_list():
    t = make(n_teams=4)
    t.start(random.Random(15))
    late = t.register_team("Latecomers", "2", "Cap", "5551112222")
    assert late.status == ACTIVE and late.chips == 4
    assert late.id in t.winners_list
    assert t.pot_cents == 5 * 2000


def test_texts_are_queued_for_table_calls():
    t = make(n_teams=2)
    t.outbox.clear()
    t.start(random.Random(16))
    assert any("TABLE 1" in msg.body for msg in t.outbox)


def test_round_trip_serialization():
    t = make(n_teams=5)
    t.start(random.Random(17))
    play(t, called(t)[0])
    again = Tournament.from_dict(t.to_dict())
    assert again.to_dict() == t.to_dict()
