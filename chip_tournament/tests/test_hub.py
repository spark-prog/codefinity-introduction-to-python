"""Control Table hub: several tournaments at once, and bracket tournaments through the web pages."""

import json
import random
import time

import pytest

from app import create_app
from chips.engine import Player, Tournament


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("VIEWER_PASSWORD", "lookonly")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


@pytest.fixture
def admin(app):
    c = app.test_client()
    c.post("/admin/login", data={"password": "secret"})
    return c


def create(admin, name, kind, **extra):
    page = admin.post("/admin/new", data={"name": name, "kind": kind, **extra})
    assert page.status_code == 302
    hub = admin.application.config["hub"]
    return [ev for ev in hub.events() if ev.load().settings.name == name][-1]  # the newest with that name


def test_hub_lists_and_creates_tournaments(app, admin):
    assert b"No tournaments are open" in app.test_client().get("/").data
    assert app.test_client().get("/admin").status_code == 302  # director only
    chips = create(admin, "Friday Chips", "chip")
    double = create(admin, "Saturday 9-Ball", "double", date="2026-11-07", venue="Corner Pocket")
    rr = create(admin, "League Round Robin", "round_robin")
    assert (chips.kind, double.kind, rr.kind) == ("chip", "bracket", "bracket")
    assert double.load().settings.format == "double" and double.load().settings.venue == "Corner Pocket"
    page = app.test_client().get("/").data.decode()
    for name in ("Friday Chips", "Saturday 9-Ball", "League Round Robin", "Double elimination", "Round robin"):
        assert name in page
    assert f"/t/{double.id}/" in page
    page = admin.get("/admin").data.decode()
    assert "Run it" in page and f"/t/{chips.id}/admin" in page
    again = create(admin, "Friday Chips", "chip")  # same name gets its own address
    assert again.id == "friday-chips-2"


def test_each_tournament_has_its_own_data(app, admin):
    a = create(admin, "Chips A", "chip")
    b = create(admin, "Bracket B", "single")
    client = app.test_client()
    client.post(f"/t/{a.id}/register", data={
        "name": "Sharks", "agree_terms": "1", "player_name_0": "Ann", "player_skill_0": "5", "player_phone_0": "5550001111",
        "player_name_1": "Bob", "player_skill_1": "4"}, follow_redirects=True)
    client.post(f"/t/{b.id}/register", data={"agree_terms": "1", "player_name_0": "Cy", "player_phone_0": "5551112222",
                                             "sms_consent": "1"}, follow_redirects=True)
    assert [x.name for x in a.load().teams.values()] == ["Sharks"]
    assert [x.name for x in b.load().entries.values()] == ["Cy"]
    assert b"Sharks" not in client.get(f"/t/{b.id}/").data
    assert b"Cy" in client.get(f"/t/{b.id}/").data
    assert (b.dir / "texts.log").exists() and not (a.dir / "texts.log").exists()
    # each page links stay inside its tournament
    page = client.get(f"/t/{b.id}/").data
    assert f'href="/t/{b.id}/rules"'.encode() in page and f'href="/t/{b.id}/board" class="">Bracket</a>'.encode() in page


def test_pages_belong_to_their_tournament_type(app, admin):
    chips = create(admin, "Chips", "chip")
    br = create(admin, "Bracket", "double")
    assert admin.get(f"/t/{br.id}/admin/import").status_code == 404  # chip-only page
    assert admin.get(f"/t/{br.id}/guide").status_code == 404
    assert admin.get(f"/t/{chips.id}/entry/abc").status_code == 404  # bracket-only page
    assert admin.get("/t/nope/").status_code == 404
    assert b"Draw the bracket" in admin.get(f"/t/{br.id}/admin").data
    assert b"Start tournament" in admin.get(f"/t/{chips.id}/admin").data


