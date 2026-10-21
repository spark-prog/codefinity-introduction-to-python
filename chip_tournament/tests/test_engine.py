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
    t.settings.require_checkin = False  # event check-in has its own tests
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
    t.settings.require_checkin = False
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


def test_start_fills_every_table_from_a_random_draw():
    t = make(n_teams=7)  # 4 tables
    t.start(random.Random(2))
    first = called(t)
    assert len(first) == 3 and {m.table for m in first} == {"1", "2", "3"}
    playing = [x for m in first for x in (m.team_a, m.team_b)]
    assert len(set(playing)) == 6
    assert len(t.queue_order(t.winners_list)) == 1  # the odd team waits, no bye
    assert all(m.bracket == "start" for m in first)


def test_pairs_wait_in_the_table_queue():
    t = make(n_teams=6, tables=["A"])
    t.start(random.Random(3))
    queue = t.table_queue()
    assert len(called(t)) == 1 and len(queue) == 2  # everyone is paired; two pairs wait for the table
    assert t.queue_order(t.winners_list) == []
    play(t, called(t)[0])
    assert called(t)[0].id == queue[0].id and called(t)[0].table == "A"


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


def test_rolling_pairs_as_soon_as_two_wait_on_the_same_list():
    t = make(n_teams=4)
    t.start(random.Random(5))
    m1, m2 = called(t)
    play(t, m2, "a")  # finishes first: one team on each list, the other match still playing
    assert len(called(t)) == 1
    play(t, m1, "b")
    new = [m for m in called(t)]
    assert sorted((m.bracket, m.team_a, m.team_b) for m in new) == sorted([
        ("winners", m2.team_a, m1.team_b),
        ("losers", m2.team_b, m1.team_a),
    ])


def test_lone_teams_on_each_list_play_when_nobody_else_is():
    t = make(n_teams=2)
    t.start(random.Random(6))
    play(t, called(t)[0])
    assert called(t)[0].bracket == "cross"


def test_odd_team_waits_for_the_next_free_table():
    t = make(n_teams=3)
    t.start(random.Random(7))
    first = called(t)[0]
    (waiting,) = t.queue_order(t.winners_list)
    play(t, first, "a")
    nxt = called(t)[0]
    assert {nxt.team_a, nxt.team_b} == {waiting, first.team_a}  # winners list: waiting team, then the winner


def test_team_behind_on_matches_jumps_the_line():
    t = make(n_teams=4, tables=["1"])
    t.start(random.Random(40))
    for _ in range(3):
        play(t, called(t)[0])
    assert max(x.matches_played for x in t.active_teams) >= 2
    late = t.register_team("Latecomers", partners(8))  # 0 played: 2+ behind the leader
    assert t.is_behind(late)
    play(t, called(t)[0])
    assert late.id in (called(t)[0].team_a, called(t)[0].team_b)


def test_call_next_puts_a_pair_first_in_line():
    t = make(n_teams=6, tables=["1"])
    t.start(random.Random(41))
    last_pair = t.table_queue()[-1]
    t.call_next(last_pair.team_b)
    assert t.table_queue()[0].id == last_pair.id
    play(t, called(t)[0])
    assert called(t)[0].id == last_pair.id
    with pytest.raises(TournamentError, match="already on a table"):
        t.call_next(last_pair.team_a)


def test_call_next_calls_immediately_when_a_table_is_free():
    t = make(n_teams=3, tables=["1", "2"])
    t.start(random.Random(42))
    (waiting,) = t.queue_order(t.winners_list)
    assert len(called(t)) == 1  # one team alone, nobody to pair with yet
    t.call_next(waiting)  # it still has no opponent free, so it stays first in line
    assert len(called(t)) == 1 and t.teams[waiting].priority


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


