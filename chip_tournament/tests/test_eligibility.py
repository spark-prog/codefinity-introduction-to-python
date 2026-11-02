"""Eligibility: who may play, checked against the player list."""

import copy
import json
from pathlib import Path

import pytest

from app import create_app
from chips import eligibility as el, players
from tests.test_players import HEADERS, row
from tests.test_register_members import Clients
from tests.test_player_login import FakeMailer

COLS = HEADERS + ["Masters"]


def person(member, first="Ann", last="Lee", status="Active", mp8="20", mp9="0", last8="10/01/2026", last9="",
           gender="F", sl8="5", sl9="0", masters="No", email=None):
    r = row(member, first=first, last=last, sl8=sl8, sl9=sl9, mp8=mp8, mp9=mp9,
            email=email if email is not None else f"{first.lower()}@x.com")
    for col, value in (("Member Status", status), ("Last Date Played 8-Ball", last8),
                       ("Last Date Played 9-Ball", last9), ("Gender", gender)):
        r[HEADERS.index(col)] = value
    return r + [masters]


def rec(**kw):
    records, _ = players.parse_rows([COLS, person("32200001", **kw)])
    return records["32200001"]


START = "2026-09-08"


def rules(**kw):
    return {**el.default_rules(), **kw}


def test_each_rule_says_why_and_how_to_fix():
    assert el.check_player(rec(status="Past Due"), rules(active=True), START) == (
        ["Membership is Past Due. Pay your dues with the league office to play."], [])
    assert "Inactive" in el.check_player(rec(status="Inactive"), rules(active=True), START)[0][0]
    assert el.check_player(rec(status="Suspended?"), rules(active=True), START)[1] == ["Member status is 'Suspended?'"]
    no, _ = el.check_player(rec(mp8="4"), rules(min_matches=6), START)
    assert no == ["Needs 6 lifetime matches. You have 4: 2 more to go."]
    assert el.check_player(rec(mp8="4", mp9="7"), rules(min_matches=6), START) == ([], [])  # either format
    assert el.check_player(rec(mp8="4", mp9="7"), rules(min_matches=6, matches_game="8"), START)[0]
    assert "Hasn't played a match since Sep 8" in el.check_player(
        rec(last8="08/30/2026"), rules(played_session=True), START)[0][0]
    assert "since Oct 1" in el.check_player(rec(last8="09/20/2026"), rules(played_session=True, played_since="2026-10-01"),
                                            START)[0][0]  # the TD's own date beats the session start
    assert el.check_player(rec(last8="08/30/2026", last9="09/10/2026"), rules(played_session=True), START) == ([], [])
    assert "Not active in 9-Ball" in el.check_player(rec(), rules(active_in="9"), START)[0][0]
    assert "for skill level 1–4; your 8-Ball skill level is 5" in el.check_player(rec(), rules(skill_max=4), START)[0][0]
    assert el.check_player(rec(gender="M"), rules(gender="women"), START)[0] == ["This event is women only."]
    masters = rules(list_label="Masters", list_column="Masters")
    assert "Not on the Masters list" in el.check_player(rec(), masters, START)[0][0]
    assert el.check_player(rec(masters="Yes"), masters, START) == ([], [])
    assert el.check_player(rec(), rules(list_label="Invited", list_members=["32200001"]), START) == ([], [])
    assert el.check_player(rec(), rules(list_label="Captains"), START)[1] == ["The Captains list hasn't been set up yet"]
    assert el.check_player(None, rules(active=True), START)[1] == ["Not in the player list, so eligibility can't be checked"]
    assert el.check_player(rec(), rules(review=["Team Captain"]), START)[1] == ["Team Captain: checked by the league office"]
    assert el.check_player(rec(status="Past Due"), rules(), START) == ([], [])  # no rules: anyone


def test_jack_and_jill():
    r = rules(gender="mixed")
    assert el.check_team([rec(gender="M"), rec(gender="F")], r) == []
    assert el.check_team([rec(gender="F"), rec(gender="F")], r) == ["This is a Jack & Jill event: a team is one man and one woman."]


def test_bonus_event_requirements():
    doc = json.loads((Path(__file__).parent / "fixtures" / "bonus_event_halloween.json").read_text())
    r, notes = el.from_bonus_event(doc)
    assert (r["active"], r["min_matches"], r["matches_game"], r["played_session"]) == (True, 6, "either", True)
    assert notes == [] and r["review"] == []
    doc = copy.deepcopy(doc)
    for item in doc["eligibility"]["requirements"]:
        if item["requirement"] in ("Active on a Masters roster", "Has at least 10 lifetime matches in either 8-Ball or 9-Ball"):
            item["selection"] = "Required"
    doc["eligibility"]["gender_requirement"] = "Jack and Jill: one male and one female player"
    r, notes = el.from_bonus_event(doc)
    assert r["min_matches"] == 10 and r["list_label"] == "Masters roster" and r["gender"] == "mixed"
    assert "needs an eligible list" in notes[0]


