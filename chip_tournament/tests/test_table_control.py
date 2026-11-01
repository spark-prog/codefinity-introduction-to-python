"""Closing, taking out and moving tables, and restoring a team that's out."""

import random

import pytest

from app import create_app
from chips.bracket import BracketTournament
from chips.engine import ACTIVE, CALLED, ELIMINATED, WAITING, Player, Tournament, TournamentError


def partners(n):
    return [Player(f"Ann{n}", f"55500000{n:02d}", 5, sms_consent=True), Player(f"Bob{n}", "", 5)]


def started(n_teams=6, tables=("1", "2", "3")):
    t = Tournament()
    t.settings.require_checkin, t.settings.race_to, t.settings.tables = False, 1, list(tables)
    for i in range(n_teams):
        t.register_team(f"Team {i}", partners(i))
    t.start(random.Random(2))
    return t


def test_close_after_match_lets_it_finish_then_takes_the_table_out():
    t = started(n_teams=8, tables=("1", "2"))
    t.call_all()
    m = t.match_on("1")
    t.close_table_after_match("1")
    assert t.settings.tables_closing == ["1"] and m.status == CALLED and "1" not in t.free_tables()
    t.score(m.id, "a", 1)
    t.submit_result(m.id)
    assert t.settings.tables_closing == [] and t.settings.tables_down == ["1"]
    assert t.match_on("1") is None  # nothing new on table 1
    t.reopen_table("1")
    assert t.match_on("1") is not None and t.match_on("1").status == WAITING  # the next pair gets it


def test_closing_a_table_that_was_only_set_sends_its_pair_back_to_the_front():
    t = started(n_teams=6, tables=("1", "2"))
    m = t.match_on("2")
    assert m.status == WAITING
    t.close_table_after_match("2")
    assert "2" in t.settings.tables_down and m.table is None and m.priority and t.table_queue()[0] is m


def test_out_of_service_now_moves_the_match_or_holds_it_with_its_score():
    t = started(n_teams=4, tables=("1", "2", "3"))
    t.call_all()
    m = t.match_on("1")
    t.outbox.clear()
    t.table_out_now("1")
    assert m.table == "3" and m.status == CALLED and "1" in t.settings.tables_down
    assert any("Table change: your match is now on table 3" in x.body for x in t.outbox)
    t.table_out_now("2")  # no free table left: the match waits at the front of the line
    m2 = next(x for x in t.matches if x.id != m.id and x.status in (CALLED, WAITING))
    assert m2.status == WAITING and m2.table is None and m2.priority


def test_move_only_to_a_free_table():
    t = started(n_teams=4, tables=("1", "2", "3"))
    m = t.match_on("1")
    with pytest.raises(TournamentError, match="isn't free"):
        t.move_match(m.id, "2")
    t.outbox.clear()
    t.move_match(m.id, "3")
    assert m.table == "3" and t.match_on("1") is None and not t.outbox  # not called yet: no text


def test_restore_a_team_that_is_out():
    t = started(n_teams=4, tables=("1", "2"))
    t.call_all()
    m = t.match_on("1")
    loser_id = m.team_b
    t.adjust_chips(loser_id, -(t.teams[loser_id].chips - 1), "test")
    t.record_result(m.id, m.team_a)
    team = t.teams[loser_id]
    assert team.status == ELIMINATED
    before = t.chip_value_cents
    expected = t.chip_value_if_restored(2)
    t.restore_eliminated(team.id, 2)
    assert team.status == ACTIVE and team.chips == 2
    assert t.chip_value_cents == pytest.approx(expected) and t.chip_value_cents < before
    assert team.id in t.losers_list or t.current_match_for(team.id)
    with pytest.raises(TournamentError, match="isn't out"):
        t.restore_eliminated(team.id)


def test_bracket_close_after_match():
    t = BracketTournament()
    t.settings.tables, t.settings.format, t.settings.race_winners = ["1", "2"], "single", 1
    for n in ("Ann", "Bob", "Cy", "Di", "Ed", "Flo"):
        t.register_entry(n, [Player(n)])
    t.start(random.Random(1))
    t.call_all()
    m = next(x for x in t.matches if x.status == "called" and x.table == "1")
    t.close_table_after_match("1")
    assert t.settings.tables_closing == ["1"]
    t.score(m.id, "a", 1)
    t.submit_result(m.id)
    assert t.settings.tables_down == ["1"] and not any(x.table == "1" and x.status in ("called", "ready") for x in t.matches)
    t.set_table_down("1", False)
    assert "1" not in t.settings.tables_down


def test_web_table_controls_restore_and_phone_list(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "VIEWER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    ev = app.config["hub"].create("Chips", "chip")
    with ev.store.change("setup") as t:
        t.settings.require_checkin, t.settings.race_to, t.settings.tables = False, 1, ["1", "2", "3"]
        for i in range(4):
            t.register_team(f"Team {i}", partners(i))
        t.start(random.Random(2))
        t.call_all()
    base = f"/t/{ev.id}/admin"
    page = admin.get(base).get_data(as_text=True)
    assert "Close after this match" in page and "Out of service now" in page and "Move to…" in page
    assert 'class="grid team-table"' in page and 'data-label="Chips"' in page
    admin.post(f"{base}/table-state/1", data={"action": "close"})
    assert ev.load().settings.tables_closing == ["1"]
    assert "Closing after this match" in admin.get(base).get_data(as_text=True)
    admin.post(f"{base}/table-state/1", data={"action": "open"})
    assert ev.load().settings.tables_closing == []
    admin.post(f"{base}/table-state/3", data={"action": "down"})
    page = admin.get(base).get_data(as_text=True)
    assert "Back in service" in page and "Out of service" in app.test_client().get(f"/t/{ev.id}/board").get_data(as_text=True)
    m = ev.load().match_on("2")
    admin.post(f"{base}/table-state/3", data={"action": "open"})
    admin.post(f"{base}/match/{m.id}/move", data={"table": "3"})
    assert ev.load().match(m.id).table == "3"
    # knock a team out, then restore it
    with ev.store.change("knock out") as t:
        mm = t.match_on("1")
        loser = mm.team_b
        t.adjust_chips(loser, -(t.teams[loser].chips - 1), "test")
        t.record_result(mm.id, mm.team_a)
    page = admin.get(base).get_data(as_text=True)
    assert f'action="/t/{ev.id}/admin/team/{loser}/restore-out"' in page and "chip value goes from" in page
    admin.post(f"{base}/team/{loser}/restore-out", data={"chips": "1"})
    assert ev.load().teams[loser].status == ACTIVE and ev.load().teams[loser].chips == 1
