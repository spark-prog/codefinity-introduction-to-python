"""Add to calendar (.ics and Google) and the day-before / 2-hour reminders."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import create_app
from chips import backup, players, reminders
from chips.bracket import BracketSettings
from chips.engine import Player
from tests.test_player_login import FakeMailer
from tests.test_register_members import HEADERS, Clients, row

START = datetime(2026, 11, 7, 10, 30)


def settings(**kw):
    s = BracketSettings(name="8-Ball Singles Board", date="2026-11-07", start_time="10:30 AM", venue="Park Ave, Billiards",
                        address="123 Main St", checkin_by="10:15 AM")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_which_reminder_is_due():
    s = settings()
    assert reminders.due(s, START - timedelta(hours=30)) is None
    assert reminders.due(s, START - timedelta(hours=23)) == reminders.DAY_BEFORE
    s.reminders_sent["day"] = {"at": "x"}
    assert reminders.due(s, START - timedelta(hours=3)) is None
    assert reminders.due(s, START - timedelta(minutes=90)) == reminders.TWO_HOURS
    s.reminders_sent["2h"] = {"at": "x"}
    assert reminders.due(s, START - timedelta(minutes=30)) is None
    late = settings()  # the app was down for the day-before one: inside 2 hours only the 2-hour one goes out
    assert reminders.due(late, START - timedelta(minutes=90)) == reminders.TWO_HOURS
    assert reminders.due(settings(), START + timedelta(minutes=1)) is None  # never after the start
    assert reminders.due(settings(test_event=True), START - timedelta(hours=1)) is None
    assert reminders.due(settings(reminders=False), START - timedelta(hours=1)) is None
    assert reminders.due(settings(date=""), START - timedelta(hours=1)) is None


def test_calendar_file(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    t = SimpleNamespace(settings=settings(details="Bring your APA card."), site_url="")
    body = reminders.ics(t, "8-ball-board", "https://controltable.app/t/8-ball-board/")
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert "DTSTART;TZID=America/New_York:20261107T103000" in body and "DTEND;TZID=America/New_York:20261107T143000" in body
    assert "SUMMARY:8-Ball Singles Board" in body and "LOCATION:Park Ave\\, Billiards\\, 123 Main St" in body
    assert "TRIGGER:-PT2H" in body and "Check in by 10:15 AM." in body.replace("\r\n ", "")
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))
    monkeypatch.delenv("TZ")
    assert "DTSTART:20261107T103000" in reminders.ics(t, "x")
    assert reminders.ics(SimpleNamespace(settings=settings(start_time="")), "x") is None


def test_google_link(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    link = reminders.google_link(SimpleNamespace(settings=settings()), "https://x/t/e/")
    assert link.startswith("https://calendar.google.com/calendar/render?action=TEMPLATE")
    assert "dates=20261107T103000%2F20261107T143000" in link and "ctz=America%2FNew_York" in link


def test_reminder_email_text():
    t = SimpleNamespace(settings=settings(), site_url="https://controltable.app")
    grp = SimpleNamespace(id="e1", name="Ann Lee", players=[SimpleNamespace(name="Ann Lee")])
    subject, text = reminders.email_text(t, "board", "bracket", grp, reminders.DAY_BEFORE)
    assert subject == "Reminder: 8-Ball Singles Board is tomorrow"
    assert "When: Sat, Nov 7 at 10:30 AM (check in by 10:15 AM)" in text and "Where: Park Ave, Billiards, 123 Main St" in text
    assert "https://controltable.app/t/board/entry/e1" in text and "/t/board/calendar.ics" in text
    subject, _ = reminders.email_text(t, "board", "bracket", grp, reminders.TWO_HOURS)
    assert subject == "Reminder: 8-Ball Singles Board is today at 10:30 AM"
    assert reminders.text_body(t, reminders.TWO_HOURS) == ("Reminder: 8-Ball Singles Board is today at 10:30 AM at "
                                                          "Park Ave, Billiards. Check in by 10:15 AM.")


# ------------------------------------------------------------------ in the app

@pytest.fixture
def mail(monkeypatch):
    fake = FakeMailer()
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: fake))
    return fake


@pytest.fixture
def app(tmp_path, monkeypatch, mail):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["players"].apply(*players.parse_rows([
        HEADERS, row("32200001", first="Ann", last="Lee", email="ann@example.com"),
        row("32200002", first="Bo", last="Ray", email="bo@example.com")]), "t", "t")
    return app


def event_soon(app, hours_ahead=20, **kw):
    start = datetime.now() + timedelta(hours=hours_ahead)
    ev = app.config["hub"].create("8-Ball Singles Board", "bracket", format="single")
    t = ev.load()
    s = t.settings
    s.date, s.start_time, s.venue = start.strftime("%Y-%m-%d"), start.strftime("%I:%M %p"), "Park Ave"
    for k, v in kw.items():
        setattr(s, k, v)
    t.register_entry("Ann Lee", [Player("Ann Lee", phone="9045550100", sms_consent=True, skill=4, member="32200001")])
    bo = t.register_entry("Bo Ray", [Player("Bo Ray", phone="9045550101", sms_consent=True, skill=4, member="32200002")])
    bo.status = "waitlisted"  # on the waiting list: no reminder
    ev.store.save(t)
    return ev


def test_the_field_gets_one_day_before_reminder(app, mail):
    ev = event_soon(app)
    assert ev.load().entries[list(ev.load().entries)[1]].status == "waitlisted"
    counts = app.config["send_reminders"](ev)
    assert counts["emails"] == 1 and counts["texts"] == 0
    assert [m["to"] for m in mail.sent] == ["ann@example.com"]  # Bo is on the waiting list
    assert mail.sent[0]["subject"] == "Reminder: 8-Ball Singles Board is tomorrow" and "Hi Ann" in mail.sent[0]["text"]
    assert app.config["send_reminders"](ev) is None and len(mail.sent) == 1  # once only
    assert ev.load().settings.reminders_sent["day"]["emails"] == 1


def test_two_hour_reminder_with_texts(app, mail):
    ev = event_soon(app, hours_ahead=1.5, reminder_texts=True, sms_name="First Coast APA")
    counts = app.config["send_reminders"](ev)
    assert counts == {"at": counts["at"], "emails": 1, "texts": 1}
    assert "is today at" in mail.sent[0]["subject"]
    log = (ev.dir / "texts.log").read_text()
    assert "First Coast APA: Reminder: 8-Ball Singles Board is today at" in log and "9045550101" not in log
    assert "day" not in ev.load().settings.reminders_sent


def test_no_reminders_when_turned_off_or_a_test_event(app, mail):
    assert app.config["send_reminders"](event_soon(app, reminders=False)) is None
    assert app.config["send_reminders"](event_soon(app, test_event=True)) is None
    assert mail.sent == []


def test_calendar_links_on_the_event_page_and_my_account(app):
    ev = event_soon(app, hours_ahead=48)
    c = app.test_client()
    page = c.get(f"/t/{ev.id}/").get_data(as_text=True)
    assert "Add to calendar" in page and f"/t/{ev.id}/calendar.ics" in page and "calendar.google.com" in page
    r = c.get(f"/t/{ev.id}/calendar.ics")
    assert r.status_code == 200 and r.mimetype == "text/calendar" and b"SUMMARY:8-Ball Singles Board" in r.data
    assert "attachment" in r.headers["Content-Disposition"]
    me = Clients(app).logged_in("32200001").get("/me").get_data(as_text=True)
    assert f"/t/{ev.id}/calendar.ics" in me
    undated = app.config["hub"].create("No Date Yet", "chip")
    assert c.get(f"/t/{undated.id}/calendar.ics").status_code == 404
    assert "Add to calendar" not in c.get(f"/t/{undated.id}/").get_data(as_text=True)


def test_director_turns_reminders_on_and_off(app):
    ev = event_soon(app, hours_ahead=48)
    c = app.test_client()
    c.post("/admin/login", data={"password": "secretpw1"})
    page = c.get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert 'name="reminders" value="1" checked' in page and 'name="reminder_texts" value="1" >' in page
    s = ev.load().settings
    form = {"name": s.name, "format": s.format, "team_size": "1", "seeding": s.seeding, "final_cut": str(s.final_cut),
            "groups": "1", "advance": "0", "race_method": s.race_method, "race_chart": s.race_chart,
            "race_winners": "5", "race_losers": "4", "race_finals": "5", "entry_fee": "20", "added_money": "0",
            "field_cap": "1", "tables": ", ".join(s.tables), "call_mode": s.call_mode, "match_minutes": "30",
            "long_match_minutes": "45", "no_rack_minutes": "15", "reminders_shown": "1", "reminder_texts": "1"}
    c.post(f"/t/{ev.id}/admin/settings", data=form)
    s = ev.load().settings
    assert (s.reminders, s.reminder_texts) == (False, True)