def test_last_call_teams_waiting_play_one_more():
    t = make(n_teams=4, tables=["1"])
    t.start(random.Random(12))
    on_table = called(t)[0]
    t.call_last_call()
    assert t.status == RUNNING  # nobody is cashed out yet

    play(t, on_table)  # started before last call: both teams play one more
    assert all(t.teams[x].status == ACTIVE for x in (on_table.team_a, on_table.team_b))

    waiting_match = called(t)[0]  # the two teams that were waiting at last call
    assert {waiting_match.team_a, waiting_match.team_b}.isdisjoint({on_table.team_a, on_table.team_b})
    play(t, waiting_match)  # their last match: both are cashed in after it
    winner, loser = t.teams[waiting_match.winner], t.teams[waiting_match.loser]
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


def test_table_call_text():
    t = make(n_teams=2)
    t.settings.contact_phone = "904-528-7657"
    t.outbox.clear()
    t.start(random.Random(16))
    m = called(t)[0]
    a, b = t.teams[m.team_a], t.teams[m.team_b]
    expected = (f"New match: Table 1: {a.name} with combined skill 10 currently holding 3 chips VS "
                f"{b.name} with combined skill 10 currently holding 3 chips. Good Luck!")
    assert [msg.body for msg in t.outbox] == [expected, expected]  # one text to each team, no questions line


def test_long_match_flag():
    t = make(n_teams=2)
    t.start(random.Random(43))
    m, base = called_at(t)
    t.check_in(m.id, "a", at=base)
    t.check_in(m.id, "b", at=base)  # starts at base + 2 min
    start = base + timedelta(minutes=2)
    assert t.match_minutes_played(m, at=base) is None  # not started yet
    assert t.match_minutes_played(m, at=start + timedelta(minutes=16)) == pytest.approx(16)
    assert t.long_matches(at=start + timedelta(minutes=19)) == []
    assert t.long_matches(at=start + timedelta(minutes=20)) == [m]


def test_round_trip_serialization():
    t = make(n_teams=5)
    t.start(random.Random(17))
    play(t, called(t)[0])
    again = Tournament.from_dict(t.to_dict())
    assert again.to_dict() == t.to_dict()


# ------------------------------------------------------------------ check-in

from datetime import datetime, timedelta  # noqa: E402


def called_at(t, minutes_ago=0):
    """The first match on a table, with its call time set `minutes_ago`."""
    m = called(t)[0]
    base = datetime(2026, 11, 7, 21, 0, 0)
    m.called_at = (base - timedelta(minutes=minutes_ago)).isoformat()
    return m, base


def test_both_arrive_sets_start_two_minutes_after_the_second():
    t = make(n_teams=2)
    t.start(random.Random(30))
    m, base = called_at(t)
    t.check_in(m.id, "a", at=base + timedelta(minutes=1))
    assert m.start_at is None
    t.check_in(m.id, "b", at=base + timedelta(minutes=3))
    assert m.start_at == (base + timedelta(minutes=5)).isoformat()
    assert any("starts at 9:05 PM" in msg.body for msg in t.outbox)
    with pytest.raises(TournamentError, match="already checked in"):
        t.check_in(m.id, "b")


def test_no_show_gets_grace_after_5_minutes_then_forfeits_after_15_more():
    t = make(n_teams=2)
    t.start(random.Random(31))
    m, base = called_at(t)
    t.check_in(m.id, "a", at=base + timedelta(minutes=1))

    assert t.check_clock(at=base + timedelta(minutes=4, seconds=59)) == 0
    assert m.grace_b is None

    t.outbox.clear()
    assert t.check_clock(at=base + timedelta(minutes=5)) == 1
    assert m.grace_b and m.grace_a is None
    late = t.teams[m.team_b]
    assert any("by 9:20 PM or you forfeit" in msg.body for msg in t.outbox if msg.team_id == late.id)
    assert t.check_clock(at=base + timedelta(minutes=10)) == 0  # grace only starts once

    t.check_clock(at=base + timedelta(minutes=20))
    assert m.status == "done" and m.forfeit == "b"
    assert m.winner == m.team_a
    assert late.chips == 2
    assert t.ledger[-1].reason.endswith("by forfeit")


