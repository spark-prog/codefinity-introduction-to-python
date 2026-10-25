"""League hub: calendar, host locations, request board, sign-ups and player/team match-ups."""

from datetime import date

import pytest

from app import create_app
from chips.league import League, LeagueError, month_grid, same_contact

TODAY = date(2026, 11, 2)  # a Monday


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("VIEWER_PASSWORD", "lookonly")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["league"]._today = TODAY
    return app


@pytest.fixture
def league(app) -> League:
    return app.config["league"]


@pytest.fixture
def office(app):
    c = app.test_client()
    c.post("/admin/login", data={"password": "secret"})
    return c


def add_location(office, name="Corner Pocket", nights="Tue 7:00 PM 8-Ball Open · Division 1"):
    office.post("/league/office/location", data={"name": name, "area": "Eastside", "address": "1420 Main St",
                                                 "nights": nights})
    s = office.application.config["league"].load()
    return next(l for l in s["locations"] if l["name"] == name)


def sign_in(app, role, **form):
    c = app.test_client()
    page = c.post("/league/signin", data={"role": role, **form})
    assert page.status_code == 302, page.data
    return c


def test_league_nights_repeat_within_the_season(league):
    league.save_location(None, {"name": "Corner Pocket"}, League.parse_nights("Tue 7pm 8-Ball\nthursday 7:30 p.m. 9-Ball"))
    s = league.load()
    nights = league.league_nights(s, "2026-11-01", "2026-11-30")
    assert [n["date"] for n in nights if n["title"] == "8-Ball"] == ["2026-11-03", "2026-11-10", "2026-11-17", "2026-11-24"]
    assert {n["time"] for n in nights} == {"7 PM", "7:30 PM"}
    league.save_settings("Fall", "2026-11-05", "2026-11-19")
    s = league.load()
    assert sorted(n["date"] for n in league.league_nights(s, "2026-11-01", "2026-11-30")) == [
        "2026-11-05", "2026-11-10", "2026-11-12", "2026-11-17", "2026-11-19"]
    with pytest.raises(LeagueError):
        League.parse_nights("Every Tuesday")
    with pytest.raises(LeagueError):
        league.save_settings("Fall", "2026-12-01", "2026-11-01")


def test_month_grid_and_contacts():
    weeks = month_grid("2026-11")
    assert weeks[0][0] == "2026-11-01" and weeks[-1][-1] == "2026-12-05" and len(weeks) == 5
    assert month_grid("2026-08")[0][0] == "2026-07-26"
    assert same_contact("(555) 201-4410", "555.201.4410") and same_contact("A@B.com", "a@b.com ")
    assert not same_contact("", "") and not same_contact("a@b.com", "c@b.com")


def test_office_pages_need_the_director(app, office):
    client = app.test_client()
    for path in ("/league/office", "/league/office/requests", "/league/office/signups", "/league/office/setup"):
        assert client.get(path).status_code == 302
        assert office.get(path).status_code == 200
    assert client.post("/league/office/rep", data={"name": "Dana"}).status_code == 302
    assert not app.config["league"].load()["reps"]
    viewer = app.test_client()
    viewer.post("/admin/login", data={"password": "lookonly"})
    assert viewer.get("/league/office/setup").status_code == 200
    viewer.post("/league/office/rep", data={"name": "Dana"})
    assert not app.config["league"].load()["reps"]


