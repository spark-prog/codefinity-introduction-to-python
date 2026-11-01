"""Director Calls: the app pairs teams and sets their table; the director presses Call match.

Nothing is forfeited automatically; forfeits are director buttons."""

import random
from datetime import datetime, timedelta

import pytest

from app import create_app
from chips.engine import CALLED, CONTINUOUS, WAITING, Player, Tournament, TournamentError


def partners(n):
    return [Player(f"A{n}", f"555000000{n}", 5, sms_consent=True), Player(f"B{n}", "", 5)]


def started(n_teams=6, tables=("1", "2")):
    t = Tournament()
    t.settings.require_checkin, t.settings.race_to, t.settings.tables = False, 1, list(tables)
    for i in range(n_teams):
        t.register_team(f"Team {i}", partners(i))
    t.outbox = []
    t.start(random.Random(5))
    return t


def test_director_calls_is_the_default_and_sets_tables_without_calling():
    t = started()
    assert t.settings.call_mode == "director"
    assigned = t.assigned_matches()
    assert [m.table for m in assigned] == ["1", "2"] and all(m.status == WAITING for m in assigned)
    assert len(t.table_queue()) == 1 and t.table_queue()[0].table is None  # the third pair waits for a table
    assert t.free_tables() == []
    assert not any("New match" in msg.body for msg in t.outbox)  # nobody is texted yet


def test_call_match_texts_both_teams_and_starts_the_clock():
    t = started()
    m = t.assigned_matches()[0]
    t.call_match(m.id)
    assert m.status == CALLED and m.called_at and m.start_at == m.called_at
    texts = [msg for msg in t.outbox if "New match: Table 1" in msg.body]
    assert {msg.team_id for msg in texts} == {m.team_a, m.team_b}
    with pytest.raises(TournamentError, match="already been called"):
        t.call_match(m.id)
    queued = t.table_queue()[0]
    with pytest.raises(TournamentError, match="doesn't have a table"):
        t.call_match(queued.id)


def test_call_all_and_the_next_pair_waits_to_be_called():
    t = started()
    assert t.call_all() == 2
    with pytest.raises(TournamentError, match="No matches are waiting"):
        t.call_all()
    m = next(m for m in t.matches if m.status == CALLED and m.table == "1")
    t.score(m.id, "a", 1)
    t.submit_result(m.id)
    nxt = next(x for x in t.matches if x.table == "1" and x.status in (WAITING, CALLED) and x is not m)
    assert nxt.status == WAITING  # table 1 is set for the next pair, but not called


def test_switching_to_continuous_calls_the_waiting_tables():
    t = started()
    t.settings.call_mode = CONTINUOUS
    t._advance()
    assert not t.assigned_matches() and sum(m.status == CALLED for m in t.matches) == 2


def test_no_automatic_forfeit_and_the_director_forfeits():
    t = started(n_teams=2, tables=("1",))
    m = t.assigned_matches()[0]
    t.call_match(m.id)
    called = datetime.fromisoformat(m.called_at)
    assert t.check_clock(at=called + timedelta(hours=3)) == 0 and m.status == CALLED
    assert t.needs_a_look(m, at=called + timedelta(minutes=15))
    with pytest.raises(TournamentError):
        t.forfeit_both(t.matches[0].id if t.matches[0].status != CALLED else "nope")
    t.record_result(m.id, m.team_a, forfeit=True)
    assert m.forfeit == "b"


# ------------------------------------------------------------------- web

def admin_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "VIEWER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    ev = app.config["hub"].create("Chips", "chip")
    with ev.store.change("setup") as t:
        t.settings.require_checkin, t.settings.tables = False, ["1", "2"]
        for i in range(4):
            t.register_team(f"Team {i}", partners(i))
        t.outbox = []
        t.start(random.Random(2))
    return app, admin, ev


