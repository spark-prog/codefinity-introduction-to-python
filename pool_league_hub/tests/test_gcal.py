"""Two-way Google Calendar sync (against a fake Google Calendar), calendar feeds, and the reps' calendar."""

import copy
import itertools

import pytest

from app import create_app
from league.gcal import NotFound, SyncTokenExpired

from test_league import TODAY, add_location, sign_in


class FakeGoogle:
    """Behaves like the Google Calendar events API for what the sync uses."""

    def __init__(self):
        self.items, self.log, self.seq = {}, [], itertools.count(1)
        self.calls = []
        self.fail = None
        self.expired = False

    def _save(self, item):
        n = next(self.seq)
        item["etag"] = f'"{n}"'
        self.items[item["id"]] = item
        self.log.append((n, item["id"]))
        return copy.deepcopy(item)

    def list_changes(self, token):
        if self.fail:
            raise self.fail
        if token and self.expired:
            self.expired = False
            raise SyncTokenExpired()
        since = int(token or 0)
        ids = dict.fromkeys(i for n, i in self.log if n > since)
        last = self.log[-1][0] if self.log else 0
        return [copy.deepcopy(self.items[i]) for i in ids], str(last)

    def insert(self, body):
        self.calls.append("insert")
        return self._save({**copy.deepcopy(body), "id": f"g{len(self.items) + 1}", "status": "confirmed"})

    def update(self, gid, body):
        self.calls.append("update")
        if gid not in self.items or self.items[gid]["status"] == "cancelled":
            raise NotFound()
        return self._save({**copy.deepcopy(body), "id": gid, "status": "confirmed"})

    def delete(self, gid):
        self.calls.append("delete")
        if gid in self.items and self.items[gid]["status"] != "cancelled":
            self._save({"id": gid, "status": "cancelled"})

    # what someone does in Google Calendar
    def edit(self, gid, **changes):
        item = copy.deepcopy(self.items[gid])
        item.update(changes)
        self._save(item)

    def add(self, **fields):
        return self._save({"id": f"g{len(self.items) + 1}", "status": "confirmed", **fields})

    def live(self, kind=None):
        return [i for i in self.items.values() if i["status"] != "cancelled"
                and (kind is None or i.get("extendedProperties", {}).get("private", {}).get("hub") == kind)]


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
def app(tmp_path, monkeypatch, google):
    monkeypatch.setenv("OFFICE_PASSWORD", "secret")
    app = create_app(str(tmp_path), gcal_client=google)
    app.testing = True
    app.config["league"]._today = TODAY
    return app


@pytest.fixture
def league(app):
    return app.config["league"]


@pytest.fixture
def sync(app):
    return app.config["sync"]


@pytest.fixture
def office(app):
    c = app.test_client()
    c.post("/office/login", data={"password": "secret"})
    return c


def event(league, **f):
    return league.add_event({"type": "singles_tourney", "time": "1:00 PM", **f})


def test_hub_sends_events_nights_and_blackouts(app, office, league, sync, google):
    league.save_settings("Fall", "2026-09-01", "2026-12-15")
    loc = add_location(office)
    ev = event(league, loc=loc["id"], title="9-Ball Open", date="2026-11-21", director="Marcus T.", notes="$100 added")
    league.add_event({"type": "general", "title": "Sign-ups due", "date": "2026-11-30", "time": "All day"})
    league.add_blackout(loc["id"], "2026-11-26", "2026-11-27", "Thanksgiving")

    report = sync.run()
    assert report["error"] == "" and report["sent"] == 4
    [tourney] = [i for i in google.live("event") if i["summary"] == "9-Ball Open"]
    assert tourney["start"] == {"dateTime": "2026-11-21T13:00:00", "timeZone": "America/New_York", "date": None}
    assert tourney["end"]["dateTime"] == "2026-11-21T16:00:00"
    assert tourney["location"] == "Corner Pocket, 1420 Main St"
    assert "$100 added" in tourney["description"] and "Run by Marcus T." in tourney["description"]
    assert tourney["extendedProperties"]["private"] == {"hub": "event", "hub_id": ev["id"]}
    [due] = [i for i in google.live("event") if i["summary"] == "Sign-ups due"]
    assert due["start"]["date"] == "2026-11-30" and not due["start"]["dateTime"] and due["location"] == ""
    [night] = google.live("night")
    assert night["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261215T235959Z"]
    assert night["start"]["dateTime"] == "2026-09-01T19:00:00"  # the first Tuesday of the season
    assert night["summary"] == "8-Ball Open · Division 1 · Corner Pocket"
    [closed] = google.live("blackout")
    assert (closed["start"]["date"], closed["end"]["date"]) == ("2026-11-26", "2026-11-28")

    s = league.load()
    assert all(r.get("google_id") and not r.get("sync") for r in s["events"] + s["blackouts"])
    assert s["locations"][0]["nights"][0]["google_id"] and not s["locations"][0].get("sync")
    # nothing changed: the next sync sees its own writes come back and sends nothing
    assert sync.run() == {"pulled": 0, "sent": 0, "deleted": 0, "error": ""}


