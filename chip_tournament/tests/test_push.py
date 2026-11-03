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
    assert "Push your eligibility to MVP Spring" in ann.get("/t/mvp-fall/register").get_data(as_text=True)
    register(ann, "mvp-fall", "Ann Lee", "32200001")
    home = ann.get("/t/mvp-fall/").get_data(as_text=True)
    assert "already registered" in home and "Push your eligibility to MVP Spring" in home
    assert "Push your eligibility to MVP Spring" in ann.get("/me").get_data(as_text=True)

    r = ann.post("/t/mvp-fall/push", follow_redirects=True)
    assert "Tick the box" in r.get_data(as_text=True)
    r = ann.post("/t/mvp-fall/push", data={"confirm": "1"}, follow_redirects=True)
    assert "pushed to MVP Spring and they&#39;re unregistered" in r.get_data(as_text=True)
    assert not hub.get("mvp-fall").load().entries
    assert any("pushed to MVP Spring" in m["body"] for m in hub.get("mvp-fall").sms.recent())
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
    assert "Pushed to MVP Spring" in page and 'name="push_to"' in page
    assert '<option value="mvp-spring" selected>MVP Spring' in page  # an older series event: its next one, picked
    admin.post("/t/mvp-fall/push", data={"member": "32200001", "confirm": "1"})
    page = admin.get("/t/mvp-fall/admin").get_data(as_text=True)
    assert "Ann Lee" in page and "Mark them <b>Push</b>" in page
    assert "On their one-time grace here (1)" in admin.get("/t/mvp-spring/admin").get_data(as_text=True)
    t = app.config["hub"].get("mvp-fall").load()
    t.status = "running"
    app.config["hub"].get("mvp-fall").store.save(t)
    r = admin.post("/t/mvp-fall/push", data={"member": "32200003", "confirm": "1"}, follow_redirects=True)
    assert "The event has started, so it&#39;s too late to push" in r.get_data(as_text=True)


def test_after_pushing_you_cant_enter_this_edition(app):
    """Pushing gives up this edition: no registering for it afterwards (the grace is for next time)."""
    hub = app.config["hub"]
    ann = Clients(app).logged_in("32200001")  # still Yes in the sheet for this edition
    ann.post("/t/mvp-fall/push", data={"confirm": "1"})
    reason = "You pushed your eligibility to MVP Spring, so you can&#39;t enter this one."
    page = ann.get("/t/mvp-fall/register").get_data(as_text=True)
    assert "You can't register for this event yet" in page and reason in page
    assert "Push your eligibility" not in page
    page = register(ann, "mvp-fall", "Ann Lee", "32200001")
    assert "Ann Lee can&#39;t register for MVP Fall: They pushed their eligibility to MVP Spring, so they can&#39;t" in page
    assert not hub.get("mvp-fall").load().entries
    mine = ann.get("/eligibility").get_data(as_text=True)
    fall = mine.split("MVP Fall", 1)[1].split("</article>", 1)[0]
    assert "Not eligible" in fall and reason in fall
    spring = mine.split(">MVP Spring</a>", 1)[1]
    assert "Eligible ✓" in spring.split("</article>", 1)[0]  # next time: eligible on her grace
    anyone = app.test_client().post("/eligibility", data={"member": "32200001"}).get_data(as_text=True)
    assert "pushed your eligibility" not in anyone  # the reason is private
    # The director can still add her, or override if the push was a mistake
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    admin.post("/t/mvp-fall/admin/overrides", data={"member": "32200001", "action": "allow", "note": "pushed by mistake"})
    assert "approved by the league office" in ann.get("/t/mvp-fall/register").get_data(as_text=True)
    register(ann, "mvp-fall", "Ann Lee", "32200001")
    assert hub.get("mvp-fall").load().registered_as("32200001") is not None


def test_the_directors_push_box_is_a_clear_confirmation(app):
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    page = admin.get("/t/mvp-fall/admin").get_data(as_text=True)
    assert "Yes, push them to MVP Spring (if they're registered here, they're unregistered and texted" in page
    assert "Unregister them here if they" not in page


