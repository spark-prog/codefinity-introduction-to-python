"""The player store: reading the office's player Sheet, checking it, and syncing it safely."""

import io
import json
from datetime import datetime, timedelta

import pytest

from app import create_app
from chips import players
from chips.backup import BackupError

HEADERS = ["Member Number", "First Name", "Nickname", "Last Name", "9-Ball Skill Level", "8-Ball Skill Level",
           "Total Matches Played 9-Ball", "Total Matches Played 8-Ball", "Last Date Played 9-Ball",
           "Last Date Played 8-Ball", "Member Status", "Cell Phone", "Home Phone", "Date Started", "Division Rep",
           "Birth Date", "EmailAddress", "Session Defense", "Gender"]


def row(member, first="Ann", last="Lee", sl9="5", sl8="4", email=None, **kw):
    values = {"Member Number": member, "First Name": first, "Nickname": kw.get("nick", ""), "Last Name": last,
              "9-Ball Skill Level": sl9, "8-Ball Skill Level": sl8, "Total Matches Played 9-Ball": kw.get("mp9", "12"),
              "Total Matches Played 8-Ball": kw.get("mp8", "1,204"), "Last Date Played 9-Ball": "10/30/2026",
              "Last Date Played 8-Ball": "2026-10-28", "Member Status": "Active", "Cell Phone": "(904) 555-0100",
              "Home Phone": "904-555-9999", "Date Started": "3/1/2019", "Division Rep": "", "Birth Date": "1/2/1980",
              "EmailAddress": email if email is not None else f"{first.lower()}{member[-3:]}@example.com",
              "Session Defense": "0.25", "Gender": "F"}
    return [values[h] for h in HEADERS]


def sheet(n=40, extra=()):
    return [HEADERS] + [row(f"322{i:05d}", first=f"P{i}") for i in range(1, n + 1)] + list(extra)


def test_rows_are_checked_and_private_columns_dropped():
    rows = sheet(3, extra=[
        row("3221234", first="Short"),               # 7 digits
        row("32100009", first="Wrong"),              # not 322
        row("32200001", first="Dupe"),               # repeat of row 2
        row("32200050", first="Bad", sl8="9", sl9="x"),
        row("32200051", first="Twin", email="p1001@example.com"),  # same email as P1
        ["", "", ""],                                # blank row
    ])
    records, problems = players.parse_rows(rows)
    assert set(records) == {"32200001", "32200002", "32200003", "32200050", "32200051"}
    p1 = records["32200001"]
    assert p1["sl8"] == 4 and p1["sl9"] == 5 and p1["mp8"] == 1204 and p1["cell"] == "9045550100"
    assert (p1["last9"], p1["last8"], p1["started"]) == ("2026-10-30", "2026-10-28", "2019-03-01")
    stored = json.dumps(records)
    assert "9999" not in stored and "1980" not in stored and "0.25" not in stored  # home phone, birth date, defense
    text = "\n".join(problems)
    assert "'3221234' isn't 8 digits starting with 322" in text and "'32100009'" in text
    assert "32200001 is already on row 2" in text
    assert records["32200050"]["sl8"] is None and "8-Ball SL '9' (must be 2–7)" in text and "9-Ball SL 'x'" in text
    assert "p1001@example.com is also on member 32200001" in text


def test_missing_columns_change_nothing():
    rows = [[h for h in HEADERS if h != "EmailAddress"]]
    with pytest.raises(players.PlayerError, match="missing these columns: EmailAddress"):
        players.parse_rows(rows)
    with pytest.raises(players.PlayerError, match="No usable player rows"):
        players.parse_rows([HEADERS, row("123")])


def test_sheet_links():
    assert players.sheet_id("https://docs.google.com/spreadsheets/d/1qX8oouXbEymd4MTfuH5z1bI4GqHu5n_aS4UXLyks-_o/edit?usp=drive_link") \
        == "1qX8oouXbEymd4MTfuH5z1bI4GqHu5n_aS4UXLyks-_o"
    with pytest.raises(players.PlayerError, match="isn't a Google Sheet link"):
        players.sheet_id("https://drive.google.com/drive/folders/abc")


def test_sync_diffs_and_holds_a_big_removal(tmp_path):
    store = players.PlayerStore(tmp_path)
    first = store.apply(*players.parse_rows(sheet(100)), "Google Sheet", "automatic")
    assert first["status"] == "synced" and store.status()["count"] == 100

    changed = sheet(100)
    changed[5][HEADERS.index("8-Ball Skill Level")] = "6"
    changed.append(row("32200500", first="New"))
    del changed[50]  # one player left
    entry = store.apply(*players.parse_rows(changed), "Google Sheet", "director")
    assert (entry["added"], entry["updated"], entry["removed"]) == (1, 1, 1)
    assert "8-Ball SL" in entry["names"]["updated"][0]

    gutted = sheet(80)  # an edit that loses 21 players at once
    held = store.apply(*players.parse_rows(gutted), "Google Sheet", "automatic")
    assert held["status"] == "held" and store.status()["count"] == 100  # the old list stays in use
    assert store.held() and not store.due()  # no automatic syncs while one is held
    store.discard_held()
    assert store.held() is None
    store.apply(*players.parse_rows(gutted), "Google Sheet", "automatic")
    store.approve_held("director")
    assert store.status()["count"] == 80 and store.held() is None


