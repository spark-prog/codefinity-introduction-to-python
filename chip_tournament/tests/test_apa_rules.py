"""The official APA page list: every link points at rules.poolplayers.com, and the Rules page shows them."""

import json

from app import create_app
from chips import apa_rules


def test_list_is_valid():
    pages = apa_rules.pages()
    assert len(pages) >= 80
    assert apa_rules.check(pages) == []
    titles = {p["title"] for p in pages}
    for must in ("Fouls", "Breaking", "Coaching", "Games Must Win Charts", "Team Skill Level Limit"):
        assert must in titles
    # Our OTM IDs follow the Chalky workbook's format.
    assert all(p["otm"] == "" or p["otm"].startswith("OTM-") for p in pages)


def test_check_catches_bad_links():
    good = {"title": "Fouls", "section": "Game Rules", "url": "https://rules.poolplayers.com/game-rules/fouls/"}
    assert apa_rules.check([good]) == []
    assert "listed twice" in apa_rules.check([good, dict(good)])[0]
    assert "must link" in apa_rules.check([{**good, "url": "https://example.com/fouls/"}])[0]
    assert "must link" in apa_rules.check([{**good, "url": "https://rules.poolplayers.com/game-rules/fouls"}])[0]
    assert "needs a title" in apa_rules.check([{**good, "title": " "}])[0]


def test_sections_keep_menu_order():
    names = [name for name, _ in apa_rules.sections()]
    assert names[0] == "Getting Started" and names.index("General Rules") < names.index("Game Rules")


def test_rules_pages_list_apa_links(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    app = create_app(str(tmp_path))
    app.testing = True
    public = app.test_client()
    ev = app.config["hub"].create("Fun Night", "bracket")
    for path in ("/rules", f"/t/{ev.id}/rules"):
        page = public.get(path).get_data(as_text=True)
        assert 'id="apa"' in page and 'href="https://rules.poolplayers.com/game-rules/fouls/"' in page
        assert 'id="rule-search"' in page  # search is there even before any rules are posted
        data = page.split('<script id="apa-pages" type="application/json">', 1)[1].split("</script>", 1)[0]
        assert len(json.loads(data)) == len(apa_rules.pages())
