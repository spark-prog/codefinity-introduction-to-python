"""The director's team spreadsheet."""

import csv
import io
import random

from app import create_app
from chips.engine import Player


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.setenv("VIEWER_PASSWORD", "lookonly1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    return app, admin


def read(resp):
    return list(csv.DictReader(io.StringIO(resp.get_data(as_text=True).lstrip("﻿"))))


def test_chip_export_has_every_team_with_pin_and_contacts(tmp_path, monkeypatch):
    app, admin = setup(tmp_path, monkeypatch)
    ev = app.config["hub"].create("Chips", "chip")
    with ev.store.change("setup") as t:
        a = t.register_team("Sharks", [Player("Ann Lee", "5551234567", 4, True), Player("Bob Ray", "5559876543", 5)])
        t.register_team("=SUM(1)", [Player("Cy", "5550001111", 3, True), Player("Dee", "", 3)])
        t.register_team("", [Player("Eve", "5550002222", 4, True)])  # still looking for a partner
    assert b"Download teams (CSV)" in admin.get(f"/t/{ev.id}/admin").data
    r = admin.get(f"/t/{ev.id}/admin/export.csv")
    assert r.mimetype == "text/csv" and "attachment" in r.headers["Content-Disposition"]
    rows = read(r)
    assert [x["Team"] for x in rows] == ["'=SUM(1)", "Eve", "Sharks"]  # A to Z; a formula is kept as text
    sharks = rows[2]
    assert sharks["PIN"] == f'="{a.pin}"' and sharks["Status"] == "registered"  # keeps a leading 0 in spreadsheets
    assert sharks["Player 1 name"] == "Ann Lee" and sharks["Player 1 cell"] == "5551234567" and sharks["Player 1 texts on"] == "yes"
    assert sharks["Player 2 cell"] == "5559876543" and sharks["Player 2 texts on"] == "no" and sharks["Combined skill"] == "9"
    assert rows[1]["Needs partner"] == "yes" and rows[1]["Player 2 name"] == ""
    # view-only and players can't download it
    viewer = app.test_client()
    viewer.post("/admin/login", data={"password": "lookonly1"})
    assert viewer.get(f"/t/{ev.id}/admin/export.csv").status_code == 302
    assert app.test_client().get(f"/t/{ev.id}/admin/export.csv").status_code == 302


def test_bracket_export_includes_skill_levels_and_groups(tmp_path, monkeypatch):
    app, admin = setup(tmp_path, monkeypatch)
    ev = app.config["hub"].create("Movin-On-Up", "bracket")
    with ev.store.change("setup") as t:
        t.settings.skill_groups, t.settings.race_method = [[1, 3], [4, 4], [5, 9]], "rack_chart"
        e = t.register_entry("", [Player("Ann", "5551234567", 0, True, skill_8=4, skill_9=6)])
    rows = read(admin.get(f"/t/{ev.id}/admin/export.csv"))
    assert rows[0]["Entry"] == "Ann" and rows[0]["PIN"] == f'="{e.pin}"' and rows[0]["Skill group"] == "SL 5–9"
    assert rows[0]["Player 1 8-Ball SL"] == "4" and rows[0]["Player 1 9-Ball SL"] == "6" and rows[0]["Player 1 skill"] == "6"
    assert b"Download entries (CSV)" in admin.get(f"/t/{ev.id}/admin").data
