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


SENT = []  # every email the app sent in a test: (to, subject, text)


class FakeMailer:
    to = "office@example.com"

    def send_to(self, to, subject, text, *a, **k):
        SENT.append((to, subject, text))


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    monkeypatch.delenv("CHIPS_PRODUCTION", raising=False)
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: FakeMailer()))
    SENT.clear()
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
    """Ann on the list, agreement published and signed, account approved by the LO, signed in."""
    admin = director(app)
    if not app.config["guests"].agreement()["version"]:
        admin.post("/admin/guests/agreement", data={"text": AGREEMENT})
    admin.post("/admin/guests/add", data={"member": member})
    c = player(app, member)
    c.post("/guest/accept", data={"agree": "1", "typed_name": "Ann Lee", "version": app.config["guests"].agreement()["version"]})
    admin.post(f"/admin/guests/{member}/approve")
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
    assert "Added Ann Lee. Publish the agreement" in page and "member 32200001" in page
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
    assert "finishing the agreement" in text(c.get("/guest"))
    page = text(admin.post("/admin/guests/agreement", data={"text": "too short"}, follow_redirects=True))
    assert "too short to publish" in page
    admin.post("/admin/guests/agreement", data={"text": AGREEMENT})
    page = text(c.get("/guest"))
    assert "Version 1" in page and "Confidentiality agreement for guest" in page and "New tournament" not in page
    # Must tick the box and type a name; a stale page (old version) is refused.
    assert "Tick the box" in text(c.post("/guest/accept", data={"typed_name": "Ann Lee", "version": "1"}, follow_redirects=True))
    assert "Type your full name" in text(c.post("/guest/accept", data={"agree": "1", "typed_name": "A", "version": "1"},
                                                follow_redirects=True))
    assert "changed while you were reading" in text(c.post("/guest/accept", data={"agree": "1", "typed_name": "Ann Lee",
                                                                                   "version": "0"}, follow_redirects=True))
    c.post("/guest/accept", data={"agree": "1", "typed_name": "Ann Lee", "version": "1"})
    assert "Waiting for the league office" in text(c.get("/guest"))
    assert "ready for your approval" in text(admin.get("/admin")) and "as “Ann Lee”" in text(admin.get("/admin"))
    admin.post("/admin/guests/32200001/approve")
    assert "New tournament" in text(c.get("/guest"))
    # A new version: everyone signs again before running anything.
    admin.post("/admin/guests/agreement", data={"text": AGREEMENT + " Updated."})
    assert "updated since you last signed" in text(c.get("/guest"))
    assert "needs to sign the new version" in text(admin.get("/admin"))


def test_players_not_on_the_list_get_nothing(app):
    ready_guest(app)
    bo = player(app, "32200002")
    assert "not set up as a guest TD" in text(bo.get("/guest"))
    assert "not on the guest TD list" in text(bo.post("/guest/new", data={"name": "X", "kind": "single"}, follow_redirects=True))
    # On the list and signed, but not approved yet: still no events.
    admin = director(app)
    admin.post("/admin/guests/add", data={"member": "32200002"})
    bo.post("/guest/accept", data={"agree": "1", "typed_name": "Bo Ray", "version": "1"})
    assert "hasn&#39;t approved your account" in text(bo.post("/guest/new", data={"name": "X", "kind": "single"},
                                                              follow_redirects=True))
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
    assert "NOT APPROVED" in text(c.get("/guest")) and "Run it" not in text(c.get("/guest"))


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
    assert "Finished" in text(c.get("/guest")) and "Run it" not in text(c.get("/guest"))
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


# ---------------------------------------------------------------- emails

