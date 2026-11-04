"""Table QR codes: the sticker sheet, and the page a scan opens."""

import random

import pytest

from app import create_app
from chips import backup, players
from chips.engine import Player
from qr_web import venue_key
from tests.test_player_login import FakeMailer
from tests.test_register_members import HEADERS, Clients, row


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.setenv("VIEWER_PASSWORD", "viewerpw1")
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: FakeMailer()))
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["players"].apply(*players.parse_rows([HEADERS, row("32200001", first="Ann", last="Lee", email="a@x.com")]),
                                "t", "t")
    return app


def bracket(app, name="8-Ball Singles Board", venue="Park Ave Billiards", start=True):
    ev = app.config["hub"].create(name, "bracket", format="single")
    t = ev.load()
    s = t.settings
    s.venue, s.call_mode, s.payouts, s.seeding, s.tables = venue, "continuous", [], "manual", ["1", "2"]
    t.register_entry("Ann Lee", [Player("Ann Lee", skill=4, member="32200001")])
    for n in ("Bo Ray", "Cy Fox", "Di Moss"):
        t.register_entry(n, [Player(n, skill=4)])
    if start:
        t.start(random.Random(1))
    ev.store.save(t)
    return ev


def director(app):
    c = app.test_client()
    c.post("/admin/login", data={"password": "secretpw1"})
    return c


def test_venue_key():
    assert venue_key("Park Ave Billiards") == "park-ave-billiards" and venue_key("  ") == ""


def test_sticker_sheet_for_the_events_tables(app):
    ev = bracket(app)
    assert app.test_client().get(f"/t/{ev.id}/admin/table-qr").status_code in (302, 401, 403)  # directors only
    page = director(app).get(f"/t/{ev.id}/admin/table-qr").get_data(as_text=True)
    assert page.count('class="qr-sticker"') == 2 and page.count('class="qr-code"><svg') == 2
    assert "Table 1" in page and "Table 2" in page and "Park Ave Billiards" in page
    assert "/table/1?at=park-ave-billiards" not in page  # the link is in the QR code, the sticker shows it short
    assert "/table/1</p>" in page
    page = director(app).get(f"/t/{ev.id}/admin/table-qr?tables=5,6,7").get_data(as_text=True)
    assert page.count('class="qr-sticker"') == 3 and "Table 7" in page
    assert "Table QR codes" in director(app).get(f"/t/{ev.id}/admin").get_data(as_text=True)


def test_scan_shows_the_match_on_the_table_and_both_sides(app):
    ev = bracket(app)
    t = ev.load()
    m = next(x for x in t.matches if x.status == "called" and x.table == "1")
    page = app.test_client().get("/table/1?at=park-ave-billiards").get_data(as_text=True)
    assert "Which side are you on?" in page
    for side in (m.a, m.b):
        assert f"/t/{ev.id}/entry/{side}" in page and t.entries[side].name in page


def test_logged_in_player_goes_straight_to_their_page(app):
    ev = bracket(app)
    t = ev.load()
    ann = next(e for e in t.entries.values() if e.name == "Ann Lee")
    m = t.current_match_for(ann.id)
    c = Clients(app).logged_in("32200001")
    r = c.get(f"/table/{m.table}?at=park-ave-billiards")
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/t/{ev.id}/entry/{ann.id}")
    other = "2" if m.table == "1" else "1"
    assert c.get(f"/table/{other}").status_code == 200  # not her table: pick a side like anyone


def test_chip_tournament_tables_too(app):
    ev = app.config["hub"].create("Masquer 8-Ball", "chip")
    t = ev.load()
    t.settings.venue, t.settings.require_checkin, t.settings.call_mode = "Park Ave Billiards", False, "continuous"
    for k in range(4):
        t.register_team(f"Team {k}", [Player(f"A{k} One", "9045550100", 4), Player(f"B{k} Two", "", 4)])
    t.start(random.Random(1))
    ev.store.save(t)
    t = ev.load()
    m = next(x for x in t.matches if x.status == "called")
    page = app.test_client().get(f"/table/{m.table}").get_data(as_text=True)
    assert f"/t/{ev.id}/team/{m.team_a}" in page and f"/t/{ev.id}/team/{m.team_b}" in page


def test_venue_on_the_sticker_picks_the_event(app):
    park = bracket(app, "Park Night", "Park Ave Billiards")
    other = bracket(app, "Other Night", "Cue Club")
    page = app.test_client().get("/table/1?at=park-ave-billiards").get_data(as_text=True)
    assert "Park Night" in page and "Other Night" not in page
    page = app.test_client().get("/table/1?at=misspelled-venue").get_data(as_text=True)
    assert "Park Night" in page and "Other Night" in page  # no event at that venue: show every running event
    assert park and other


def test_nothing_on_the_table(app):
    bracket(app, start=False)
    page = app.test_client().get("/table/1").get_data(as_text=True)
    assert "No match on this table right now" in page and "no tournament running here" in page
    ev = bracket(app, "Running Night")
    page = app.test_client().get("/table/9").get_data(as_text=True)
    assert "Nothing is set for Table 9" in page and f"/t/{ev.id}/board" in page


def test_match_set_for_the_table_but_not_called_yet(app):
    ev = app.config["hub"].create("Director Calls Night", "bracket", format="single")
    t = ev.load()
    s = t.settings
    s.venue, s.payouts, s.seeding, s.tables = "Park Ave Billiards", [], "manual", ["1", "2"]
    for n in ("Ann Lee", "Bo Ray", "Cy Fox", "Di Moss"):
        t.register_entry(n, [Player(n, skill=4)])
    t.start(random.Random(1))
    ev.store.save(t)
    page = app.test_client().get("/table/1").get_data(as_text=True)
    assert "No match on this table right now" in page and "Up next here:" in page and "scan again once it" in page


def test_stickers_use_the_site_address_when_set(app, monkeypatch):
    ev = bracket(app)
    monkeypatch.setenv("SITE_URL", "https://controltable.app/")
    page = director(app).get(f"/t/{ev.id}/admin/table-qr").get_data(as_text=True)
    assert "controltable.app/table/1</p>" in page
