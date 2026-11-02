"""Phase 8: the public 'Am I eligible?' page."""

import pytest

from app import create_app
from chips import backup, players
from chips.engine import Player
from tests.test_eligibility import COLS, person, rules
from tests.test_player_login import FakeMailer
from tests.test_register_members import Clients


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: FakeMailer()))
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["players"].apply(*players.parse_rows([
        COLS, person("32200001", first="Ann", last="Lee", masters="Yes"),
        person("32200002", first="Bo", last="Ray", status="Past Due", mp8="4")]), "t", "t")
    hub = app.config["hub"]
    hub.create("Open Night", "chip", date="2026-11-14")
    masters = hub.create("Masters", "bracket", format="single", date="2026-12-05")
    t = masters.load()
    t.settings.eligibility = rules(active=True, min_matches=6, list_label="Masters", list_column="Masters")
    masters.store.save(t)
    reg = hub.create("Doubles", "chip", date="2026-11-21")
    t = reg.load()
    t.settings.eligibility = rules(active=True)
    t.register_team("Sharks", [Player("Ann Lee", "9045550100", 4, member="32200001"), Player("Cy Day", "", 3)])
    reg.store.save(t)
    test = hub.create("Practice", "chip", date="2026-11-10", test_event=True)
    return app


def lookup(c, member):
    return c.post("/eligibility", data={"member": member}).get_data(as_text=True)


def test_status_only_for_anyone(app):
    c = app.test_client()
    assert "Am I eligible?" in c.get("/").get_data(as_text=True)
    page = lookup(c, "322-00002")
    assert page.count(">Not eligible<") == 2 and page.count("Eligible ✓") == 1  # Past Due: only Open Night
    assert "Past Due" not in page and "lifetime" not in page and "Bo" not in page and "Ray" not in page
    assert "The reasons are private" in page and "Practice" not in page  # no test events
    page = lookup(c, "32200001")
    assert "Registered" in page and page.count("Eligible ✓") == 2
    assert "isn't in the league's player list" in lookup(c, "32299999")
    assert "8 digits" in lookup(c, "1234")


def test_logged_in_player_sees_their_reasons(app):
    bo = Clients(app).logged_in("32200002")
    page = bo.get("/eligibility").get_data(as_text=True)
    assert "Your events" in page and "Membership is Past Due" in page and "Ask for a review" in page


def test_lookups_are_limited(app):
    c = app.test_client()
    for _ in range(20):
        lookup(c, "32200001")
    assert "a lot of lookups" in lookup(c, "32200001")
