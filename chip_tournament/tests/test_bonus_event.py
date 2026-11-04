"""Phase 2: create a tournament from the Bonus Event form's export."""

import copy
import io
import json
from pathlib import Path

import pytest

from app import create_app
from chips import bonus_event
from chips.bracket import MODIFIED
from chips.race_charts import RACK_RACE
from chips.engine import TournamentError
from chips.rules import DEFAULT_RULE_SETS, slugify

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "bonus_event_halloween.json").read_text())
RULES = [{"id": slugify(t), "title": t, "body": ""} for t in DEFAULT_RULE_SETS]


def test_halloween_doubles_becomes_a_chip_tournament():
    p = bonus_event.plan(copy.deepcopy(SAMPLE), RULES)
    s = p.settings
    assert p.kind == "chip" and p.name == "Halloween Doubles" and p.warnings == []
    assert (s["date"], s["start_time"], s["checkin_by"], s["venue"]) == ("2026-10-31", "9:00 PM", "8:30 PM", "Park Ave")
    assert s["race_to"] == 2  # maximum 3 racks
    assert (s["entry_fee_cents"], s["field_cap"], s["registration_cap"]) == (4000, 55, 10)
    assert s["tables"] == [str(i) for i in range(1, 17)]
    assert (s["registration_opens"], s["registration_closes"]) == ("2026-09-30T00:00", "2026-10-28T23:59")
    assert s["registration_closed"] is True
    assert s["rule_set"] == "fast-saratoga-rules"
    assert "Practice tables open at 8:00 PM." in s["details"] and "Payment: Cash." in s["details"]
    assert "Prizes: Bonus Event tee" in s["details"] and "Check-in: 30 minutes before start time." in s["details"]
    assert "- No Sudden Death." in s["event_rules"] and "wheel of torture" in s["event_rules"]
    assert "- No Refunds." in s["event_rules"] and "racks won, timeouts" in s["event_rules"]


def test_a_skill_group_bracket():
    doc = copy.deepcopy(SAMPLE)
    doc["format"].update(tournament_structure="Modified Single Elimination", players_per_entry=1,
                         race_method="Rack Race Chart", grouping_method="Skill Level Groups")
    doc["registration"]["entry_fee"] = 0
    doc["source"]["approval_status"] = "Pending"
    p = bonus_event.plan(doc, RULES)
    s = p.settings
    assert p.kind == "bracket" and s["format"] == MODIFIED and s["team_size"] == 1 and s["race_method"] == RACK_RACE
    assert s["entry_fee_cents"] == 0 and "race_winners" not in s
    text = " ".join(p.warnings)
    assert "skill groups" in text and "Payouts" in text and "not Approved" in text.replace("'Pending', not", "not")


@pytest.mark.parametrize("change, message", [
    (lambda d: d.update(schema_version="other"), "isn't a Bonus Event export"),
    (lambda d: d["format"].update(tournament_structure="Swiss"), "can't run a 'Swiss'"),
    (lambda d: d["event"].update(date=None), "no event date"),
])
def test_unusable_exports_are_refused(change, message):
    doc = copy.deepcopy(SAMPLE)
    change(doc)
    with pytest.raises(TournamentError, match=message):
        bonus_event.plan(bonus_event.read(json.dumps(doc)), RULES)
    with pytest.raises(TournamentError, match="isn't JSON"):
        bonus_event.read(b"not json")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


def upload(client, doc, test=False):
    data = {"file": (io.BytesIO(json.dumps(doc).encode()), "export.json")}
    if test:
        data["test_event"] = "1"
    return client.post("/admin/new/bonus-event", data=data, content_type="multipart/form-data")


def test_upload_preview_then_create(app):
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    assert 'id="from-form"' in admin.get("/admin").get_data(as_text=True)
    page = upload(admin, SAMPLE, test=True).get_data(as_text=True)
    assert "Halloween Doubles" in page and "Race to 2" in page and "Fast Saratoga Rules" in page
    assert app.config["hub"].events() == []  # nothing made yet
    r = admin.post("/admin/new/bonus-event/create", data={"raw": json.dumps(SAMPLE), "test_event": "1"})
    assert r.status_code == 302
    ev = app.config["hub"].get("halloween-doubles")
    s = ev.load().settings
    assert s.race_to == 2 and s.registration_cap == 10 and s.registration_closed and s.test_event
    assert s.bonus_event_id == "sample-halloween-0001"
    assert json.loads((ev.dir / bonus_event.SAVED_AS).read_text())["eligibility"]["requirements"]
    # The same form entry can't be made twice.
    r = upload(admin, SAMPLE)
    assert r.status_code == 302 and len(app.config["hub"].events()) == 1
    page = admin.get("/admin").get_data(as_text=True)
    assert "already made into" in page


def test_only_the_director_can_import(app):
    assert upload(app.test_client(), SAMPLE).status_code == 302
    assert app.config["hub"].events() == []