def test_extra_sheet_columns_are_kept_but_private_ones_dropped():
    records, _ = players.parse_rows([COLS, person("32200001", masters="Yes")])
    assert records["32200001"]["extra"] == {"Masters": "Yes"}
    assert "Home Phone" not in records["32200001"]["extra"] and "Birth Date" not in records["32200001"]["extra"]


@pytest.fixture
def app(tmp_path, monkeypatch):
    from chips import backup
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: FakeMailer()))
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["players"].apply(*players.parse_rows([
        COLS, person("32200001", first="Ann", last="Lee", masters="Yes"),
        person("32200002", first="Bo", last="Ray", status="Past Due", gender="M"),
        person("32200003", first="Cy", last="Day", mp8="3", gender="M", masters="Yes"),
        person("32200004", first="Di", last="Fox", status="Retired", masters="Yes")]), "t", "t")
    ev = app.config["hub"].create("Masters Night", "bracket", format="single")
    t = ev.load()
    t.settings.team_size = 1
    t.settings.eligibility = rules(active=True, min_matches=6, played_session=True, list_label="Masters",
                                   list_column="Masters")
    ev.store.save(t)
    return app


def signup(c, name, member):
    return c.post("/t/masters-night/register", data={"agree_terms": "1", "player_name_0": name, "player_skill_0": "5",
                                                     "player_member_0": member, "player_phone_0": "904-555-0100"},
                  follow_redirects=True).get_data(as_text=True)


def test_registration_checks_eligibility(app):
    ev = app.config["hub"].get("masters-night")
    home = app.test_client().get("/t/masters-night/").get_data(as_text=True)
    assert "Who can play" in home and "At least 6 lifetime matches" in home and "On the Masters list" in home

    assert "Membership is Past Due" in signup(app.test_client(), "Bo Ray", "32200002")
    assert "Needs 6 lifetime matches. You have 3: 3 more to go." in signup(app.test_client(), "Cy Day", "32200003")
    assert not ev.load().entries
    signup(app.test_client(), "Di Fox", "32200004")  # odd status: in, but for the director to check
    di = next(iter(ev.load().entries.values())).players[0]
    assert di.pending == "Eligibility: Member status is 'Retired'"

    ann = Clients(app).logged_in("32200001")
    assert "You're eligible for Masters Night" in ann.get("/t/masters-night/register").get_data(as_text=True)
    bo = Clients(app).logged_in("32200002")
    page = bo.get("/t/masters-night/register").get_data(as_text=True)
    assert "You can't register for this event yet" in page and "Membership is Past Due" in page

    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    page = admin.get("/t/masters-night/admin/skill-check").get_data(as_text=True)
    assert "Eligibility" in page and "Member status is &#39;Retired&#39;" in page
    assert 'name="elig_min_matches"' in admin.get("/t/masters-night/admin").get_data(as_text=True)
    admin.post("/admin/players/session", data={"session_start": "2026-10-15"})
    assert app.config["players"].config()["session_start"] == "2026-10-15"
    assert "Hasn&#39;t played a match since Oct 15" in signup(app.test_client(), "Ann Lee", "32200001")


def test_highest_of_8_and_9():
    from chips.players import event_skill
    r = rec(sl8="5", sl9="8")
    assert event_skill(r, "high") == (8, "highest of 8-Ball & 9-Ball")
    assert event_skill(r, "high7") == (7, "highest of 8-Ball & 9-Ball, capped at 7 (SL 8)")
    assert event_skill(rec(sl8="6", sl9="0"), "high7") == (6, "highest of 8-Ball & 9-Ball")
    assert event_skill(rec(sl8="0", sl9="0"), "high") == (None, "needs review")
    no, _ = el.check_player(r, rules(skill_max=6), START, "high7")
    assert no == ["This event is for skill level 1–6; your highest skill level is 7."]


# ------------------------------------------------- Singles Boards: Eligible / Not Eligible / Qualified in a sheet column

def boards(**kw):
    return rules(list_label="Singles Boards", list_column="Masters", **kw)


