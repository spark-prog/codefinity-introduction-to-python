"""Fixes from the automated Movin-On-Up practice night."""

import pytest

from app import create_app
from chips import backup, players
from chips.engine import Player
from tests.test_eligibility import COLS, person
from tests.test_player_login import FakeMailer
from tests.test_register_members import Clients


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: FakeMailer()))
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["players"].apply(*players.parse_rows([COLS, person("32200001", first="Ann", last="Lee")]), "t", "t")
    ev = app.config["hub"].create("Practice Night", "bracket", format="single", test_event=True)
    t = ev.load()
    t.settings.team_size = 1
    ev.store.save(t)
    return app


def test_test_events_show_on_my_account_only_to_the_directors_phone(app):
    ann = Clients(app).logged_in("32200001")
    assert "Practice Night" not in ann.get("/me").get_data(as_text=True).split('id="upcoming"')[1]
    ann.post("/admin/login", data={"password": "secretpw1"})  # the director's own practice phone
    upcoming = ann.get("/me").get_data(as_text=True).split('id="upcoming"')[1]
    assert "Practice Night" in upcoming and "Register" in upcoming


def test_the_draw_warns_about_pending_verification(app):
    ev = app.config["hub"].get("practice-night")
    t = ev.load()
    t.register_entry("", [Player("New Member", "9045550100", 4, member="32299999", pending="Not in the player list")])
    t.register_entry("", [Player("Ann Lee", "9045550100", 4, member="32200001")])
    ev.store.save(t)
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    assert "1 player(s) are still waiting in Pending verification" in admin.get("/t/practice-night/admin").get_data(as_text=True)
