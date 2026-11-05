"""Rules search, phase 2: [apa: …] links, shorthand and popular searches, and the APA link check."""

import pytest

from app import create_app
from chips import apa_rules, rules
from chips.rules import SearchSettings, parse_popular, parse_shorthand, render, unknown_apa_links

FOULS = "https://rules.poolplayers.com/game-rules/fouls/"


def make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("CHIPS_PRODUCTION", raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    return app, admin


# ---- [apa: …] links ------------------------------------------------------------

def test_apa_link_renders_to_the_right_page():
    html = render("- Any foul gives ball in hand, see [apa: Fouls].")
    assert f'<a class="apa-link" href="{FOULS}" target="_blank" rel="noopener">Fouls<span class="apa-badge">APA</span></a>' in html
    # Capitals, extra spaces and ® don't matter; & in a title works.
    assert FOULS in render("[APA:   fouls ]")
    assert "the-equalizer-handicap-system/" in render("[apa: The Equalizer Handicap System]")
    assert "who-plays-first-order-of-play/" in render("[apa: Who Plays First & Order Of Play]")


def test_unknown_apa_link_never_becomes_a_link():
    assert unknown_apa_links("[apa: Fouls] and [apa: Foulz] and [apa: ]") == ["Foulz", "(blank)"]
    html = render("See [apa: Foulz].")
    assert "<a" not in html and "[apa: Foulz]" in html


def test_heading_with_apa_link_gets_a_clean_address():
    html = render("# Fouls [apa: Fouls]")
    assert 'id="rules--fouls-fouls"' in html and 'data-section="Fouls Fouls"' in html and FOULS in html


def test_saving_a_rule_set_with_a_bad_apa_link_is_refused_and_keeps_the_text(tmp_path, monkeypatch):
    app, admin = make_app(tmp_path, monkeypatch)
    body = "# Fouls\n- Scratch is ball in hand [apa: Foulz]\n- Typed a lot here"
    page = admin.post("/admin/rules/8-ball-rules", data={"title": "8-Ball Rules", "body": body}).get_data(as_text=True)
    assert "Not saved: [apa: Foulz] doesn&#39;t match" in page or "Not saved: [apa: Foulz] doesn't match" in page
    assert "Typed a lot here" in page  # what was typed is still in the box
    assert rules.RuleBook(tmp_path).get("8-ball-rules")["body"] == ""
    admin.post("/admin/rules/8-ball-rules", data={"title": "8-Ball Rules", "body": body.replace("Foulz", "Fouls")})
    assert "[apa: Fouls]" in rules.RuleBook(tmp_path).get("8-ball-rules")["body"]
    assert FOULS in app.test_client().get("/rules").get_data(as_text=True)


def test_event_rules_with_a_bad_apa_link_are_refused(tmp_path, monkeypatch):
    app, admin = make_app(tmp_path, monkeypatch)
    ev = app.config["hub"].create("Bracket", "bracket")
    s = ev.load().settings
    form = {"name": s.name, "tables": ", ".join(s.tables)}
    page = admin.post(f"/t/{ev.id}/admin/settings", data={**form, "event_rules": "See [apa: Nope]"},
                      follow_redirects=True).get_data(as_text=True)
    assert "Settings not saved" in page and ev.load().settings.event_rules == ""
    admin.post(f"/t/{ev.id}/admin/settings", data={**form, "event_rules": "See [apa: Breaking]"})
    assert ev.load().settings.event_rules == "See [apa: Breaking]"
    assert "game-rules/breaking/" in app.test_client().get(f"/t/{ev.id}/rules").get_data(as_text=True)


# ---- shorthand and popular searches ---------------------------------------------

def test_parse_shorthand():
    assert parse_shorthand("BIH = ball in hand\n\nSL = skill level, handicap") == [
        ["bih", "ball in hand"], ["sl", "skill level", "handicap"]]
    with pytest.raises(ValueError, match="line 2"):
        parse_shorthand("BIH = ball in hand\njust words")
    with pytest.raises(ValueError, match="line 1"):
        parse_shorthand("BIH =")
    assert parse_shorthand(rules.DEFAULT_SHORTHAND)  # the defaults are valid


def test_parse_popular_caps_the_list():
    assert parse_popular(" foul \n\nlag") == ["foul", "lag"]
    with pytest.raises(ValueError, match="12 or fewer"):
        parse_popular("\n".join(f"s{i}" for i in range(13)))


def test_director_edits_search_settings(tmp_path, monkeypatch):
    app, admin = make_app(tmp_path, monkeypatch)
    page = app.test_client().get("/rules").get_data(as_text=True)
    assert 'data-q="ball in hand"' in page and '["bih", "ball in hand"]' in page  # defaults
    admin.post("/admin/rules/search", data={"shorthand": "BIH = ball in hand\nTO = timeout", "popular": "jump cue\nlag"})
    page = app.test_client().get("/rules").get_data(as_text=True)
    assert 'data-q="jump cue"' in page and 'data-q="ball in hand"' not in page and '["to", "timeout"]' in page
    # A bad line is refused, and what was typed stays in the box.
    page = admin.post("/admin/rules/search", data={"shorthand": "oops", "popular": "lag"}).get_data(as_text=True)
    assert "Shorthand line 1" in page and ">oops</textarea>" in page
    assert SearchSettings(tmp_path).load()["shorthand"] == "BIH = ball in hand\nTO = timeout"
    assert app.test_client().post("/admin/rules/search", data={"shorthand": "", "popular": ""}).status_code == 302  # director only
    assert SearchSettings(tmp_path).load()["popular"] == "jump cue\nlag"


# ---- link check -------------------------------------------------------------------

def test_check_links_reports_problems():
    def fetch(url):
        if url == FOULS:
            return 404, url
        if url.endswith("/breaking/"):
            return 200, "https://rules.poolplayers.com/somewhere-else/"
        if url.endswith("/lagging/"):
            raise TimeoutError()
        return 200, url
    result = apa_rules.check_links(fetch)
    assert result["total"] == len(apa_rules.pages())
    problems = {b["title"]: b["problem"] for b in result["bad"]}
    assert problems == {"Fouls": "answered 404", "Breaking": "moved to https://rules.poolplayers.com/somewhere-else/",
                        "Lagging": "didn't answer (TimeoutError)"}


def test_director_runs_link_check_and_players_see_moved_pages(tmp_path, monkeypatch):
    app, admin = make_app(tmp_path, monkeypatch)
    assert "Not checked yet" in admin.get("/admin/rules").get_data(as_text=True)
    app.config["apa_link_fetch"] = lambda url: (200, url)
    page = admin.post("/admin/rules/check-links", follow_redirects=True).get_data(as_text=True)
    assert f"All {len(apa_rules.pages())} APA links open." in page
    assert "all open" in app.test_client().get("/rules").get_data(as_text=True)

    app.config["apa_link_fetch"] = lambda url: (404, url) if url == FOULS else (200, url)
    page = admin.post("/admin/rules/check-links", follow_redirects=True).get_data(as_text=True)
    assert "1 of" in page and "answered 404" in page
    public = app.test_client().get("/rules").get_data(as_text=True)
    assert f'{FOULS}" target="_blank" rel="noopener">Fouls</a> <span class="moved-note">may have moved</span>' in public
    assert "1 may have moved" in public


def test_link_check_is_stale_after_a_week(tmp_path):
    s = SearchSettings(tmp_path)
    assert s.check_is_stale()
    s.record_check({"at": "2020-01-01T00:00:00", "total": 1, "bad": []})
    assert s.check_is_stale()
    s.record_check(apa_rules.check_links(lambda url: (200, url)))
    assert not s.check_is_stale()
