"""Player login by emailed code, 90 days on a device."""

import re
from datetime import datetime, timedelta

import pytest

from app import create_app
from chips import accounts as acc, backup, players
from tests.test_players import HEADERS, row


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send_to(self, to, subject, text, attachment=None, sender="", reply_to=""):
        self.sent.append({"to": to, "subject": subject, "text": text, "sender": sender, "reply_to": reply_to})

    def code(self):
        return re.search(r"\b(\d{6})\b", self.sent[-1]["subject"]).group(1)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    mail = FakeMailer()
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: mail))
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["mail"] = mail
    rows = [HEADERS, row("32200001", first="Ann", last="Lee", email="ann@example.com", mp8="40"),
            row("32200002", first="Bo", last="Ray", email="")]
    app.config["players"].apply(*players.parse_rows(rows), "test", "test")
    return app


def test_log_in_with_member_number_and_code(app):
    c, mail = app.test_client(), app.config["mail"]
    assert "Log in" in c.get("/").get_data(as_text=True)
    r = c.post("/login", data={"who": "3220 0001"})
    assert r.headers["Location"].endswith("/login/code")
    assert mail.sent[0]["to"] == "ann@example.com" and mail.sent[0]["reply_to"] == "firstcoast@apaleagues.com"
    assert "a•••@example.com" in c.get("/login/code").get_data(as_text=True)
    code = mail.code()
    wrong = "000000" if code != "000000" else "111111"
    r = c.post("/login/code", data={"code": wrong}, follow_redirects=True)
    assert "isn&#39;t right. 4 more tries" in r.get_data(as_text=True)
    r = c.post("/login/code", data={"code": code})
    assert r.headers["Location"].endswith("/me")
    cookie = c.get_cookie("ct_player")
    assert cookie and cookie.max_age == 90 * 86400 and cookie.http_only
    page = c.get("/me").get_data(as_text=True)
    assert "Ann Lee" in page and "Member 32200001" in page and "SL 4" in page and "9999" not in page
    assert ">Ann<" in c.get("/").get_data(as_text=True)  # the menu shows their name
    # The code can't be used again.
    c2 = app.test_client()
    with c2.session_transaction() as s:
        s["login_member"] = "32200001"
    assert "isn&#39;t valid any more" in c2.post("/login/code", data={"code": code}, follow_redirects=True).get_data(as_text=True)


def test_log_in_with_email_and_the_refusals(app):
    c, mail = app.test_client(), app.config["mail"]
    c.post("/login", data={"who": "ANN@example.com"})
    assert mail.sent and mail.sent[-1]["to"] == "ann@example.com"
    for who, message in (("1234", "8 digits and start with 322"), ("32299999", "isn&#39;t in the player list"),
                         ("nobody@example.com", "That email isn&#39;t in the player list"),
                         ("32200002", "no email on file")):
        page = app.test_client().post("/login", data={"who": who}, follow_redirects=True).get_data(as_text=True)
        assert message in page, who
    page = c.post("/login", data={"who": "32200001"}, follow_redirects=True).get_data(as_text=True)
    assert "A code was just sent" in page  # no new code within a minute


def test_codes_expire_and_run_out_of_tries(tmp_path):
    a = acc.Accounts(tmp_path, "secret")
    t0 = datetime(2026, 11, 2, 12, 0)
    code = a.new_code("32200001", t0)
    with pytest.raises(acc.LoginError, match="expired"):
        a.check_code("32200001", code, t0 + timedelta(minutes=11))
    code = a.new_code("32200001", t0 + timedelta(minutes=20))
    for _ in range(4):
        with pytest.raises(acc.LoginError, match="isn't right"):
            a.check_code("32200001", "999999" if code != "999999" else "888888", t0 + timedelta(minutes=21))
    with pytest.raises(acc.LoginError, match="Too many wrong codes"):
        a.check_code("32200001", "999999" if code != "999999" else "888888", t0 + timedelta(minutes=21))
    with pytest.raises(acc.LoginError, match="isn't valid any more"):
        a.check_code("32200001", code, t0 + timedelta(minutes=21))  # the right code is gone too
    assert "secret" not in (tmp_path / "players" / "accounts.json").read_text() and code not in \
        (tmp_path / "players" / "accounts.json").read_text()