def test_host_blackout_request_rep_claim_and_office_schedule(app, office, league):
    loc = add_location(office)
    assert len(loc["pin"]) == 6 and loc["nights"][0]["dow"] == 2
    office.post("/league/office/rep", data={"name": "Dana M.", "division": "Division 2"})
    rep = league.load()["reps"][0]

    # wrong PIN, then the host signs in
    page = app.test_client().post("/league/signin", data={"role": "host", "who": loc["id"], "pin": "000000"},
                                  follow_redirects=True)
    assert b"doesn&#39;t match" in page.data
    host = sign_in(app, "host", who=loc["id"], pin=loc["pin"])
    page = host.get("/league/host").data.decode()
    assert "Corner Pocket" in page and "8-Ball Open" in page and "Tue, Nov 3" not in page  # agenda shows the day, not repeated

    # a blackout on a league night is flagged for the office
    page = host.post("/league/host/blackout", data={"start": "2026-11-10", "reason": "Closed for a private party"},
                     follow_redirects=True).data.decode()
    assert "1 scheduled event on those days" in page
    assert host.post("/league/host/blackout", data={"start": "2026-10-01", "reason": "x"}, follow_redirects=True) \
        .data.decode().count("can&#39;t be in the past") == 1
    page = office.get("/league/office?month=2026-11").data.decode()
    assert "Blackout conflicts" in page and "Closed for a private party" in page

    # the host can't request a date it blacked out; a good date goes on the board
    page = host.post("/league/host/request", data={"type": "singles_board", "date": "2026-11-10"},
                     follow_redirects=True).data.decode()
    assert "You have Tue, Nov 10 blacked out" in page
    host.post("/league/host/request", data={"type": "singles_board", "date": "2026-11-21", "alt": "2026-11-28",
                                            "notes": "$50 added"})
    req = league.load()["requests"][0]
    assert req["status"] == "open"

    # the office can't schedule an open request; a rep claims it
    office.post("/league/office/event", data={"type": "singles_board", "loc": loc["id"], "title": "Board",
                                              "date": "2026-11-21", "request": req["id"]})
    assert not league.load()["events"]
    rep_client = sign_in(app, "rep", who=rep["id"], pin=rep["pin"])
    assert "Claim this event" in rep_client.get("/league/rep").data.decode()
    rep_client.post(f"/league/rep/claim/{req['id']}")
    assert league.load()["requests"][0]["status"] == "claimed"
    with pytest.raises(LeagueError):
        league.claim(req["id"], "someone-else")

    # the office's "Add to calendar" form is filled in from the request
    page = office.get(f"/league/office?schedule={req['id']}").data.decode()
    assert "Corner Pocket Singles board" in page and "Dana M." in page and "$50 added" in page
    office.post("/league/office/event", data={"type": "singles_board", "loc": loc["id"], "title": "Sunday Board",
                                              "date": "2026-11-28", "time": "2:00 PM", "director": "Dana M.",
                                              "request": req["id"]})
    s = league.load()
    assert s["requests"][0]["status"] == "scheduled" and s["requests"][0]["date"] == "2026-11-28"
    event = s["events"][0]
    assert event["request"] == req["id"]
    assert "Sunday Board" in host.get("/league/host?days=60").data.decode()
    assert "Sunday Board" in app.test_client().get(f"/league/loc/{loc['id']}").data.decode()

    # deleting the event puts the request back with its rep
    office.post(f"/league/office/event/{event['id']}/delete")
    assert league.load()["requests"][0]["status"] == "claimed"


def test_office_events_respect_blackouts_and_can_become_tournaments(app, office, league):
    loc = add_location(office)
    league.add_blackout(loc["id"], "2026-11-14", "2026-11-15", "Re-felting")
    page = office.post("/league/office/event", data={"type": "singles_tourney", "loc": loc["id"], "title": "9-Ball Open",
                                                     "date": "2026-11-14"}, follow_redirects=True).data.decode()
    assert "unavailable Sat, Nov 14 (Re-felting)" in page and not league.load()["events"]
    office.post("/league/office/event", data={"type": "general", "loc": loc["id"], "title": "Captains' meeting",
                                              "date": "2026-11-14", "time": "4:00 PM"})
    assert league.load()["events"][0]["loc"] == "all"  # league-wide dates aren't held to one location's blackouts
    office.post("/league/office/event", data={"type": "singles_tourney", "loc": loc["id"], "title": "9-Ball Open",
                                              "date": "2026-11-21", "time": "1:00 pm"})
    event = next(e for e in league.load()["events"] if e["title"] == "9-Ball Open")

    page = office.post(f"/league/office/event/{event['id']}/tournament", data={"kind": "double"})
    hub = app.config["hub"]
    [ev] = hub.events()
    t = ev.load()
    assert page.headers["Location"].endswith(f"/t/{ev.id}/admin#settings")
    assert (t.settings.name, t.settings.date, t.settings.start_time, t.settings.venue, t.settings.format) == (
        "9-Ball Open", "2026-11-21", "1:00 PM", "Corner Pocket", "double")
    assert league.load()["events"][1]["tournament"] == ev.id
    assert f"/t/{ev.id}/" in app.test_client().get("/league/").data.decode()
    assert "Run it in Control Table" in office.get("/league/office?month=2026-11&day=2026-11-21").data.decode()


