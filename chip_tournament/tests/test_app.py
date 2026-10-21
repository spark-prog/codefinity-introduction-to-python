import time

import pytest

from app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app.test_client()


def register(client, name, skills=(5, 5)):
    return client.post(
        "/register",
        data={"name": name, "player_name_0": "Ann", "player_phone_0": "5551234567", "player_skill_0": str(skills[0]),
              "player_name_1": "Bob", "player_phone_1": "", "player_skill_1": str(skills[1])},
        follow_redirects=True,
    )


def check_in_and_start(admin):
    """Check every team in at the desk, then start (as a director would)."""
    store = admin.application.config["store"]
    for team in store.load().field_teams:
        admin.post(f"/admin/team/{team.id}/check-in")
    return admin.post("/admin/start", follow_redirects=True)


def test_full_flow(client):
    assert client.get("/").status_code == 200
    assert client.get("/admin").status_code == 302

    for name in ("Sharks", "Rails", "Bankers", "Scratch"):
        assert name.encode() in register(client, name).data

    client.post("/admin/login", data={"password": "secret"})
    page = client.post("/admin/settings", data={
        "name": "Fall Chips", "venue": "Corner Pocket", "address": "1 Main St", "entry_fee": "$25",
        "added_money": "0", "race_to": "2", "tables": "1, 2", "chip_brackets": "6 = 5\n8 = 4\n10 = 3\n11 = 2",
        "registration_cap": "10",
    }, follow_redirects=True)
    assert b"Settings saved" in page.data

    page = client.post("/admin/start", follow_redirects=True)
    assert b"Table 1" in page.data
    assert b"$100.00" in page.data  # pot

    store = client.application.config["store"]
    t = store.load()
    m = t.open_matches()[0]
    client.post(f"/admin/match/{m.id}/score", data={"side": "a", "delta": "1"})
    client.post(f"/admin/match/{m.id}/score", data={"side": "a", "delta": "1"})
    t = store.load()
    assert t.teams[m.team_b].chips == 2

    client.post("/admin/undo")
    assert store.load().teams[m.team_b].chips == 3  # undoing the winning point gives the chip back
    assert store.load().match(m.id).score_a == 1

    client.post("/admin/cash-in/open")
    team = store.load().teams[m.team_b]
    page = client.post(f"/admin/team/{team.id}/cash-in", follow_redirects=True)
    assert b"in progress" in page.data  # match has a score, can't cash in yet

    page = client.post("/admin/last-call", follow_redirects=True)
    assert b"Last call is on" in page.data
    assert store.load().status == "running"

    page = client.post("/admin/end-now", follow_redirects=True)
    assert b"Tournament ended" in page.data
    t = store.load()
    assert sum(x.payout_cents for x in t.teams.values()) == 10000

    assert client.get("/board").status_code == 200
    assert client.get(f"/team/{team.id}").status_code == 200


def test_roster_needs_pin(client):
    register(client, "Sharks")
    store = client.application.config["store"]
    team = next(iter(store.load().teams.values()))

    other = client.application.test_client()
    page = other.get(f"/team/{team.id}/roster")
    assert b"Team PIN" in page.data
    other.post(f"/team/{team.id}/roster", data={"pin": "wrong"})
    other.post(f"/team/{team.id}/roster", data={"name": "Hacked", "player_name_0": "x", "player_skill_0": "1",
                                                "player_phone_0": "1", "player_name_1": "y", "player_skill_1": "1"})
    assert store.load().teams[team.id].name == "Sharks"

    other.post(f"/team/{team.id}/roster", data={"pin": team.pin})
    # Same partners, skill went up: allowed up to 11.
    other.post(f"/team/{team.id}/roster", data={"name": "Sharks", "player_name_0": "Ann", "player_phone_0": "5559990000",
                                                "player_skill_0": "6", "player_name_1": "Bob", "player_skill_1": "5"})
    updated = store.load().teams[team.id]
    assert [(p.name, p.skill) for p in updated.players] == [("Ann", 6), ("Bob", 5)]
    assert updated.combined_skill == 11

    # Swapping in a new partner counts as registering again: 10 or less.
    page = other.post(f"/team/{team.id}/roster", follow_redirects=True, data={
        "name": "Sharks", "player_name_0": "Ann", "player_phone_0": "5559990000", "player_skill_0": "6",
        "player_name_1": "Cy", "player_skill_1": "5"})
    assert b"partner swap counts as a new registration" in page.data
    assert store.load().teams[team.id].players[1].name == "Bob"


