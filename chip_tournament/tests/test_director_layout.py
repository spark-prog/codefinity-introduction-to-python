"""The director page: Start and Undo in the top menu, the Summary at the bottom, settings in closed fold-out
sections, and a director-only menu."""

import re

import pytest

from app import create_app
from chips.engine import Player
from tests.test_phase7 import nested_forms


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.setenv("VIEWER_PASSWORD", "viewerpw1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    hub = app.config["hub"]
    chip = hub.create("Masquer", "chip")
    t = chip.load()
    t.settings.require_checkin = True
    t.register_team("Sharks", [Player("Ann Lee", "9045550100", 4), Player("Cy Day", "", 3)])
    chip.store.save(t)
    hub.create("Singles Board", "bracket", format="double")
    return app


def login(app, password="secretpw1"):
    c = app.test_client()
    c.post("/admin/login", data={"password": password})
    return c


def jump_nav(page):
    return page.split('<nav class="jump-nav"', 1)[1].split("</nav>", 1)[0]


SETTING_KEYS = {"masquer": ["look", "event", "registration", "who", "rules", "money", "tables", "texts", "chips"],
                "singles-board": ["event", "registration", "who", "rules", "format", "races", "money", "tables", "texts"]}


@pytest.mark.parametrize("event", ["masquer", "singles-board"])
def test_start_and_undo_in_the_top_menu_and_the_summary_at_the_bottom(app, event):
    page = login(app).get(f"/t/{event}/admin").get_data(as_text=True)
    nav = jump_nav(page)
    assert "admin/start" in nav and "admin/undo" in nav  # the two actions ride in the top menu
    links = re.findall(r'<a href="#[^"]+"[^>]*>([^<]+)</a>', nav)
    assert links[-1] == "Summary" and "Settings" in links
    tops = re.findall(r'<(?:section|div)[^>]*class="[^"]*jump-target[^"]*"[^>]*id="([^"]+)"|<(?:section|div)[^>]*id="([^"]+)"[^>]*class="[^"]*jump-target', page)
    ids = [a or b for a, b in tops]
    assert ids.index("summary") > ids.index("settings") and ids.index("summary") > ids.index("new-tournament")
    assert nested_forms(page) == []


def test_mark_all_not_checked_in_lives_in_teams_and_chips(app):
    page = login(app).get("/t/masquer/admin").get_data(as_text=True)
    teams = page.split('<section id="teams"', 1)[1].split("</section>", 1)[0]
    assert "Mark all 1 not checked in as no-shows" in teams
    assert page.count("not checked in as no-shows") == 1  # not in the summary any more
    assert "disabled title=\"Every team must be checked in" in jump_nav(page)  # Start waits for check-in


@pytest.mark.parametrize("event", ["masquer", "singles-board"])
def test_settings_are_closed_fold_out_sections_with_every_field(app, event):
    page = login(app).get(f"/t/{event}/admin").get_data(as_text=True)
    form = page[page.index(f'action="/t/{event}/admin/settings"'):]  # the settings form itself
    form = form[:form.index("</form>")]
    keys = re.findall(r'<details class="settings-section" data-key="([a-z]+)">', form)
    assert keys == SETTING_KEYS[event]
    assert "<details class=\"settings-section\" data-key" in form and "open" not in re.findall(r"<details[^>]*>", form)[0]
    assert 'class="settings-find"' in form and 'data-settings="open"' in form and 'data-settings="close"' in form
    for name in ("name", "date", "start_time", "venue", "registration_closed", "require_member", "waitlist_offer_hours",
                 "registration_opens", "registration_closes", "skill_game", "rule_set", "event_rules", "entry_fee",
                 "added_money", "field_cap", "tables", "call_mode", "test_event", "sms_name", "theme"):
        assert f'name="{name}"' in form, (event, name)
    assert 'id="unsaved-bar"' in page and "ct-open:" in page
    assert "Texts &amp; log</h2>" in page.split('id="texts"', 1)[1][:400] and 'class="fold-section"' in page.split('id="texts"', 1)[1][:200]


def test_saving_with_every_section_closed_keeps_the_settings(app):
    """Fields in a closed section are still sent: one Save keeps everything."""
    c = login(app)
    ev = app.config["hub"].get("singles-board")
    page = c.get("/t/singles-board/admin").get_data(as_text=True)
    form = page[page.index('id="settings"'):]
    form = form[form.index("<form"):form.index("</form>")]
    data = {}
    for m in re.finditer(r"<(input|select|textarea)\b([^>]*)>(.*?)(?=<)", form, re.S):
        tag, attrs = m.group(1), m.group(2)
        n = re.search(r'name="([^"]+)"', attrs)
        if not n or "disabled" in attrs:
            continue
        if 'type="checkbox"' in attrs and "checked" not in attrs:
            continue
        if tag == "select":
            sel = form[m.start():form.index("</select>", m.start())]
            opt = re.search(r'<option value="([^"]*)"[^>]*selected', sel) or re.search(r'<option value="([^"]*)"', sel)
            data[n.group(1)] = opt.group(1) if opt else ""
        elif tag == "textarea":
            data[n.group(1)] = form[m.end(0) - len(m.group(3)):form.index("</textarea>", m.start())].split(">", 1)[-1]
        else:
            v = re.search(r'value="([^"]*)"', attrs)
            data.setdefault(n.group(1), v.group(1) if v else "")
    before = ev.load().settings
    data["venue"] = "Park Ave"
    c.post("/t/singles-board/admin/settings", data=data)
    after = ev.load().settings
    assert after.venue == "Park Ave"
    for k in ("name", "format", "team_size", "race_winners", "race_losers", "race_finals", "entry_fee_cents", "tables",
              "call_mode", "waitlist_offer_hours", "require_member", "skill_game"):
        assert getattr(after, k) == getattr(before, k), k


def test_director_only_menu_and_view_only(app):
    c = login(app)
    assert c.get("/").headers["Location"].endswith("/admin")
    page = c.get("/t/singles-board/admin").get_data(as_text=True)
    nav = page.split('<nav class="nav', 1)[1].split("</nav>", 1)[0]
    assert re.findall(r">([^<>]+)</a>", nav) == ["Events", "Singles Board", "Bracket ↗", "Players", "Rules", "Backups"]
    assert "Register" not in nav and "Log in" not in nav and "View as player ↗" in page
    viewer = login(app, "viewerpw1")
    page = viewer.get("/t/singles-board/admin").get_data(as_text=True)
    nav = page.split('<nav class="nav', 1)[1].split("</nav>", 1)[0]
    assert "Players" in nav and "Rules" not in nav  # the rules editor is for the director only
    assert "admin/start" not in jump_nav(page) and "admin/undo" not in jump_nav(page)
    assert "data-viewer-ok" in page  # Find and Open/Close all still work for view-only