def test_log_out_on_all_devices(app):
    phone, laptop, mail = app.test_client(), app.test_client(), app.config["mail"]
    for client in (phone, laptop):
        with client.session_transaction() as s:
            s["login_member"] = "32200001"
        client.post("/login/code", data={"code": app.config["accounts"].new_code("32200001", datetime.now()
                                                                                     - timedelta(minutes=2))})
        assert client.get("/me").status_code == 200
    phone.post("/logout/everywhere")
    assert phone.get("/me").status_code == 302 and laptop.get("/me").status_code == 302


def test_a_tampered_cookie_is_ignored(app):
    c = app.test_client()
    c.set_cookie("ct_player", "eyJtIjoiMzIyMDAwMDEiLCJ2IjoxfQ.forged.sig")
    assert c.get("/me").status_code == 302


def test_both_login_pages_show_their_own_form(app):
    c = app.test_client()
    director = c.get("/admin/login").get_data(as_text=True)
    assert 'name="password"' in director and 'name="who"' not in director
    player = c.get("/login").get_data(as_text=True)
    assert 'name="who"' in player and 'value="code"' in player and 'name="who"' not in director



def test_optional_password(app):
    import time
    c, mail = app.test_client(), app.config["mail"]
    accounts = app.config["accounts"]
    page = c.post("/login", data={"who": "32200001", "action": "password", "password": "whatever1"},
                  follow_redirects=True).get_data(as_text=True)
    assert "haven&#39;t set a password" in page
    # Log in with a code, then set a password right away (a fresh code login).
    c.post("/login", data={"who": "32200001", "action": "code"})
    c.post("/login/code", data={"code": mail.code()})
    assert "Set password" in c.get("/me").get_data(as_text=True)
    r = c.post("/me/password", data={"action": "set", "password": "password1", "confirm": "password1"},
               follow_redirects=True)
    assert "too easy to guess" in r.get_data(as_text=True)
    r = c.post("/me/password", data={"action": "set", "password": "corner pocket 9", "confirm": "corner pocket 9"},
               follow_redirects=True)
    assert "Password saved" in r.get_data(as_text=True) and c.get("/me").status_code == 200  # this device stays in
    assert "corner pocket 9" not in (app.config["data_dir"] / "players" / "accounts.json").read_text()

    other = app.test_client()
    r = other.post("/login", data={"who": "ann@example.com", "action": "password", "password": "nope nope 1"},
                   follow_redirects=True)
    assert "isn&#39;t right" in r.get_data(as_text=True)
    other.post("/login", data={"who": "ann@example.com", "action": "password", "password": "corner pocket 9"})
    assert other.get("/me").status_code == 200
    # Without a fresh code, changing it needs the current password.
    page = other.get("/me").get_data(as_text=True)
    assert "Current password" in page
    r = other.post("/me/password", data={"action": "set", "current": "wrong", "password": "eight ball run",
                                         "confirm": "eight ball run"}, follow_redirects=True)
    assert "confirm with an emailed code first" in r.get_data(as_text=True)
    other.post("/me/password", data={"action": "set", "current": "corner pocket 9", "password": "eight ball run",
                                     "confirm": "eight ball run"})
    assert accounts.check_password("32200001", "eight ball run")
    assert c.get("/me").status_code == 302  # the change logged the first device out


def test_setting_a_password_needs_a_fresh_code(app, monkeypatch):
    c, mail = app.test_client(), app.config["mail"]
    c.post("/login", data={"who": "32200001", "action": "code"})
    c.post("/login/code", data={"code": mail.code()})
    import time as _time
    real = _time.time
    monkeypatch.setattr(_time, "time", lambda: real() + 3600)  # an hour later: no longer fresh
    page = c.get("/me").get_data(as_text=True)
    assert "Set a password: confirm with an emailed code first" in page and "Set password</button>" not in page
    r = c.post("/me/password", data={"action": "set", "password": "corner pocket 9", "confirm": "corner pocket 9"},
               follow_redirects=True)
    assert "confirm with an emailed code first" in r.get_data(as_text=True)
    monkeypatch.setattr(_time, "time", real)
    c.post("/me/verify")
    assert mail.sent[-1]["to"] == "ann@example.com"
