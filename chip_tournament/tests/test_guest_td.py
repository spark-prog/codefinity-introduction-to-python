"""Guest TDs: a division rep runs their own event, and nothing more.

The most important test here is test_guest_never_sees_a_full_phone_number: it opens every page of
a guest TD's events (and the CSV download) and checks no full phone number appears anywhere.
"""

import re
from datetime import datetime, timedelta

import pytest

from app import create_app
from chips import backup, guest_td, players
from chips.engine import Player
from tests.test_players import HEADERS, row

AGREEMENT = ("Confidentiality agreement for guest tournament directors. " * 6).strip()
PHONES = ["9045550111", "9045550122", "9045550133", "9045550144", "9045550155"]


class FakeMailer:
    to = "office@example.com"

    def send_to(self, *a, **k):
        pass


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    monkeypatch.delenv("CHIPS_PRODUCTION", raising=False)
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: FakeMailer()))
    app = create_app(str(tmp_path))
    app.testing = True
    rows = [HEADERS, row("32200001", first="Ann", last="Lee", email="ann@example.com"),
            row("32200002", first="Bo", last="Ray", email="bo@example.com"),
            row("32200003", first="Cy", last="Day", email="")]
    app.config["players"].apply(*players.parse_rows(rows), "test", "test")
    return app


def director(app):
    c = app.test_client()
    c.post("/admin/login", data={"password": "secretpw1"})
    return c


def player(app, member):
    c = app.test_client()
    with c.session_transaction() as s:
        s["login_member"] = member
    code = app.config["accounts"].new_code(member, datetime.now() - timedelta(minutes=2))
    c.post("/login/code", data={"code": code})
    return c


def text(resp):
    return resp.get_data(as_text=True)


def ready_guest(app, member="32200001"):
    """Ann on the list, agreement published and accepted, signed in."""
    admin = director(app)
    admin.post("/admin/guests/add", data={"member": member})
    if not app.config["guests"].agreement()["version"]:
        admin.post("/admin/guests/agreement", data={"text": AGREEMENT})
    c = player(app, member)
    c.post("/guest/accept", data={"agree": "1", "typed_name": "Ann Lee", "version": app.config["guests"].agreement()["version"]})
    return admin, c


def guest_event(app, c, name="Div 3 Singles", kind="single"):
    c.post("/guest/new", data={"name": name, "kind": kind, "date": "2026-12-05"})
    hub = app.config["hub"]
    return next(ev for ev in hub.events() if ev.load().settings.name == name)


# ---------------------------------------------------------------- the list and the agreement

def test_director_adds_and_removes_guest_tds(app):
    admin = director(app)
    assert "No guest TDs yet" in text(admin.get("/admin"))
    page = text(admin.post("/admin/guests/add", data={"member": "3220 0001"}, follow_redirects=True))
    assert "Added Ann Lee as a guest TD" in page and "member 32200001" in page
    page = text(admin.post("/admin/guests/add", data={"member": "32200003"}, follow_redirects=True))
    assert "Cy Day has no email" in page  # can't sign in without one
    page = text(admin.post("/admin/guests/add", data={"member": "32299999"}, follow_redirects=True))
    assert "isn&#39;t in the player list" in page
    # Only the league director manages the list.
    assert player(app, "32200001").post("/admin/guests/add", data={"member": "32200002"}).status_code == 302
    assert app.config["guests"].guest("32200002") is None
    admin.post("/admin/guests/32200001/remove")
    assert app.config["guests"].guest("32200001") is None