def test_old_single_tournament_data_moves_into_the_hub(tmp_path, monkeypatch):
    t = Tournament()
    t.settings.name = "Fall Chips"
    team = t.register_team("Sharks", [Player(name="Ann", skill=4, phone="5550001111"), Player(name="Bob", skill=4)])
    (tmp_path / "tournament.json").write_text(json.dumps(t.to_dict()))
    (tmp_path / "texts.log").write_text("")
    (tmp_path / "tournament-20260101-120000.json").write_text(json.dumps(Tournament().to_dict()))
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    app = create_app(str(tmp_path))
    app.testing = True
    hub = app.config["hub"]
    [ev] = hub.events()
    assert ev.id == "fall-chips" and ev.kind == "chip"
    assert not (tmp_path / "tournament.json").exists() and (ev.dir / "tournament.json").exists()
    assert len(ev.store.archives()) == 1
    client = app.test_client()
    # links in texts sent before the move still work
    page = client.get(f"/team/{team.id}")
    assert page.status_code == 302 and page.headers["Location"].endswith(f"/t/fall-chips/team/{team.id}")
    assert client.get("/board").headers["Location"].endswith("/t/fall-chips/board")
    assert b"Sharks" in client.get(f"/t/fall-chips/team/{team.id}").data


def test_double_elimination_night_through_the_pages(app, admin):
    ev = create(admin, "Nine Ball", "double")
    base = f"/t/{ev.id}"
    page = admin.post(f"{base}/admin/settings", data={
        "name": "Nine Ball", "format": "double", "team_size": "1", "seeding": "random", "final_cut": "8",
        "true_double": "1", "groups": "1", "advance": "0", "race_winners": "3", "race_losers": "2", "race_finals": "4",
        "entry_fee": "$10", "added_money": "20", "payouts": "60, 40", "field_cap": "0", "tables": "1, 2, 3",
        "auto_call": "1", "match_minutes": "30", "long_match_minutes": "45", "sms_name": "APA",
    }, follow_redirects=True)
    assert b"Settings saved" in page.data
    page = admin.post(f"{base}/admin/entries/add", data={"bulk": "Ann\nBob, 555-222-3333\nCy\nDi\nEd\n\nAnn"},
                      follow_redirects=True)
    assert b"Added 5 entries" in page.data and b"already registered" in page.data
    player = app.test_client()
    page = player.post(f"{base}/register", data={"agree_terms": "1", "player_name_0": "Flo",
                                                  "player_phone_0": "5553334444", "sms_consent": "1"},
                       follow_redirects=True)
    assert b"Flo is registered" in page.data and b"Your PIN is" in page.data
    t = ev.load()
    assert len(t.field_entries) == 6 and t.projected_pot_cents == 8000
    page = admin.post(f"{base}/admin/start", follow_redirects=True)
    assert b"The bracket is drawn" in page.data
    t = ev.load()
    assert t.status == "running" and len(t.in_play()) == 2  # 6 entries: 2 byes, 2 first-round matches
    assert b"Losers side" in player.get(f"{base}/").data and b"Table 1" in player.get(f"{base}/board").data
    page = player.post(f"{base}/register", data={"agree_terms": "1", "player_name_0": "Gus"}, follow_redirects=True)
    assert b"Registration is closed" in page.data and len(ev.load().entries) == 6

    # Flo keeps score from her own page (already signed in from registering)
    flo = next(e for e in t.entries.values() if e.name == "Flo")
    m = t.current_match_for(flo.id)
    while m is None or m.status != "called":  # the director plays other matches until Flo is on a table
        x = t.in_play()[0]
        admin.post(f"{base}/admin/m/{x.id}/winner", data={"winner": x.a, "forfeit": "1"})
        t = ev.load()
        m = t.current_match_for(flo.id)
    side = "a" if m.a == flo.id else "b"
    for _ in range(m.race_to):
        player.post(f"{base}/entry/{flo.id}/score", data={"side": side, "delta": "1"})
    page = player.get(f"{base}/entry/{flo.id}").data
    assert b"Submit: Flo won" in page
    player.post(f"{base}/entry/{flo.id}/submit")
    assert ev.load().match(m.id).winner == flo.id
    # a stranger can't score
    stranger = app.test_client()
    page = stranger.post(f"{base}/entry/{flo.id}/score", data={"side": side, "delta": "1"}, follow_redirects=True)
    assert b"Enter your PIN first" in page.data

    # fix the last result, then undo the fix
    page = admin.post(f"{base}/admin/m/{m.id}/fix", follow_redirects=True)
    if b"Result corrected" in page.data:
        assert ev.load().match(m.id).winner != flo.id
        admin.post(f"{base}/admin/undo")
        assert ev.load().match(m.id).winner == flo.id

    # a table goes down mid-match
    t = ev.load()
    busy = t.in_play()[0]
    admin.post(f"{base}/admin/table/{busy.table}", data={"down": "1"})
    t = ev.load()
    assert busy.table in t.settings.tables_down and t.match(busy.id).table != busy.table

    # play it out from the director page
    rng = random.Random(1)
    for _ in range(100):
        t = ev.load()
        if t.status == "finished":
            break
        x = t.in_play()[0]
        winner = rng.choice((x.a, x.b))
        for _ in range(x.race_to - (x.score_a if winner == x.a else x.score_b)):
            admin.post(f"{base}/admin/m/{x.id}/score", data={"side": "a" if winner == x.a else "b", "delta": "1"})
        admin.post(f"{base}/admin/m/{x.id}/submit")
    t = ev.load()
    assert t.status == "finished"
    payouts = sorted((e.payout_cents for e in t.entries.values()), reverse=True)
    assert payouts[:3] == [4800, 3200, 0]
    page = player.get(f"{base}/").data
    assert b"Final results" in page and b"$48.00" in page
    for _ in range(40):  # texts are sent from a background thread
        texts = (ev.dir / "texts.log").read_text()
        if "APA: Table" in texts:
            break
        time.sleep(0.05)
    assert "APA: Table" in texts and "Flo is registered" in texts


