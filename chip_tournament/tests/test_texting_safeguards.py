"""Texting safeguards: STOP replies are honored league-wide, guest TD broadcasts are limited, and each
signature keeps an auditable record."""

import base64
import hashlib
import hmac
import io
import json
import urllib.error

from chips import alerts, guest_td
from chips.alerts import OptOuts, SmsSender, valid_twilio_signature
from tests.test_guest_td import AGREEMENT, app, director, guest_event, player, ready_guest, seed, text  # noqa: F401

TOKEN = "twilio-test-token"


def sign(url, params, token=TOKEN):
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


# ---------------------------------------------------------------- opt-outs

def test_stop_and_start_replies(tmp_path):
    outs = OptOuts(tmp_path / "o.json")
    assert outs.handle_reply("(904) 555-0111", " Stop ") == "stop" and outs.blocked("904-555-0111")
    assert outs.all()["+19045550111"]["how"] == "replied STOP"
    assert outs.handle_reply("9045550111", "Where's table 3?") == "" and outs.blocked("9045550111")
    assert outs.handle_reply("9045550111", "START") == "start" and not outs.blocked("9045550111")
    for word in ("UNSUBSCRIBE", "cancel", "quit", "end", "STOPALL"):
        outs.handle_reply("9045550122", word)
        assert outs.blocked("9045550122")
        outs.remove("9045550122")


def test_sender_skips_opted_out_numbers(tmp_path):
    outs = OptOuts(tmp_path / "o.json")
    outs.add("9045550111", "replied STOP")
    sms = SmsSender(tmp_path / "texts.log", outs)
    assert sms.send("904-555-0111", "Table 3") == "not sent: opted out (replied STOP)"
    assert sms.send("904-555-0122", "Table 3") == "logged (SMS not configured)"


def test_twilio_refusal_adds_the_number(tmp_path, monkeypatch):
    outs = OptOuts(tmp_path / "o.json")
    sms = SmsSender(tmp_path / "texts.log", outs)
    sms.sid, sms.token, sms.from_number = "AC1", "tok", "+19045550000"

    def refuse(req, timeout=10):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(json.dumps({"code": 21610}).encode()))
    monkeypatch.setattr(alerts.urllib.request, "urlopen", refuse)
    assert sms.send("9045550133", "hi") == "not sent: opted out (replied STOP)"
    assert outs.blocked("9045550133") and "Twilio reported" in outs.all()["+19045550133"]["how"]


def test_signature_check():
    url, params = "https://ct.example.com/sms/inbound", {"From": "+19045550111", "Body": "STOP"}
    assert valid_twilio_signature(TOKEN, url, params, sign(url, params))
    assert not valid_twilio_signature(TOKEN, url, params, sign(url, params, "wrong"))
    assert not valid_twilio_signature("", url, params, sign(url, params))
    assert not valid_twilio_signature(TOKEN, url, {**params, "Body": "START"}, sign(url, params))


def test_inbound_route(app, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("SITE_URL", "https://ct.example.com")
    c = app.test_client()
    params = {"From": "+19045550144", "Body": "STOP"}
    assert c.post("/sms/inbound", data=params, headers={"X-Twilio-Signature": "forged"}).status_code == 403
    assert not app.config["hub"].optouts.blocked("+19045550144")
    r = c.post("/sms/inbound", data=params, headers={"X-Twilio-Signature": sign("https://ct.example.com/sms/inbound", params)})
    assert r.status_code == 200 and b"<Response>" in r.data and app.config["hub"].optouts.blocked("+19045550144")
    page = text(director(app).get("/admin"))
    assert "Text opt-outs" in page and "•••-•••-0144" in page and "+19045550144" not in page


def test_opted_out_players_are_left_out_of_the_reach_count(app):
    admin, c = ready_guest(app)
    _, br, _ = seed(app, c)  # two entries with phones, both opted in
    assert "Goes to 2 phones" in text(c.get(f"/t/{br.id}/admin"))
    app.config["hub"].optouts.add("9045550111", "replied STOP")
    assert "Goes to 1 phone:" in text(c.get(f"/t/{br.id}/admin"))


# ---------------------------------------------------------------- guest broadcast limits

def test_guest_broadcast_limits(app):
    admin, c = ready_guest(app)
    _, br, _ = seed(app, c)
    page = text(c.post(f"/t/{br.id}/admin/broadcast", data={"body": "x" * 161}, follow_redirects=True))
    assert "160 characters or fewer" in page and guest_td.BroadcastLog(br.dir).all() == []
    for i in range(guest_td.GUEST_BROADCASTS_PER_DAY):
        c.post(f"/t/{br.id}/admin/broadcast", data={"body": f"Update {i}"})
    page = text(c.post(f"/t/{br.id}/admin/broadcast", data={"body": "One more"}, follow_redirects=True))
    assert "you&#39;ve sent them all today" in page
    assert len(guest_td.BroadcastLog(br.dir).all()) == guest_td.GUEST_BROADCASTS_PER_DAY
    # The league office isn't limited.
    admin.post(f"/t/{br.id}/admin/broadcast", data={"body": "From the office " + "y" * 200})
    assert guest_td.BroadcastLog(br.dir).all()[-1]["by"] == "League office"
    assert "Event news only" in text(c.get(f"/t/{br.id}/admin")) and "Event news only" not in text(admin.get(f"/t/{br.id}/admin"))


# ---------------------------------------------------------------- signature record

def test_signature_record(app):
    admin = director(app)
    admin.post("/admin/guests/agreement", data={"text": AGREEMENT})
    admin.post("/admin/guests/add", data={"member": "32200001"})
    c = player(app, "32200001")
    c.post("/guest/accept", data={"agree": "1", "typed_name": "Ann Lee", "version": "1"},
           headers={"User-Agent": "TestPhone/1.0", "X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
    acc = app.config["guests"].guest("32200001")["accepted"]
    assert acc["text_sha256"] == hashlib.sha256(AGREEMENT.encode()).hexdigest()
    assert acc["ip"] == "203.0.113.7" and acc["device"] == "TestPhone/1.0" and acc["member"] == "32200001"
    admin.post("/admin/guests/32200001/approve")
    page = text(admin.get("/admin"))
    assert "Signature record" in page and acc["text_sha256"][:16] in page and "203.0.113.7" in page


def test_inbound_accepts_the_onrender_address_when_site_url_is_a_custom_domain(app, monkeypatch):
    """Production: SITE_URL is the league's own domain, but Twilio is set to the onrender.com address."""
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("SITE_URL", "https://controltable.app")
    c = app.test_client()
    params = {"From": "+19045550155", "Body": "STOP"}
    onrender = {"base_url": "https://chip-tournament.onrender.com"}
    r = c.post("/sms/inbound", data=params, **onrender,
               headers={"X-Twilio-Signature": sign("https://chip-tournament.onrender.com/sms/inbound", params)})
    assert r.status_code == 200 and app.config["hub"].optouts.blocked("+19045550155")
    # The custom domain works too, and a forged request still fails.
    params2 = {"From": "+19045550166", "Body": "STOP"}
    assert c.post("/sms/inbound", data=params2, **onrender,
                  headers={"X-Twilio-Signature": sign("https://controltable.app/sms/inbound", params2)}).status_code == 200
    assert c.post("/sms/inbound", data={"From": "+19045550177", "Body": "STOP"}, **onrender,
                  headers={"X-Twilio-Signature": sign("https://chip-tournament.onrender.com/sms/inbound", params2)}).status_code == 403
