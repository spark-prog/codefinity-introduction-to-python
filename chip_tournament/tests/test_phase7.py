"""Phase 7: overrides with a note, review requests, locking eligibility when registration closes."""

from datetime import datetime

import pytest

from app import create_app
from chips import backup, eligibility as el, players
from chips.bracket import BracketTournament
from chips.engine import Player, TournamentError
from tests.test_eligibility import COLS, person, rules
from tests.test_player_login import FakeMailer
from tests.test_register_members import Clients

START = "2026-09-08"


def store_with(tmp_path, *rows):
    s = players.PlayerStore(tmp_path)
    s.apply(*players.parse_rows([COLS, *rows]), "t", "t")
    return s


def test_override_review_and_lock(tmp_path):
    store = store_with(tmp_path, person("32200001", first="Ann", last="Lee"),
                       person("32200003", first="Cy", last="Day", mp8="3"))
    t = BracketTournament()
    t.settings.team_size = 1
    t.settings.eligibility = rules(min_matches=6)
    t.register_entry("", [Player("Ann Lee", "9045550100", 5, member="32200001")])
    t.register_entry("", [Player("Cy Day", "9045550100", 5, member="32200003")])  # added before the rule, say
    with pytest.raises(TournamentError, match="Add a note"):
        el.add_override(t, "32200003", "Cy Day", "")

    req = el.request_review(t, "32200003", "Cy Day", ["Needs 6 lifetime matches."], "New team this session")
    with pytest.raises(TournamentError, match="already with the league office"):
        el.request_review(t, "32200003", "Cy Day", [], "")
    with pytest.raises(TournamentError, match="Add a note"):
        el.decide_review(t, req["id"], approve=False, note="")

    assert el.lock_due(t, datetime(2026, 11, 20)) is False  # no closing time yet, still in setup
    t.settings.registration_closes = "2026-11-20T22:00"
    assert el.lock_due(t, datetime(2026, 11, 20, 21, 59)) is False and el.lock_due(t, datetime(2026, 11, 20, 22, 0))
    assert el.lock(t, store, START) == 1
    cy = next(e for e in t.entries.values() if e.name == "Cy Day").players[0]
    assert cy.pending.startswith("Not eligible when registration closed: Needs 6 lifetime matches")
    assert t.settings.elig_snapshot["32200003"]["no"] and not el.lock_due(t, datetime(2026, 12, 1))

    el.decide_review(t, req["id"], approve=True, note="Played on a new team Sep 15")
    assert el.override_for(t, "32200003")["note"] == "Played on a new team Sep 15" and cy.pending == ""
    assert [a["what"] for a in t.settings.elig_audit] == ["review requested by", "locked", "override added for",
                                                          "review approved for"]
    el.unlock(t)
    assert t.settings.elig_locked_at == "" and t.settings.elig_snapshot == {}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    mail = FakeMailer()
    monkeypatch.setattr(backup.Mailer, "from_env", classmethod(lambda cls: mail))
    app = create_app(str(tmp_path))
    app.testing = True
    app.config["mail"] = mail
    app.config["players"].apply(*players.parse_rows([
        COLS, person("32200001", first="Ann", last="Lee"), person("32200003", first="Cy", last="Day", mp8="3")]),
        "t", "t")
    ev = app.config["hub"].create("Singles", "bracket", format="single")
    t = ev.load()
    t.settings.team_size, t.settings.eligibility = 1, rules(min_matches=6)
    ev.store.save(t)
    return app


def test_player_asks_for_review_and_director_approves(app):
    ev, mail = app.config["hub"].get("singles"), app.config["mail"]
    cy = Clients(app).logged_in("32200003")
    page = cy.get("/t/singles/register").get_data(as_text=True)
    assert "You can't register for this event yet" in page and "Ask for a review" in page
    cy.post("/t/singles/review-request", data={"message": "New team this session"})
    assert "You asked the league office to review this" in cy.get("/t/singles/register").get_data(as_text=True)

    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    page = admin.get("/t/singles/admin").get_data(as_text=True)
    assert "1 review request" in page and "New team this session" in page
    rid = ev.load().settings.review_requests[0]["id"]
    r = admin.post(f"/t/singles/admin/review/{rid}", data={"action": "approve", "note": "OK by Vicki"},
                   follow_redirects=True)
    assert "They&#39;ve been emailed" in r.get_data(as_text=True)
    sent = mail.sent[-1]
    assert sent["to"] == "cy@x.com" and "approved you for Singles" in sent["text"] and "/t/singles/register" in sent["text"]
    assert "approved by the league office" in cy.get("/t/singles/register").get_data(as_text=True)
    cy.post("/t/singles/register", data={"agree_terms": "1", "player_name_0": "Cy Day", "player_skill_0": "5",
                                         "player_member_0": "32200003", "player_phone_0": "904-555-0100"})
    assert len(ev.load().entries) == 1 and not next(iter(ev.load().entries.values())).players[0].pending
    assert "Eligibility decisions" in admin.get("/t/singles/admin/skill-check").get_data(as_text=True)


def test_director_lock_and_override_from_the_check_page(app):
    ev = app.config["hub"].get("singles")
    t = ev.load()
    t.register_entry("", [Player("Cy Day", "9045550100", 5, member="32200003")])
    ev.store.save(t)
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    r = admin.post("/t/singles/admin/eligibility-lock", data={"action": "lock"}, follow_redirects=True)
    assert "1 player(s) sent to Pending verification" in r.get_data(as_text=True)
    page = admin.get("/t/singles/admin/skill-check").get_data(as_text=True)
    assert "as of the lock" in page and ">Override</button>" in page
    admin.post("/t/singles/admin/overrides", data={"member": "32200003", "note": "LO approved",
                                                   "back": "/t/singles/admin/skill-check"})
    assert "Eligible (override)" in admin.get("/t/singles/admin/skill-check").get_data(as_text=True)
    assert next(iter(ev.load().entries.values())).players[0].pending == ""
    declined = app.test_client()
    declined.post("/t/singles/admin/overrides", data={"member": "32200001", "note": "x"})  # not signed in
    assert len(ev.load().settings.elig_overrides) == 1


def nested_forms(html: str) -> list[str]:
    """Forms inside forms: browsers drop the inner <form> and its </form> ends the outer one early."""
    from html.parser import HTMLParser
    found, depth = [], [0]

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == "form":
                if depth[0]:
                    found.append(dict(attrs).get("action", "?"))
                depth[0] += 1

        def handle_endtag(self, tag):
            if tag == "form":
                depth[0] = max(0, depth[0] - 1)
    P().feed(html)
    return found


def test_the_review_request_box_is_its_own_form_not_inside_registration(app):
    """The review box sat inside the registration form: its button did nothing useful and the fields below fell
    out of the form's layout. It now comes before the form, and the request goes in from the page itself."""
    cy = Clients(app).logged_in("32200003")
    page = cy.get("/t/singles/register").get_data(as_text=True)
    assert nested_forms(page) == []
    assert page.index("Ask for a review") < page.index('name="player_name_0"')  # above the registration form
    action = page.split("Ask for a review", 1)[1].split('action="', 1)[1].split('"', 1)[0]
    assert action == "/t/singles/review-request"
    cy.post(action, data={"message": "Played on a new team"})
    assert app.config["hub"].get("singles").load().settings.review_requests[0]["message"] == "Played on a new team"
    ann = Clients(app).logged_in("32200001")  # an eligible player: no empty box, no nested forms
    page = ann.get("/t/singles/register").get_data(as_text=True)
    assert nested_forms(page) == [] and "You can&#39;t register" not in page
