"""The facts a player needs at a glance: All Events cards and the top of an event page (status, fee, skill rule, spots)."""

import random
from datetime import datetime, timedelta

import pytest

from app import create_app
from chips import backup, event_info, players
from chips.bracket import BracketTournament
from chips.engine import Player, Tournament
from tests.test_eligibility import COLS, person, rules
from tests.test_player_login import FakeMailer
from tests.test_register_members import Clients


def bracket(**kw):
    t = BracketTournament()
    t.settings.name, t.settings.date, t.settings.start_time = "8-Ball Board", "2026-11-07", "10:30 AM"
    for k, v in kw.items():
        setattr(t.settings, k, v)
    return t


# ------------------------------------------------------------------ the helpers

def test_status_in_words():
    open_ = event_info.status(bracket())
    assert (open_["key"], open_["label"], open_["detail"]) == ("open", "Registration open", "")
    soon = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT18:00")
    st = event_info.status(bracket(registration_closes=soon))
    assert st["key"] == "open" and st["detail"].startswith("Closes ")
    st = event_info.status(bracket(registration_opens=soon))
    assert st["key"] == "before" and st["label"].startswith("Registration opens ")
    assert event_info.status(bracket(registration_closed=True))["key"] == "closed"
    past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT09:00")
    st = event_info.status(bracket(registration_closes=past))
    assert st["key"] == "closed" and st["detail"].startswith("Closed ")
    t = bracket()
    t.status = "running"
    assert event_info.status(t)["label"] == "Live now"
    t.status = "finished"
    assert event_info.status(t)["key"] == "finished"


def test_fee_and_spots():
    assert event_info.fee(bracket(entry_fee_cents=0)) == "Free"
    assert event_info.fee(bracket(entry_fee_cents=2000)) == "$20.00 entry"
    assert event_info.fee(bracket(entry_fee_cents=2000), word=False) == "$20.00"
    assert event_info.fee(bracket(entry_fee_cents=4000, team_size=2)) == "$40.00 per team"
    assert event_info.fee(Tournament()) == "$20.00 per team"
    t = bracket(field_cap=10)
    assert event_info.spots(t) == "0 of 10 spots filled"
    t.register_entry("A", [Player("A", skill=4)])
    assert event_info.spots(t) == "1 of 10 spots filled"
    assert event_info.spots(bracket()) == "0 entries"
    chip = Tournament()
    chip.settings.field_cap = 2
    for n in "ABC":
        chip.register_team(n, [Player(n + "1", "9045550100", 4), Player(n + "2", "", 4)])
    assert event_info.spots(chip) == "Full · 1 waiting"


def test_the_skill_rule():
    assert event_info.skill_rule(bracket()) == "Singles · all skill levels"
    assert event_info.skill_rule(bracket(team_size=2, team_skill_cap=9, max_player_skill=6)) == \
        "Doubles · combined SL 9 or less · no player above SL 6"
    assert event_info.skill_rule(bracket(team_size=4)) == "Teams of 4 · all skill levels"
    assert event_info.skill_rule(bracket(skill_groups=[[1, 3], [4, 4], [5, 9]])) == "Singles · skill groups SL 1–3, 4, 5–9"
    assert event_info.skill_short(bracket(team_size=2, team_skill_cap=9, max_player_skill=6)) == \
        "combined SL 9 or less · no player above SL 6"
    assert event_info.skill_short(bracket(skill_groups=[[1, 3], [4, 9]])) == "skill groups"
    assert event_info.skill_short(bracket()) == "all skill levels"
    chip = Tournament()
    chip.settings.registration_cap = 9
    assert event_info.skill_rule(chip) == "Teams of 2 · combined SL 9 or less to register"
    gated = bracket(eligibility=rules(min_matches=6))
    assert event_info.skill_rule(gated).endswith("eligibility rules apply")


def test_short_when():
    assert event_info.short_when(bracket().settings) == "Sat, Nov 7 · 10:30 AM"
    assert event_info.short_when(bracket(start_time="").settings) == "Sat, Nov 7"
    assert event_info.short_when(bracket(date="", start_time="").settings) == ""


# ------------------------------------------------------------------ the pages

@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: FakeMailer()))
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["players"].apply(*players.parse_rows([
        COLS, person("32200001", first="Ann", last="Lee"), person("32200003", first="Cy", last="Day", mp8="3")]), "t", "t")
    return app


