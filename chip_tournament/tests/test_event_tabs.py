"""The tabbed event page: Details, Teams (Players), Bracket (Live for chip events) and Rules, under one header."""

import random
import re

import pytest

from app import create_app
from chips import event_info
from chips.engine import Player


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


def bracket(app, name="Board Night", size=1, entries=4, start=False, finish=False, **kw):
    ev = app.config["hub"].create(name, "bracket", format="single")
    t = ev.load()
    t.settings.team_size, t.settings.call_mode, t.settings.payouts, t.settings.date = size, "continuous", [], "2026-11-14"
    for k, v in kw.items():
        setattr(t.settings, k, v)
    for n in range(entries):
        people = [Player(f"P{n}x{i}", skill=4) for i in range(size)]
        t.register_entry(f"Team {n}" if size > 1 else people[0].name, people)
    if start or finish:
        t.start(random.Random(1))
    while finish and t.status == "running":
        m = next(m for m in t.matches if m.status == "called")
        while not t.race_winner(m):
            t.score(m.id, "a", 1)
        t.submit_result(m.id)
    ev.store.save(t)
    return ev


def chip(app, name="Chips Night", teams=3, start=False):
    ev = app.config["hub"].create(name, "chip")
    t = ev.load()
    t.settings.date, t.settings.require_checkin = "2027-01-02", False
    for k in range(teams):
        t.register_team(f"Team {k}", [Player(f"A{k} One", "9045550100", 4), Player(f"B{k} Two", "", 4)])
    if start:
        t.start(random.Random(1))
    ev.store.save(t)
    return ev


def get(app, path):
    return app.test_client().get(path).get_data(as_text=True)


def tab_bar(page):
    bar = page.split('<nav class="event-tabs"', 1)[1].split("</nav>", 1)[0]
    return [(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", a)).strip(), "active" in cls) for cls, a in
            re.findall(r'class="(event-tab[^"]*)"[^>]*>(.*?)</a>', bar, re.S)]


def test_the_tab_helper():
    from chips.bracket import BracketTournament
    from chips.engine import Tournament
    t = BracketTournament()
    t.settings.team_size = 1
    assert [x["label"] for x in event_info.tabs(t)] == ["Details", "Players", "Bracket", "Rules"]
    t.settings.team_size = 2
    assert event_info.tabs(t)[1]["label"] == "Teams"
    assert [x["label"] for x in event_info.tabs(Tournament())] == ["Details", "Teams", "Live", "Rules"]
    assert event_info.default_tab(t) == "teams"
    t.status = "running"
    assert event_info.default_tab(t) == "bracket"
    t.status = "finished"
    assert event_info.default_tab(t) == "bracket"


def test_an_event_opens_on_its_teams_while_people_sign_up(app):
    ev = bracket(app, size=2, entries=3)
    page = get(app, f"/t/{ev.id}/")
    assert tab_bar(page) == [("Details", False), ("Teams 3", True), ("Bracket", False), ("Rules", False)]
    assert 'aria-current="page"' in page and page.count('aria-current="page"') == 1
    assert "Who's in" in page and "Team 0" in page
    assert get(app, f"/t/{ev.id}/teams").count("Who's in") == page.count("Who's in")  # the same page, at its own address


def test_it_opens_on_the_bracket_once_running_and_when_finished(app):
    running = bracket(app, "Running Night", entries=4, start=True)
    page = get(app, f"/t/{running.id}/")
    assert dict(tab_bar(page))["Bracket"] is True and "Live" in page and "Who's in" not in page
    assert '<meta name="auto-refresh"' in page  # the live tab refreshes itself
    done = bracket(app, "Done Night", entries=2, finish=True)
    page = get(app, f"/t/{done.id}/")
    assert dict(tab_bar(page))["Bracket"] is True and '<meta name="auto-refresh"' not in page
    assert "Final results" in get(app, f"/t/{done.id}/teams")  # the standings are on the Teams tab


def test_each_tab_has_its_own_page_under_one_header(app):
    ev = bracket(app, size=1, entries=3, skill_groups=[[1, 3], [4, 9]], venue="Park Ave")
    for tab, label, marker in (("details", "Details", "Skill groups"), ("teams", "Players 3", "Who's in"),
                               ("bracket", "Bracket", "The bracket is drawn when the tournament starts"),
                               ("rules", "Rules", "Search the rules")):
        page = get(app, f"/t/{ev.id}/{tab}")
        assert marker in page, tab
        assert ("event-hero-end" in page) and 'id="event-strip"' in page  # the same header and slim bar everywhere
        assert [name for name, on in tab_bar(page) if on] == [label], tab
    assert get(app, f"/t/{ev.id}/nope").count("<h1>") == 1  # not a tab: the not-found page


def test_details_has_the_facts_but_not_the_fee_twice(app):
    ev = bracket(app, entry_fee_cents=2000, venue="Park Ave", checkin_by="10:15 AM", start_time="10:30 AM")
    page = get(app, f"/t/{ev.id}/details")
    panel = page.split('id="panel"', 1)[1]
    assert "Check-in" in panel and "Format" in panel and "Add to calendar" in panel
    assert '<span class="k">Entry</span>' not in panel and '<span class="k">Field</span>' not in panel
    assert "<dt>Entry</dt><dd>$20.00" in page  # it's in the header


def test_chip_events_get_the_tabs_too(app):
    ev = chip(app, teams=3)
    page = get(app, f"/t/{ev.id}/")
    assert tab_bar(page) == [("Details", False), ("Teams 3", True), ("Live", False), ("Rules", False)]
    details = get(app, f"/t/{ev.id}/details")
    assert "How a chip tournament works" in details and "Starting chips" in details
    assert "The tables open when the tournament starts" in get(app, f"/t/{ev.id}/bracket")
    started = chip(app, "Chips Live", teams=4, start=True)
    page = get(app, f"/t/{started.id}/")
    assert dict(tab_bar(page))["Live"] is True and "Chip value" in page


def test_rules_inside_an_event_is_a_tab_and_the_league_rules_are_not(app):
    ev = bracket(app)
    inside = get(app, f"/t/{ev.id}/rules")
    assert 'class="event-tabs"' in inside and "<h1>Board Night</h1>" in inside and "Official APA rules" in inside
    league = get(app, "/rules")
    assert 'class="event-tabs"' not in league and "<h1>Rules</h1>" in league


def test_the_top_menu_loses_bracket_and_rules_only_on_the_tab_pages(app):
    ev = bracket(app)
    def menu(page):
        nav = page[page.index('<nav class="nav'):page.index("</nav>")]
        return [re.sub(r"<[^>]+>", "", a) for a in re.findall(r"<a [^>]*>(.*?)</a>", nav)]
    assert "Rules" not in menu(get(app, f"/t/{ev.id}/")) and "Bracket" not in menu(get(app, f"/t/{ev.id}/details"))
    assert "Rules" in menu(get(app, f"/t/{ev.id}/register")) and "Bracket" in menu(get(app, f"/t/{ev.id}/register"))


def test_the_register_button_is_in_the_header_on_every_tab(app):
    ev = bracket(app)
    assert ">Register</a>" in get(app, f"/t/{ev.id}/teams") and ">Register</a>" in get(app, f"/t/{ev.id}/rules")
