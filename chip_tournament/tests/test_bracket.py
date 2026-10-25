import random
from collections import Counter

import pytest

from chips.bracket import (
    ACTIVE, BYE, CALLED, CHAMPION, DONE, DOUBLE, ELIMINATED, FINISHED, MODIFIED, READY, ROUND_ROBIN, RUNNING, SINGLE,
    VOID, WAITLISTED, BracketTournament, bracket_order,
)
from chips.engine import Player, TournamentError


def make(fmt, n, **settings):
    t = BracketTournament()
    t.settings.format = fmt
    t.settings.tables = [str(i) for i in range(1, 7)]
    for key, value in settings.items():
        setattr(t.settings, key, value)
    for i in range(n):
        t.register_entry(f"P{i + 1}", [Player(name=f"Player {i + 1}", phone="555000%04d" % i, sms_consent=True)])
    return t


def play_out(t, rng, max_steps=2000):
    """Play every match to the end, checking no entry is ever on two tables."""
    steps = 0
    while t.status == RUNNING:
        steps += 1
        assert steps < max_steps, "tournament never finished"
        playing = t.in_play()
        assert playing, f"stuck: ready={[m.num for m in t.ready_queue()]}"
        on_tables = Counter(x for m in playing for x in (m.a, m.b))
        assert max(on_tables.values()) == 1
        assert len({m.table for m in playing}) == len(playing)
        m = rng.choice(playing)
        side = rng.choice("ab")
        other = "b" if side == "a" else "a"
        setattr(m, f"score_{other}", rng.randrange(m.race_to))
        while t.race_winner(m) is None:
            t.score(m.id, side, 1)
        t.submit_result(m.id)


def losses(t):
    out = Counter()
    for m in t.matches:
        if m.status == DONE and not m.is_bye:
            out[m.loser] += 1
    return out


def check_places(t):
    places = t.places()
    field_ids = [e.id for e in t.entries.values() if e.seed]
    assert set(places) == set(field_ids)
    groups = Counter(places.values())
    for (lo, hi), count in groups.items():
        assert hi - lo + 1 == count
    covered = sorted(p for (lo, hi) in groups for p in range(lo, hi + 1))
    assert covered == list(range(1, len(field_ids) + 1))
    champs = [e for e in t.entries.values() if e.status == CHAMPION]
    assert len(champs) == 1 and places[champs[0].id] == (1, 1)


def check_money(t):
    paid = sum(e.payout_cents for e in t.entries.values())
    n = len([e for e in t.entries.values() if e.seed])
    if n >= len(t.settings.payouts):
        assert paid == round(t.pot_cents * sum(t.settings.payouts) / 100)


def test_bracket_order():
    assert bracket_order(2) == [1, 2]
    assert bracket_order(8) == [1, 8, 4, 5, 2, 7, 3, 6]
    assert sorted(bracket_order(32)) == list(range(1, 33))


@pytest.mark.parametrize("n", list(range(2, 41)))
def test_single_elimination(n):
    rng = random.Random(n)
    t = make(SINGLE, n)
    t.start(rng)
    play_out(t, rng)
    assert t.status == FINISHED
    lost = losses(t)
    champ = next(e for e in t.entries.values() if e.status == CHAMPION)
    assert lost[champ.id] == 0
    assert all(lost[e.id] == 1 for e in t.entries.values() if e is not champ)
    check_places(t)
    check_money(t)


@pytest.mark.parametrize("true_double", [True, False])
@pytest.mark.parametrize("n", list(range(2, 41)))
def test_double_elimination(n, true_double):
    rng = random.Random(n * 7 + true_double)
    t = make(DOUBLE, n, true_double=true_double)
    t.start(rng)
    play_out(t, rng)
    lost = losses(t)
    champ = next(e for e in t.entries.values() if e.status == CHAMPION)
    assert lost[champ.id] <= 1
    for e in t.entries.values():
        if e is champ:
            continue
        if true_double:
            assert lost[e.id] == 2, (e.name, lost[e.id])
        else:
            assert lost[e.id] in (1, 2)  # only the winners-side champion can go out after one loss
    assert sum(1 for e in t.entries.values() if e is not champ and lost[e.id] == 1) <= (0 if true_double else 1)
    check_places(t)
    check_money(t)


@pytest.mark.parametrize("cut", [4, 8, 16])
@pytest.mark.parametrize("n", [5, 9, 12, 17, 24, 33, 40])
def test_modified_single_elimination(n, cut):
    if n <= cut:
        t = make(MODIFIED, n, final_cut=cut)
        with pytest.raises(TournamentError):
            t.start()
        return
    rng = random.Random(n * 31 + cut)
    t = make(MODIFIED, n, final_cut=cut)
    t.start(rng)
    playoff_first = [m for m in t.matches if m.stage == "P" and m.round == 1]
    assert len(playoff_first) == cut // 2
    play_out(t, rng)
    lost = losses(t)
    in_playoff = {x for m in playoff_first for x in (m.a, m.b)} - {BYE}
    for e in t.entries.values():
        if e.status == CHAMPION:
            continue
        if e.id in in_playoff:
            assert 1 <= lost[e.id] <= 2
        else:
            assert lost[e.id] == 2  # double elimination until the final cut
    check_places(t)
    check_money(t)