def settings_save(admin, slug, **fields):
    """Post the settings form with just these fields changed (the rest as they are)."""
    import re
    page = admin.get(f"/t/{slug}/admin").get_data(as_text=True)
    form = page[page.index(f'action="/t/{slug}/admin/settings"'):]
    form = form[:form.index("</form>")]
    data = {}
    for m in re.finditer(r"<(input|select|textarea)\b([^>]*)>", form):
        tag, attrs = m.group(1), m.group(2)
        n = re.search(r'name="([^"]+)"', attrs)
        if not n or "disabled" in attrs or ('type="checkbox"' in attrs and "checked" not in attrs):
            continue
        if tag == "select":
            sel = form[m.start():form.index("</select>", m.start())]
            opts = re.findall(r"<option\b([^>]*)>([^<]*)", sel)  # (attributes, text): no value means the text
            pick = next((o for o in opts if "selected" in o[0]), opts[0] if opts else None)
            if pick:
                v = re.search(r'value="([^"]*)"', pick[0])
                data[n.group(1)] = v.group(1) if v else pick[1].strip()
        elif tag == "textarea":
            data[n.group(1)] = form[m.end():form.index("</textarea>", m.start())]
        else:
            v = re.search(r'value="([^"]*)"', attrs)
            data.setdefault(n.group(1), v.group(1) if v else "")
    data.update(fields)
    return admin.post(f"/t/{slug}/admin/settings", data=data, follow_redirects=True).get_data(as_text=True)


def test_allow_players_to_push_to_a_chosen_event(app):
    hub = app.config["hub"]
    classic = hub.create("Ladies Classic", "bracket", format="single", date="2027-01-09")  # no series at all
    t = classic.load()
    t.settings.team_size = 1
    classic.store.save(t)
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    page = admin.get("/t/mvp-fall/admin").get_data(as_text=True)
    assert "Allow players to push to" in page and "Don't allow pushing" in page
    assert '<option value="ladies-classic"' in page and '<option value="mvp-fall"' not in page  # not itself
    settings_save(admin, "mvp-fall", push_to="ladies-classic")
    assert hub.get("mvp-fall").load().settings.push_to == "ladies-classic"
    ann = Clients(app).logged_in("32200001")
    assert "Push your eligibility to Ladies Classic" in ann.get("/t/mvp-fall/register").get_data(as_text=True)
    r = ann.post("/t/mvp-fall/push", data={"confirm": "1"}, follow_redirects=True).get_data(as_text=True)
    assert "pushed to Ladies Classic" in r
    push = app.config["pushes"].from_event("mvp-fall")[0]
    assert (push["to_event"], push["to_name"]) == ("ladies-classic", "Ladies Classic")
    assert "On their one-time grace here (1)" in admin.get("/t/ladies-classic/admin").get_data(as_text=True)
    assert "On their one-time grace here" not in admin.get("/t/mvp-spring/admin").get_data(as_text=True)
    # Don't allow pushing
    settings_save(admin, "mvp-fall", push_to="none")
    assert app.config["hub"].get("mvp-fall").load().settings.eligibility["list_column"] == "MVP"  # the rest kept
    assert "Push your eligibility" not in Clients(app).logged_in("32200001").get("/t/mvp-spring/register").get_data(as_text=True)
    # A made-up or self pick is refused (saved as no pushing)
    settings_save(admin, "mvp-fall", push_to="mvp-fall")
    assert hub.get("mvp-fall").load().settings.push_to == "none"
    settings_save(admin, "mvp-fall", push_to="nope")
    assert hub.get("mvp-fall").load().settings.push_to == "none"
    # Copy doesn't carry the pick (the copy would point at the wrong event)
    settings_save(admin, "mvp-fall", push_to="ladies-classic")
    copy, _ = hub.copy("mvp-fall", "MVP Fall 2027", "2027-11-13")
    assert copy.load().settings.push_to == ""


def test_pushing_switched_off_is_refused(app):
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    settings_save(admin, "mvp-fall", push_to="none")
    ann = Clients(app).logged_in("32200001")
    page = ann.get("/t/mvp-fall/register").get_data(as_text=True)
    assert "Push your eligibility" not in page
    r = ann.post("/t/mvp-fall/push", data={"confirm": "1"}, follow_redirects=True).get_data(as_text=True)
    assert "Pushing to another event isn&#39;t turned on for this one" in r
    assert "Allow players to push to" in admin.get("/t/mvp-fall/admin").get_data(as_text=True)
    assert '<option value="none" selected>' in admin.get("/t/mvp-fall/admin").get_data(as_text=True)


def test_older_pushes_without_a_target_still_land(app):
    """Pushes made before the pick existed (series only) still count where they always did."""
    app.config["pushes"].add("MVP", "32200001", "Ann Lee", "mvp-fall", "MVP Fall", "2026-11-14", "player")
    t = app.config["hub"].get("mvp-spring").load()
    assert app.config["pushed_in"]("mvp-spring", t, "32200001")["member"] == "32200001"