def test_arriving_during_grace_avoids_forfeit():
    t = make(n_teams=2)
    t.start(random.Random(32))
    m, base = called_at(t)
    t.check_clock(at=base + timedelta(minutes=6))
    assert m.grace_a and m.grace_b
    t.check_in(m.id, "a", at=base + timedelta(minutes=12))
    t.check_in(m.id, "b", at=base + timedelta(minutes=19))
    t.check_clock(at=base + timedelta(minutes=30))
    assert m.status == CALLED
    assert m.start_at == (base + timedelta(minutes=21)).isoformat()


def test_neither_team_shows_both_forfeit_a_chip():
    t = make(n_teams=4, tables=["1"])
    t.start(random.Random(33))
    m, base = called_at(t)
    next_pair = t.table_queue()[0]
    t.check_clock(at=base + timedelta(minutes=20))
    assert m.forfeit == "both"
    assert t.teams[m.team_a].chips == 2 and t.teams[m.team_b].chips == 2
    assert called(t)[0].id == next_pair.id  # table goes to the next match


def test_scoring_counts_as_checked_in():
    t = make(n_teams=2)
    t.start(random.Random(34))
    m, base = called_at(t)
    t.score(m.id, "a", 1)
    assert m.arrived_a and m.arrived_b and m.start_at
    t.check_clock(at=base + timedelta(minutes=60))
    assert m.status == CALLED


def test_undone_forfeit_is_not_reapplied():
    t = make(n_teams=2)
    t.start(random.Random(35))
    m, base = called_at(t)
    before = t.to_dict()  # what Undo would restore
    t.check_clock(at=base + timedelta(minutes=20))
    assert m.forfeit == "both"

    restored = Tournament.from_dict(before)
    assert restored.pause_overdue_forfeits(at=base + timedelta(minutes=21)) == 1
    assert restored.check_clock(at=base + timedelta(minutes=22)) == 0
    rm = restored.match(m.id)
    assert rm.status == CALLED and rm.clock_paused


def test_double_forfeit_chips_count_as_returned():
    t = make(n_teams=2)
    t.start(random.Random(36))
    m, base = called_at(t)
    t.check_clock(at=base + timedelta(minutes=20))
    assert t.chips_returned == 2
    assert t.chips_in_play + t.chips_returned == t.total_chips_issued


def test_texts_carry_sender_name_and_questions_phone():
    t = Tournament()
    t.settings.require_checkin = False
    t.settings.sms_name = "First Coast APA"
    t.settings.contact_phone = "904-528-7657"
    t.register_team("Sharks", partners(8))
    body = t.outbox[-1].body
    assert body.startswith("First Coast APA: Sharks is registered")
    t.register_team("Rails", partners(8))
    t.start(random.Random(1))
    assert "Questions?" not in t.outbox[-1].body  # only on the registration text
    assert "Reply STOP to opt out." in body
    assert body.endswith("Questions? Call/text 904-528-7657")


# ------------------------------------------------------------- no rematches

from chips.engine import DONE, Match  # noqa: E402


def scenario(names, played=(), playing=(), winners=(), losers=(), tables=("1", "2", "3", "4")):
    """A running tournament with a made-up history.

    played: (winner, loser) pairs already finished; playing: pairs on a table now.
    """
    t = Tournament()
    t.settings.tables = list(tables)
    t.settings.require_checkin = False
    ids = {n: t.register_team(n, partners(8)).id for n in names}
    t.start(random.Random(0))
    t.matches.clear(); t.winners_list.clear(); t.losers_list.clear(); t.round = 0
    for team in t.teams.values():
        team.wins = team.losses = 0
    for w, l in played:
        t.round += 1
        t.matches.append(Match(id=f"p{t.round}", round=t.round, team_a=ids[w], team_b=ids[l], bracket="winners",
                               status=DONE, winner=ids[w], loser=ids[l]))
        t.teams[ids[w]].wins += 1
        t.teams[ids[l]].losses += 1
    for i, (a, b) in enumerate(playing):
        t.round += 1
        t.matches.append(Match(id=f"m{t.round}", round=t.round, team_a=ids[a], team_b=ids[b], bracket="winners",
                               status=CALLED, table=tables[i], called_at=datetime.now().isoformat()))
    for n in winners:
        t._enqueue(ids[n], t.winners_list)
    for n in losers:
        t._enqueue(ids[n], t.losers_list)
    t._advance()
    return t, ids


