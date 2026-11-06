"""A bracket event's field cap: exactly that many entries get in; the next one waits."""

from chips import event_info
from chips.bracket import BracketTournament, REGISTERED, WAITLISTED
from chips.engine import Player


def event(cap):
    t = BracketTournament()
    t.settings.field_cap = cap
    return t


def add(t, name, by_director=False):
    return t.register_entry(name, [Player(name, skill=4)], by_director=by_director)


def test_a_cap_of_three_lets_three_in_and_the_fourth_waits():
    t = event(3)
    statuses = [add(t, n).status for n in "ABCD"]
    assert statuses == [REGISTERED, REGISTERED, REGISTERED, WAITLISTED]
    assert len(t.field_entries) == 3 and len(t.waitlist) == 1 and t.field_full
    assert event_info.spots(t) == "Full · 1 waiting"


def test_a_cap_of_one_and_no_cap():
    t = event(1)
    assert [add(t, n).status for n in "AB"] == [REGISTERED, WAITLISTED]
    t = event(0)
    assert {add(t, n).status for n in "ABCDE"} == {REGISTERED} and not t.waitlist


def test_the_director_adding_past_the_cap_also_waits():
    t = event(2)
    add(t, "A"); add(t, "B")
    assert add(t, "C", by_director=True).status == WAITLISTED
    assert len(t.field_entries) == 2


def test_a_freed_spot_goes_to_the_next_in_line():
    t = event(2)
    a = add(t, "A")
    add(t, "B")
    c = add(t, "C")
    assert c.status == WAITLISTED
    t.remove_entry(a.id)
    assert c.status == REGISTERED and len(t.field_entries) == 2 and not t.waitlist


def test_restoring_a_no_show_into_a_full_field_waits():
    t = event(2)
    a = add(t, "A")
    add(t, "B")
    c = add(t, "C")
    t.mark_no_show(a.id)  # C moves up into A's spot
    assert c.status == REGISTERED
    t.restore_entry(a.id)
    assert a.status == WAITLISTED and len(t.field_entries) == 2
