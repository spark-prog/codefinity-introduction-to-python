"""Shared tables (Mini-Mania): several events, one set of tables, players in more than one event."""

import random

import pytest

from app import create_app
from chips.engine import Player


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("VIEWER_PASSWORD", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


def member(i):
    return f"322{i:05d}"


def setup_day(app, seed):
    rng = random.Random(seed)
    hub = app.config["hub"]
    events = []
    for name, kind, fmt in (("9-Ball Mini", "bracket", "single"), ("8-Ball Mini", "bracket", "double"),
                            ("Chip Mini", "chip", None)):
        ev = hub.create(name, kind, **({"format": fmt} if fmt else {}))
        t = ev.load()
        if kind == "bracket":
            t.settings.team_size = 1
            # Players 0-11 in the 9-Ball, 6-17 in the 8-Ball: six of them are in both.
            ids = range(0, 12) if fmt == "single" else range(6, 18)
            for i in ids:
                t.register_entry(f"P{i}", [Player(f"Player {i}", "9045550100", rng.randint(2, 7), member=member(i))])
        else:
            t.settings.require_checkin = False
            t.settings.race_to = 1
            for k in range(6):  # teams that include players 15-20 (some also in the 8-Ball)
                t.register_team(f"Team {k}", [Player(f"Player {15 + k}", "9045550100", 4, member=member(15 + k)),
                                              Player(f"Partner {k}", "", 4)])
        t.settings.call_mode = "director"
        ev.store.save(t)
        events.append(ev)
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    r = admin.post("/admin/pools", data={"name": "Mini-Mania", "tables": "1, 2, 3, 4, 5, 6",
                                         "events": [e.id for e in events]})
    assert r.status_code == 302
    return admin, events, rng


def check_no_clashes(app, events):
    tables, players = {}, {}
    for ev in events:
        t = ev.load()
        chip = ev.kind == "chip"
        people = t.teams if chip else t.entries
        for m in t.matches:
            if m.status in ("called", "waiting", "ready") and m.table:
                assert m.table not in tables, f"table {m.table} in {tables[m.table]} and {ev.id}"
                tables[m.table] = ev.id
            if m.status == "called":
                for side in ((m.team_a, m.team_b) if chip else (m.a, m.b)):
                    for p in people[side].players:
                        if p.member:
                            assert p.member not in players, f"{p.name} called in {players[p.member]} and {ev.id}"
                            players[p.member] = ev.id


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_mini_mania_day(app, seed):
    admin, events, rng = setup_day(app, seed)
    assert all(e.load().settings.tables == ["1", "2", "3", "4", "5", "6"] for e in events)
    for ev in events:
        assert admin.post(f"/t/{ev.id}/admin/start").status_code == 302
        check_no_clashes(app, events)
    for step in range(800):
        live = [e for e in events if e.load().status == "running"]
        if not live:
            break
        if rng.random() < 0.4:
            r = admin.post("/admin/pool/mini-mania/call-all")
            assert r.status_code == 302
        else:
            ev = rng.choice(live)
            t = ev.load()
            called = [m for m in t.matches if m.status == "called"]
            if not called:
                admin.post(f"/t/{ev.id}/admin/call-all")
            else:
                m = rng.choice(called)
                if ev.kind == "chip":
                    admin.post(f"/t/{ev.id}/admin/match/{m.id}/winner", data={"winner": rng.choice([m.team_a, m.team_b])})
                    t = ev.load()
                    if step > 150 and t.status == "running":
                        admin.post(f"/t/{ev.id}/admin/end-now", data={"confirm": "END"})
                else:
                    admin.post(f"/t/{ev.id}/admin/m/{m.id}/winner", data={"winner": rng.choice([m.a, m.b])})
        check_no_clashes(app, events)
    assert all(e.load().status == "finished" for e in events), [e.load().status for e in events]
    for url in ("/admin/pool/mini-mania", "/pool/mini-mania", "/admin"):
        assert admin.get(url).status_code == 200


def test_pool_pages_and_rules(app):
    admin, events, _ = setup_day(app, 9)
    page = admin.get("/admin/pool/mini-mania").get_data(as_text=True)
    assert "9-Ball Mini" in page and "8-Ball Mini" in page and "Chip Mini" in page
    r = admin.post("/admin/pools", data={"name": "Other", "tables": "7, 8", "events": [events[0].id]},
                   follow_redirects=True)
    assert "already shares tables in Mini-Mania" in r.get_data(as_text=True)
    viewer = app.test_client()
    assert viewer.post("/admin/pools", data={"name": "X", "tables": "1"}).status_code == 302  # login page
    assert app.config["pools"].get("x") is None
    assert "Table 6" in app.test_client().get("/pool/mini-mania").get_data(as_text=True)


def test_running_events_with_the_same_table_cant_share(app):
    hub = app.config["hub"]
    events = []
    for name, base in (("Early A", 0), ("Early B", 40)):
        ev = hub.create(name, "bracket", format="single")
        t = ev.load()
        t.settings.team_size = 1
        t.settings.tables = ["1", "2"]
        for i in range(4):
            t.register_entry(f"P{base + i}", [Player(f"Player {base + i}", "9045550100", 4, member=member(base + i))])
        t.start(random.Random(1))
        ev.store.save(t)
        events.append(ev)
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    r = admin.post("/admin/pools", data={"name": "Late", "tables": "1, 2", "events": [e.id for e in events]},
                   follow_redirects=True)
    assert "is in use in both Early A and Early B" in r.get_data(as_text=True)
    assert app.config["pools"].get("late") is None


def test_a_player_cant_enter_minis_within_four_hours(app):
    """Mini-Mania: each mini takes about 4 hours, across both table groups (8-foot and 9-foot)."""
    hub = app.config["hub"]
    made = {}
    for name, start in (("8B Singles 10am", "10:00 AM"), ("9B Singles 1pm", "1:00 PM"), ("8B Singles 2pm", "2:00 PM")):
        ev = hub.create(name, "bracket", format="single")
        t = ev.load()
        t.settings.team_size, t.settings.date, t.settings.start_time = 1, "2027-02-21", start
        ev.store.save(t)
        made[name] = ev
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    admin.post("/admin/pools", data={"name": "Eight foot", "tables": "1, 2",
                                     "events": [made["8B Singles 10am"].id, made["8B Singles 2pm"].id]})
    admin.post("/admin/pools", data={"name": "Nine foot", "tables": "17, 18", "events": [made["9B Singles 1pm"].id]})
    assert app.config["pools"].get("eight-foot")["hours"] == 4

    def enter(name, member_no):
        c = app.test_client()
        return c.post(f"/t/{made[name].id}/register", data={
            "player_name_0": f"Pat Example{member_no[-1]}", "player_phone_0": f"904555010{member_no[-1]}", "player_skill_0": "4",
            "player_member_0": member_no, "agree_terms": "1"}, follow_redirects=True).get_data(as_text=True)

    enter("8B Singles 10am", member(500))
    assert made["8B Singles 10am"].load().registered_as(member(500)) is not None
    page = enter("9B Singles 1pm", member(500))  # 3 hours later, in the other table group
    assert "Pat Example0 is already in 8B Singles 10am, which starts Sun at 10:00 AM" in page.replace("&#39;", "'")
    assert made["9B Singles 1pm"].load().registered_as(member(500)) is None
    enter("8B Singles 2pm", member(500))  # 4 hours later: fine
    assert made["8B Singles 2pm"].load().registered_as(member(500)) is not None
    enter("9B Singles 1pm", member(501))  # someone else: fine
    assert made["9B Singles 1pm"].load().registered_as(member(501)) is not None
    # 0 turns it off
    admin.post("/admin/pools", data={"pool_id": "nine-foot", "name": "Nine foot", "tables": "17, 18", "hours": "0",
                                     "events": [made["9B Singles 1pm"].id]})
    admin.post("/admin/pools", data={"pool_id": "eight-foot", "name": "Eight foot", "tables": "1, 2", "hours": "0",
                                     "events": [made["8B Singles 10am"].id, made["8B Singles 2pm"].id]})
    enter("9B Singles 1pm", member(500))
    assert made["9B Singles 1pm"].load().registered_as(member(500)) is not None