def test_field_cap_and_waiting_list(client):
    client.post("/admin/login", data={"password": "secret"})
    client.post("/admin/settings", data={"name": "Cups", "race_to": "2", "tables": "1", "entry_fee": "20",
                                         "added_money": "0", "chip_brackets": "6 = 5\n8 = 4\n10 = 3\n11 = 2",
                                         "registration_cap": "10", "field_cap": "2"})
    for name in ("A Team", "B Team", "C Team"):
        register(client, name)
    store = client.application.config["store"]
    t = store.load()
    assert [x.status for x in sorted(t.teams.values(), key=lambda x: x.name)] == ["registered", "registered", "waitlisted"]
    assert b"Waiting list" in client.get("/").data
    first = next(x for x in t.teams.values() if x.name == "A Team")
    client.post(f"/admin/team/{first.id}/remove")
    assert next(x for x in store.load().teams.values() if x.name == "C Team").status == "registered"


def test_registration_over_cap_is_rejected(client):
    page = register(client, "Too Good", skills=(6, 5))
    assert b"registration cap of 10" in page.data
    assert b"Too Good" in page.data  # form keeps what was typed
    assert not client.application.config["store"].load().teams


def test_arrived_buttons_set_start_time(client):
    for name in ("Sharks", "Rails"):
        register(client, name)
    client.post("/admin/login", data={"password": "secret"})
    check_in_and_start(client)
    store = client.application.config["store"]
    m = store.load().open_matches()[0]
    page = client.get("/admin").data
    assert b"Arrived" in page and b"check in by" in page
    client.post(f"/admin/match/{m.id}/arrived", data={"side": "a"})
    page = client.post(f"/admin/match/{m.id}/arrived", data={"side": "b"}, follow_redirects=True).data
    assert b"Match starts" in page
    assert store.load().match(m.id).start_at


def test_undo_of_automatic_forfeit_sticks(client):
    from datetime import datetime, timedelta
    for name in ("Sharks", "Rails"):
        register(client, name)
    client.post("/admin/login", data={"password": "secret"})
    check_in_and_start(client)
    store = client.application.config["store"]
    with store.change("make it overdue") as t:
        m = t.open_matches()[0]
        m.called_at = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
    client.application.config["tick"]()
    assert store.load().match(m.id).forfeit == "both"
    page = client.post("/admin/undo", follow_redirects=True).data  # the admin page runs the clock again
    assert b"Auto-forfeit is paused" in page
    restored = store.load().match(m.id)
    assert restored.status == "called" and restored.clock_paused and restored.forfeit is None


def test_guide_page(client):
    page = client.get("/guide")
    assert page.status_code == 200
    assert b"Last call" in page.data and b"Fixing mistakes" in page.data