def pairs(t, ids, status=None):
    name = {v: k for k, v in ids.items()}
    return [{name[m.team_a], name[m.team_b]} for m in t.matches
            if m.status in ((status,) if status else ("waiting", "called")) and m.id[0] not in "mp"]


def test_no_rematch_waits_for_the_next_team_to_arrive():
    t, ids = scenario(["Sharks", "Dynamic Duo", "Cue Crew", "8 Ballers"],
                      played=[("Sharks", "8 Ballers"), ("Dynamic Duo", "Cue Crew"), ("Sharks", "Dynamic Duo")],
                      playing=[("Cue Crew", "8 Ballers")], winners=["Sharks", "Dynamic Duo"])
    assert pairs(t, ids) == []  # they played each other; Cue Crew vs 8 Ballers is still going
    on_table = next(m for m in t.matches if m.status == CALLED)
    t.record_result(on_table.id, ids["Cue Crew"])  # Cue Crew joins the winners list...
    # ...and is compared with the top team first: Sharks hasn't played Cue Crew.
    assert {"Sharks", "Cue Crew"} in pairs(t, ids)
    assert ids["Dynamic Duo"] in t.queue_order(t.winners_list)


def test_newcomer_who_played_the_top_team_takes_the_second():
    t, ids = scenario(["Sharks", "Dynamic Duo", "Cue Crew", "8 Ballers", "Rails", "Banks"],
                      played=[("Sharks", "Dynamic Duo"), ("Cue Crew", "Sharks"), ("Rails", "Banks")],
                      playing=[("Cue Crew", "8 Ballers"), ("Rails", "Banks")],
                      winners=["Sharks", "Dynamic Duo"])
    on_table = next(m for m in t.matches if m.status == CALLED and ids["Cue Crew"] in (m.team_a, m.team_b))
    t.record_result(on_table.id, ids["Cue Crew"])  # Cue Crew already played Sharks (top)...
    assert {"Dynamic Duo", "Cue Crew"} in pairs(t, ids)  # ...so it takes Dynamic Duo (second)


def test_rematch_when_both_have_played_everyone_left():
    t, ids = scenario(["X", "Y", "Z", "W"],
                      played=[("X", "Y"), ("X", "Z"), ("X", "W"), ("Y", "Z"), ("Y", "W")],
                      playing=[("Z", "W")], winners=["X", "Y"])
    (m,) = [m for m in t.matches if m.status in ("waiting", "called") and m.id[0] not in "mp"]
    assert m.rematch  # no new opponent exists for either team, so no point waiting


def test_rematch_when_nothing_else_is_coming():
    t, ids = scenario(["X", "Y", "Z"], played=[("X", "Y")], winners=["X", "Y"])
    # Z sits on no list and isn't playing: the safety net puts it on the winners list,
    # and X (top) takes Z, a new opponent, instead of a rematch with Y.
    assert {"X", "Z"} in pairs(t, ids)
    t2, ids2 = scenario(["X", "Y"], played=[("X", "Y")], winners=["X", "Y"])
    (m,) = [m for m in t2.matches if m.status in ("waiting", "called") and m.id[0] not in "mp"]
    assert m.rematch  # only two teams left: the rematch is the only option