def test_quiet_checks_dont_fill_the_log(tmp_path):
    store = players.PlayerStore(tmp_path)
    for _ in range(3):
        store.apply(*players.parse_rows(sheet(5)), "Google Sheet", "automatic")
    log = store.log()
    assert len(log) == 1 and log[0].get("checked_at")


class FakeReader:
    def __init__(self, rows=None, error=None):
        self._rows, self.error = rows, error

    def rows(self, sid, tab):
        if self.error:
            raise players.PlayerError(self.error)
        assert tab == "Current Player Records"
        return self._rows

    def modified(self, sid):
        return "2026-11-02T14:00:00.000Z"


def test_sync_from_the_sheet_and_failures(tmp_path):
    store = players.PlayerStore(tmp_path)
    with pytest.raises(players.PlayerError, match="Link the player sheet"):
        store.sync_sheet("director", FakeReader(sheet(3)))
    store.set_sheet("https://docs.google.com/spreadsheets/d/SHEET1234567890abcdefXYZ/edit")
    with pytest.raises(players.PlayerError, match="no tab"):
        store.sync_sheet("automatic", FakeReader(error="The sheet has no tab named 'Current Player Records'."))
    assert "no tab" in store.status()["error"]["error"]
    assert not store.due()  # waits 15 minutes before trying again
    assert store.due(datetime.now() + timedelta(minutes=16))
    store.sync_sheet("director", FakeReader(sheet(3)))
    st = store.status()
    assert st["count"] == 3 and st["error"] is None and st["sheet_modified"].startswith("2026-11-02")
    assert store.get("32200002")["first"] == "P2" and store.by_email("P3003@example.com")["member"] == "32200003"
    assert [r["member"] for r in store.search("p2")] == ["32200002"] and store.search("200003")[0]["first"] == "P3"


def test_sheet_reader_asks_google_for_the_tab_and_explains_errors():
    calls = []

    class FakeDrive:
        email = "app@ct.iam.gserviceaccount.com"

        def _request(self, req, auth=True, raw=False):
            calls.append(req.full_url)
            if "fail" in req.full_url:
                raise BackupError("Google Drive refused access. Add ... (403)")
            return {"values": [HEADERS]}

    reader = players.SheetReader(FakeDrive())
    assert reader.rows("SHEET") == [HEADERS]
    assert "/spreadsheets/SHEET/values/%27Current%20Player%20Records%27" in calls[0]
    with pytest.raises(players.PlayerError, match="won't let Control Table open the sheet"):
        reader.rows("fail")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.setenv("VIEWER_PASSWORD", "viewpw123")
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


def csv_bytes(rows):
    out = io.StringIO()
    import csv
    csv.writer(out).writerows(rows)
    return out.getvalue().encode("cp1252")


def test_players_page(app):
    admin, viewer, anon = app.test_client(), app.test_client(), app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    viewer.post("/admin/login", data={"password": "viewpw123"})
    assert anon.get("/admin/players").status_code == 302
    for path in ("/admin/players/sync", "/admin/players/sheet", "/admin/players/upload", "/admin/players/held"):
        assert viewer.post(path).status_code == 302 and anon.post(path).status_code == 302
    assert "Players" in admin.get("/admin").get_data(as_text=True)

    r = admin.post("/admin/players/sheet", data={"sheet": "not a link"}, follow_redirects=True)
    assert "isn&#39;t a Google Sheet link" in r.get_data(as_text=True)
    r = admin.post("/admin/players/sheet", data={"sheet": "https://docs.google.com/spreadsheets/d/SHEET1234567890abcdefXYZ/edit"},
                   follow_redirects=True)
    assert "GOOGLE_SERVICE_ACCOUNT_JSON isn&#39;t set" in r.get_data(as_text=True)

    r = admin.post("/admin/players/upload", data={"file": (io.BytesIO(csv_bytes(sheet(4, [row("123")]))), "p.csv")},
                   content_type="multipart/form-data", follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "Synced 4 players: 4 added" in page and "1 row(s) need a look" in page and "isn&#39;t 8 digits" in page
    page = viewer.get("/admin/players?q=p3").get_data(as_text=True)
    assert "32200003" in page and "SL 4" in page and "9999" not in page