def test_production_refuses_default_password(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPS_PRODUCTION", "1")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        create_app(str(tmp_path))
    monkeypatch.setenv("ADMIN_PASSWORD", "a-real-password")
    app = create_app(str(tmp_path))
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.test_client().get("/healthz").json == {"ok": True}


def test_csv_import_flow(client):
    import io
    client.post("/admin/login", data={"password": "secret"})
    csv_text = ("Timestamp,Team Name,Player 1 Name,Player 1 Skill,Player 1 Phone,Player 2 Name,Player 2 Skill\n"
                "10/1/2026 9:00,Sharks,Ann,5,5551110001,Bob,4\n"
                "10/1/2026 9:05,Rails,Cy,6,5551110002,Di,6\n")
    page = client.post("/admin/import", data={"step": "upload", "file": (io.BytesIO(csv_text.encode()), "responses.csv")},
                       content_type="multipart/form-data")
    assert b"Import 1 team(s)" in page.data and b"registration cap" in page.data
    store = client.application.config["store"]
    assert not store.load().teams  # preview saves nothing

    form = {"step": "import", "send_texts": "1"}
    for key, col in {"team": 1, "name1": 2, "skill1": 3, "phone1": 4, "name2": 5, "skill2": 6}.items():
        form[f"map_{key}"] = str(col)
    page = client.post("/admin/import", data=form, follow_redirects=True)
    assert b"Imported 1 team(s)" in page.data
    assert [x.name for x in store.load().teams.values()] == ["Sharks"]
    sms = client.application.config["sms"]
    for _ in range(50):  # texts go out on a background thread
        if any("PIN" in x["body"] for x in sms.recent()):
            break
        time.sleep(0.02)
    assert any("PIN" in x["body"] for x in sms.recent())


def test_registration_shows_text_consent(client):
    page = client.get("/register").data
    assert b"you agree to receive text alerts" in page and b"Reply STOP to opt out" in page


def test_call_next_button(client):
    for name in ("Sharks", "Rails", "Bankers", "Scratch", "Cues"):
        register(client, name)
    client.post("/admin/login", data={"password": "secret"})
    client.post("/admin/settings", data={"name": "X", "race_to": "2", "tables": "1", "entry_fee": "20",
                                         "added_money": "0", "chip_brackets": "6 = 5\\n8 = 4\\n10 = 3\\n11 = 2",
                                         "registration_cap": "10", "field_cap": "0"})
    check_in_and_start(client)
    store = client.application.config["store"]
    t = store.load()
    last = t.queue_order(t.winners_list)[-1]
    page = client.get("/admin").data
    assert b"Call next" in page and b"Winners list" in page
    page = client.post(f"/admin/team/{last}/call-next", follow_redirects=True).data
    assert b"is first in line" in page
    t = store.load()
    assert t.queue_order(t.winners_list)[0] == last


def start_two_teams(client):
    register(client, "Sharks"); register(client, "Rails")
    store = client.application.config["store"]
    admin = client.application.test_client()
    admin.post("/admin/login", data={"password": "secret"})
    admin.post("/admin/settings", data={"name": "X", "race_to": "2", "tables": "1", "entry_fee": "20",
                                        "added_money": "0", "chip_brackets": "6 = 5\n8 = 4\n10 = 3\n11 = 2",
                                        "registration_cap": "10", "field_cap": "0"})
    admin.post("/admin/start")
    t = store.load()
    return store, admin, t.open_matches()[0], {x.name: x for x in t.teams.values()}


def test_team_checks_in_and_keeps_score(client):
    store, admin, m, teams = start_two_teams(client)
    sharks = teams["Sharks"]
    side = "a" if m.team_a == sharks.id else "b"
    other = "b" if side == "a" else "a"
    player = client.application.test_client()  # a phone that hasn't entered the PIN
    page = player.get(f"/team/{sharks.id}").data
    assert b"Enter your team PIN" in page and b"We&#39;re at the table" not in page
    page = player.post(f"/team/{sharks.id}/score", data={"side": side, "delta": "1"}, follow_redirects=True).data
    assert b"Enter your team PIN first" in page and store.load().match(m.id).score_a + store.load().match(m.id).score_b == 0

    player.post(f"/team/{sharks.id}/login", data={"pin": sharks.pin})
    assert "We're at the table" in player.get(f"/team/{sharks.id}").data.decode()
    player.post(f"/team/{sharks.id}/arrived")
    mm = store.load().match(m.id)
    assert getattr(mm, "arrived_" + side) and not getattr(mm, "arrived_" + other)  # only checks in itself

    player.post(f"/team/{sharks.id}/score", data={"side": other, "delta": "1"})  # either side can be scored
    player.post(f"/team/{sharks.id}/score", data={"side": side, "delta": "1"})
    player.post(f"/team/{sharks.id}/score", data={"side": side, "delta": "1"})  # race to 2: Sharks win
    done = store.load().match(m.id)
    assert done.status == "done" and done.winner == sharks.id
    assert any("Sharks entered +1" in line for line in store.load().log)


def test_phone_numbers_stay_on_the_director_side(client):
    register(client, "Sharks")  # partner 1 cell 5551234567
    store = client.application.config["store"]
    team = next(iter(store.load().teams.values()))
    player = client.application.test_client()
    player.post(f"/team/{team.id}/login", data={"pin": team.pin})
    assert b"5551234567" not in player.get(f"/team/{team.id}").data
    roster = player.get(f"/team/{team.id}/roster").data
    assert b"5551234567" not in roster and b"ending in 4567" in roster
    # Saving with the box left blank keeps the number on file.
    player.post(f"/team/{team.id}/roster", data={"name": "Sharks", "player_name_0": "Ann", "player_skill_0": "5",
                                                 "player_phone_0": "", "player_name_1": "Bob", "player_skill_1": "5"})
    assert store.load().teams[team.id].players[0].phone == "5551234567"
    for page in ("/", "/board", "/register"):
        assert b"5551234567" not in player.get(page).data
    admin = client.application.test_client()
    admin.post("/admin/login", data={"password": "secret"})
    assert b"5551234567" in admin.get("/admin").data
    assert b"5551234567" in admin.get(f"/team/{team.id}/roster").data


def test_event_check_in_and_no_show(client):
    for name in ("Sharks", "Rails", "Bankers"):
        register(client, name)
    client.post("/admin/login", data={"password": "secret"})
    store = client.application.config["store"]
    teams = {x.name: x for x in store.load().teams.values()}
    page = client.get("/admin").data
    assert b"0 of 3 teams in the field checked in" in page and b"Not checked in yet" in page
    client.post(f"/admin/team/{teams['Sharks'].id}/check-in")
    client.post(f"/admin/team/{teams['Rails'].id}/check-in")
    page = client.post("/admin/start", follow_redirects=True).data
    assert b"haven&#39;t checked in: Bankers" in page and store.load().status == "setup"
    client.post("/admin/no-show-rest")
    assert store.load().teams[teams["Bankers"].id].status == "no_show"
    assert b"Bankers" not in client.get("/").data  # no-shows aren't listed publicly
    client.post("/admin/start")
    assert store.load().status == "running" and len(store.load().active_teams) == 2


def test_money_settings_editable_until_every_team_has_played(client):
    for name in ("Sharks", "Rails", "Bankers", "Scratch"):
        register(client, name)
    client.post("/admin/login", data={"password": "secret"})
    settings = {"name": "X", "race_to": "1", "tables": "1", "entry_fee": "20", "added_money": "0",
                "chip_brackets": "6 = 5\n8 = 4\n10 = 3\n11 = 2", "registration_cap": "10", "field_cap": "0"}
    client.post("/admin/settings", data=settings)
    check_in_and_start(client)
    store = client.application.config["store"]
    assert b"Late entries are open" in client.get("/admin").data
    page = client.post("/admin/settings", data={**settings, "entry_fee": "25"}, follow_redirects=True).data
    assert b"Settings saved" in page and store.load().pot_cents == 4 * 2500
    while store.load().teams_yet_to_play:
        m = next(x for x in store.load().matches if x.status == "called")
        client.post(f"/admin/match/{m.id}/winner", data={"winner": m.team_a})
    assert store.load().entries_locked
    assert b"Registration is closed" in client.get("/register").data
    client.post("/admin/settings", data={**settings, "entry_fee": "30"})
    assert store.load().settings.entry_fee_cents == 2500  # locked: ignored
