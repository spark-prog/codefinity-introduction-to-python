"""One scorekeeper per match, director approval, and correcting forfeits."""

import random

import pytest

from app import create_app
from chips.engine import ACTIVE, CALLED, CANCELLED, ELIMINATED, WAITING, Player, Tournament, TournamentError


def partners(n):
    return [Player(f"Ann{n}", f"555000000{n}", 5, sms_consent=True), Player(f"Bob{n}", "", 5)]


def called_match(n_teams=2, chips_each=None):
    t = Tournament()
    t.settings.require_checkin, t.settings.race_to, t.settings.tables = False, 2, ["1"]
    for i in range(n_teams):
        t.register_team(f"Team {i}", partners(i))
    t.start(random.Random(4))
    m = t.assigned_matches()[0]
    t.call_match(m.id)
    return t, m


def test_only_the_scorekeepers_phone_scores_and_takeover_is_logged():
    t, m = called_match()
    a = t.teams[m.team_a]
    with pytest.raises(TournamentError, match="keep score first"):
        t.score(m.id, "a", 1, device="phone-1")
    t.claim_scorekeeper(m.id, a.id, a.players[0].name, "phone-1")
    assert m.scorekeeper == f"{a.players[0].name} ({a.name})"
    t.score(m.id, "a", 1, device="phone-1")
    with pytest.raises(TournamentError, match="is keeping score"):
        t.score(m.id, "a", 1, device="phone-2")
    b = t.teams[m.team_b]
    t.claim_scorekeeper(m.id, b.id, b.players[1].name, "phone-2")  # phone 1 died
    assert m.score_a == 1 and m.scorekeeper_device == "phone-2"
    assert "Scorekeeper changed" in m.scorekeeper_note and any("Scorekeeper changed" in x for x in t.log)
    with pytest.raises(TournamentError, match="Pick your name"):
        t.claim_scorekeeper(m.id, b.id, "Nobody", "phone-3")


def test_director_lock_blocks_takeovers_and_change_frees_the_spot():
    t, m = called_match()
    a, b = t.teams[m.team_a], t.teams[m.team_b]
    t.claim_scorekeeper(m.id, a.id, a.players[0].name, "phone-1")
    t.lock_scorekeeper(m.id, True)
    with pytest.raises(TournamentError, match="locked scoring"):
        t.claim_scorekeeper(m.id, b.id, b.players[0].name, "phone-2")
    t.clear_scorekeeper(m.id)
    assert m.scorekeeper is None and not m.scorekeeper_locked
    t.claim_scorekeeper(m.id, b.id, b.players[0].name, "phone-2")


def test_sent_result_holds_the_table_until_approved():
    t, m = called_match(n_teams=4)
    a = t.teams[m.team_a]
    t.claim_scorekeeper(m.id, a.id, a.players[0].name, "p")
    with pytest.raises(TournamentError, match="reached"):
        t.send_result(m.id, "p")
    t.score(m.id, "a", 1, device="p"); t.score(m.id, "a", 1, device="p")
    t.send_result(m.id, "p")
    assert t.results_to_approve() == [m] and m.status == CALLED
    assert t.free_tables() == []  # held: no new match gets table 1
    assert t.teams[m.team_b].chips == 3
    with pytest.raises(TournamentError, match="sent to the director"):
        t.score(m.id, "a", -1, device="p")
    t.send_back(m.id)
    assert not m.sent_at and any("return to table 1" in x.body for x in t.outbox)
    t.send_result(m.id, "p")
    t.score(m.id, "a", -1)  # the director fixes a rack, then approves
    with pytest.raises(TournamentError, match="Neither team has reached"):
        t.approve_result(m.id)
    t.score(m.id, "a", 1)
    t.approve_result(m.id)
    assert m.status == "done" and m.winner == a.id and t.teams[m.team_b].chips == 2
    assert any(x.table == "1" and x.status == WAITING for x in t.matches)  # table 1 opens for the next pair


def test_fix_result_works_on_a_forfeit():
    t, m = called_match()
    a, b = t.teams[m.team_a], t.teams[m.team_b]
    t.record_result(m.id, a.id, forfeit=True)  # wrong team forfeited
    t.fix_result(m.id)
    assert m.winner == b.id and m.forfeit == "a" and a.chips == 2 and b.chips == 3