def make(app, name, kind="bracket", **kw):
    ev = app.config["hub"].create(name, kind, **({"format": "single"} if kind == "bracket" else {}))
    t = ev.load()
    for k, v in kw.items():
        setattr(t.settings, k, v)
    ev.store.save(t)
    return ev


def test_all_events_groups_and_cards(app):
    soon = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%dT18:00")
    make(app, "Open Night", date="2026-11-14", entry_fee_cents=2000, team_size=2, team_skill_cap=9)
    make(app, "Later Night", date="2026-12-05", registration_opens=soon)
    live = make(app, "Live Night", date="2026-11-07", call_mode="continuous", payouts=[])
    t = live.load()
    for n in ("A One", "B Two", "C Three", "D Four"):
        t.register_entry(n, [Player(n, skill=4)])
    t.start(random.Random(1))
    live.store.save(t)
    done = make(app, "Done Night", date="2026-10-31", call_mode="continuous", payouts=[])
    t = done.load()
    for n in ("E One", "F Two"):
        t.register_entry(n, [Player(n, skill=4)])
    t.start(random.Random(1))
    while t.status == "running":
        m = next(m for m in t.matches if m.status == "called")
        while not t.race_winner(m):
            t.score(m.id, "a", 1)
        t.submit_result(m.id)
    done.store.save(t)
    page = app.test_client().get("/").get_data(as_text=True)
    assert page.index("Live now") < page.index("Open for registration") < page.index("Upcoming") < page.index("Results")
    assert "Registration open" in page and ">Opens " in page
    assert "$20.00 per team" in page and "Bring a partner" in page and "Combined SL 9 or less" in page
    assert page.count("Live Night") == 1 and "Done Night" in page.split("Results", 1)[1]


def test_the_home_page_has_a_search_box_and_a_count(app):
    for n in range(3):
        make(app, f"Night {n}", date=f"2026-12-{n + 1:02d}")
    page = app.test_client().get("/").get_data(as_text=True)
    assert 'id="event-search"' in page and 'placeholder="Find a tournament"' in page
    assert '<span id="home-count-n">3</span> tournaments shown' in page and "control-table-wordmark.png" in page


def test_what_each_event_means_for_the_logged_in_player(app):
    ev_in = make(app, "Joined Night", date="2026-11-14")
    t = ev_in.load()
    t.register_entry("Ann Lee", [Player("Ann Lee", skill=4, member="32200001")])
    ev_in.store.save(t)
    make(app, "Gated Night", date="2026-11-21", eligibility=rules(min_matches=6, list_label="Gated"))
    make(app, "Anyone Night", date="2026-11-28")
    ann = Clients(app).logged_in("32200001").get("/").get_data(as_text=True)
    assert "✓ You&#39;re registered" in ann or "✓ You're registered" in ann
    assert "You can register" in ann
    cy = Clients(app).logged_in("32200003").get("/").get_data(as_text=True)
    assert "Not eligible" in cy and "You can register" in cy  # Anyone Night is open to Cy
    anon = app.test_client().get("/").get_data(as_text=True)
    assert "You can register" not in anon and "Not eligible yet" not in anon


def test_the_event_page_header_and_slim_bar(app):
    ev = make(app, "Qualifier", date="2026-11-21", start_time="10:00 AM", team_size=4, team_skill_cap=22, max_player_skill=7,
              entry_fee_cents=0, field_cap=16, venue="Park Ave", address="1 Main St")
    c = app.test_client()
    page = c.get(f"/t/{ev.id}/").get_data(as_text=True)
    for want in ("<dt>Entry</dt><dd>Free", "<dt>Field</dt><dd>0 of 16 spots filled", "Teams of 4 · combined SL 22 or less · no player above SL 7",
                 "Registration open", 'id="event-strip"', 'class="event-hero-end"', ">Register</a>"):
        assert want in page, want
    strip = page.split('id="event-strip"', 1)[1].split("</div>", 3)[0:3]
    assert "Sat, Nov 21 · 10:00 AM · Free · combined SL 22 or less · no player above SL 7" in " ".join(strip)
    # closed: no Register button, the strip says why
    t = ev.load()
    t.settings.registration_closed = True
    ev.store.save(t)
    page = c.get(f"/t/{ev.id}/").get_data(as_text=True)
    assert "Registration closed" in page and 'href="/t/%s/register"' % ev.id not in page.split('id="event-strip"')[1]


