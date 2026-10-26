"""Fixes from the review of the multi-tournament and bracket build."""

import random
import threading

from app import create_app
from chips.bracket import BracketTournament
from chips.engine import Player
from chips.hub import Hub
from chips.storage import Store


def admin_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "VIEWER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    return app, admin


def test_undo_step_keeps_counting_past_the_history_limit(tmp_path):
    from chips.engine import Tournament
    store = Store(tmp_path, Tournament)
    depths = []
    for i in range(50):
        with store.change(f"step {i}") as t:
            t.settings.name = f"T{i}"
        depths.append(store.undo_depth())
    assert depths[1:] == list(range(1, 50))  # a new number every step, even past the 40 kept
    store.undo()
    assert store.undo_depth() == 48
    with store.change("new") as t:
        t.settings.name = "new"
    assert store.undo_depth() == 50  # never reuses a number


def play_out(t):
    while t.status == "running":
        m = next(m for m in t.matches if m.status == "called")
        for _ in range(m.race_to):
            t.score(m.id, "a", 1)
        t.submit_result(m.id)


def test_finished_round_robin_marks_everyone_done_with_places_and_winnings():
    t = BracketTournament()
    t.settings.format = "round_robin"
    for i, n in enumerate(["Ann", "Bob", "Cy", "Di"]):
        t.register_entry(n, [Player(n, f"904555000{i}", 0, True)])
    t.start(random.Random(2))
    play_out(t)
    assert t.status == "finished"
    statuses = sorted(e.status for e in t.entries.values())
    assert statuses == ["champion", "eliminated", "eliminated", "eliminated"]


def test_fixed_final_texts_the_corrected_payouts():
    t = BracketTournament()
    t.settings.format, t.settings.seeding = "single", "manual"
    for i, n in enumerate(["Ann", "Bob", "Cy", "Di"]):
        t.register_entry(n, [Player(n, f"904555000{i}", 0, True)])
    t.start(random.Random(1))
    play_out(t)
    final = max(t.matches, key=lambda m: m.num)
    t.outbox.clear()
    t.fix_result(final.id)
    corrections = [m.body for m in t.outbox if m.body.startswith("Correction:")]
    assert len(corrections) == 2 and any("won $40.00" in c for c in corrections)
    t.outbox.clear()
    t._settle()
    assert not t.outbox  # told once, not on every later change


def test_old_links_find_the_running_or_original_tournament(tmp_path, monkeypatch):
    (tmp_path / "tournament.json").write_text('{"settings": {"name": "Halloween Chips"}}')
    app, admin = admin_app(tmp_path, monkeypatch)
    assert app.test_client().get("/board").headers["Location"] == "/t/halloween-chips/board"
    admin.post("/admin/new", data={"name": "November Bracket", "kind": "single"})
    assert len(app.config["hub"].events()) == 2
    assert app.test_client().get("/board").headers["Location"] == "/t/halloween-chips/board"
    assert app.test_client().get("/register").headers["Location"] == "/t/halloween-chips/register"


def test_old_team_link_for_a_bracket_entry_goes_to_its_page(tmp_path, monkeypatch):
    app, admin = admin_app(tmp_path, monkeypatch)
    admin.post("/admin/new", data={"name": "Bracket", "kind": "single"})
    ev = app.config["hub"].events()[0]
    admin.post(f"/t/{ev.id}/admin/entries/add", data={"bulk": "Ann\nBob"})
    entry = next(iter(ev.load().entries.values()))
    r = app.test_client().get(f"/team/{entry.id}")
    assert r.headers["Location"] == f"/t/{ev.id}/entry/{entry.id}"
    assert app.test_client().get(r.headers["Location"]).status_code == 200


def test_bracket_table_with_a_slash_can_go_out_of_service(tmp_path, monkeypatch):
    app, admin = admin_app(tmp_path, monkeypatch)
    admin.post("/admin/new", data={"name": "Bracket", "kind": "single"})
    ev = app.config["hub"].events()[0]
    with ev.store.change("tables") as t:
        t.settings.tables = ["Bar 1/2", "7"]
    assert admin.post(f"/t/{ev.id}/admin/table/Bar%201/2", data={"down": "1"}).status_code == 302
    assert "Bar 1/2" in ev.load().settings.tables_down


def test_shared_wifi_doesnt_lock_out_other_teams_or_the_director(tmp_path, monkeypatch):
    app, admin = admin_app(tmp_path, monkeypatch)
    hub = app.config["hub"]
    ev = hub.create("Chips", "chip")
    with ev.store.change("teams") as t:
        sharks = t.register_team("Sharks", [Player("A", "5551110000", 4), Player("B", "", 4)])
        jets = t.register_team("Jets", [Player("C", "5552220000", 4), Player("D", "", 4)])
    wifi = {"REMOTE_ADDR": "203.0.113.5"}
    prankster = app.test_client()
    for _ in range(12):
        prankster.post(f"/t/{ev.id}/team/{sharks.id}/login", data={"pin": "x"}, environ_base=wifi)
    for _ in range(6):
        prankster.post("/admin/login", data={"password": "guess"}, environ_base=wifi)
    team = app.test_client()
    team.post(f"/t/{ev.id}/team/{jets.id}/login", data={"pin": jets.pin}, environ_base=wifi)
    with team.session_transaction() as s:
        assert s.get(f"team_{jets.id}")
    director = app.test_client()
    director.post("/admin/login", data={"password": "secretpw1"}, environ_base=wifi)
    with director.session_transaction() as s:
        assert s.get("admin")
    # The prankster's own device stays locked.
    page = prankster.post("/admin/login", data={"password": "secretpw1"}, environ_base=wifi, follow_redirects=True)
    assert b"Too many wrong passwords" in page.data


def test_one_store_per_tournament_even_under_a_race(tmp_path):
    Hub(tmp_path).create("Chips", "chip")
    for _ in range(30):
        hub = Hub(tmp_path)
        got, gate = [], threading.Barrier(4)

        def grab():
            gate.wait()
            got.append(hub.get("chips").store)
        threads = [threading.Thread(target=grab) for _ in range(4)]
        [th.start() for th in threads]
        [th.join() for th in threads]
        assert len({id(s) for s in got}) == 1


def test_finished_chip_tournament_leaves_nobody_in_line():
    from chips.engine import Tournament
    t = Tournament()
    t.settings.require_checkin, t.settings.race_to = False, 1
    for i in range(3):
        t.register_team(f"Team {i}", [Player("A", f"555000000{i}", 6), Player("B", "", 5)])  # 2 chips each
    t.start(random.Random(1))
    while t.status == "running":
        m = next(m for m in t.matches if m.status == "called")
        t.score(m.id, "a", 1)
        t.submit_result(m.id)
    assert t.winners_list == [] and t.losers_list == []
