import time

import pytest
from flask.testing import FlaskClient

from app import create_app


# Pages every tournament shares (or that cover all of them) stay where they are; the rest of a
# chip tournament's pages live under /t/<id>/, so the client below adds that for these tests.
GLOBAL_PATHS = ("/t/", "/static", "/admin/login", "/admin/logout", "/admin/rules", "/healthz", "/favicon.ico",
                "/background", "/contact.vcf")


class ChipEventClient(FlaskClient):
    event_id = ""

    def open(self, *args, **kwargs):
        if args and isinstance(args[0], str) and not args[0].startswith(GLOBAL_PATHS):
            path = args[0]
            args = (f"/t/{self.event_id}" + ("/" if path == "/" else path),) + args[1:]
        return super().open(*args, **kwargs)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("VIEWER_PASSWORD", "lookonly")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    event = app.config["hub"].create("Chip Tournament", "chip")
    app.config["store"], app.config["sms"] = event.store, event.sms
    ChipEventClient.event_id = event.id
    app.test_client_class = ChipEventClient
    return app.test_client()


def register(client, name, skills=(5, 5)):
    return client.post(
        "/register",
        data={"name": name, "agree_terms": "1", "sms_consent": "1",
              "player_name_0": "Ann", "player_phone_0": "5551234567", "player_skill_0": str(skills[0]),
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
    assert store.load().match(m.id).status == "called"  # waits for the result to be submitted
    client.post(f"/admin/match/{m.id}/submit")
    t = store.load()
    assert t.teams[m.team_b].chips == 2

    client.post("/admin/undo")
    assert store.load().teams[m.team_b].chips == 3  # undoing the submit gives the chip back
    assert store.load().match(m.id).score_a == 2 and store.load().match(m.id).status == "called"

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


def test_guide_is_for_directors_only(client):
    page = client.get("/guide")
    assert page.status_code == 302 and "/admin/login" in page.headers["Location"]
    assert b"Director&#39;s guide" not in client.get("/admin/login?next=/admin").data
    assert b"/guide" not in client.get("/admin/login?next=/admin").data
    client.post("/admin/login", data={"password": "secret"})
    page = client.get("/guide")
    assert page.status_code == 200 and b"Fixing mistakes" in page.data


def test_home_shows_the_date_in_words(client):
    client.post("/admin/login", data={"password": "secret"})
    client.post("/admin/settings", data={"name": "Halloween", "date": "2026-10-31", "start_time": "9:00 PM",
                                         "race_to": "3", "tables": "1", "entry_fee": "20", "added_money": "0",
                                         "chip_brackets": "6 = 5\n8 = 4\n10 = 3\n11 = 2", "registration_cap": "10"})
    assert b"October 31, 2026 beginning at 9 PM" in client.get("/").data


def test_rules_editor_and_page(client):
    league = FlaskClient(client.application)  # the league rules page, outside any event
    page = league.get("/rules").data
    for title in (b"8-Ball Rules", b"9-Ball Rules", b"Fast 8 Rules", b"Fast Saratoga Rules"):
        assert title in page
    assert b"coming soon" in page and b'id="rule-search"' in page
    assert client.get("/admin/rules").status_code == 302  # director only
    client.post("/admin/login", data={"password": "secret"})
    body = "# The break\n- A scratch on the break is a **foul**\n1. Rack the balls\n<script>alert(1)</script>"
    page = client.post("/admin/rules/8-ball-rules", data={"title": "8-Ball Rules", "body": body}, follow_redirects=True).data
    assert b"Saved 8-Ball Rules" in page
    page = league.get("/rules").data
    assert b'<h3 class="rule-block">The break</h3>' in page
    assert b"<b>foul</b>" in page and b"<ol>" in page
    assert b"<script>alert(1)</script>" not in page and b"&lt;script&gt;" in page
    client.post("/admin/rules/new", data={"title": "10-Ball Rules"})
    client.post("/admin/rules/10-ball-rules/move", data={"dir": "up"})
    from chips.rules import RuleBook
    ids = [r["id"] for r in RuleBook(client.application.config["data_dir"]).load()]
    assert ids[-2:] == ["10-ball-rules", "fast-saratoga-rules"]
    client.post("/admin/rules/10-ball-rules/delete")
    assert "10-ball-rules" not in [r["id"] for r in RuleBook(client.application.config["data_dir"]).load()]


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

    form = {"step": "import"}
    for key, col in {"team": 1, "name1": 2, "skill1": 3, "phone1": 4, "name2": 5, "skill2": 6}.items():
        form[f"map_{key}"] = str(col)
    page = client.post("/admin/import", data=form, follow_redirects=True)
    assert b"Imported 1 team(s)" in page.data
    assert [x.name for x in store.load().teams.values()] == ["Sharks"]
    # Imported players never ticked the consent box, so nobody is texted until they opt in.
    time.sleep(0.1)
    assert not client.application.config["sms"].recent()
    assert store.load().teams[next(iter(store.load().teams))].phones == []


def test_registration_shows_text_consent(client):
    page = client.get("/register").get_data(as_text=True)
    assert 'name="sms_consent" value="1" >' in page or 'name="sms_consent" value="1">' in page  # not pre-checked
    assert "Msg &amp; data rates may apply. Reply HELP for help, STOP to opt out." in page
    assert 'name="agree_terms" value="1" required' in page
    assert 'href="/t/chip-tournament/terms"' in page and 'href="/t/chip-tournament/privacy"' in page
    for path in ("/terms", "/privacy"):
        body = client.get(path).get_data(as_text=True)
        assert "Maximum English LLC dba First Coast APA" in body and "firstcoast@apaleagues.com" in body
    assert "do not sell or share your SMS opt-in data" in client.get("/privacy").get_data(as_text=True)
    assert "Carriers are not liable" in client.get("/terms").get_data(as_text=True)


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
    player.post(f"/team/{sharks.id}/score", data={"side": side, "delta": "1"})  # race to 2: Sharks reach it
    page = player.get(f"/team/{sharks.id}").get_data(as_text=True)
    assert 'id="result-dialog"' in page and "Sharks won the match" in page and "Go back" in page
    player.post(f"/team/{sharks.id}/score", data={"side": side, "delta": "-1"})  # Go back
    assert store.load().match(m.id).status == "called"
    player.post(f"/team/{sharks.id}/score", data={"side": side, "delta": "1"})
    player.post(f"/team/{sharks.id}/submit")
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


def test_background_photo_upload_and_reset(client):
    import io
    default = client.get("/background")
    assert default.status_code == 200 and default.mimetype == "image/jpeg"
    client.post("/admin/login", data={"password": "secret"})
    page = client.post("/admin/background", data={"photo": (io.BytesIO(b"not an image"), "x.txt")},
                       content_type="multipart/form-data", follow_redirects=True).data
    assert b"Choose a JPG, PNG or WebP photo" in page
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    page = client.post("/admin/background", data={"photo": (io.BytesIO(png), "room.png")},
                       content_type="multipart/form-data", follow_redirects=True).data
    assert b"Background photo updated" in page and b"using your own photo" in page
    custom = client.get("/background")
    assert custom.mimetype == "image/png" and custom.data == png
    client.post("/admin/background", data={"reset": "1"})
    assert client.get("/background").mimetype == "image/jpeg"


def test_header_has_league_logo_and_home_icon(client):
    html = client.get("/").get_data(as_text=True)
    assert 'href="https://firstcoast.poolplayers.com/"' in html
    assert "img/apa-first-coast-logo.png" in html
    assert 'class="home-icon"' in html and "brand-mark" not in html
    assert client.get("/static/img/apa-first-coast-logo.png").status_code == 200


def wrong_pins(client, team, n):
    for _ in range(n):
        client.post(f"/team/{team.id}/login", data={"pin": "0000" if team.pin != "0000" else "1111"})


def test_wrong_pin_guesses_lock_out(client):
    register(client, "Sharks")
    team = client.application.config["store"].load().field_teams[0]
    guesser = client.application.test_client()
    wrong_pins(guesser, team, 10)
    page = guesser.post(f"/team/{team.id}/login", data={"pin": team.pin}, follow_redirects=True)
    assert b"Too many wrong PINs" in page.data
    with guesser.session_transaction() as s:
        assert not s.get(f"team_{team.id}")
    # A different phone isn't blocked until the team itself has had 20 wrong guesses.
    other = client.application.test_client()
    other.environ_base["REMOTE_ADDR"] = "10.0.0.2"
    other.post(f"/team/{team.id}/login", data={"pin": team.pin})
    with other.session_transaction() as s:
        assert s.get(f"team_{team.id}")
    for i in range(3):
        c = client.application.test_client()
        c.environ_base["REMOTE_ADDR"] = f"10.0.1.{i}"
        wrong_pins(c, team, 5)
    # The team is under attack: a device that has guessed wrong for it is locked out...
    late = client.application.test_client()
    late.environ_base["REMOTE_ADDR"] = "10.0.2.1"
    wrong_pins(late, team, 1)
    page = late.post(f"/team/{team.id}/roster", data={"pin": team.pin}, follow_redirects=True)
    assert b"Too many wrong PINs" in page.data
    # ...but the real players, on a phone that hasn't guessed wrong, still get in.
    real = client.application.test_client()
    real.environ_base["REMOTE_ADDR"] = "10.0.3.1"
    real.post(f"/team/{team.id}/login", data={"pin": team.pin})
    with real.session_transaction() as s:
        assert s.get(f"team_{team.id}")


def test_wrong_password_guesses_lock_out(client):
    for _ in range(5):
        assert b"Wrong password" in client.post("/admin/login", data={"password": "nope"}, follow_redirects=True).data
    page = client.post("/admin/login", data={"password": "secret"}, follow_redirects=True)
    assert b"Too many wrong passwords" in page.data
    assert client.get("/admin").status_code == 302


def test_view_only_login(client):
    register(client, "Sharks")
    team = client.application.config["store"].load().field_teams[0]
    viewer = client.application.test_client()
    viewer.post("/admin/login", data={"password": "lookonly"})
    page = viewer.get("/admin").get_data(as_text=True)
    assert "view-only" in page
    assert f"PIN {team.pin}" not in page and "PIN ••••" in page and "5551234567" not in page and "+15551234567" not in page
    assert "ending in 4567" in page
    assert viewer.get("/guide").status_code == 200
    assert viewer.get("/admin/import").status_code == 302
    assert viewer.get("/admin/rules").status_code == 302
    viewer.post("/admin/settings", data={"name": "Hacked", "tables": "1"})
    viewer.post(f"/admin/team/{team.id}/check-in")
    viewer.post("/admin/reset", data={"confirm": "RESET", "mode": "delete"})
    t = client.application.config["store"].load()
    assert t.settings.name != "Hacked" and t.teams
    # The view-only login doesn't unlock team pages either.
    assert b"5551234567" not in viewer.get(f"/team/{team.id}/roster").data
    director = client.application.test_client()
    director.post("/admin/login", data={"password": "secret"})
    page = director.get("/admin").get_data(as_text=True)
    assert f"PIN {team.pin}" in page and "view-only" not in page


def test_delete_permanently_and_past_tournaments(client):
    tmp_path = client.application.config["store"].dir  # this tournament's folder
    client.post("/admin/login", data={"password": "secret"})
    store = client.application.config["store"]
    register(client, "Sharks")
    client.post("/admin/reset", data={"confirm": "RESET", "mode": "archive"})
    assert len(store.archives()) == 1 and not store.load().teams
    assert list(tmp_path.glob("texts-*.log")) and not (tmp_path / "texts.log").exists()
    assert "Past tournaments" in client.get("/admin").get_data(as_text=True)
    register(client, "Hustlers")
    client.post("/admin/reset", data={"confirm": "RESET", "mode": "delete"})
    assert len(store.archives()) == 1 and not store.load().teams
    assert not (tmp_path / "texts.log").exists() and not (tmp_path / "history.json").exists()
    assert "Hustlers" not in "".join(p.read_text() for p in tmp_path.glob("*.json"))
    client.post("/admin/archives/delete", data={"stamp": "../rules"})
    assert len(store.archives()) == 1
    client.post("/admin/archives/delete", data={"all": "1"})
    assert store.archives() == [] and not list(tmp_path.glob("texts-*.log"))
    assert "Sharks" not in "".join(p.read_text() for p in tmp_path.iterdir() if p.is_file() and p.suffix in (".json", ".log"))


def test_register_alone_public_list_and_add_partner(client):
    store = client.application.config["store"]
    with store.change("dates") as t:
        t.settings.date, t.settings.start_time = "2099-10-31", "9:00 PM"
    page = client.post("/register", data={"agree_terms": "1", "sms_consent": "1",
        "name": "Ann's team", "solo": "1", "list_seeking": "1",
        "player_name_0": "Ann", "player_skill_0": "4", "player_phone_0": "5551110000",
        "player_name_1": "", "player_skill_1": "", "player_phone_1": ""}, follow_redirects=True)
    assert b"still needs a partner" in page.data
    team = store.load().field_teams[0]
    assert team.needs_partner and team.list_seeking
    reg = client.application.test_client().get("/register").get_data(as_text=True)
    assert 'id="looking"' in reg and "<td>Ann</td>" in reg and "5551110000" not in reg
    assert "looking for a partner" in client.application.test_client().get("/").get_data(as_text=True)
    # Not listed unless they tick the box.
    client.application.test_client().post("/register", data={"agree_terms": "1", "sms_consent": "1",
        "name": "Cy's team", "solo": "1", "player_name_0": "Cy", "player_skill_0": "3",
        "player_phone_0": "5552220000"})
    assert ">Cy<" not in client.get("/register").get_data(as_text=True)
    # Add the partner from the roster page (the registering browser is signed in).
    client.post(f"/team/{team.id}/roster", data={
        "name": "Ann's team", "player_name_0": "Ann", "player_skill_0": "4", "player_phone_0": "",
        "player_name_1": "Bo", "player_skill_1": "5", "player_phone_1": ""})
    team = store.load().teams[team.id]
    assert team.partners == "Ann & Bo" and not team.list_seeking
    assert team.players[0].phone == "5551110000"  # blank cell keeps the number on file
    assert 'id="looking"' not in client.get("/register").get_data(as_text=True)


def test_director_pairs_solo_players(client):
    store = client.application.config["store"]
    for name, skill, phone in (("Ann", "4", "5551110000"), ("Cy", "5", "5552220000")):
        client.application.test_client().post("/register", data={"agree_terms": "1", "sms_consent": "1",
            "name": f"{name}'s team", "solo": "1", "player_name_0": name, "player_skill_0": skill,
            "player_phone_0": phone})
    ann, cy = sorted(store.load().teams.values(), key=lambda t: t.registered_at)
    viewer = client.application.test_client()
    viewer.post("/admin/login", data={"password": "lookonly"})
    viewer.post(f"/admin/team/{ann.id}/pair", data={"other": cy.id})
    assert len(store.load().teams) == 2
    client.post("/admin/login", data={"password": "secret"})
    page = client.get("/admin").get_data(as_text=True)
    assert "Needs partner" in page and "Partner deadline" in page
    client.post(f"/admin/team/{ann.id}/pair", data={"other": cy.id})
    teams = list(store.load().teams.values())
    assert len(teams) == 1 and teams[0].partners == "Ann & Cy"


def test_director_page_has_section_buttons_and_team_search(client):
    register(client, "Sharks")
    client.post("/admin/login", data={"password": "secret"})
    page = client.get("/admin").get_data(as_text=True)
    for target in ("summary", "teams", "texts", "settings", "new-tournament"):
        assert f'href="#{target}"' in page and f'id="{target}"' in page
    assert 'href="#matches"' not in page  # Tables only once the tournament is running
    assert 'id="team-search"' in page and 'data-search="sharks ann bob 4567"' in page
    viewer = client.application.test_client()
    viewer.post("/admin/login", data={"password": "lookonly"})
    assert 'data-search="sharks ann bob"' in viewer.get("/admin").get_data(as_text=True)  # no phone digits


def test_share_preview_tags(client):
    with client.application.config["store"].change("details") as t:
        t.settings.name, t.settings.date, t.settings.start_time, t.settings.venue = "Fall Classic", "2026-10-31", "9:00 PM", "Ace's"
    page = client.get("/register").get_data(as_text=True)
    assert '<meta property="og:image" content="http://localhost/static/img/share.jpg">' in page
    assert '<meta property="og:title" content="Fall Classic">' in page
    assert "October 31, 2026 beginning at 9 PM at Ace&#39;s." in page
    assert '<meta property="og:url" content="http://localhost/t/chip-tournament/register">' in page
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200 and icon.mimetype == "image/x-icon"
    assert 'href="/favicon.ico"' in page
    for path in ("share.jpg", "favicon.png", "favicon-32.png", "apple-touch-icon.png"):
        assert client.get(f"/static/img/{path}").status_code == 200


def test_terms_required_and_partner_opts_in_on_the_team_page(client):
    store = client.application.config["store"]
    page = client.post("/register", data={"name": "Sharks", "player_name_0": "Ann", "player_phone_0": "5551234567",
                                          "player_skill_0": "5", "player_name_1": "Bob", "player_phone_1": "5559990000",
                                          "player_skill_1": "4"}, follow_redirects=True)
    assert b"agree to the Terms" in page.data and not store.load().teams
    client.post("/register", data={"name": "Sharks", "agree_terms": "1", "player_name_0": "Ann",
                                   "player_phone_0": "5551234567", "player_skill_0": "5", "player_name_1": "Bob",
                                   "player_phone_1": "5559990000", "player_skill_1": "4"})
    team = next(iter(store.load().teams.values()))
    assert team.phones == []  # no box ticked: no texts, still registered
    # The director can't opt a player in; the partner does it with the team PIN.
    director = client.application.test_client()
    director.post("/admin/login", data={"password": "secret"})
    director.post(f"/team/{team.id}/texts", data={"player": "1", "sms_consent": "1"})
    assert store.load().teams[team.id].phones == []
    bob = client.application.test_client()
    assert b"Enter your team PIN to turn on text alerts" in bob.get(f"/team/{team.id}").data
    bob.post(f"/team/{team.id}/login", data={"pin": team.pin})
    assert b"Turn on text alerts for Bob" in bob.get(f"/team/{team.id}").data
    page = bob.post(f"/team/{team.id}/texts", data={"player": "1", "sms_consent": "1"}, follow_redirects=True)
    assert b"Text alerts are on for Bob" in page.data
    assert store.load().teams[team.id].phones == ["5559990000"]
    sms = client.application.config["sms"]
    for _ in range(50):
        if sms.recent():
            break
        time.sleep(0.02)
    assert f"/team/{team.id}" in sms.recent()[0]["body"]  # welcome text links to the team page


def test_director_fixes_a_wrong_result(client):
    for name in ("Sharks", "Rails", "Bankers"):
        register(client, name)
    client.post("/admin/login", data={"password": "secret"})
    check_in_and_start(client)
    store = client.application.config["store"]
    m = store.load().open_matches()[0]
    client.post(f"/admin/match/{m.id}/winner", data={"winner": m.team_a})
    page = client.get("/admin").get_data(as_text=True)
    assert "Recent results" in page and "Fix result" in page and 'data-dialog-title="Fix match #' in page
    page = client.post(f"/admin/match/{m.id}/fix", follow_redirects=True).get_data(as_text=True)
    assert "Result corrected on match #" in page
    assert store.load().match(m.id).winner == m.team_b


def test_partner_deadline_undo_sticks_and_labels_name_the_teams(client):
    store = client.application.config["store"]
    with store.change("dates") as t:
        t.settings.date, t.settings.start_time = "2099-10-31", "9:00 PM"
    client.application.test_client().post("/register", data={
        "name": "Ann's team", "solo": "1", "agree_terms": "1", "player_name_0": "Ann", "player_skill_0": "4",
        "player_phone_0": "5551110000"})
    register(client, "Sharks")
    client.post("/admin/login", data={"password": "secret"})
    page = client.get("/admin").get_data(as_text=True)
    assert "Registration: Sharks" in page and "use Remove next to it instead" in page  # Undo names what it removes
    assert "Needs a partner to check in" in page
    ann = next(x for x in store.load().teams.values() if x.name == "Ann's team")
    page = client.post(f"/admin/team/{ann.id}/check-in", follow_redirects=True).get_data(as_text=True)
    assert "needs a partner before it can check in" in page and not store.load().teams[ann.id].checked_in_at
    # Move the tournament to the past: saving warns, then the clock moves Ann to the waiting list.
    page = client.post("/admin/settings", data={
        "name": "Test", "date": "2020-10-31", "start_time": "9:00 PM", "tables": "1, 2", "race_to": "3",
        "entry_fee": "20", "added_money": "0", "registration_cap": "10", "chip_brackets": "6 = 5\n8 = 4\n10 = 3\n11 = 2",
        "field_cap": "0"}, follow_redirects=True).get_data(as_text=True)
    assert "Heads up" in page and "Ann&#39;s team will move to the waiting list" in page
    assert store.load().teams[ann.id].status == "waitlisted"
    assert "Partner deadline: moved Ann&#39;s team to the waiting list" in page  # the Undo button says what it undoes
    page = client.post("/admin/undo", follow_redirects=True).get_data(as_text=True)
    assert "partner deadline is paused" in page and "Move them to the waiting list now" in page
    client.get("/admin")
    client.application.config["tick"]()
    assert store.load().teams[ann.id].status == "registered"  # it stuck
    client.post("/admin/partner-deadline")
    assert store.load().teams[ann.id].status == "waitlisted"


def test_stale_undo_does_nothing(client):
    register(client, "Sharks")
    client.post("/admin/login", data={"password": "secret"})
    store = client.application.config["store"]
    page = client.get("/admin").get_data(as_text=True)
    import re as _re
    step = _re.search(r'name="step" value="(\d+)"', page).group(1)
    register(client.application.test_client(), "Rails")  # a player registers meanwhile
    page = client.post("/admin/undo", data={"step": step}, follow_redirects=True).get_data(as_text=True)
    assert "Nothing was undone" in page and len(store.load().teams) == 2
    step = _re.search(r'name="step" value="(\d+)"', page).group(1)
    client.post("/admin/undo", data={"step": step})
    assert [x.name for x in store.load().teams.values()] == ["Sharks"]


def test_settings_reject_bad_dates_and_a_race_below_a_live_score(client):
    for name in ("Sharks", "Rails"):
        register(client, name)
    client.post("/admin/login", data={"password": "secret"})
    form = {"name": "T", "date": "0001-01-01", "start_time": "9:00 PM", "tables": "1, 2", "race_to": "3",
            "entry_fee": "20", "added_money": "0", "registration_cap": "10",
            "chip_brackets": "6 = 5\n8 = 4\n10 = 3\n11 = 2", "field_cap": "0"}
    page = client.post("/admin/settings", data=form, follow_redirects=True).get_data(as_text=True)
    assert "Enter a real tournament date" in page and client.get("/admin").status_code == 200
    check_in_and_start(client)
    store = client.application.config["store"]
    m = store.load().open_matches()[0]
    client.post(f"/admin/match/{m.id}/score", data={"side": "a", "delta": "1"})
    client.post(f"/admin/match/{m.id}/score", data={"side": "a", "delta": "1"})
    page = client.post("/admin/settings", data={**form, "date": "2099-10-31", "race_to": "1"},
                       follow_redirects=True).get_data(as_text=True)
    assert "can&#39;t be lowered" in page and store.load().settings.race_to == 3


def test_viewer_page_load_changes_nothing(client):
    store = client.application.config["store"]
    register(client, "Sharks")
    before = store.undo_depth()
    viewer = client.application.test_client()
    viewer.post("/admin/login", data={"password": "lookonly"})
    viewer.get("/admin")
    assert store.undo_depth() == before


def test_name_fix_on_the_roster_keeps_the_hidden_cell(client):
    register(client, "Sharks")
    store = client.application.config["store"]
    team = next(iter(store.load().teams.values()))
    page = client.get(f"/team/{team.id}/roster").get_data(as_text=True)
    assert 'name="same_person_0"' in page
    client.post(f"/team/{team.id}/roster", data={"name": "Sharks", "player_name_0": "Annie", "player_skill_0": "5",
                                                 "player_phone_0": "", "same_person_0": "1",
                                                 "player_name_1": "Bob", "player_skill_1": "5"})
    p = store.load().teams[team.id].players[0]
    assert p.name == "Annie" and p.phone == "5551234567" and p.sms_consent


def test_director_picks_a_theme_for_every_page(client):
    from chips.themes import THEMES
    client.post("/admin/login", data={"password": "secret"})
    assert 'data-theme="electric"' in client.get("/").get_data(as_text=True)  # default
    page = client.get("/admin").get_data(as_text=True)
    assert all(f'value="{key}"' in page for key, *_ in THEMES)
    form = {"name": "T", "date": "2099-10-31", "start_time": "9:00 PM", "tables": "1, 2", "race_to": "3",
            "entry_fee": "20", "added_money": "0", "registration_cap": "10",
            "chip_brackets": "6 = 5\n8 = 4\n10 = 3\n11 = 2", "field_cap": "0", "theme": "halloween"}
    client.post("/admin/settings", data=form)
    for path in ("/", "/board", "/register", "/rules"):
        assert 'data-theme="halloween"' in client.get(path).get_data(as_text=True)
    client.post("/admin/settings", data={**form, "theme": "made-up"})
    assert 'data-theme="electric"' in client.get("/").get_data(as_text=True)
    css = client.get("/static/style.css").get_data(as_text=True)
    assert all(f'[data-theme="{key}"]' in css for key, *_ in THEMES if key != "electric")


def test_save_contact_note_and_card(tmp_path, monkeypatch, client):
    assert "Save contact" not in client.get("/register").get_data(as_text=True)  # no sending number set
    assert client.get("/contact.vcf").status_code == 404
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+19049135680")
    app = create_app(str(tmp_path / "other"))
    app.testing = True
    event = app.config["hub"].create("Chip Tournament", "chip")
    c = app.test_client()
    page = c.get(f"/t/{event.id}/register").get_data(as_text=True)
    assert "Texts come from <b class=\"nowrap\">(904) 913-5680</b>" in page and 'href="/contact.vcf"' in page
    card = c.get("/contact.vcf")
    assert card.mimetype == "text/vcard" and "attachment" in card.headers["Content-Disposition"]
    body = card.get_data(as_text=True)
    assert "FN:First Coast APA Tournaments" in body and "TEL;TYPE=CELL:+19049135680" in body


def test_house_icon_goes_to_all_events(client):
    page = client.get("/").get_data(as_text=True)  # an event's home page
    assert '<a class="home-icon" href="/" title="All events"' in page
    assert f'<a class="brand-name" href="/t/{ChipEventClient.event_id}/">' in page
    assert ">Home</a>" in page and "All events</a>" not in page