def test_winners_vs_losers_only_when_stuck():
    t, ids = scenario(["A", "B", "C", "D"], played=[("A", "B"), ("C", "D")],
                      playing=[], winners=["A", "B", "C"], losers=["D"])
    assert pairs(t, ids) == [{"A", "C"}]  # A skips B (played) and takes C
    # B and D could cross lists, but A-C will bring a team back, so they wait.
    m = next(x for x in t.matches if x.status in ("waiting", "called") and x.id[0] not in "mp")
    if m.status == "waiting":
        t._call_waiting()
    t.record_result(m.id, ids["A"])
    # Now winners [B, A] (played) and losers [D, C] (played): nothing else is coming, so cross.
    assert {"B", "D"} in pairs(t, ids)


def test_table_queue_favors_fewer_rounds_played():
    t, ids = scenario(["A", "B", "C", "D", "E", "F"],
                      played=[("A", "E"), ("A", "F"), ("B", "E"), ("B", "F")],
                      playing=[("E", "F")], tables=("1",),
                      winners=["A", "B", "C", "D"])
    name = {v: k for k, v in ids.items()}
    # A-B is set first (they're at the top), but C and D have played fewer rounds, so they get the table first.
    assert [{name[m.team_a], name[m.team_b]} for m in t.table_queue()] == [{"C", "D"}, {"A", "B"}]


def test_last_two_teams_both_no_show_still_pays_the_pot():
    t = make(skills=[10, 10], fee=2000)  # 3 chips each
    t.start(random.Random(44))
    m = called(t)[0]
    t.adjust_chips(m.team_a, -2, "test"); t.adjust_chips(m.team_b, -2, "test")  # down to 1 chip each
    m, base = called_at(t)
    t.check_clock(at=base + timedelta(minutes=20))
    assert t.status == FINISHED and t.pot_cents == 0
    assert sum(x.payout_cents for x in t.teams.values()) == 4000


def test_call_next_overrides_a_held_team():
    t, ids = scenario(["Sharks", "Dynamic Duo", "Cue Crew", "8 Ballers"],
                      played=[("Sharks", "Dynamic Duo")], playing=[("Cue Crew", "8 Ballers")],
                      winners=["Sharks", "Dynamic Duo"])
    assert pairs(t, ids) == []  # held: they've played each other
    t.call_next(ids["Dynamic Duo"])
    (m,) = [m for m in t.matches if m.status in ("waiting", "called") and m.id[0] not in "mp"]
    assert {m.team_a, m.team_b} == {ids["Sharks"], ids["Dynamic Duo"]} and m.rematch  # only option: rematch


# ------------------------------------------------------------ event check-in

def test_start_needs_everyone_checked_in():
    t = Tournament()
    a, b, c = (t.register_team(n, partners(8)) for n in "ABC")
    t.check_in_team(a.id); t.check_in_team(b.id)
    with pytest.raises(TournamentError, match="haven't checked in: C"):
        t.start(random.Random(1))
    with pytest.raises(TournamentError, match="already checked in"):
        t.check_in_team(a.id)
    t.mark_no_show(c.id)
    assert c.status == "no_show" and c not in t.field_teams
    t.start(random.Random(1))
    assert len(t.active_teams) == 2 and t.pot_cents == 2 * 2000


def test_no_show_lets_the_waiting_list_move_up():
    t = Tournament()
    t.set_field_cap(2)
    a, b, w1, w2 = (t.register_team(n, partners(8)) for n in ("A", "B", "W1", "W2"))
    t.mark_no_show(b.id)
    assert w1.status == "registered" and t.waitlist == [w2.id]
    assert any("marked as a no-show" in m.body for m in t.outbox if m.team_id == b.id)
    t.restore_team(b.id)  # field is full again, so B goes to the end of the waiting list
    assert b.status == "waitlisted" and t.waitlist == [w2.id, b.id]


def test_no_show_everyone_not_checked_in():
    t = Tournament()
    teams = [t.register_team(n, partners(8)) for n in "ABCD"]
    t.check_in_team(teams[0].id); t.check_in_team(teams[2].id)
    assert t.no_show_everyone_not_checked_in() == 2
    assert [x.status for x in teams] == ["registered", "no_show", "registered", "no_show"]
    t.start(random.Random(2))
    assert t.status == "running"