def test_agreement_must_be_published_then_accepted(app):
    admin = director(app)
    admin.post("/admin/guests/add", data={"member": "32200001"})
    c = player(app, "32200001")
    assert "finishing the confidentiality agreement" in text(c.get("/guest"))
    page = text(admin.post("/admin/guests/agreement", data={"text": "too short"}, follow_redirects=True))
    assert "too short to publish" in page
    admin.post("/admin/guests/agreement", data={"text": AGREEMENT})
    page = text(c.get("/guest"))
    assert "Version 1" in page and "Confidentiality agreement for guest" in page and "Ask for an event" not in page
    # Must tick the box and type a name; a stale page (old version) is refused.
    assert "Tick the box" in text(c.post("/guest/accept", data={"typed_name": "Ann Lee", "version": "1"}, follow_redirects=True))
    assert "Type your full name" in text(c.post("/guest/accept", data={"agree": "1", "typed_name": "A", "version": "1"},
                                                follow_redirects=True))
    assert "changed while you were reading" in text(c.post("/guest/accept", data={"agree": "1", "typed_name": "Ann Lee",
                                                                                   "version": "0"}, follow_redirects=True))
    c.post("/guest/accept", data={"agree": "1", "typed_name": "Ann Lee", "version": "1"})
    assert "Ask for an event" in text(c.get("/guest"))
    assert "Accepted v1 on" in text(admin.get("/admin")) and "as “Ann Lee”" in text(admin.get("/admin"))
    # A new version: everyone accepts again before running anything.
    admin.post("/admin/guests/agreement", data={"text": AGREEMENT + " Updated."})
    assert "updated since you last accepted" in text(c.get("/guest"))
    assert "Needs to accept the new version" in text(admin.get("/admin"))


def test_players_not_on_the_list_get_nothing(app):
    ready_guest(app)
    bo = player(app, "32200002")
    assert "not set up as a guest TD" in text(bo.get("/guest"))
    assert "not on the guest TD list" in text(bo.post("/guest/new", data={"name": "X", "kind": "single"}, follow_redirects=True))
    assert len(app.config["hub"].events()) == 0
    assert app.test_client().get("/guest").status_code == 302  # signed out: to the login page


# ---------------------------------------------------------------- asking for an event, approval

def test_event_is_hidden_until_approved(app):
    admin, c = ready_guest(app)
    ev = guest_event(app, c)
    t = ev.load()
    assert t.settings.test_event and t.settings.registration_closed
    assert "Div 3 Singles" not in text(app.test_client().get("/"))  # not on the public list
    page = text(c.get(f"/t/{ev.id}/admin"))
    assert "WAITING FOR APPROVAL" in page and "Run event" in page
    # The guest TD can't un-hide it or open registration while it waits.
    s = ev.load().settings
    c.post(f"/t/{ev.id}/admin/settings", data={"name": s.name, "tables": ", ".join(s.tables), "test_event_shown": "1"})
    assert ev.load().settings.test_event and ev.load().settings.registration_closed
    # The league director sees it waiting, and approves.
    page = text(admin.get("/admin"))
    assert "Waiting for your approval" in page and "asked by Ann Lee" in page and "WAITING FOR APPROVAL" in page
    page = text(admin.post(f"/admin/guests/event/{ev.id}/approve", follow_redirects=True))
    assert "Approved Div 3 Singles" in page
    assert not ev.load().settings.test_event and ev.load().settings.registration_closed  # the guest TD opens it
    assert "Div 3 Singles" in text(app.test_client().get("/"))
    c.post(f"/t/{ev.id}/admin/settings", data={"name": s.name, "tables": ", ".join(s.tables)})
    assert not ev.load().settings.registration_closed
    assert "already approved" in text(admin.post(f"/admin/guests/event/{ev.id}/decline", follow_redirects=True))


def test_declined_event_closes_to_the_guest(app):
    admin, c = ready_guest(app)
    ev = guest_event(app, c)
    admin.post(f"/admin/guests/event/{ev.id}/decline")
    assert ev.load().settings.test_event
    assert c.get(f"/t/{ev.id}/admin").headers["Location"].endswith("/guest")
    assert "Declined" in text(c.get("/guest"))


def test_pending_requests_are_capped(app):
    admin, c = ready_guest(app)
    for i in range(3):
        guest_event(app, c, name=f"Event {i}")
    page = text(c.post("/guest/new", data={"name": "One too many", "kind": "single"}, follow_redirects=True))
    assert "3 events waiting for approval" in page and len(app.config["hub"].events()) == 3


# ---------------------------------------------------------------- only their own event

