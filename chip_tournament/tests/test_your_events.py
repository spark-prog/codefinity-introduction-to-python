"""My account: Your events."""

import pytest

from app import create_app
from chips import players
from chips.engine import Player
from tests.test_player_login import FakeMailer
from tests.test_register_members import Clients
from tests.test_players import HEADERS, row


@pytest.fixture
def app(tmp_path, monkeypatch):
    from chips import backup
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: FakeMailer()))
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["players"].apply(*players.parse_rows([HEADERS, row("32200001", first="Ann", last="Lee", email="a@x.com"),
                                                     row("32200002", first="Bo", last="Ray", email="b@x.com")]), "t", "t")
    hub = app.config["hub"]
    masq = hub.create("Masquerade", "chip", date="2026-12-19", start_time="7:00 PM", venue="Park Ave")
    t = masq.load()
    t.register_team("Sharks", [Player("Ann Lee", "9045550100", 4, member="32200001"), Player("Bo Ray", "", 5)])
    masq.store.save(t)
    movin = hub.create("Movin-On-Up", "bracket", format="modified", date="2026-11-21", start_time="1:00 PM")
    t = movin.load()
    t.settings.team_size, t.settings.skill_groups, t.settings.race_method = 1, [[1, 5], [6, 9]], "rack_chart"
    t.settings.skill_game = "high"  # placed by the higher of the two levels
    t.register_entry("", [Player("ann lee", "9045550100", 0, skill_8=4, skill_9=6)])  # registered by name only
    movin.store.save(t)
    old = hub.create("Halloween Doubles", "chip", date="2026-10-31")
    t = old.load()
    t.register_team("Ghosts", [Player("Ann Lee", "9045550100", 4, member="32200001"), Player("Cy Day", "", 3)])
    t.status = "finished"
    old.store.save(t)
    hub.create("Not Mine", "chip", date="2026-11-15")
    return app


def test_your_events(app):
    page = Clients(app).logged_in("32200001").get("/me").get_data(as_text=True)
    events = page.split('<section class="your-events">')[1].split("</section>")[0]
    upcoming = events
    past = page.split('id="history"')[1].split("</section>")[0]
    assert upcoming.index("Movin-On-Up") < upcoming.index("Masquerade")  # soonest first
    assert "Sat, Nov 21" in upcoming and "1:00 PM" in upcoming and "Sat, Dec 19" in upcoming
    assert "Your SL <b>4</b> · team SL <b>9</b> · starts with <b>3 chips</b>" in upcoming
    assert "8-Ball <b>4</b> · 9-Ball <b>6</b> · <b>SL 6–9</b> group" in upcoming
    assert "/t/masquerade/rules" in upcoming and "/t/movin-on-up/rules" in upcoming
    assert "My team page" in upcoming and "with Bo Ray" in upcoming
    assert "Halloween Doubles" in past and "Halloween Doubles" not in upcoming  # finished: in Your history
    assert "Not Mine" not in events  # not in Your events...
    assert "Not Mine" in page.split('id="upcoming"')[1]  # ...but offered under Upcoming events


def test_no_events_yet(app):
    page = Clients(app).logged_in("32200002").get("/me").get_data(as_text=True)
    assert "Masquerade" in page  # Bo is on Sharks
    app.config["players"].apply(*players.parse_rows([HEADERS, row("32200001", first="Ann", last="Lee", email="a@x.com"),
                                                     row("32200002", first="Bo", last="Ray", email="b@x.com"),
                                                     row("32200003", first="Zoe", last="New", email="z@x.com")]),
                                "t", "t")
    page = Clients(app).logged_in("32200003").get("/me").get_data(as_text=True)
    assert "not registered for any upcoming events" in page
