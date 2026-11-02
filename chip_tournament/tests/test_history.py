"""Phase 13: player history (finishes and payouts), kept after events are deleted."""

import random

import pytest

from app import create_app
from chips import backup, history as hist, players
from chips.bracket import BracketTournament
from chips.engine import Player, Tournament
from tests.test_eligibility import COLS, person
from tests.test_player_login import FakeMailer
from tests.test_register_members import Clients


def finished_bracket():
    t = BracketTournament()
    t.settings.name, t.settings.date, t.settings.team_size, t.settings.format = "Singles", "2026-10-10", 1, "single"
    t.settings.tables = ["1", "2"]
    for n, m in (("Ann Lee", "32200001"), ("Bo Ray", "32200002"), ("Cy Day", ""), ("Di Fox", "32200004")):
        t.register_entry("", [Player(n, "9045550100", 5, member=m)])
    t.settings.call_mode = "continuous"
    t.start(random.Random(1))
    for _ in range(50):  # Ann wins every match she's in
        if t.status == "finished":
            break
        m = next(x for x in t.matches if x.status in ("ready", "called"))
        ann_side = next((x for x in (m.a, m.b) if t.entries[x].players[0].member == "32200001"), m.a)
        t.record_result(m.id, ann_side)
    return t


def test_results_and_store(tmp_path):
    t = finished_bracket()
    rows = dict(hist.results("singles", "bracket", t))
    assert set(rows) == {"32200001", "32200002", "32200004"}  # Cy has no member number
    ann = rows["32200001"]
    assert ann["finish"] == "Champion" and ann["title"] and ann["wins"] == 2 and ann["losses"] == 0
    assert ann["team_payout_cents"] == t.entries[next(e for e in t.entries if "Ann" in t.entries[e].name)].payout_cents
    store = hist.History(tmp_path)
    assert store.record("singles", "bracket", t) == 3 and store.record("singles", "bracket", t) == 0  # once
    assert store.for_member("32200001")[0]["name"] == "Singles"
    s = hist.summary(store.for_member("32200001"))
    assert (s["events"], s["titles"], s["wins"], s["losses"]) == (1, 1, 2, 0)


def test_chip_champion_and_cash_in():
    t = Tournament()
    t.settings.require_checkin, t.settings.tables, t.settings.race_to = False, ["1"], 1
    t.register_team("Sharks", [Player("Ann Lee", "9045550100", 4, member="32200001"), Player("Bo Ray", "", 4, member="32200002")])
    t.register_team("Jets", [Player("Cy Day", "9045550100", 4, member="32200003"), Player("Di Fox", "", 4)])
    t.start(random.Random(1))
    t.cash_out_everyone()
    rows = dict(hist.results("chips", "chip", t))
    assert rows["32200001"]["finish"].startswith("Cashed in $") and rows["32200001"]["partners"] == ["Bo Ray"]
    assert rows["32200001"]["team"] == "Sharks" and "32200004" not in rows


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: FakeMailer()))
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["players"].apply(*players.parse_rows([COLS, person("32200001", first="Ann", last="Lee")]), "t", "t")
    return app


def test_my_account_history_survives_deleting_the_event(app):
    ev = app.config["hub"].create("Singles", "bracket", format="single")
    ev.store.save(finished_bracket())
    test_ev = app.config["hub"].create("Practice", "bracket", format="single", test_event=True)
    practice = finished_bracket()
    practice.settings.name, practice.settings.test_event = "Practice", True
    test_ev.store.save(practice)
    ann = Clients(app).logged_in("32200001")
    page = ann.get("/me").get_data(as_text=True).split('id="history"')[1]
    assert "1 tournament" in page and "1 title" in page and "Champion" in page and "Practice" not in page
    app.config["hub"].delete(ev.id)
    assert "Champion" in ann.get("/me").get_data(as_text=True).split('id="history"')[1]