def test_emails_along_the_way(app):
    admin = director(app)
    admin.post("/admin/guests/agreement", data={"text": AGREEMENT})
    page = text(admin.post("/admin/guests/add", data={"member": "32200001"}, follow_redirects=True))
    assert "emailed them the agreement to sign" in page and "Agreement emailed" in page
    to, subject, body = SENT[-1]
    assert to == "ann@example.com" and "guest tournament director agreement" in subject
    assert "http://localhost/guest" in body and AGREEMENT in body
    # Signing tells the league office.
    c = player(app, "32200001")
    c.post("/guest/accept", data={"agree": "1", "typed_name": "Ann Lee", "version": "1"})
    to, subject, body = SENT[-1]
    assert to == "firstcoast@apaleagues.com" and subject == "Guest TD agreement signed: Ann Lee"
    assert "/admin#guest-tds" in body and 'typing their name as "Ann Lee"' in body
    # Approving tells the rep.
    assert "Approved Ann Lee as a guest TD" in text(admin.post("/admin/guests/32200001/approve", follow_redirects=True))
    assert SENT[-1][0] == "ann@example.com" and "approved as a guest TD" in SENT[-1][1]
    assert "already approved" in text(admin.post("/admin/guests/32200001/approve", follow_redirects=True))
    # A new event goes to the league office; approving it tells the rep.
    ev = guest_event(app, c)
    assert SENT[-1][0] == "firstcoast@apaleagues.com" and SENT[-1][1] == "Guest TD event to approve: Div 3 Singles"
    admin.post(f"/admin/guests/event/{ev.id}/approve")
    assert SENT[-1] [0] == "ann@example.com" and SENT[-1][1] == "Div 3 Singles: approved"
    # Resending the agreement.
    admin.post("/admin/guests/agreement", data={"text": AGREEMENT + " v2"})
    assert "Emailed Ann Lee the agreement" in text(admin.post("/admin/guests/32200001/email", follow_redirects=True))
    assert "version 2" in SENT[-1][2]


def test_cannot_approve_before_signing(app):
    admin = director(app)
    admin.post("/admin/guests/agreement", data={"text": AGREEMENT})
    admin.post("/admin/guests/add", data={"member": "32200001"})
    page = text(admin.post("/admin/guests/32200001/approve", follow_redirects=True))
    assert "hasn&#39;t signed the current agreement" in page and not app.config["guests"].is_approved("32200001")


def test_guest_page_lists_events_like_the_director_page(app):
    admin, c = ready_guest(app)
    ev = guest_event(app, c)
    page = text(c.get("/guest"))
    assert "Div 3 Singles" in page and "WAITING FOR APPROVAL" in page and "Setting up" in page and "Run it" in page
    assert "Public page" not in page
    admin.post(f"/admin/guests/event/{ev.id}/approve")
    page = text(c.get("/guest"))
    assert "WAITING FOR APPROVAL" not in page and "Public page" in page


# ---------------------------------------------------------------- eligibility is the league office's call (audit #2)

def test_guest_cannot_change_who_can_play(app):
    admin, c = ready_guest(app)
    ev = guest_event(app, c)
    admin.post(f"/admin/guests/event/{ev.id}/approve")
    s = ev.load().settings
    base = {"name": s.name, "tables": ", ".join(s.tables)}
    # The league office sets Jack & Jill rules: 20 lifetime 8-Ball matches, mixed pairs, member numbers required.
    admin.post(f"/t/{ev.id}/admin/settings", data={**base, "require_member_shown": "1", "require_member": "1",
                                                   "elig_shown": "1", "elig_min_matches": "20", "elig_matches_game": "8",
                                                   "elig_gender": "mixed"})
    set_by_office = ev.load().settings
    assert set_by_office.require_member and set_by_office.eligibility["min_matches"] == 20
    rules_before = dict(set_by_office.eligibility)
    # The guest TD tries to loosen everything, and changes a field they're allowed to.
    c.post(f"/t/{ev.id}/admin/settings", data={**base, "venue": "Ace Billiards", "require_member_shown": "1",
                                               "elig_shown": "1", "elig_min_matches": "0", "elig_matches_game": "either",
                                               "elig_gender": "", "push_to": "none"})
    after = ev.load().settings
    assert after.venue == "Ace Billiards"  # their own settings still save
    assert after.require_member and after.eligibility == rules_before  # who can play didn't move
    # They see the rules, not the controls.
    page = text(c.get(f"/t/{ev.id}/admin"))
    assert "Set by the league office" in page and "At least 20 lifetime matches in 8-Ball" in page
    assert 'name="elig_min_matches"' not in page and 'name="require_member"' not in page and 'name="push_to"' not in page
    # The league office still sees and uses the controls.
    page = text(admin.get(f"/t/{ev.id}/admin"))
    assert 'name="elig_min_matches"' in page and 'name="require_member"' in page and "Set by the league office" not in page
