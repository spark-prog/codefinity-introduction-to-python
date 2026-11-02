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
    assert "Hasn't played a match this session (since Sep 8)" in el.check_player(
        rec(last8="08/30/2026"), rules(played_session=True), START)[0][0]
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
    assert "Hasn&#39;t played a match this session (since Oct 15)" in signup(app.test_client(), "Ann Lee", "32200001")
