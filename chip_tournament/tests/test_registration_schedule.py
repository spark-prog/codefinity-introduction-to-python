"""Phase 1: online registration opens and closes on its own at set times."""

from datetime import datetime, timedelta

import pytest

from app import create_app
from chips import schedule
from chips.engine import Settings, TournamentError


def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def test_window_states():
    s = Settings()
    at = datetime(2026, 11, 7, 12, 0)
    assert schedule.reg_window(s, at) == ("open", None)
    s.registration_opens, s.registration_closes = "2026-11-07T18:00", "2026-11-20T22:00"
    assert schedule.reg_window(s, at) == ("before", datetime(2026, 11, 7, 18, 0))
    assert schedule.reg_window(s, datetime(2026, 11, 8)) == ("open", datetime(2026, 11, 20, 22, 0))
    assert schedule.reg_window(s, datetime(2026, 11, 21))[0] == "after"
    assert schedule.shut_message(s, at) == "Registration opens Sat, Nov 7 at 6:00 PM."
    s.registration_closed = True  # the director's box wins
    assert schedule.reg_window(s, datetime(2026, 11, 8))[0] == "closed"


def test_director_times_are_checked():
    assert schedule.check_times("2026-11-07T18:00", "") == ("2026-11-07T18:00", "")
    assert schedule.check_times("", "") == ("", "")
    with pytest.raises(TournamentError, match="close after it opens"):
        schedule.check_times("2026-11-07T18:00", "2026-11-07T17:00")
    with pytest.raises(TournamentError, match="isn't a date"):
        schedule.check_times("next friday", "")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "VIEWER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


@pytest.mark.parametrize("kind", ["chip", "bracket"])
def test_register_is_refused_before_it_opens_and_after_it_closes(app, kind):
    hub = app.config["hub"]
    ev = hub.create("Night", kind, **({"format": "single"} if kind == "bracket" else {}))
    later = datetime.now() + timedelta(days=2, hours=3)
    t = ev.load()
    t.settings.registration_opens = fmt(later)
    ev.store.save(t)

    player = app.test_client()
    home = player.get(f"/t/{ev.id}/").get_data(as_text=True)
    assert "Registration opens" in home and 'class="countdown"' in home and 'data-label="Opens in"' in home
    assert f'href="/t/{ev.id}/register" class="nav-cta' not in home  # no Register button yet
    lst = player.get("/").get_data(as_text=True)
    assert ">Registration opens " in lst and "Registration open<" not in lst
    form = {"name": "Sharks", "agree_terms": "1", "player_name_0": "Ann Lee", "player_name_1": "Bob Ray",
            "player_skill_0": "5", "player_skill_1": "5", "player_phone_0": "9045550100"}
    r = player.post(f"/t/{ev.id}/register", data=form, follow_redirects=True)
    assert "Registration opens" in r.get_data(as_text=True)
    t = ev.load()
    assert not (getattr(t, "teams", None) or getattr(t, "entries", None))

    t.settings.registration_opens = fmt(datetime.now() - timedelta(hours=1))
    t.settings.registration_closes = fmt(datetime.now() + timedelta(hours=5))
    ev.store.save(t)
    page = player.get(f"/t/{ev.id}/register").get_data(as_text=True)
    assert "Registration closes" in page and 'data-label="Closes in"' in page
    player.post(f"/t/{ev.id}/register", data=form)
    t = ev.load()
    assert len(getattr(t, "teams", None) or getattr(t, "entries", None)) == 1

    t.settings.registration_closes = fmt(datetime.now() - timedelta(minutes=1))
    ev.store.save(t)
    r = player.post(f"/t/{ev.id}/register", data={**form, "name": "Jets", "player_name_0": "Cy Day"},
                    follow_redirects=True)
    assert "Registration closed" in r.get_data(as_text=True)
    t = ev.load()
    assert len(getattr(t, "teams", None) or getattr(t, "entries", None)) == 1


def test_director_sets_the_times(app):
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    ev = app.config["hub"].create("Bracket", "bracket", format="single")
    page = admin.get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert 'name="registration_opens"' in page and "Right now: <b>open</b>" in page
    s = ev.load().settings
    form = {"name": "Bracket", "tables": "1, 2", "race_winners": str(s.race_winners), "race_losers": str(s.race_losers),
            "race_finals": str(s.race_finals), "match_minutes": "40", "long_match_minutes": "60", "field_cap": "0",
            "no_rack_minutes": "15", "call_mode": "director", "format": "single", "team_size": "1",
            "entry_fee": "0", "added_money": "0", "payouts": "100",
            "registration_opens": "2099-11-07T18:00", "registration_closes": "2099-11-20T22:00"}
    r = admin.post(f"/t/{ev.id}/admin/settings", data=form, follow_redirects=True)
    s = ev.load().settings
    assert (s.registration_opens, s.registration_closes) == ("2099-11-07T18:00", "2099-11-20T22:00"), \
        r.get_data(as_text=True)[:3000]
    form["registration_closes"] = "2099-11-01T10:00"
    r = admin.post(f"/t/{ev.id}/admin/settings", data=form, follow_redirects=True)
    assert "close after it opens" in r.get_data(as_text=True)
    assert ev.load().settings.registration_closes == "2099-11-20T22:00"