def test_director_page_calls_and_forfeits(tmp_path, monkeypatch):
    app, admin, ev = admin_app(tmp_path, monkeypatch)
    base = f"/t/{ev.id}/admin"
    page = admin.get(base).get_data(as_text=True)
    assert "Call all (2)" in page and page.count(">Call match</button>") == 2 and "ready to call" in page
    first = ev.load().assigned_matches()[0]
    team = ev.load().teams[first.team_a]
    team_page = app.test_client().get(f"/t/{ev.id}/team/{team.id}").get_data(as_text=True)
    assert "set for <b>table 1</b>" in team_page and "we'll text you when the director calls" in team_page

    r = admin.post(f"{base}/match/{first.id}/call", follow_redirects=True).get_data(as_text=True)
    assert "Table 1 called" in r and ev.load().match(first.id).status == CALLED
    r = admin.post(f"{base}/call-all", follow_redirects=True).get_data(as_text=True)
    assert "Called 1 match(es)" in r
    page = admin.get(base).get_data(as_text=True)
    assert "Forfeit</summary>" in page and "Both forfeit" in page and "Arrived" not in page

    with ev.store.change("quiet table") as t:  # called 20 minutes ago, no rack yet
        t.match(first.id).called_at = (datetime.now() - timedelta(minutes=20)).isoformat(timespec="seconds")
    assert "No rack scored yet" in admin.get(base).get_data(as_text=True)

    other = next(m for m in ev.load().matches if m.status == CALLED and m.id != first.id)
    admin.post(f"{base}/match/{first.id}/forfeit", data={"winner": first.team_a})
    assert ev.load().match(first.id).forfeit == "b"
    admin.post(f"{base}/match/{other.id}/forfeit", data={"winner": "both"})
    assert ev.load().match(other.id).forfeit == "both"


def test_settings_choose_how_matches_are_called(tmp_path, monkeypatch):
    app, admin, ev = admin_app(tmp_path, monkeypatch)
    s = ev.load().settings
    page = admin.get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert 'name="call_mode"' in page and 'name="no_rack_minutes"' in page and "grace_minutes" not in page
    admin.post(f"/t/{ev.id}/admin/settings", data={
        "name": s.name, "tables": "1, 2", "race_to": "1", "call_mode": "continuous", "no_rack_minutes": "10",
        "entry_fee": "20", "added_money": "0", "chip_brackets": "6 = 5\n8 = 4\n10 = 3\n11 = 2", "registration_cap": "10"})
    t = ev.load()
    assert t.settings.call_mode == "continuous" and t.settings.no_rack_minutes == 10
    assert not t.assigned_matches() and sum(m.status == CALLED for m in t.matches) == 2  # waiting tables were called


def test_bracket_director_page_calls(tmp_path, monkeypatch):
    from chips.bracket import READY
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "VIEWER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    ev = app.config["hub"].create("Bracket", "bracket")
    with ev.store.change("setup") as t:
        t.settings.tables = ["1", "2"]
        for n in ("Ann", "Bob", "Cy", "Di", "Ed", "Flo"):
            t.register_entry(n, [Player(n)])
        t.start(random.Random(1))
    base = f"/t/{ev.id}/admin"
    assert ev.load().settings.call_mode == "director"
    page = admin.get(base).get_data(as_text=True)
    assert "Call all (2)" in page and page.count(">Call match</button>") == 2
    m = ev.load().assigned_matches()[0]
    admin.post(f"{base}/m/{m.id}/call")
    assert ev.load().match(m.id).status == CALLED
    admin.post(f"{base}/call-all")
    assert not ev.load().assigned_matches()
    s = ev.load().settings
    admin.post(f"{base}/settings", data={"name": s.name, "tables": "1, 2", "call_mode": "continuous", "no_rack_minutes": "9"})
    assert ev.load().settings.call_mode == "continuous" and ev.load().settings.no_rack_minutes == 9
    assert all(x.status != READY or not x.table for x in ev.load().matches)