def test_player_sign_up_goes_privately_to_the_office(app, office, league):
    loc = add_location(office)
    client = app.test_client()
    page = client.post(f"/league/loc/{loc['id']}/signup", data={"name": "Devon"}, follow_redirects=True).data.decode()
    assert "Give your name and a phone or email" in page
    client.post(f"/league/loc/{loc['id']}/signup", data={"name": "Devon Carter", "contact": "devon@example.com",
                                                         "formats": ["8-Ball"], "nights": ["Wed", "Funday"]})
    [su] = league.load()["signups"]
    assert su["nights"] == ["Wed"] and su["status"] == "new"
    assert "devon@example.com" not in client.get(f"/league/loc/{loc['id']}").data.decode()
    page = office.get("/league/office/signups").data.decode()
    assert "Devon Carter wants to play out of Corner Pocket" in page and "devon@example.com" in page
    assert "Sign-ups (1)" in page
    office.post(f"/league/office/signup/{su['id']}/connected")
    assert league.load()["signups"][0]["status"] == "connected"


def test_captain_invites_player_and_contact_stays_private(app, office, league):
    cp = add_location(office)
    eb = add_location(office, "Eight Ball Saloon", "Wed 7:00 PM 8-Ball Masters")
    client = app.test_client()

    # a player posts a listing and gets a PIN
    page = client.post("/league/players/new", data={
        "first": "Jordan", "last": "Rivera", "contact": "(555) 301-2211", "level": "Intermediate",
        "formats": ["8-Ball"], "nights": ["Wed"], "locs": [eb["id"]], "visible": "1", "about": "Back after a break",
    }, follow_redirects=True).data.decode()
    player = league.load()["players"][0]
    assert player["last"] == "Riv" and f"Your PIN is {player['pin']}" in page
    assert "already a listing" in app.test_client().post("/league/players/new", data={
        "first": "Jo", "contact": "555-301-2211"}, follow_redirects=True).data.decode()

    # a captain posts a team; players with a better fit come first
    league.save_player(None, {"first": "Chris", "contact": "chris@example.com", "formats": ["9-Ball"], "nights": ["Mon"],
                              "visible": True})
    captain = app.test_client()
    captain.post("/league/teams/new", data={"name": "Rack City", "loc": eb["id"], "night": "Wed", "format": "8-Ball",
                                            "spots": "1", "captain": "Tasha L.", "contact": "tasha@example.com"})
    team = league.load()["teams"][0]
    page = captain.get("/league/captain").data.decode()
    assert page.index("<h3>Jordan") < page.index("<h3>Chris") and "Great fit" in page
    assert "301-2211" not in page and "chris@example.com" not in page  # captains never see player contacts
    assert "<h3>Chris" not in captain.get("/league/captain?night=Wed").data.decode()

    captain.post(f"/league/captain/invite/{player['id']}", data={"message": "Come play Wednesdays!"})
    page = captain.post(f"/league/captain/invite/{player['id']}", data={"message": "Again"},
                        follow_redirects=True).data.decode()
    assert "already sent Jordan" in page and "Not opened yet" in page

    # the player sees the invite with the captain's contact, after signing in again with contact + PIN
    player_client = sign_in(app, "player", contact="555 301 2211", pin=player["pin"])
    page = player_client.get("/league/me").data.decode()
    assert "Rack City invited you to join" in page and "tasha@example.com" in page and "Come play Wednesdays!" in page
    inv = league.load()["invites"][0]
    player_client.post(f"/league/me/invite/{inv['id']}", data={"status": "declined"})
    assert league.load()["invites"][0]["status"] == "declined"
    assert "Player passed" in captain.get("/league/captain").data.decode()

    # a hidden listing drops out of the captain's list; an edit keeps the contact on file
    player_client.post("/league/me", data={"first": "Jordan", "last": "R", "level": "Advanced", "contact": ""})
    p = league.load()["players"][0]
    assert not p["visible"] and p["contact"] == "(555) 301-2211" and p["level"] == "Advanced"
    assert "<h3>Jordan" not in captain.get("/league/captain").data.decode()

    # the public pages show the team's open spot, never the captain's contact
    page = app.test_client().get(f"/league/loc/{eb['id']}").data.decode()
    assert "Rack City" in page and "tasha@example.com" not in page
    assert cp["id"] != eb["id"]

    # a new PIN from the office signs the captain out
    office.post(f"/league/office/pin/teams/{team['id']}")
    assert captain.get("/league/captain").status_code == 302
    assert sign_in(app, "captain", contact="TASHA@example.com", pin=league.load()["teams"][0]["pin"])


