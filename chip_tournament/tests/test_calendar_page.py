"""The Calendar page: every upcoming tournament by date, grouped by month, with the same filters as Home."""

import pytest

from app import create_app
from chips import event_info
from chips.engine import Player
from chips.bracket import BracketTournament


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


def make(app, name, date="2026-11-14", **kw):
    ev = app.config["hub"].create(name, "bracket", format="single")
    t = ev.load()
    t.settings.date = date
    for k, v in kw.items():
        setattr(t.settings, k, v)
    ev.store.save(t)
    return ev


def test_the_calendar_groups_events_by_month_and_leaves_out_finished_and_test_events(app):
    make(app, "Nov Doubles", "2026-11-14", team_size=2, game="9", categories_set=True, start_time="11:00 AM", venue="Hotshots")
    make(app, "Dec Singles", "2026-12-12", team_size=1, game="8", categories_set=True)
    make(app, "Later Night", "", team_size=1)
    make(app, "Practice", "2026-11-20", test_event=True)
    done = make(app, "Old Night", "2026-10-31")
    t = done.load()
    t.status = "finished"
    done.store.save(t)
    page = app.test_client().get("/calendar").get_data(as_text=True)
    assert page.index("November 2026") < page.index("December 2026") < page.index("Date to be announced")
    assert "Nov Doubles" in page and "Dec Singles" in page and "Later Night" in page
    assert "Practice" not in page and "Old Night" not in page
    assert "9-Ball · Doubles" in page and "Hotshots" in page and "11:00 AM" in page
    assert 'data-date="2026-11-14"' in page and 'data-tone="open"' in page
    assert '<span id="home-count-n">3</span>' in page
    for want in ('id="cal-grid"', 'id="filters-panel"', 'data-token="doubles"', 'id="cal-prev"', "SAT"):
        assert want in page, want


def test_the_tone_of_an_event_on_the_calendar():
    t = BracketTournament()
    t.settings.field_cap = 10
    assert event_info.calendar_tone(t) == "open"
    for n in range(9):
        t.register_entry(f"P{n}", [Player(f"P{n}", skill=4)])
    assert event_info.calendar_tone(t) == "low"
    t.register_entry("P9", [Player("P9", skill=4)])
    assert event_info.calendar_tone(t) == "closed"
    t = BracketTournament()
    t.settings.registration_closed = True
    assert event_info.calendar_tone(t) == "closed"
    t.status = "running"
    assert event_info.calendar_tone(t) == "live"
    assert event_info.weekday("2026-11-14") == "SAT" and event_info.weekday("") == ""


def test_the_menu_links_to_the_calendar_outside_events(app):
    ev = make(app, "Nov Night")
    c = app.test_client()
    nav = lambda p: p[p.index('<nav class="nav'):p.index("</nav>")]
    assert 'href="/calendar"' in nav(c.get("/").get_data(as_text=True))
    assert 'href="/calendar"' not in nav(c.get(f"/t/{ev.id}/").get_data(as_text=True))
