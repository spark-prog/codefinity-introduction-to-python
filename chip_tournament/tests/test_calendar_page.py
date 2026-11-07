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
    for want in ('id="cal-months"', 'data-count="3"', 'id="filters-panel"', 'data-token="doubles"', 'class="button secondary mine-btn"'):
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


def test_home_and_calendar_share_the_three_column_layout(app):
    make(app, "Nov Night", "2026-11-14", team_size=1, game="8", categories_set=True)
    c = app.test_client()
    for path in ("/", "/calendar"):
        page = c.get(path).get_data(as_text=True)
        for want in ('class="home-head"', 'class="home-side"', 'class="home-main"', 'class="home-cal"', 'id="cal-months"',
                     'class="ct-wordmark"', 'mine-btn'):
            assert want in page, (path, want)
        assert "cal-prev" not in page  # no arrows: the next three months are stacked
    home = c.get("/").get_data(as_text=True)
    assert 'data-date="2026-11-14" data-tone="open"' in home
    assert '<a class="button secondary mine-btn" href=' in home  # logged out: My Tournaments leads to the login


def test_my_tournaments_button_for_a_logged_in_player(app):
    from chips import players
    from tests.test_eligibility import COLS, person
    from tests.test_register_members import Clients
    app.config["players"].apply(*players.parse_rows([COLS, person("32200001", first="Ann", last="Lee")]), "t", "t")
    ann = Clients(app).logged_in("32200001")
    for path in ("/", "/calendar"):
        page = ann.get(path).get_data(as_text=True)
        assert '<button type="button" class="button secondary mine-btn" data-token="registered"' in page, path
        assert 'button.mine-btn[data-token]' in page  # the script that makes it filter to registered events


def test_the_home_page_button_asks_visitors_to_log_in_and_players_dont_see_it(app):
    from chips import players
    from tests.test_eligibility import COLS, person
    from tests.test_register_members import Clients
    app.config["players"].apply(*players.parse_rows([COLS, person("32200001", first="Ann", last="Lee")]), "t", "t")
    assert "Log in to see eligibility" in app.test_client().get("/").get_data(as_text=True)
    assert "Log in to see eligibility" not in Clients(app).logged_in("32200001").get("/").get_data(as_text=True)
