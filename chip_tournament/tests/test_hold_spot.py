"""A team with a player who isn't eligible yet can hold its spot while the league office reviews, instead of being turned away."""

from tests.test_eligibility import app, signup  # noqa: F401  (the Masters Night fixture: Cy has only 3 matches)


def register(c, name, member, hold=True):
    data = {"agree_terms": "1", "player_name_0": name, "player_skill_0": "5", "player_member_0": member,
            "player_phone_0": "904-555-0100"}
    if hold:
        data["hold_review"] = "1"
    return c.post("/t/masters-night/register", data=data, follow_redirects=True).get_data(as_text=True)


def admin(app):
    c = app.test_client()
    c.post("/admin/login", data={"password": "secretpw1"})
    return c


def test_holding_a_spot_registers_the_team_and_files_a_review(app):
    ev = app.config["hub"].get("masters-night")
    page = register(app.test_client(), "Cy Day", "32200003")
    assert "Your spot is held while the league office reviews" in page
    entry = next(iter(ev.load().entries.values()))
    assert entry.players[0].pending.startswith("Not eligible, review requested: Needs 6 lifetime matches")
    reviews = ev.load().settings.review_requests
    assert len(reviews) == 1 and reviews[0]["member"] == "32200003" and reviews[0]["status"] == "open"
    assert "Pending verification" in admin(app).get("/t/masters-night/admin").get_data(as_text=True)
    assert len(ev.load().entries) == 1


def test_a_refusal_without_the_box_is_unchanged(app):
    ev = app.config["hub"].get("masters-night")
    assert "Needs 6 lifetime matches. You have 3" in register(app.test_client(), "Cy Day", "32200003", hold=False)
    assert not ev.load().entries and not ev.load().settings.review_requests


def test_a_held_spot_really_holds_it(app):
    ev = app.config["hub"].get("masters-night")
    t = ev.load()
    t.settings.field_cap = 1
    ev.store.save(t)
    register(app.test_client(), "Cy Day", "32200003")  # not eligible yet, but holds the one spot
    register(app.test_client(), "Ann Lee", "32200001")  # eligible, but the field is full
    t = ev.load()
    assert [e.name for e in t.field_entries] == ["Cy Day"]
    assert [t.entries[i].name for i in t.waitlist] == ["Ann Lee"]


def test_approving_the_review_keeps_the_spot_and_declining_lets_the_director_release_it(app):
    ev = app.config["hub"].get("masters-night")
    register(app.test_client(), "Cy Day", "32200003")
    rid = ev.load().settings.review_requests[0]["id"]
    d = admin(app)
    d.post(f"/t/masters-night/admin/review/{rid}", data={"action": "approve", "note": "Played in the spring"})
    t = ev.load()
    assert next(iter(t.entries.values())).players[0].pending == "" and len(t.field_entries) == 1  # cleared, still in
    # a second player held and then released
    register(app.test_client(), "Bo Ray", "32200002")  # Past Due: not eligible, holds a spot too
    entry = next(e for e in ev.load().entries.values() if e.name == "Bo Ray")
    assert entry.players[0].pending.startswith("Not eligible")
    r = d.post(f"/t/masters-night/admin/pending/{entry.id}/0", data={"action": "remove"}, follow_redirects=True).get_data(as_text=True)
    assert "Removed Bo Ray" in r
    assert all(e.name != "Bo Ray" for e in ev.load().entries.values())


def test_the_box_is_on_the_registration_form_only_when_the_event_has_eligibility_rules(app):
    page = app.test_client().get("/t/masters-night/register").get_data(as_text=True)
    assert 'name="hold_review" value="1" checked' in page and "hold our spot while the league office reviews" in page
    ev = app.config["hub"].create("Open Night", "bracket", format="single")
    page = app.test_client().get(f"/t/{ev.id}/register").get_data(as_text=True)
    assert 'name="hold_review"' not in page
    chip = app.config["hub"].create("Chips Open", "chip")
    assert 'name="hold_review"' not in app.test_client().get(f"/t/{chip.id}/register").get_data(as_text=True)