def test_undo_forfeit_gives_chips_back_and_replays_next():
    t, m = called_match(n_teams=4)
    a, b = t.teams[m.team_a], t.teams[m.team_b]
    t.forfeit_both(m.id)
    assert a.chips == b.chips == 2
    with pytest.raises(TournamentError, match="Both teams forfeited"):
        t.fix_result(m.id)
    replay = t.undo_forfeit(m.id)
    assert m.status == CANCELLED and a.chips == b.chips == 3 and a.losses == b.losses == 0
    assert {replay.team_a, replay.team_b} == {a.id, b.id} and replay.status == WAITING
    assert replay.table == "1" or t.table_queue()[0] is replay  # its old table, or first in line for the next one
    assert t.chips_in_play + t.chips_returned == t.total_chips_issued


def test_undo_forfeit_brings_back_a_team_it_knocked_out():
    t, m = called_match(n_teams=4)
    a, b = t.teams[m.team_a], t.teams[m.team_b]
    t.adjust_chips(b.id, -2, "test")  # down to 1 chip
    m = t.match(m.id)
    t.record_result(m.id, a.id, forfeit=True)
    assert b.status == ELIMINATED
    t.undo_forfeit(m.id)
    assert b.status == ACTIVE and b.chips == 1 and a.wins == 0


# ------------------------------------------------------------------- web

def test_web_scorekeeper_send_and_director_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "VIEWER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    ev = app.config["hub"].create("Chips", "chip")
    with ev.store.change("setup") as t:
        t.settings.require_checkin, t.settings.race_to, t.settings.tables = False, 1, ["1"]
        for i in range(2):
            t.register_team(f"Team {i}", partners(i))
        t.start(random.Random(4))
        t.call_all()
    m = ev.load().matches[0]
    a, b = ev.load().teams[m.team_a], ev.load().teams[m.team_b]
    base = f"/t/{ev.id}"
    ann, cy = app.test_client(), app.test_client()
    ann.post(f"{base}/team/{a.id}/login", data={"pin": a.pin})
    cy.post(f"{base}/team/{b.id}/login", data={"pin": b.pin})
    assert "I'll keep score" in ann.get(f"{base}/team/{a.id}").get_data(as_text=True)
    ann.post(f"{base}/team/{a.id}/keep-score", data={"player": a.players[0].name})
    page = cy.get(f"{base}/team/{b.id}").get_data(as_text=True)
    assert f"Scorekeeper: <b>{a.players[0].name} ({a.name})</b>" in page and "Take over scoring" in page
    cy.post(f"{base}/team/{b.id}/score", data={"side": "a", "delta": "1"})
    assert ev.load().match(m.id).score_a == 0  # not the scorekeeper
    ann.post(f"{base}/team/{a.id}/score", data={"side": "a", "delta": "1"})
    ann.post(f"{base}/team/{a.id}/submit")
    page = admin.get(f"{base}/admin").get_data(as_text=True)
    assert "Results to approve (1)" in page and ">Approve</button>" in page and "Send back" in page
    admin.post(f"{base}/admin/match/{m.id}/scorekeeper", data={"action": "lock"})
    assert ev.load().match(m.id).scorekeeper_locked
    admin.post(f"{base}/admin/match/{m.id}/approve")
    assert ev.load().match(m.id).status == "done"
    page = admin.get(f"{base}/admin").get_data(as_text=True)
    assert "Fix result" in page


def test_bracket_scorekeeper_and_approval():
    from chips.bracket import BracketTournament
    t = BracketTournament()
    t.settings.tables, t.settings.race_winners, t.settings.race_finals, t.settings.format = ["1"], 2, 2, "single"
    for n in ("Ann", "Bob"):
        t.register_entry(n, [Player(n)])
    t.start(random.Random(1))
    m = t.assigned_matches()[0]
    t.call_match(m.id)
    with pytest.raises(TournamentError, match="keep score first"):
        t.score(m.id, "a", 1, device="p1")
    t.claim_scorekeeper(m.id, m.a, t.entries[m.a].name, "p1")
    assert m.scorekeeper == t.entries[m.a].name  # singles: just the player's name
    t.claim_scorekeeper(m.id, m.b, t.entries[m.b].name, "p2")  # takeover
    assert "Scorekeeper changed" in m.scorekeeper_note
    t.lock_scorekeeper(m.id, True)
    with pytest.raises(TournamentError, match="locked"):
        t.claim_scorekeeper(m.id, m.a, t.entries[m.a].name, "p1")
    for _ in range(2):
        t.score(m.id, "b", 1, device="p2")
    t.send_result(m.id, "p2")
    assert t.results_to_approve() == [m] and m.status == CALLED and t.free_tables() == []
    t.send_back(m.id)
    t.send_result(m.id, "p2")
    t.approve_result(m.id)
    assert m.status == "done" and m.winner == m.b and t.status == "finished"