def test_a_registered_player_sees_registered_in_the_slim_bar(app):
    ev = make(app, "Joined Night", date="2026-11-14")
    t = ev.load()
    t.register_entry("Ann Lee", [Player("Ann Lee", skill=4, member="32200001")])
    ev.store.save(t)
    page = Clients(app).logged_in("32200001").get(f"/t/{ev.id}/").get_data(as_text=True)
    assert page.split('id="event-strip"', 1)[1].split("<script>", 1)[0].count("Registered") == 1


def test_chip_event_header(app):
    ev = make(app, "Chips Night", kind="chip", date="2027-01-02", start_time="5:00 PM", field_cap=32, registration_cap=9)
    page = app.test_client().get(f"/t/{ev.id}/").get_data(as_text=True)
    assert "$20.00 per team" in page and "Teams of 2 · combined SL 9 or less to register" in page
    assert "0 of 32 spots filled" in page and ">Register a team</a>" in page and 'id="event-strip"' in page


# ------------------------------------------------------------------ Alert me, before registration opens

def test_alert_me_before_registration_opens(app, monkeypatch):
    from chips import open_alerts
    soon = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT18:00")
    ev = make(app, "Later Night", date="2026-12-05", registration_opens=soon)
    page = app.test_client().get(f"/t/{ev.id}/").get_data(as_text=True)
    assert "Registration opens " in page and "Log in to get an alert" in page and 'class="countdown"' in page
    c = Clients(app).logged_in("32200001")
    page = c.get(f"/t/{ev.id}/").get_data(as_text=True)
    assert "Alert me 5 min before" in page and ">Register<" not in page.split('class="button-row')[1].split("</div>")[0]
    c.post(f"/t/{ev.id}/alert-me", data={})
    s = ev.load().settings
    assert "32200001" in s.open_alerts and s.open_alerts["32200001"]["texts"] is False
    assert "alert you 5 min before" in c.get(f"/t/{ev.id}/").get_data(as_text=True)
    c.post(f"/t/{ev.id}/alert-me", data={"cancel": "1"})
    assert not ev.load().settings.open_alerts


def test_alert_goes_out_five_minutes_before_and_only_once(app):
    from chips import open_alerts
    opens = (datetime.now() + timedelta(minutes=4)).replace(second=0, microsecond=0)
    ev = make(app, "Soon Night", date="2026-12-05", registration_opens=opens.strftime("%Y-%m-%dT%H:%M"))
    t = ev.load()
    open_alerts.add(t.settings, "32200001", True, "+19045550100")
    open_alerts.add(t.settings, "32200003", False, "")
    ev.store.save(t)
    sent, texts = [], []
    ev.sms.send = lambda to, body, test=False: texts.append((to, body))
    # no mailer in tests: counting what the sender handled is enough
    assert open_alerts.due(ev.load().settings, opens - timedelta(minutes=10)) == []
    assert sorted(open_alerts.due(ev.load().settings, opens - timedelta(minutes=4))) == ["32200001", "32200003"]
    assert open_alerts.due(ev.load().settings, opens + timedelta(minutes=31)) == []
    assert app.config["send_open_alerts"](ev) == 2
    assert [x[0] for x in texts] == ["+19045550100"] and "registration opens at" in texts[0][1]
    assert app.config["send_open_alerts"](ev) == 0
    assert open_alerts.due(ev.load().settings, opens) == []


def test_a_card_answers_the_players_questions(app):
    ev = make(app, "Vegas Night", date="2026-11-14", start_time="11:00 AM", team_size=2, team_skill_cap=10, entry_fee_cents=6000,
              field_cap=10, game="9", categories_set=True, is_qualifier=True, eligibility=rules(min_matches=6, list_label="Gated"),
              venue="Hotshots")
    t = ev.load()
    for n in range(9):
        t.register_entry(f"Team {n}", [Player(f"A{n}", skill=4), Player(f"B{n}", skill=4)])
    ev.store.save(t)
    page = app.test_client().get("/").get_data(as_text=True)
    card = page.split('<article class="card hc', 1)[1].split("</article>", 1)[0]
    for want in ("Sat, Nov 14 · 11:00 AM", "9-Ball · Doubles · Qualifier", "Hotshots", "$60.00 per team", "1 spot left",
                 "9 of 10 teams", 'class="hc-bar low"', "Bring a partner", "Eligibility required", "Combined SL 10 or less",
                 "Need a partner?", "View &amp; Register"):
        assert want in card, want
