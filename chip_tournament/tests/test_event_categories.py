"""What kind of event this is: players per entry, game, and Qualifier / Bonus Tournament (tag line, creation, settings)."""

import pytest

from app import create_app
from chips import event_info
from chips.bracket import BracketTournament
from chips.engine import Tournament, TournamentError


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


def admin(app):
    c = app.test_client()
    c.post("/admin/login", data={"password": "secretpw1"})
    return c


def bracket(**kw):
    t = BracketTournament()
    for k, v in kw.items():
        setattr(t.settings, k, v)
    return t


def test_the_tag_line():
    t = bracket(team_size=2, game="9", is_qualifier=True, categories_set=True, name="Whatever")
    assert event_info.tag_line(t) == "9-Ball · Doubles · Qualifier"
    t = bracket(team_size=1, game="8", is_qualifier=True, is_bonus=True, categories_set=True)
    assert event_info.tag_line(t) == "8-Ball · Singles · Qualifier · Bonus Tournament"
    assert event_info.tag_line(bracket(team_size=4, game="other", categories_set=True)) == "Other · Teams"
    chip = Tournament()
    chip.settings.game, chip.settings.categories_set = "8", True
    assert event_info.tag_line(chip) == "8-Ball · Doubles"  # chip tournaments are teams of two


def test_an_event_nobody_has_categorized_is_guessed_from_its_name():
    t = bracket(team_size=2, name="9-Ball Doubles Vegas Qualifier")
    assert event_info.tag_line(t) == "9-Ball · Doubles · Qualifier" and not event_info.categories(t)["confirmed"]
    t = bracket(team_size=1, name="Movin-On-Up", bonus_event_id="abc")
    assert event_info.categories(t)["types"] == ["Bonus Tournament"]
    # what the director chose beats the name
    t = bracket(team_size=1, name="Qualifier Night", categories_set=True, is_qualifier=False, game="8")
    assert event_info.categories(t)["types"] == []


def test_creating_an_event_asks_for_entry_game_and_type(app):
    c = admin(app)
    hub = app.config["hub"]
    r = c.post("/admin/new", data={"name": "No Choices", "kind": "single"}, follow_redirects=True).get_data(as_text=True)
    assert "Choose the game" in r or "Choose singles" in r and not hub.events()
    r = c.post("/admin/new", data={"name": "No Game", "kind": "single", "entry": "2"}, follow_redirects=True).get_data(as_text=True)
    assert "Choose the game" in r
    r = c.post("/admin/new", data={"name": "No Entry", "kind": "single", "game": "8"}, follow_redirects=True).get_data(as_text=True)
    assert "Choose singles, doubles or teams" in r
    c.post("/admin/new", data={"name": "Doubles Qualifier", "kind": "double", "entry": "2", "game": "9", "qualifier": "1"})
    c.post("/admin/new", data={"name": "Team Night", "kind": "single", "entry": "teams", "team_n": "4", "game": "other", "bonus": "1"})
    s = hub.get("doubles-qualifier").load().settings
    assert (s.team_size, s.game, s.is_qualifier, s.is_bonus, s.categories_set) == (2, "9", True, False, True)
    s = hub.get("team-night").load().settings
    assert (s.team_size, s.game, s.is_bonus) == (4, "other", True)
    r = c.post("/admin/new", data={"name": "Tiny Team", "kind": "single", "entry": "teams", "team_n": "2", "game": "8"}, follow_redirects=True)
    assert "Teams need 3 or more players" in r.get_data(as_text=True)
    c.post("/admin/new", data={"name": "Chips Night", "kind": "chip", "game": "8", "bonus": "1"})  # no entry choice for chips
    chips = hub.get("chips-night").load()
    assert event_info.tag_line(chips) == "8-Ball · Doubles · Bonus Tournament"


def test_the_director_page_asks_to_categorize_older_events_once(app):
    hub = app.config["hub"]
    old = hub.create("Old Bracket Qualifier", "bracket", format="single")
    c = admin(app)
    page = c.get("/admin").get_data(as_text=True)
    assert "Categorize your tournaments (1)" in page and "Old Bracket Qualifier" in page
    c.post(f"/admin/categorize/{old.id}", data={"game": "9", "qualifier": "1"})
    s = old.load().settings
    assert (s.game, s.is_qualifier, s.is_bonus, s.categories_set) == ("9", True, False, True)
    assert "Categorize your tournaments" not in c.get("/admin").get_data(as_text=True)
    r = c.post(f"/admin/categorize/{old.id}", data={"qualifier": "1"}, follow_redirects=True).get_data(as_text=True)
    assert "Choose the game" in r  # the game has to be chosen
    assert app.test_client().post(f"/admin/categorize/{old.id}", data={"game": "8"}).status_code == 302  # not signed in


def test_settings_confirm_the_categories_but_a_blank_game_leaves_them_alone(app):
    hub = app.config["hub"]
    ev = hub.create("Plain Night", "bracket", format="single")
    c = admin(app)
    base = {"name": "Plain Night", "date": "2026-12-01", "race_to": "5", "call_mode": "director", "format": "single",
            "team_size": "1", "seeding": "random", "tables": "1,2", "categories_shown": "1", "game": ""}
    c.post(f"/t/{ev.id}/admin/settings", data={**base, "qualifier": "1"})
    assert not ev.load().settings.categories_set  # nothing chosen: not confirmed
    c.post(f"/t/{ev.id}/admin/settings", data={**base, "game": "9", "qualifier": "1", "bonus": "1"})
    s = ev.load().settings
    assert (s.game, s.is_qualifier, s.is_bonus, s.categories_set) == ("9", True, True, True)


def test_the_tag_line_shows_on_cards_and_the_event_page(app):
    hub = app.config["hub"]
    ev = hub.create("Vegas Night", "bracket", format="single", date="2026-12-12")
    t = ev.load()
    t.settings.team_size, t.settings.game, t.settings.is_qualifier, t.settings.categories_set = 2, "9", True, True
    ev.store.save(t)
    c = app.test_client()
    assert "9-Ball · Doubles · Qualifier" in c.get("/").get_data(as_text=True)
    assert "9-Ball · Doubles · Qualifier" in c.get(f"/t/{ev.id}/").get_data(as_text=True)


def test_a_bonus_event_import_marks_the_event_as_bonus(app):
    from chips import bonus_event
    ev = app.config["hub"].create("Movin-On-Up", "bracket", format="single")
    t = ev.load()
    plan = bonus_event.Plan(kind="bracket", name="Movin-On-Up", settings={}, summary=[], warnings=[], external_id="x1")
    bonus_event.apply(t, plan)
    assert t.settings.is_bonus and event_info.categories(t)["types"] == ["Bonus Tournament"]