def test_singles_boards_eligible_qualified_and_not_eligible():
    assert el.check_player(rec(masters="Eligible"), boards(), START, "8") == ([], [])
    assert el.check_player(rec(masters="ELIGIBLE "), boards(), START, "8") == ([], [])  # any capitals and spaces
    assert el.check_player(rec(masters="Qualified"), boards(), START, "8")[0] == [
        "Already qualified for Singles Boards: you've earned your spot, so you can't enter this qualifier again. "
        "Ask the league office if that's not right."]
    # Not Eligible: says the format and their count from the player list
    assert el.check_player(rec(masters="Not Eligible", mp8="7"), boards(), START, "8")[0] == [
        "Not eligible yet: Singles Boards needs 10 lifetime 8-Ball matches. You have 7: 3 more to go."]
    assert el.check_player(rec(masters="Not Eligible", mp8="40", mp9="4"), boards(), START, "9")[0] == [
        "Not eligible yet: Singles Boards needs 10 lifetime 9-Ball matches. You have 4: 6 more to go."]
    # The list is behind: they have the matches now
    assert el.check_player(rec(masters="Not Eligible", mp8="12"), boards(), START, "8")[0] == [
        "The Singles Boards list marks you Not Eligible (10 lifetime 8-Ball matches needed), but the player list "
        "shows 12. Ask the league office to update it."]
    # Blank: not on the list, as before
    assert el.check_player(rec(masters=""), boards(), START, "8")[0] == [
        "Not on the Singles Boards list for this event. Ask the league office if you think you should be."]
    # With the event's own match rule too, the reason is said once
    no, _ = el.check_player(rec(masters="Not Eligible", mp8="7"), boards(min_matches=10, matches_game="8"), START, "8")
    assert no == ["Needs 10 lifetime 8-Ball matches. You have 7: 3 more to go."]
    # Qualified players can't push into the next edition either
    assert el.list_value(rec(masters="Qualified"), boards()) == "qualified"
    assert el.list_value(rec(masters="Eligible"), boards()) == "yes"


def test_a_plain_no_still_means_not_on_the_list():
    assert el.check_player(rec(masters="No"), boards(), START, "8")[0] == [
        "Not on the Singles Boards list for this event. Ask the league office if you think you should be."]


def test_players_see_why_on_am_i_eligible(tmp_path, monkeypatch):
    from app import create_app
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["players"].apply(*players.parse_rows([
        COLS, person("32200001", masters="Qualified"), person("32200002", first="Bo", masters="Not Eligible", mp8="6"),
        person("32200003", first="Cy", masters="Eligible")]), "t", "t")
    ev = app.config["hub"].create("8-Ball Singles Boards", "bracket", format="double")
    t = ev.load()
    t.settings.eligibility, t.settings.skill_game = boards(), "8"
    ev.store.save(t)
    from tests.test_register_members import Clients
    anon = app.test_client().post("/eligibility", data={"member": "32200001"}).get_data(as_text=True)
    assert "reasons are private" in anon and "Already qualified" not in anon  # someone else looking: no reasons
    pages = {m: Clients(app).logged_in(m).get("/eligibility").get_data(as_text=True).replace("&#39;", "'")
             for m in ("32200001", "32200002", "32200003")}
    assert "Already qualified for Singles Boards" in pages["32200001"]
    assert "needs 10 lifetime 8-Ball matches. You have 6: 4 more to go" in pages["32200002"]
    assert "Already qualified" not in pages["32200003"] and "Not eligible yet" not in pages["32200003"]


def test_eligible_with_a_hand_check_shows_green_like_the_register_page(tmp_path, monkeypatch):
    """Eligible, with something the league office confirms by hand: green Eligible on Am I eligible?, the same as
    the green "You're eligible" on the Register page, with the note underneath (the detail only for the player)."""
    from app import create_app
    from tests.test_register_members import Clients
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["players"].apply(*players.parse_rows([COLS, person("32200001")]), "t", "t")
    ev = app.config["hub"].create("8-Ball Singles Board", "bracket", format="double")
    t = ev.load()
    t.settings.eligibility = rules(active=True, review=["Played in a Singles Board qualifier this year"])
    ev.store.save(t)
    anon = app.test_client().post("/eligibility", data={"member": "32200001"}).get_data(as_text=True)
    assert '<span class="check-badge ok">Eligible ✓</span>' in anon and "The league office will check" not in anon
    assert "The league office will confirm a detail after you register" in anon and "qualifier this year" not in anon
    mine = Clients(app).logged_in("32200001").get("/eligibility").get_data(as_text=True)
    assert "Eligible ✓" in mine and "The league office will confirm: Played in a Singles Board qualifier this year" in mine
    register = Clients(app).logged_in("32200001").get(f"/t/{ev.id}/register").get_data(as_text=True)
    assert "You're eligible for 8-Ball Singles Board" in register
