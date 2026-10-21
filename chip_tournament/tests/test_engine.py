import random

import pytest

from chips.engine import (
    ACTIVE,
    CALLED,
    CASHED_OUT,
    ELIMINATED,
    FINISHED,
    RUNNING,
    WAITING,
    Player,
    Tournament,
    TournamentError,
)


def partners(combined, phone="5550000000"):
    """Two partners whose skill levels add up to `combined`."""
    return [Player("A", phone, combined // 2), Player("B", "", combined - combined // 2)]


def make(n_teams=4, skills=None, tables=None, fee=2000):
    """Teams with the given combined skill levels (default 10 = 3 chips)."""
    t = Tournament()
    t.settings.entry_fee_cents = fee
    t.settings.race_to = 2
    if tables:
        t.settings.tables = tables
    skills = skills or [10] * n_teams
    for i, skill in enumerate(skills):
        t.register_team(f"Team {i}", partners(skill, f"555000000{i}"))
    return t


def play(t, match, winner_side="a"):
    return t.record_result(match.id, match.team_a if winner_side == "a" else match.team_b)


def called(t):
    return [m for m in t.matches if m.status == CALLED]


@pytest.mark.parametrize("combined, chips", [(4, 5), (6, 5), (7, 4), (8, 4), (9, 3), (10, 3), (11, 2)])
def test_starting_chips_follow_combined_skill(combined, chips):
    assert Tournament().chips_for_skill(combined) == chips


def test_combined_skill_of_12_needs_a_new_partner():
    with pytest.raises(TournamentError, match="new partner"):
        Tournament().chips_for_skill(12)


def test_starting_chips_and_pot():
    t = make(skills=[6, 8, 10])
    t.start(random.Random(1))
    chips = sorted(team.chips for team in t.teams.values())
    assert chips == [3, 4, 5]
    assert t.pot_cents == 3 * 2000
    assert t.chip_value_cents == pytest.approx(6000 / 12)


def test_registration_cap_is_10():
    t = Tournament()
    t.register_team("Ten", partners(10))
    with pytest.raises(TournamentError, match="registration cap of 10"):
        t.register_team("Eleven", partners(11))


def test_team_needs_two_named_partners_with_skill_and_a_phone():
    t = Tournament()
    with pytest.raises(TournamentError):
        t.register_team("Solo", [Player("A", "555", 3)])
    with pytest.raises(TournamentError):
        t.register_team("No skill", [Player("A", "555", 3), Player("B", "", 0)])
    with pytest.raises(TournamentError):
        t.register_team("No phone", [Player("A", "", 3), Player("B", "", 3)])


def test_skill_can_rise_to_11_before_the_start_but_not_12():
    t = make(skills=[10, 10])
    team = next(iter(t.teams.values()))
    t.update_roster(team.id, partners(11))
    assert team.combined_skill == 11
    with pytest.raises(TournamentError, match="new partner"):
        t.update_roster(team.id, partners(12))
    assert team.combined_skill == 11  # rejected change left the roster alone
    t.start(random.Random(1))
    assert team.chips == 2


def test_partner_swap_counts_as_registering_again():
    t = make(skills=[10, 10])
    team = next(iter(t.teams.values()))
    swapped = [Player("A", "5550000000", 5), Player("New Partner", "", 6)]
    with pytest.raises(TournamentError, match="new registration"):
        t.update_roster(team.id, swapped)
    t.update_roster(team.id, [Player("A", "5550000000", 5), Player("New Partner", "", 4)])
    assert team.partners == "A & New Partner"


def test_field_cap_waiting_list_fills_in_order():
    t = Tournament()
    t.set_field_cap(2)
    a, b, c, d = (t.register_team(n, partners(8)) for n in "ABCD")
    assert (a.status, b.status) == ("registered", "registered")
    assert t.waitlist == [c.id, d.id] and t.waitlist_position(d) == 2
    t.remove_team(a.id)
    assert c.status == "registered" and t.waitlist == [d.id]
    t.set_field_cap(3)
    assert d.status == "registered" and t.waitlist == []
    with pytest.raises(TournamentError):
        t.set_field_cap(2)  # below the teams already in the field


def test_swap_keeps_waiting_list_spot():
    t = Tournament()
    t.set_field_cap(1)
    t.register_team("In", partners(8))
    first, second = t.register_team("W1", partners(8)), t.register_team("W2", partners(8))
    t.update_roster(first.id, [Player("A", "5550000000", 4), Player("Swapped In", "", 5)])
    assert t.waitlist == [first.id, second.id]


def test_waitlisted_teams_do_not_play():
    t = Tournament()
    t.set_field_cap(2)
    for n in "ABC":
        t.register_team(n, partners(8))
    t.start(random.Random(5))
    assert len(t.active_teams) == 2
    assert t.pot_cents == 2 * 2000


def test_partners_and_skill_lock_at_the_start():
    t = make(skills=[8, 8])
    t.start(random.Random(1))
    team = next(iter(t.teams.values()))
    with pytest.raises(TournamentError, match="locked"):
        t.update_roster(team.id, partners(6))
    # Phone numbers and the team name can still change.
    new = [Player(p.name, "5559999999", p.skill) for p in team.players]
    t.update_roster(team.id, new, name="Renamed")
    assert team.name == "Renamed" and team.phones == ["5559999999"]


def test_start_blocked_when_a_team_is_over_the_top_bracket():
    t = make(skills=[10, 10])
    t.settings.chip_brackets = [[6, 5], [8, 4], [9, 3]]
    with pytest.raises(TournamentError, match="need a new partner"):
        t.start(random.Random(1))


def test_bracket_labels():
    assert Tournament().bracket_labels() == [("6 or less", 5), ("7–8", 4), ("9–10", 3), ("11", 2)]


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
    t = make(skills=[6, 10])
    weak = list(t.teams.values())[1]
    t.update_roster(weak.id, partners(11))  # 2 chips vs 5 chips
    t.start(random.Random(8))
    m = called(t)[0]
    t.record_result(m.id, m.team_a if m.team_b == weak.id else m.team_b)
    assert weak.chips == 1 and weak.status == ACTIVE
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


def test_last_call_waiting_match_is_the_last_one():
    t = make(n_teams=4, tables=["1"])
    t.start(random.Random(12))
    on_table = called(t)[0]
    waiting = next(m for m in t.matches if m.status == WAITING)
    t.call_last_call()
    assert t.status == RUNNING  # nobody is cashed out yet

    play(t, on_table)  # started before last call: both teams play one more
    assert all(t.teams[x].status == ACTIVE for x in (on_table.team_a, on_table.team_b))

    assert waiting.status == CALLED
    play(t, waiting)  # not started by last call: both teams are cashed in after it
    winner, loser = t.teams[waiting.winner], t.teams[waiting.loser]
    assert winner.status == CASHED_OUT and winner.payout_cents > 0
    assert loser.status == CASHED_OUT and loser.chips == 0
    # Two chips have gone back to the table (12 -> 10); the loser cashes in its remaining 2.
    assert loser.payout_cents == round(2 * 8000 / 10)


def test_after_last_call_everyone_plays_out_and_the_pot_is_paid_exactly():
    rng = random.Random(21)
    t = make(n_teams=7, skills=[4, 6, 7, 8, 9, 10, 5], fee=1500, tables=["1", "2"])
    t.settings.added_money_cents = 1000
    t.start(rng)
    for _ in range(4):
        play(t, called(t)[0], rng.choice("ab"))
    in_play = {x for m in called(t) for x in (m.team_a, m.team_b)}
    t.call_last_call()
    played_after = {team_id: 0 for team_id in t.teams}
    for _ in range(100):
        if t.status == FINISHED:
            break
        m = called(t)[0]
        for x in (m.team_a, m.team_b):
            played_after[x] += 1
        play(t, m, rng.choice("ab"))
    assert t.status == FINISHED
    assert t.pot_cents == 0
    assert sum(team.payout_cents for team in t.teams.values()) == 7 * 1500 + 1000
    assert all(team.chips == 0 for team in t.teams.values())
    # Nobody plays more than their allowance after last call.
    for team_id, n in played_after.items():
        assert n <= (2 if team_id in in_play else 1)


def test_cash_out_everyone_ends_immediately():
    t = make(n_teams=5)
    t.start(random.Random(22))
    t.cash_out_everyone()
    assert t.status == FINISHED and t.pot_cents == 0
    assert sum(team.payout_cents for team in t.teams.values()) == 5 * 2000


def test_no_registration_after_last_call():
    t = make(n_teams=2)
    t.start(random.Random(23))
    t.call_last_call()
    with pytest.raises(TournamentError, match="closed"):
        t.register_team("Late", partners(8))


def test_full_tournament_runs_to_a_single_winner():
    rng = random.Random(13)
    t = make(n_teams=9, skills=[rng.choice([5, 6, 7, 8, 9, 10]) for _ in range(9)])
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
    late = t.register_team("Latecomers", partners(8, "5551112222"))
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