def test_guest_can_only_run_their_own_event(app):
    admin, c = ready_guest(app)
    mine = guest_event(app, c)
    other = app.config["hub"].create("League Night", "bracket")
    assert c.get(f"/t/{mine.id}/admin").status_code == 200
    # Someone else's event: sent back to their own page, nothing changed.
    r = c.get(f"/t/{other.id}/admin")
    assert r.status_code == 302 and r.headers["Location"].endswith("/guest")
    r = c.post(f"/t/{other.id}/admin/entries/add", data={"bulk": "Mallory"})
    assert r.status_code == 302 and not other.load().entries
    # League-wide pages: none of them.
    for url in ("/admin", "/admin/players", "/admin/backups", "/admin/rules", "/admin/backup.zip"):
        r = c.get(url)
        assert r.status_code == 302 and not r.headers["Location"].endswith(url), url
    for url in ("/admin/new", f"/admin/event/{other.id}/delete", "/admin/guests/add", "/admin/rules/search"):
        assert c.post(url, data={"name": "x", "kind": "single", "confirm": "DELETE", "member": "32200001"}).status_code == 302
    assert app.config["hub"].get(other.id) is not None
    # Inside their own event, the league-only actions are refused too.
    chip = guest_event(app, c, "Chip Night", "chip")
    for ev, endpoint_url in ((mine, "admin/reset"), (mine, "admin/archives/delete"), (chip, "admin/background"),
                             (chip, "admin/import")):
        r = c.post(f"/t/{ev.id}/{endpoint_url}", follow_redirects=True)
        assert "for the league office only" in text(r), endpoint_url
    assert "for the league office only" in text(c.get(f"/t/{chip.id}/admin/import", follow_redirects=True))
    for ev in (mine, chip):
        page = text(c.get(f"/t/{ev.id}/admin"))
        for hidden in ("Edit rules", "Start over", "Background photo", "Import teams"):
            assert hidden not in page, (ev.id, hidden)
        assert "handled by the league office" in page


def test_access_ends(app, monkeypatch):
    admin, c = ready_guest(app)
    ev = guest_event(app, c)
    admin.post(f"/admin/guests/event/{ev.id}/approve")
    assert c.get(f"/t/{ev.id}/admin").status_code == 200
    # A new agreement version: no access until accepted again.
    admin.post("/admin/guests/agreement", data={"text": AGREEMENT + " v2"})
    assert c.get(f"/t/{ev.id}/admin").status_code == 302
    c.post("/guest/accept", data={"agree": "1", "typed_name": "Ann Lee", "version": "2"})
    assert c.get(f"/t/{ev.id}/admin").status_code == 200
    # Signed in too long ago.
    monkeypatch.setattr(guest_td, "FRESH_HOURS", 0)
    page = text(c.get(f"/t/{ev.id}/admin", follow_redirects=True))
    assert "sign in again" in page
    monkeypatch.setattr(guest_td, "FRESH_HOURS", 16)
    # The event finishes.
    with ev.store.change("finish") as t:
        t.status = "finished"
    assert c.get(f"/t/{ev.id}/admin").status_code == 302
    assert "Finished (access ended)" in text(c.get("/guest"))
    with ev.store.change("reopen") as t:
        t.status = "setup"
    assert c.get(f"/t/{ev.id}/admin").status_code == 200
    # Removed from the list: access ends at once.
    admin.post("/admin/guests/32200001/remove")
    assert c.get(f"/t/{ev.id}/admin").status_code == 302
    assert "not set up as a guest TD" in text(c.get("/guest"))


# ---------------------------------------------------------------- phone numbers

def seed(app, c):
    """A guest TD's bracket event and chip event, with players who have phone numbers."""
    admin = director(app)
    br = guest_event(app, c, "Div 3 Singles", "single")
    chip = guest_event(app, c, "Chip Night", "chip")
    for ev in (br, chip):
        admin.post(f"/admin/guests/event/{ev.id}/approve")
    t = br.load()
    t.register_entry("", [Player("Ann Lee", PHONES[0], 5, member="32200001", sms_consent=True)])
    t.register_entry("", [Player("Bo Ray", PHONES[1], 5, member="32200002", sms_consent=True)])
    br.store.save(t)
    t = chip.load()
    t.register_team("Sharks", [Player("Cy Day", PHONES[2], 5), Player("Di Fox", PHONES[3], 5)])
    chip.store.save(t)
    return admin, br, chip


