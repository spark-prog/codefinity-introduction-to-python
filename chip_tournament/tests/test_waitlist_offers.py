"""Phase 12: an open spot is offered to the next team, who has hours to accept."""

from datetime import datetime, timedelta

import pytest

from app import create_app
from chips import engine, waitlist
from chips.bracket import BracketTournament
from chips.engine import Player, Tournament, TournamentError


@pytest.fixture(autouse=True)
def offers_on(monkeypatch):
    monkeypatch.setattr(engine, "WAITLIST_OFFER_HOURS", 12)


def chip(start_in=timedelta(days=5)):
    t = Tournament()
    start = datetime.now() + start_in
    t.settings.date, t.settings.start_time = start.strftime("%Y-%m-%d"), start.strftime("%I:%M %p")
    t.settings.field_cap = 1
    for name in ("A", "B", "C"):
        t.register_team(name, [Player(f"{name}1", "9045550100", 4, sms_consent=True), Player(f"{name}2", "", 4)])
    return t


def team(t, name):
    return next(x for x in t.teams.values() if x.name == name)


def test_offer_accept():
    t = chip()
    t.outbox.clear()
    t.remove_team(team(t, "A").id)
    b, c = team(t, "B"), team(t, "C")
    assert b.status == "waitlisted" and b.offer_until and not c.offer_until
    assert "A spot opened" in t.outbox[-1].body and "has until" in t.outbox[-1].body
    assert t.where(b).startswith("Offered a spot until")
    t._fill_from_waitlist()
    assert not c.offer_until  # the one open spot is already offered
    waitlist.accept(t, b.id)
    assert b.status == "registered" and b.id not in t.waitlist and not b.offer_until
    with pytest.raises(TournamentError, match="no open spot"):
        waitlist.accept(t, c.id)


def test_decline_and_running_out():
    t = chip()
    t.remove_team(team(t, "A").id)
    b = team(t, "B")
    waitlist.decline(t, b.id)
    assert b.id not in t.teams and team(t, "C").offer_until  # C is offered next
    t2 = chip()
    t2.remove_team(team(t2, "A").id)
    b, c = team(t2, "B"), team(t2, "C")
    late = datetime.fromisoformat(b.offer_until) + timedelta(minutes=1)
    with pytest.raises(TournamentError, match="ran out"):
        waitlist.accept(t2, b.id, at=late)
    assert waitlist.expire(t2, at=late) == 1
    assert t2.waitlist == [c.id, b.id] and c.offer_until and not b.offer_until
    assert any("ran out" in m.body for m in t2.outbox)


def test_deadline_stops_30_minutes_before_the_start_and_moves_in_when_too_close():
    t = chip(start_in=timedelta(hours=5))
    t.remove_team(team(t, "A").id)
    until = datetime.fromisoformat(team(t, "B").offer_until)
    start = waitlist.start_of(t.settings)
    assert until == start - timedelta(minutes=30)
    near = chip(start_in=timedelta(minutes=40))
    near.remove_team(team(near, "A").id)
    assert team(near, "B").status == "registered"  # no time to wait: straight in
    off = chip()
    off.settings.waitlist_offer_hours = 0
    off.remove_team(team(off, "A").id)
    assert team(off, "B").status == "registered"


def test_bracket_offers():
    t = BracketTournament()
    t.settings.team_size, t.settings.field_cap = 1, 1
    t.settings.date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    t.settings.start_time = "7:00 PM"
    for n in ("Ann", "Bo"):
        t.register_entry(n, [Player(n, "9045550100", 4)])
    ann, bo = (next(e for e in t.entries.values() if e.name == n) for n in ("Ann", "Bo"))
    t.remove_entry(ann.id)
    assert bo.offer_until and bo.status == "waitlisted"
    waitlist.accept(t, bo.id)
    assert bo.status == "registered"


def test_team_page_accept(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("VIEWER_PASSWORD", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    ev = app.config["hub"].create("Chips", "chip", date=(datetime.now() + timedelta(days=4)).strftime("%Y-%m-%d"),
                                  start_time="7:00 PM")
    t = ev.load()
    t.settings.field_cap = 1
    ev.store.save(t)
    clients = {}
    for name, n in (("A", 1), ("B", 2)):
        c = app.test_client()
        c.post("/t/chips/register", data={"name": name, "agree_terms": "1", "player_name_0": f"{name} One",
                                          "player_name_1": f"{name} Two", "player_skill_0": "4", "player_skill_1": "4",
                                          "player_phone_0": f"90455501{n:02d}"})
        clients[name] = c
    a, b = team(ev.load(), "A"), team(ev.load(), "B")
    clients["A"].post(f"/t/chips/team/{a.id}/unregister", data={"confirm": "1"})
    page = clients["B"].get(f"/t/chips/team/{b.id}").get_data(as_text=True)
    assert "A spot opened for B!" in page and "Accept the spot" in page
    assert "Enter your PIN below" in app.test_client().get(f"/t/chips/team/{b.id}").get_data(as_text=True)
    r = clients["B"].post(f"/t/chips/team/{b.id}/offer", data={"action": "accept"}, follow_redirects=True)
    assert "B is in the field" in r.get_data(as_text=True) and team(ev.load(), "B").status == "registered"


def test_a_team_holding_an_offer_that_leaves_passes_it_on():
    t = chip()
    t.remove_team(team(t, "A").id)
    b = team(t, "B")
    t.unregister(b.id)
    assert team(t, "C").offer_until