def test_changes_in_google_come_into_the_hub(app, office, league, sync, google):
    loc = add_location(office, "Corner Pocket Pub")
    rep = league.add_rep("Dana M.", "")
    req = league.add_request(loc["id"], "singles_board", "2026-11-14", "", "")
    league.claim(req["id"], rep["id"])
    board = league.add_event({"type": "singles_board", "loc": loc["id"], "title": "Board", "date": "2026-11-14",
                              "time": "2:00 PM", "request": req["id"]})
    ev = event(league, loc=loc["id"], title="9-Ball Open", date="2026-11-21", notes="Bring cash")
    sync.run()
    s = league.load()
    gid = next(e["google_id"] for e in s["events"] if e["id"] == ev["id"])

    # the office moves the tournament in Google: new day, time and title come back
    google.edit(gid, summary="9-Ball Open (moved)", start={"dateTime": "2026-11-22T12:30:00-05:00"})
    assert sync.run()["pulled"] == 1
    e = next(e for e in league.load()["events"] if e["id"] == ev["id"])
    assert (e["title"], e["date"], e["time"], e["notes"]) == ("9-Ball Open (moved)", "2026-11-22", "12:30 PM", "Bring cash")
    assert sync.run() == {"pulled": 0, "sent": 0, "deleted": 0, "error": ""}

    # an event added in Google that names a host location lands there
    google.add(summary="Corner Pocket Pub 8-Ball Classic", location="1420 Main St",
               start={"dateTime": "2026-12-05T18:00:00-05:00"}, description="Added in Google")
    google.add(summary="League banquet", start={"date": "2026-12-12"})
    google.add(summary="Last spring's banquet", start={"date": "2026-05-01"})  # history: not imported
    sync.run()
    s = league.load()
    classic = next(e for e in s["events"] if e["title"] == "Corner Pocket Pub 8-Ball Classic")
    banquet = next(e for e in s["events"] if e["title"] == "League banquet")
    assert (classic["type"], classic["loc"], classic["time"], classic["notes"]) == ("calendar", loc["id"], "6:00 PM", "Added in Google")
    assert (banquet["loc"], banquet["time"]) == ("all", "All day")
    assert not any(e["title"] == "Last spring's banquet" for e in s["events"])
    tagged = google.items[classic["google_id"]]["extendedProperties"]["private"]
    assert tagged == {"hub": "event", "hub_id": classic["id"]}  # sent back once, tagged
    assert sync.run()["sent"] == 0

    # deleted in Google: gone from the hub; the request it came from goes back to its rep
    google.delete(next(e["google_id"] for e in league.load()["events"] if e["id"] == board["id"]))
    sync.run()
    s = league.load()
    assert board["id"] not in {e["id"] for e in s["events"]}
    assert s["requests"][0]["status"] == "claimed" and s["requests"][0]["rep"] == rep["id"]
    assert "Corner Pocket Pub 8-Ball Classic" in office.get("/office?month=2026-12&day=2026-12-05").data.decode()


