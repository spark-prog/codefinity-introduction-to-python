"""Bracket and round robin players reach their entry page and keep score with their PIN."""

import random

import pytest

from app import create_app
from chips.engine import Player


@pytest.mark.parametrize("fmt", ["single", "double", "round_robin"])
def test_entry_finds_its_page_and_scores_with_pin(tmp_path, monkeypatch, fmt):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "VIEWER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    ev = app.config["hub"].create("Bracket", "bracket")
    with ev.store.change("setup") as t:
        t.settings.format, t.settings.tables = fmt, ["1", "2"]
        for name in ("Ann", "Bob", "Cy", "Di"):
            t.register_entry(name, [Player(name)])
        t.start(random.Random(1))
    t = ev.load()
    m = next(x for x in t.matches if x.status == "called")
    entry = t.entries[m.a]
    player = app.test_client()
    for path in ("/", "/board"):
        assert f'href="/t/{ev.id}/entry/{entry.id}"' in player.get(f"/t/{ev.id}{path}").get_data(as_text=True)
    assert player.get(f"/t/{ev.id}/find?team={entry.id}").headers["Location"] == f"/t/{ev.id}/entry/{entry.id}"

    page = player.get(f"/t/{ev.id}/entry/{entry.id}").get_data(as_text=True)
    assert "Enter your PIN to keep score" in page and "Enter scores for your match" not in page
    player.post(f"/t/{ev.id}/entry/{entry.id}/score", data={"side": "a", "delta": "1"})
    assert ev.load().match(m.id).score_a == 0  # no PIN, no scoring
    player.post(f"/t/{ev.id}/entry/{entry.id}/login", data={"pin": entry.pin})
    player.post(f"/t/{ev.id}/entry/{entry.id}/score", data={"side": "a", "delta": "1"})
    assert ev.load().match(m.id).score_a == 1