@pytest.mark.parametrize("fmt", ["single", "modified", "round_robin"])
def test_other_formats_render_and_finish(app, admin, fmt):
    ev = create(admin, f"Test {fmt}", fmt)
    base = f"/t/{ev.id}"
    admin.post(f"{base}/admin/settings", data={
        "name": "X", "format": fmt, "team_size": "2", "seeding": "manual", "final_cut": "4", "groups": "2",
        "advance": "2", "race_winners": "2", "race_losers": "2", "race_finals": "3", "entry_fee": "20",
        "added_money": "0", "payouts": "70, 30", "field_cap": "0", "tables": "A, B, C, D", "auto_call": "1",
        "match_minutes": "30", "long_match_minutes": "45"})
    lines = "\n".join(f"Team {i}: P{i}a & P{i}b" for i in range(1, 11))
    admin.post(f"{base}/admin/entries/add", data={"bulk": lines})
    t = ev.load()
    assert len(t.field_entries) == 10 and t.field_entries[0].name == "Team 1"
    first = t.field_entries[1]
    admin.post(f"{base}/admin/entry/{first.id}/up")
    assert ev.load().field_entries[0].id == first.id  # manual seed order
    admin.post(f"{base}/admin/start")
    t = ev.load()
    assert t.status == "running"
    assert t.entries[first.id].seed == 1
    for path in ("/", "/board", f"/entry/{first.id}", "/admin"):
        assert admin.get(base + path).status_code == 200, path
    for _ in range(200):
        t = ev.load()
        if t.status == "finished":
            break
        x = t.in_play()[0]
        admin.post(f"{base}/admin/m/{x.id}/winner", data={"winner": x.b, "forfeit": "1"})
    t = ev.load()
    assert t.status == "finished"
    assert sum(e.payout_cents for e in t.entries.values()) == 20000
    assert admin.get(base + "/").status_code == 200


def test_viewer_cannot_change_a_bracket(app, admin):
    ev = create(admin, "Look only", "single")
    viewer = app.test_client()
    viewer.post("/admin/login", data={"password": "lookonly"})
    assert viewer.get(f"/t/{ev.id}/admin").status_code == 200
    page = viewer.post(f"/t/{ev.id}/admin/entries/add", data={"bulk": "Ann"}, follow_redirects=True)
    assert b"view-only" in page.data and not ev.load().entries
    assert viewer.post("/admin/new", data={"name": "Nope", "kind": "chip"}).status_code == 302
    assert len(app.config["hub"].events()) == 1


def test_delete_a_tournament(app, admin):
    ev = create(admin, "Gone Soon", "double")
    page = admin.post(f"/admin/event/{ev.id}/delete", data={"confirm": "nope"}, follow_redirects=True)
    assert b"Type DELETE" in page.data and app.config["hub"].get(ev.id)
    page = admin.post(f"/admin/event/{ev.id}/delete", data={"confirm": "DELETE"}, follow_redirects=True)
    assert b"Deleted Gone Soon" in page.data
    assert app.config["hub"].get(ev.id) is None and not ev.dir.exists()
    assert admin.get(f"/t/{ev.id}/").status_code == 404
