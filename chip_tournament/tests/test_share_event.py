"""The event page has a "Share this event" button that shares the event page's own link."""

from app import create_app


def make_app(tmp_path, monkeypatch, site=""):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("CHIPS_PRODUCTION", raising=False)
    monkeypatch.setenv("SITE_URL", site) if site else monkeypatch.delenv("SITE_URL", raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


def test_share_button_on_both_kinds_of_event_page(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    for kind in ("bracket", "chip"):
        ev = app.config["hub"].create(f"Fall {kind}", kind)
        page = app.test_client().get(f"/t/{ev.id}/").get_data(as_text=True)
        assert "Share this event" in page and f'data-url="http://localhost/t/{ev.id}/"' in page, kind
        assert f'data-title="Fall {kind}"' in page


def test_shared_link_uses_the_site_address(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, site="https://controltable.app/")
    ev = app.config["hub"].create("Jack & Jill", "bracket")
    page = app.test_client().get(f"/t/{ev.id}/").get_data(as_text=True)
    assert f'data-url="https://controltable.app/t/{ev.id}/"' in page and 'data-title="Jack &amp; Jill"' in page