def test_the_hub_wins_when_both_change_and_owns_nights_and_blackouts(app, office, league, sync, google):
    loc = add_location(office)
    ev = event(league, loc=loc["id"], title="9-Ball Open", date="2026-11-21")
    bk_hits = league.add_blackout(loc["id"], "2026-11-26", "2026-11-26", "Thanksgiving")
    assert bk_hits == 0
    sync.run()
    s = league.load()
    gid = s["events"][0]["google_id"]
    night_gid = s["locations"][0]["nights"][0]["google_id"]
    blackout_gid = s["blackouts"][0]["google_id"]

    # changed in both places before the next sync: the hub's version goes to Google
    google.edit(gid, summary="Changed in Google")
    league.edit_event(ev["id"], {**ev, "title": "Changed in the hub"})
    sync.run()
    assert league.load()["events"][0]["title"] == "Changed in the hub"
    assert google.items[gid]["summary"] == "Changed in the hub"

    # a league night edited in Google is put back; a blackout deleted there is made again
    google.edit(night_gid, summary="Someone renamed it")
    google.delete(blackout_gid)
    sync.run()
    assert google.items[night_gid]["summary"] == "8-Ball Open · Division 1 · Corner Pocket"
    [closed] = google.live("blackout")
    assert closed["id"] != blackout_gid and league.load()["blackouts"][0]["google_id"] == closed["id"]
    assert sync.run() == {"pulled": 0, "sent": 0, "deleted": 0, "error": ""}

    # one week of a league night moved in Google (a holiday) is left alone
    google.add(summary="8-Ball Open (moved for the holiday)", recurringEventId=night_gid,
               start={"dateTime": "2026-11-25T19:00:00-05:00"},
               extendedProperties={"private": {"hub": "night", "hub_id": f"{loc['id']}:0"}})
    assert sync.run() == {"pulled": 0, "sent": 0, "deleted": 0, "error": ""}


def test_deleting_in_the_hub_removes_from_google(app, office, league, sync, google):
    loc = add_location(office)
    ev = event(league, loc=loc["id"], title="9-Ball Open", date="2026-11-21")
    league.add_blackout(loc["id"], "2026-11-26", "2026-11-26", "Thanksgiving")
    sync.run()
    old_night = league.load()["locations"][0]["nights"][0]["google_id"]

    office.post(f"/office/event/{ev['id']}/delete")
    office.post(f"/office/location/{loc['id']}", data={"name": "Corner Pocket", "address": "1420 Main St",
                                                       "nights": "Wed 7:30 PM 9-Ball · Division 3"})
    report = sync.run()
    assert report["deleted"] == 2 and not google.live("event")
    [night] = google.live("night")
    assert night["id"] != old_night and night["summary"] == "9-Ball · Division 3 · Corner Pocket"
    assert google.items[old_night]["status"] == "cancelled"

    # saving a location without changing its nights doesn't remake them in Google
    calls = len(google.calls)
    office.post(f"/office/location/{loc['id']}", data={"name": "Corner Pocket", "address": "1420 Main St",
                                                       "nights": "Wed 7:30 PM 9-Ball · Division 3"})
    sync.run()
    assert google.calls[calls:] == []

    # renaming a location updates its nights, events and blackouts in Google
    league.save_location(loc["id"], {"name": "Corner Pocket Pub"})
    sync.run()
    assert google.live("night")[0]["summary"].endswith("· Corner Pocket Pub")
    assert google.live("blackout")[0]["summary"] == "Corner Pocket Pub closed: Thanksgiving"

    # the whole location: its nights and blackout go too
    office.post(f"/office/location/{loc['id']}/delete", data={"confirm": "DELETE"})
    sync.run()
    assert google.live() == []


def test_sync_problems_show_and_recover(app, office, league, sync, google):
    loc = add_location(office)
    event(league, loc=loc["id"], title="9-Ball Open", date="2026-11-21")
    google.fail = RuntimeError("calendar not shared with the service account")
    page = office.post("/office/sync", follow_redirects=True).data.decode()
    assert "Google Calendar sync failed" in page and "not shared with the service account" in page
    assert "2 changes" in office.get("/office/setup").data.decode()  # the event and the league nights, still to send
    google.fail = None
    page = office.post("/office/sync", follow_redirects=True).data.decode()
    assert "Synced with Google Calendar" in page and not league.load()["gcal"]["error"]
    google.expired = True  # Google forgot the sync token: list everything again
    assert sync.run()["error"] == ""
    # changes in the hub wake the background sync
    sync.wake.clear()
    league.add_blackout(loc["id"], "2026-11-26", "2026-11-26", "Thanksgiving")
    assert sync.wake.is_set()


