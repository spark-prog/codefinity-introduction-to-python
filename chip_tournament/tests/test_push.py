"""Series events: pushing eligibility once to the next edition."""

import pytest

from app import create_app
from chips import backup, players
from tests.test_eligibility import HEADERS, person, rules
from tests.test_player_login import FakeMailer
from tests.test_register_members import Clients

COLS = HEADERS + ["MVP"]


def sheet(ann="Yes", bo="Push", cy=""):
    return [COLS, person("32200001", first="Ann", last="Lee")[:-1] + [ann],
            person("32200002", first="Bo", last="Ray")[:-1] + [bo],
            person("32200003", first="Cy", last="Day")[:-1] + [cy]]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: FakeMailer()))
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["players"].apply(*players.parse_rows(sheet()), "t", "t")
    for name, date in (("MVP Fall", "2026-11-14"), ("MVP Spring", "2027-03-13")):
        ev = app.config["hub"].create(name, "bracket", format="single", date=date)
        t = ev.load()
        t.settings.team_size, t.settings.series = 1, "MVP"
        t.settings.eligibility = rules(list_label="MVP", list_column="MVP")
        ev.store.save(t)
    return app


def register(c, slug, name, member):
    return c.post(f"/t/{slug}/register", data={"agree_terms": "1", "player_name_0": name, "player_skill_0": "5",
                                               "player_member_0": member, "player_phone_0": "904-555-0100",
                                               "sms_consent": "1"}, follow_redirects=True).get_data(as_text=True)


def test_player_pushes_once(app):
    hub, pushes = app.config["hub"], app.config["pushes"]
    ann = Clients(app).logged_in("32200001")
    assert "Push your eligibility to the next MVP" in ann.get("/t/mvp-fall/register").get_data(as_text=True)
    register(ann, "mvp-fall", "Ann Lee", "32200001")
    home = ann.get("/t/mvp-fall/").get_data(as_text=True)
    assert "already registered" in home and "Push your eligibility to the next MVP" in home
    assert "Push your eligibility to the next MVP" in ann.get("/me").get_data(as_text=True)

    r = ann.post("/t/mvp-fall/push", follow_redirects=True)
    assert "Tick the box" in r.get_data(as_text=True)
    r = ann.post("/t/mvp-fall/push", data={"confirm": "1"}, follow_redirects=True)
    assert "pushed to the next MVP and they&#39;re unregistered" in r.get_data(as_text=True)
    assert not hub.get("mvp-fall").load().entries
    assert any("pushed to the next MVP" in m["body"] for m in hub.get("mvp-fall").sms.recent())
    assert [p["member"] for p in pushes.from_event("mvp-fall")] == ["32200001"]

    # Next edition: the office hasn't updated the sheet (or took her off), she's still in, on her grace.
    app.config["players"].apply(*players.parse_rows(sheet(ann="")), "t", "t")
    page = ann.get("/t/mvp-spring/register").get_data(as_text=True)
    assert "your one-time grace entry" in page and "Push your eligibility" not in page
    r = ann.post("/t/mvp-spring/push", data={"confirm": "1"}, follow_redirects=True)
    assert "one-time grace entry, so it can&#39;t be pushed again" in r.get_data(as_text=True)
    register(ann, "mvp-spring", "Ann Lee", "32200001")  # her grace entry lets her register
    assert len(hub.get("mvp-spring").load().entries) == 1


def test_push_in_the_sheet_and_not_on_the_list(app):
    bo = Clients(app).logged_in("32200002")
    page = bo.get("/t/mvp-fall/register").get_data(as_text=True)
    assert "your one-time grace entry" in page and "Push your eligibility" not in page
    cy = Clients(app).logged_in("32200003")
    r = cy.post("/t/mvp-fall/push", data={"confirm": "1"}, follow_redirects=True)
    assert "not on the MVP list" in r.get_data(as_text=True)


def test_director_pushes_for_a_player_and_sees_the_lists(app):
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    page = admin.get("/t/mvp-fall/admin").get_data(as_text=True)
    assert "Pushed to the next MVP" in page and 'name="elig_series"' in page
    admin.post("/t/mvp-fall/push", data={"member": "32200001", "confirm": "1"})
    page = admin.get("/t/mvp-fall/admin").get_data(as_text=True)
    assert "Ann Lee" in page and "Mark them <b>Push</b>" in page
    assert "On their one-time grace here (1)" in admin.get("/t/mvp-spring/admin").get_data(as_text=True)
    t = app.config["hub"].get("mvp-fall").load()
    t.status = "running"
    app.config["hub"].get("mvp-fall").store.save(t)
    r = admin.post("/t/mvp-fall/push", data={"member": "32200003", "confirm": "1"}, follow_redirects=True)
    assert "The event has started, so it&#39;s too late to push" in r.get_data(as_text=True)
