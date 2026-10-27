"""The director can close online registration until an event is ready."""

from app import create_app


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "VIEWER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    return app, admin


def test_closed_bracket_registration(tmp_path, monkeypatch):
    app, admin = setup(tmp_path, monkeypatch)
    ev = app.config["hub"].create("Movin-On-Up", "bracket")
    s = ev.load().settings
    form = {"name": s.name, "tables": ", ".join(s.tables)}
    admin.post(f"/t/{ev.id}/admin/settings", data={**form, "registration_closed": "1"})
    assert ev.load().settings.registration_closed
    player = app.test_client()
    home = player.get(f"/t/{ev.id}/").get_data(as_text=True)
    assert f'href="/t/{ev.id}/register"' not in home
    page = player.get(f"/t/{ev.id}/register").get_data(as_text=True)
    assert "Registration is closed for now" in page and 'name="player_name_0"' not in page
    r = player.post(f"/t/{ev.id}/register", data={"agree_terms": "1", "player_name_0": "Ann"}, follow_redirects=True)
    assert b"Registration is closed for now" in r.data and not ev.load().entries
    admin.post(f"/t/{ev.id}/admin/entries/add", data={"bulk": "Bob"})  # the director can still add players
    assert [e.name for e in ev.load().entries.values()] == ["Bob"]
    admin.post(f"/t/{ev.id}/admin/settings", data=form)  # unticked: open again
    assert not ev.load().settings.registration_closed
    assert f'href="/t/{ev.id}/register"' in player.get(f"/t/{ev.id}/").get_data(as_text=True)


def test_closed_chip_registration(tmp_path, monkeypatch):
    app, admin = setup(tmp_path, monkeypatch)
    ev = app.config["hub"].create("Chips", "chip")
    s = ev.load().settings
    admin.post(f"/t/{ev.id}/admin/settings", data={
        "name": s.name, "tables": ", ".join(s.tables), "race_to": str(s.race_to), "entry_fee": "20", "added_money": "0",
        "registration_cap": str(s.registration_cap), "chip_brackets": "6 = 5\n8 = 4\n10 = 3\n11 = 2", "registration_closed": "1"})
    assert ev.load().settings.registration_closed
    player = app.test_client()
    page = player.get(f"/t/{ev.id}/register").get_data(as_text=True)
    assert "Registration is closed for now" in page and 'name="player_name_0"' not in page
    assert "Register a team</a>" not in player.get(f"/t/{ev.id}/").get_data(as_text=True)
    player.post(f"/t/{ev.id}/register", data={"name": "Sharks", "agree_terms": "1", "player_name_0": "Ann",
                                             "player_phone_0": "5551234567", "player_skill_0": "4",
                                             "player_name_1": "Bob", "player_skill_1": "4"})
    assert not ev.load().teams
