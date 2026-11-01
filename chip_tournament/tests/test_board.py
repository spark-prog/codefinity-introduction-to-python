"""The board: every table in a fixed spot, Tables/Teams tabs, search data, and player names."""

import random

from app import create_app
from chips.bracket import BracketTournament
from chips.engine import Player


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "VIEWER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    return app, admin


def test_chip_board_and_director_show_every_table_in_order(tmp_path, monkeypatch):
    app, admin = setup(tmp_path, monkeypatch)
    ev = app.config["hub"].create("Chips", "chip")
    with ev.store.change("setup") as t:
        t.settings.require_checkin, t.settings.tables = False, ["1", "2", "3", "10"]
        t.register_team("Sharks", [Player("Ann Lee", "5550000001", 4, True), Player("Bob Ray", "", 4)])
        t.register_team("", [Player("Cy Diaz", "5550000002", 4, True), Player("Dee Fox", "", 4)])  # named after the players
        t.start(random.Random(1))
        t.call_all()
    board = app.test_client().get(f"/t/{ev.id}/board").get_data(as_text=True)
    order = [board.index(f'id="tbl-{n}"') for n in ("1", "2", "3", "10")]
    assert order == sorted(order)  # table-number order, every table
    assert board.count('class="tcell open"') == 3 and 'class="tcell playing"' in board
    assert "<small>Ann &amp; Bob</small>" in board and "Cy &amp; Dee" in board
    assert 'id="tab-tables"' in board and 'id="tab-teams" hidden' in board and "On table 1" in board
    director = admin.get(f"/t/{ev.id}/admin").get_data(as_text=True)
    order = [director.index(f'id="table-{n}"') for n in ("1", "2", "3", "10")]
    assert order == sorted(order) and director.count("match open-table") == 3 and "⋯ More" in director


def test_bracket_board_lists_players_and_where_they_are(tmp_path, monkeypatch):
    app, admin = setup(tmp_path, monkeypatch)
    ev = app.config["hub"].create("Bracket", "bracket")
    with ev.store.change("setup") as t:
        t.settings.tables, t.settings.format = ["1", "2"], "single"
        for n in ("Ann", "Bob", "Cy"):
            t.register_entry(n, [Player(n)])
        t.start(random.Random(1))
    board = app.test_client().get(f"/t/{ev.id}/board").get_data(as_text=True)
    assert ">Players</button>" in board and 'id="tbl-1"' in board and 'id="tbl-2"' in board
    assert "Table 1, waiting for the call" in board or "On table 1" in board
    t = ev.load()
    rows = [e for e in t.entries.values()]
    assert all(f'id="team-{e.id}"' in board for e in rows)


def test_bracket_where():
    t = BracketTournament()
    t.settings.tables, t.settings.format, t.settings.race_winners, t.settings.race_finals = ["1"], "single", 1, 1
    for n in ("Ann", "Bob", "Cy"):
        t.register_entry(n, [Player(n)])
    assert {t.where(e) for e in t.entries.values()} == {"Registered"}
    t.start(random.Random(2))
    m = t.assigned_matches()[0]
    assert t.where(t.entries[m.a]) == "Table 1, waiting for the call"
    t.call_match(m.id)
    assert t.where(t.entries[m.a]) == "On table 1"
    t.record_result(m.id, m.a)
    assert t.where(t.entries[m.b]).startswith("Out")