def test_deleting_a_location_or_rep_cleans_up(app, office, league):
    loc = add_location(office)
    rep = league.add_rep("Lee K.", "Division 5")
    req = league.add_request(loc["id"], "singles_tourney", "2026-12-05", "", "")
    league.claim(req["id"], rep["id"])
    league.delete_rep(rep["id"])
    assert league.load()["requests"][0]["status"] == "open"
    team = league.save_team(None, {"name": "Sharks", "loc": loc["id"], "night": "Tue", "format": "8-Ball", "spots": "2",
                                   "captain": "Ray", "contact": "ray@example.com"})
    p = league.save_player(None, {"first": "Sam", "contact": "sam@example.com", "locs": [loc["id"]], "visible": True})
    league.invite(team["id"], p["id"], "Hi")
    host = sign_in(app, "host", who=loc["id"], pin=loc["pin"])
    office.post(f"/league/office/location/{loc['id']}/delete", data={"confirm": "nope"})
    assert league.load()["locations"]
    office.post(f"/league/office/location/{loc['id']}/delete", data={"confirm": "DELETE"})
    s = league.load()
    assert not s["locations"] and not s["requests"] and not s["teams"] and not s["invites"]
    assert s["players"][0]["locs"] == []
    assert host.get("/league/host").status_code == 302  # signed out: the location is gone


def test_every_league_page_renders(app, office, league):
    loc = add_location(office)
    league.save_location(loc["id"], {"hours": "Mon–Thu: 3 pm – 1 am", "specials": "Tue: $3 domestics", "menu": "Wings",
                                     "rates": "$1.25 per game"})
    rep = league.add_rep("Dana M.", "")
    league.add_request(loc["id"], "doubles_tourney", "2026-11-22", "", "Turkey shoot")
    league.add_blackout(loc["id"], "2026-11-26", "2026-11-27", "Thanksgiving")
    client = app.test_client()
    for path in ("/league/", f"/league/loc/{loc['id']}", "/league/players/new", "/league/teams/new", "/league/signin",
                 "/"):
        page = client.get(path)
        assert page.status_code == 200, path
    page = client.get(f"/league/loc/{loc['id']}").data.decode()
    assert "$3 domestics" in page and "Tuesdays, 7:00 PM" in page
    for path in ("/league/office", "/league/office?month=2026-12&loc=" + loc["id"], "/league/office?month=bad",
                 "/league/office?day=2026-11-26", "/league/office/requests", "/league/office/signups",
                 f"/league/office/setup?edit={loc['id']}"):
        assert office.get(path).status_code == 200, path
    assert "Thanksgiving" in office.get("/league/office?day=2026-11-26").data.decode()
    assert sign_in(app, "rep", who=rep["id"], pin=rep["pin"]).get("/league/rep").status_code == 200
    host = sign_in(app, "host", who=loc["id"], pin=loc["pin"])
    page = host.get("/league/host?days=60").data.decode()
    assert "You're unavailable" in page and "Your request: Doubles tournament" in page
    host.post("/league/host/page", data={"name": "Corner Pocket Pub", "specials": "Thu: Half-price wings"})
    loc = league.load()["locations"][0]
    assert loc["name"] == "Corner Pocket Pub" and loc["menu"] == "Wings" and loc["nights"]  # host can't touch nights