def test_office_edits_events(app, office, league):
    loc = add_location(office)
    ev = event(league, loc=loc["id"], title="9-Ball Open", date="2026-11-21")
    page = office.get(f"/office?month=2026-11&day=2026-11-21&edit={ev['id']}").data.decode()
    assert "Edit: 9-Ball Open" in page and "Save changes" in page
    office.post(f"/office/event/{ev['id']}", data={"type": "singles_tourney", "loc": loc["id"], "title": "9-Ball Open",
                                                   "date": "2026-11-28", "time": "2:00 PM", "notes": "Moved a week"})
    e = league.load()["events"][0]
    assert (e["date"], e["time"], e["notes"]) == ("2026-11-28", "2:00 PM", "Moved a week")
    league.add_blackout(loc["id"], "2026-12-05", "2026-12-05", "Private party")
    page = office.post(f"/office/event/{ev['id']}", data={"type": "singles_tourney", "loc": loc["id"],
                                                          "title": "9-Ball Open", "date": "2026-12-05"},
                       follow_redirects=True).data.decode()
    assert "unavailable Sat, Dec 5" in page and league.load()["events"][0]["date"] == "2026-11-28"


def test_feeds_for_hosts_and_the_league(app, office, league):
    league.save_settings("Fall", "2026-09-01", "2026-12-15")
    cp = add_location(office)
    eb = add_location(office, "Eight Ball Saloon", "Wed 7:00 PM 8-Ball Masters")
    event(league, loc=cp["id"], title="9-Ball Open, Fall", date="2026-11-21")
    event(league, loc=eb["id"], title="Eight Ball Doubles", date="2026-11-22")
    league.add_event({"type": "general", "title": "Captains' meeting", "date": "2026-11-08", "time": "4:00 PM"})
    league.add_blackout(cp["id"], "2026-11-26", "2026-11-27", "Thanksgiving")

    host = sign_in(app, "host", who=cp["id"], pin=cp["pin"])
    page = host.get("/host").data.decode()
    url = page.split('<p class="contact">')[1].split("</p>")[0]
    assert "/feed/" in url
    cal = app.test_client().get(url.replace("http://localhost", ""))
    assert cal.status_code == 200 and cal.mimetype == "text/calendar"
    text = cal.data.decode()
    assert "SUMMARY:9-Ball Open\\, Fall" in text and "Captains' meeting" in text and "Eight Ball" not in text
    assert "DTSTART;TZID=America/New_York:20261121T130000" in text
    assert "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261215T235959Z" in text
    assert "DTSTART;VALUE=DATE:20261126" in text and "DTEND;VALUE=DATE:20261128" in text
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))

    league_feed = office.get("/office").data.decode().split('<p class="contact">')[1].split("</p>")[0]
    text = app.test_client().get(league_feed.replace("http://localhost", "")).data.decode()
    assert "Eight Ball Doubles" in text and "9-Ball Open" in text and "8-Ball Masters · Eight Ball Saloon" in text
    assert app.test_client().get("/feed/not-a-real-link.ics").status_code == 404


def test_reps_see_the_whole_calendar_but_cannot_change_it(app, office, league):
    loc = add_location(office)
    event(league, loc=loc["id"], title="9-Ball Open", date="2026-11-21")
    rep = league.add_rep("Dana M.", "Division 2")
    assert app.test_client().get("/rep/calendar").status_code == 302
    client = sign_in(app, "rep", who=rep["id"], pin=rep["pin"])
    page = client.get("/rep/calendar?month=2026-11&day=2026-11-21").data.decode()
    assert "Master calendar" in page and "Division rep · Dana M." in page and "9-Ball Open" in page
    assert "Save to calendar" not in page and "Delete" not in page and "/rep/calendar?month=2026-12" in page
    assert ">Calendar</a>" in client.get("/rep").data.decode()
    assert client.post("/office/event", data={"type": "general", "title": "x", "date": "2026-11-30"}).status_code == 302
    assert len(league.load()["events"]) == 1