def leaks(body: str) -> list[str]:
    """Any full phone number from PHONES, in any common format."""
    found = []
    for p in PHONES:
        for form in (p, f"{p[:3]}-{p[3:6]}-{p[6:]}", f"({p[:3]}) {p[3:6]}-{p[6:]}", f"{p[3:6]}-{p[6:]}", p[3:]):
            if form in body:
                found.append(form)
    return found


def test_guest_never_sees_a_full_phone_number(app):
    admin, c = ready_guest(app)
    _, br, chip = seed(app, c)
    # The guest TD types in a walk-up's number: it's saved, but shown masked afterward.
    r = c.post(f"/t/{br.id}/admin/entries/add", data={"bulk": f"Ed Moe, {PHONES[4][:3]}-{PHONES[4][3:6]}-{PHONES[4][6:]}"},
               follow_redirects=True)
    assert "Ed Moe" in text(r) and leaks(text(r)) == []  # not even echoed back in the "added" message
    assert any(p.phone.endswith("0155") for e in br.load().entries.values() for p in e.players)
    c.post(f"/t/{br.id}/admin/broadcast", data={"body": "Break until 8:15"})
    c.post(f"/t/{chip.id}/admin/broadcast", data={"body": "Chip count at 9"})

    pages = 0
    for ev in (br, chip):
        t = ev.load()
        ids = {"team_id": next(iter(getattr(t, "teams", {}) or {}), None), "entry_id": next(iter(getattr(t, "entries", {}) or {}), None)}
        for rule in app.url_map.iter_rules():
            if "GET" not in rule.methods or "event" not in rule.arguments:
                continue
            args = {"event": ev.id}
            for a in rule.arguments - {"event"}:
                args[a] = ids.get(a)
            if None in args.values():
                continue
            url = rule.rule
            for k, v in args.items():
                url = re.sub(rf"<(?:[^:>]+:)?{k}>", v, url)
            r = c.get(url)
            assert r.status_code < 500, url
            assert leaks(text(r)) == [], (url, leaks(text(r)))
            pages += 1
        # The director page itself, the CSV and the edit forms are what matter most.
        page = text(c.get(f"/t/{ev.id}/admin"))
        assert "•••-•••-" in text(c.get(f"/t/{ev.id}/admin/export.csv")) and leaks(page) == []
    assert pages > 20
    assert "0111" in text(c.get(f"/t/{br.id}/admin"))  # the last 4 still tell players apart
    # The league director still sees full numbers.
    assert PHONES[0] in text(admin.get(f"/t/{br.id}/admin/export.csv")).replace("-", "")


def test_guest_edit_keeps_numbers_they_cannot_see(app):
    admin, c = ready_guest(app)
    _, br, chip = seed(app, c)
    team = next(iter(chip.load().teams.values()))
    page = text(c.get(f"/t/{chip.id}/team/{team.id}/roster"))
    assert "ending in 0133" in page and leaks(page) == []
    c.post(f"/t/{chip.id}/team/{team.id}/roster", data={
        "name": "Sharks", "player_name_0": "Cy Day", "player_skill_0": "6", "player_phone_0": "",
        "player_name_1": "Di Fox", "player_skill_1": "5", "player_phone_1": ""})
    phones = [p.phone for p in chip.load().teams[team.id].players]
    assert phones == [PHONES[2], PHONES[3]] and chip.load().teams[team.id].players[0].skill == 6


def test_broadcasts_are_logged_with_who_sent_them(app):
    admin, c = ready_guest(app)
    _, br, _ = seed(app, c)
    c.post(f"/t/{br.id}/admin/broadcast", data={"body": "Break until 8:15"})
    admin.post(f"/t/{br.id}/admin/broadcast", data={"body": "Thanks for coming"})
    log = guest_td.BroadcastLog(br.dir).all()
    assert [(x["by"], x["body"]) for x in log] == [("Ann Lee (guest TD)", "Break until 8:15"),
                                                   ("League office", "Thanks for coming")]
    page = text(admin.get(f"/t/{br.id}/admin"))
    assert "Texts sent to everyone (2)" in page and "Ann Lee (guest TD)" in page


def test_mask_phone():
    assert guest_td.mask_phone("904-555-1234") == "•••-•••-1234"
    assert guest_td.mask_phone("") == ""
