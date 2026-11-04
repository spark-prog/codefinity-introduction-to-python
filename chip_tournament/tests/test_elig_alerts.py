"""Eligibility alerts: the office hears about review requests, and players hear when they're approved or removed."""

from chips.engine import Player
import tests.test_phase7 as phase7
from tests.test_register_members import Clients

app = phase7.app  # the fixture: event "singles" needing 6 lifetime matches


def director(app):
    c = app.test_client()
    c.post("/admin/login", data={"password": "secretpw1"})
    return c


def register_cy(app, phone="9045550100"):
    ev = app.config["hub"].get("singles")
    t = ev.load()
    e = t.register_entry("", [Player("Cy Day", phone, 5, member="32200003", sms_consent=True)])
    ev.store.save(t)
    return ev, e.id


def test_the_office_is_emailed_when_a_player_asks_for_a_review(app, monkeypatch):
    mail = app.config["mail"]
    Clients(app).logged_in("32200003").post("/t/singles/review-request", data={"message": "New team this session"})
    alert = mail.sent[-1]
    assert alert["to"] == "firstcoast@apaleagues.com" and alert["subject"] == "Review requested: Cy Day for Singles"
    assert "member 32200003" in alert["text"] and "New team this session" in alert["text"]
    assert "Why they're not eligible:" in alert["text"] and "lifetime" in alert["text"]
    assert "/t/singles/admin#eligibility" in alert["text"]
    monkeypatch.setenv("DIRECTOR_EMAIL", "td@example.com")
    ev = app.config["hub"].get("singles")
    t = ev.load()
    t.settings.review_requests.clear()
    ev.store.save(t)
    Clients(app).logged_in("32200003").post("/t/singles/review-request", data={"message": ""})
    assert mail.sent[-1]["to"] == "td@example.com"


def test_an_override_tells_the_player_and_closes_their_request(app):
    mail = app.config["mail"]
    register_cy(app)
    Clients(app).logged_in("32200003").post("/t/singles/review-request", data={"message": "please"})
    r = director(app).post("/t/singles/admin/overrides", data={"member": "32200003", "note": "LO approved",
                                                              "back": "/t/singles/admin/skill-check"},
                           follow_redirects=True)
    assert "They&#39;ve been emailed" in r.get_data(as_text=True)
    sent = mail.sent[-1]
    assert sent["to"] == "cy@x.com" and sent["subject"] == "Singles: you're approved"
    assert "registered and cleared to play" in sent["text"] and "Note: LO approved" in sent["text"]
    reqs = app.config["hub"].get("singles").load().settings.review_requests
    assert [x["status"] for x in reqs] == ["approved"]
    # someone not registered yet gets the Register link instead
    director(app).post("/t/singles/admin/overrides", data={"member": "32200001", "note": "ok"})
    assert "Register here:" in mail.sent[-1]["text"] and mail.sent[-1]["to"] == "ann@x.com"


def test_remove_from_the_check_page_and_tell_the_player(app):
    mail = app.config["mail"]
    ev, entry_id = register_cy(app)
    admin = director(app)
    page = admin.get("/t/singles/admin/skill-check").get_data(as_text=True)
    assert ">Remove</button>" in page and 'name="reason"' in page
    r = admin.post("/t/singles/admin/eligibility-remove", data={"id": entry_id, "reason": "Needs 6 lifetime matches."},
                   follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Removed Cy Day. Emailed 1 player. They were texted too." in html
    assert entry_id not in ev.load().entries
    sent = mail.sent[-1]
    assert sent["to"] == "cy@x.com" and sent["subject"] == "Singles: you've been removed"
    assert "removed from Singles by the league office: Needs 6 lifetime matches." in sent["text"]
    assert "/t/singles/register" in sent["text"]
    assert "removed from Singles by the league office" in (ev.dir / "texts.log").read_text()
    assert any(a["what"] == "removed (not eligible)" for a in ev.load().settings.elig_audit)


def test_no_remove_button_for_view_only_or_after_the_start(app, monkeypatch):
    ev, entry_id = register_cy(app)
    t = ev.load()
    t.status = "running"
    ev.store.save(t)
    assert ">Remove</button>" not in director(app).get("/t/singles/admin/skill-check").get_data(as_text=True)
    anon = app.test_client().post("/t/singles/admin/eligibility-remove", data={"id": entry_id})
    assert anon.status_code in (302, 401, 403) and entry_id in ev.load().entries