@pytest.mark.parametrize("groups,advance", [(1, 0), (2, 0), (2, 2), (3, 1), (4, 2)])
@pytest.mark.parametrize("n", [6, 9, 12, 16])
def test_round_robin(n, groups, advance):
    rng = random.Random(n + groups * 10 + advance)
    t = make(ROUND_ROBIN, n, groups=groups, advance=advance)
    if n < 2 * groups or (advance and advance >= n // groups):
        with pytest.raises(TournamentError):
            t.start(rng)
        return
    t.start(rng)
    assert sum(len(v) for v in t.groups.values()) == n
    sizes = [len(v) for v in t.groups.values()]
    assert max(sizes) - min(sizes) <= 1
    play_out(t, rng)
    for g, members in t.groups.items():
        pairs = Counter(frozenset((m.a, m.b)) for m in t.matches if m.stage == "G" and m.group == g)
        assert len(pairs) == len(members) * (len(members) - 1) // 2
        assert set(pairs.values()) == {1}
    check_places(t)
    check_money(t)
    if advance:
        winner = next(e for e in t.entries.values() if e.status == CHAMPION)
        group_of = next(g for g, members in t.groups.items() if winner.id in members)
        top = [r["entry"].id for r in t.group_standings(group_of)[:advance]]
        assert winner.id in top


def test_double_elimination_eight_structure():
    t = make(DOUBLE, 8)
    t.start(random.Random(1))
    stages = Counter(m.stage for m in t.matches)
    assert stages == {"W": 7, "L": 6, "F": 2}
    assert t.round_title("L", 4) == "Losers final"
    assert t.round_title("F", 2) == "Grand final (reset)"
    first = t.in_play()
    assert len(first) == 4 and all(m.race_to == 5 for m in first)


def test_byes_go_to_top_seeds_and_resolve_themselves():
    t = make(SINGLE, 5, seeding="manual")
    t.start()
    first = [m for m in t.matches if m.round == 1]
    byes = [m for m in first if m.is_bye]
    assert len(byes) == 3 and all(m.status == DONE for m in byes)
    assert {t.entries[m.winner].seed for m in byes} == {1, 2, 3}
    assert [m for m in first if not m.is_bye][0].status == CALLED  # seeds 4 v 5 on a table


def test_grand_final_reset_only_when_losers_champion_wins():
    rng = random.Random(3)
    t = make(DOUBLE, 4, true_double=True)
    t.start(rng)
    while True:
        gf = next(m for m in t.matches if m.stage == "F" and m.round == 1)
        if gf.status == CALLED:
            break
        m = t.in_play()[0]
        t.record_result(m.id, m.a, forfeit=True)
    t.record_result(gf.id, gf.b, forfeit=True)  # losers-side champion wins the first final
    reset = next(m for m in t.matches if m.reset)
    assert reset.status == CALLED and {reset.a, reset.b} == {gf.a, gf.b}
    assert t.status == RUNNING
    t.record_result(reset.id, reset.a, forfeit=True)
    assert t.status == FINISHED and t.entries[reset.a].status == CHAMPION


def test_no_reset_when_winners_champion_wins():
    t = make(DOUBLE, 4)
    t.start(random.Random(5))
    while t.status == RUNNING:
        m = t.in_play()[0]
        t.record_result(m.id, m.a, forfeit=True)
    assert next(m for m in t.matches if m.reset).status == VOID


def test_places_and_payouts_as_entries_go_out():
    t = make(DOUBLE, 8, payouts=[50, 30, 20], entry_fee_cents=1000, added_money_cents=500)
    t.start(random.Random(2))
    assert t.pot_cents == 8500
    rng = random.Random(9)
    seen_third = False
    while t.status == RUNNING:
        m = t.in_play()[0]
        t.record_result(m.id, rng.choice((m.a, m.b)), forfeit=True)
        third = [e for e in t.entries.values() if t.places().get(e.id) == (3, 3)]
        if third and not seen_third:
            seen_third = True
            assert third[0].payout_cents == 1700
            assert any("finished 3rd" in msg.body and "$17.00" in msg.body for msg in t.outbox)
    assert seen_third
    payouts = sorted((e.payout_cents for e in t.entries.values()), reverse=True)
    assert payouts[:3] == [4250, 2550, 1700]


def test_ties_split_the_places_they_cover():
    t = make(SINGLE, 4, payouts=[50, 30, 10, 10], entry_fee_cents=1000)
    t.start(random.Random(1))
    while t.status == RUNNING:
        m = t.in_play()[0]
        t.record_result(m.id, m.a, forfeit=True)
    places = t.places()
    semis = [e for e in t.entries.values() if places[e.id] == (3, 4)]
    assert len(semis) == 2 and all(e.payout_cents == 400 for e in semis)


def test_an_entry_is_never_on_two_tables_in_round_robin():
    t = make(ROUND_ROBIN, 4, groups=1)
    t.settings.tables = [str(i) for i in range(1, 9)]
    t.start(random.Random(1))
    playing = t.in_play()
    assert len(playing) == 2  # 4 entries: only 2 matches at once, even with 8 tables free


def test_score_and_submit():
    t = make(SINGLE, 2, race_finals=3)
    t.start(random.Random(1))
    m = t.in_play()[0]
    with pytest.raises(TournamentError):
        t.submit_result(m.id)
    for _ in range(3):
        t.score(m.id, "b", 1)
    with pytest.raises(TournamentError):
        t.score(m.id, "a", 4)
    with pytest.raises(TournamentError):
        t.score(m.id, "b", 1)  # past the race
    t.submit_result(m.id)
    assert t.status == FINISHED and t.entries[m.b].status == CHAMPION


def test_fix_result_before_the_next_match_starts():
    t = make(SINGLE, 8)
    t.settings.tables = ["1"]
    t.start(random.Random(4))
    first = t.in_play()[0]
    t.record_result(first.id, first.a, forfeit=True)
    t.fix_result(first.id)
    assert first.winner == first.b
    nxt = next(m for m in t.matches if m.src_a and m.src_a.get("match") == first.id
               or m.src_b and m.src_b.get("match") == first.id)
    assert first.b in (nxt.a, nxt.b) and first.a not in (nxt.a, nxt.b)
    assert t.entries[first.a].status == ELIMINATED and t.entries[first.b].status == ACTIVE


def test_fix_result_refused_once_used():
    t = make(SINGLE, 4)
    t.settings.tables = ["1", "2"]
    t.start(random.Random(4))
    a, b = t.in_play()
    t.record_result(a.id, a.a, forfeit=True)
    t.record_result(b.id, b.a, forfeit=True)
    final = t.in_play()[0]
    with pytest.raises(TournamentError):
        t.fix_result(a.id)
    assert final.status == CALLED


def test_table_out_of_service_moves_or_requeues():
    t = make(SINGLE, 8)
    t.settings.tables = ["1", "2", "3", "4", "5"]
    t.start(random.Random(1))
    on_one = next(m for m in t.in_play() if m.table == "1")
    t.set_table_down("1", True)
    assert on_one.table == "5"
    t.set_table_down("2", True)
    requeued = [m for m in t.matches if m.status == READY]
    assert len(requeued) == 1 and requeued[0].priority
    t.set_table_down("2", False)
    assert requeued[0].status == CALLED and requeued[0].table == "2"


def test_manual_calling_when_auto_call_is_off():
    t = make(DOUBLE, 4, auto_call=False)
    t.start(random.Random(1))
    assert not t.in_play() and len(t.ready_queue()) == 2
    m = t.ready_queue()[0]
    t.call_to_table(m.id, "3")
    assert m.status == CALLED and m.table == "3"
    with pytest.raises(TournamentError):
        t.call_to_table(t.ready_queue()[0].id, "3")


def test_withdraw_forfeits_remaining_matches():
    t = make(DOUBLE, 4)
    t.start(random.Random(1))
    m = t.in_play()[0]
    quitter = m.a
    t.withdraw(quitter)
    assert m.status == DONE and m.winner == m.b and m.forfeit
    rng = random.Random(2)
    play_out(t, rng)
    assert t.entries[quitter].status == ELIMINATED


def test_waiting_list_and_field_cap():
    t = make(SINGLE, 0)
    t.settings.field_cap = 2
    for name in ("A", "B", "C"):
        t.register_entry(name, [Player(name=name)])
    c = next(e for e in t.entries.values() if e.name == "C")
    assert c.status == WAITLISTED
    a = next(e for e in t.entries.values() if e.name == "A")
    t.mark_no_show(a.id)
    assert c.status != WAITLISTED
    t.start(random.Random(1))
    assert len(t.matches) == 1


def test_doubles_need_both_names_and_names_are_unique():
    t = BracketTournament()
    t.settings.team_size = 2
    with pytest.raises(TournamentError):
        t.register_entry("", [Player(name="Ann")])
    e = t.register_entry("", [Player(name="Ann"), Player(name="Bob")])
    assert e.name == "Ann & Bob"
    with pytest.raises(TournamentError):
        t.register_entry("ann & bob", [Player(name="Cy"), Player(name="Di")])


def test_round_trip_through_json():
    t = make(DOUBLE, 6)
    t.start(random.Random(1))
    copy = BracketTournament.from_dict(t.to_dict())
    assert [m.id for m in copy.matches] == [m.id for m in t.matches]
    assert copy.entries.keys() == t.entries.keys()
    m = copy.in_play()[0]
    copy.record_result(m.id, m.a, forfeit=True)


def test_texts_on_call_and_result():
    t = make(SINGLE, 2, sms_name="APA")
    t.outbox.clear()
    t.start(random.Random(1))
    assert any(msg.body.startswith("APA: Table 1:") and "race to" in msg.body for msg in t.outbox)
    m = t.in_play()[0]
    t.outbox.clear()
    t.record_result(m.id, m.a, forfeit=True)
    bodies = " ".join(msg.body for msg in t.outbox)
    assert "is out" in bodies and "won $" in bodies
