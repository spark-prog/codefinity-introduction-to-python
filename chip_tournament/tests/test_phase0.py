"""Phase 0: test events (no texts, hidden) and nightly backups (Drive, email fallback, restore)."""

import io
import json
import urllib.error
import zipfile
from datetime import datetime

import pytest

from app import create_app
from chips import backup
from chips.alerts import SmsSender
from chips.engine import Player


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.setenv("VIEWER_PASSWORD", "viewpw123")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "GOOGLE_SERVICE_ACCOUNT_JSON",
                "BACKUP_DRIVE_FOLDER_ID", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "BACKUP_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


def login(app, password="secretpw1"):
    c = app.test_client()
    c.post("/admin/login", data={"password": password})
    return c


# ------------------------------------------------------------------ test events

def test_a_test_event_never_texts_and_is_hidden_from_the_public(app, monkeypatch):
    admin = login(app)
    admin.post("/admin/new", data={"name": "Practice Night", "kind": "chip", "test_event": "1"})
    admin.post("/admin/new", data={"name": "Real Night", "kind": "chip"})
    hub = app.config["hub"]
    practice, real = hub.get("practice-night"), hub.get("real-night")
    assert practice.load().settings.test_event and not real.load().settings.test_event

    public = app.test_client().get("/").get_data(as_text=True)
    assert "Real Night" in public and "Practice Night" not in public
    assert "Practice Night" in admin.get("/").get_data(as_text=True)
    assert ">TEST<" in admin.get("/admin").get_data(as_text=True)

    page = app.test_client().get("/t/practice-night/").get_data(as_text=True)  # a direct link still works
    assert "TEST EVENT" in page and 'name="robots" content="noindex"' in page

    sent = []
    monkeypatch.setattr(SmsSender, "_send_twilio", lambda self, to, body: sent.append(to) or "sent (201)")
    monkeypatch.setattr(SmsSender, "enabled", property(lambda self: True))
    with practice.store.change("setup") as t:
        t.register_team("Sharks", [Player("Ann Lee", "5551110000", 5, sms_consent=True), Player("Bob Ray", "", 5)])
    admin.post("/t/practice-night/admin/text-pins")
    import time
    for _ in range(50):
        if practice.sms.recent():
            break
        time.sleep(0.02)
    assert sent == [] and practice.sms.recent()[0]["status"] == "not sent (test event)"


def test_settings_turn_test_mode_on_and_off(app):
    admin = login(app)
    admin.post("/admin/new", data={"name": "Chips", "kind": "chip"})
    admin.post("/admin/new", data={"name": "Bracket", "kind": "single"})
    hub = app.config["hub"]
    for ev_id in ("chips", "bracket"):
        page = admin.get(f"/t/{ev_id}/admin").get_data(as_text=True)
        assert 'name="test_event"' in page
    s = hub.get("chips").load().settings
    form = {"test_event_shown": "1", "test_event": "1", "name": "Chips", "race_to": "3", "tables": "1, 2",
            "entry_fee": "0", "added_money": "0", "registration_cap": "0", "chip_brackets": "8 = 4\n11 = 2", "field_cap": "0",
            "no_rack_minutes": "15", "match_minutes": str(s.match_minutes), "long_match_minutes": str(s.long_match_minutes),
            "catchup_matches": str(s.catchup_matches), "call_mode": "director"}
    admin.post("/t/chips/admin/settings", data=form)
    assert hub.get("chips").load().settings.test_event
    form.pop("test_event")
    admin.post("/t/chips/admin/settings", data=form)
    assert not hub.get("chips").load().settings.test_event


# --------------------------------------------------------------------- the zip

def make_data(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "secret_key").write_text("server-key")
    (root / backup.STATUS_FILE).write_text("{}")
    (root / "events.json").write_text(json.dumps([{"id": "night", "kind": "chip"}]))
    (root / "events" / "night").mkdir(parents=True)
    (root / "events" / "night" / "tournament.json").write_text('{"x": 1}')
    (root / "rules.json").write_text("[]")


def test_zip_has_everything_but_the_secret_key(tmp_path):
    make_data(tmp_path)
    names = zipfile.ZipFile(io.BytesIO(backup.make_zip(tmp_path))).namelist()
    assert {"events.json", "events/night/tournament.json", "rules.json", backup.MANIFEST} <= set(names)
    assert "secret_key" not in names and backup.STATUS_FILE not in names


def zip_of(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


@pytest.mark.parametrize("files, message", [
    ({"x.txt": "hi"}, "isn't a Control Table backup"),
    ({backup.MANIFEST: "{}", "events.json": "[]", "../evil": "x"}, "shouldn't be"),
    ({backup.MANIFEST: "{}", "events.json": "[]", "secret_key": "stolen"}, "shouldn't be"),
    ({backup.MANIFEST: "{}", "events.json": '[{"id": "../x"}]'}, "bad name"),
    ({backup.MANIFEST: "{}", "events.json": "not json"}, "damaged"),
])
def test_bad_backups_are_refused(tmp_path, files, message):
    with pytest.raises(backup.BackupError, match=message):
        backup.restore_zip(tmp_path, zip_of(files))
    with pytest.raises(backup.BackupError, match="not a zip"):
        backup.describe(b"plain text")


def test_restore_puts_everything_back_and_keeps_a_copy_first(tmp_path):
    make_data(tmp_path)
    saved = backup.make_zip(tmp_path)
    (tmp_path / "events" / "night" / "tournament.json").write_text('{"x": 2}')
    (tmp_path / "stray.txt").write_text("made after the backup")
    info = backup.restore_zip(tmp_path, saved)
    assert info["events"] == 1
    assert (tmp_path / "events" / "night" / "tournament.json").read_text() == '{"x": 1}'
    assert not (tmp_path / "stray.txt").exists()
    assert (tmp_path / "secret_key").read_text() == "server-key"  # never replaced
    safety = list((tmp_path / backup.SAFETY_DIR).glob("before-restore-*.zip"))
    assert len(safety) == 1 and b"made after the backup" in zipfile.ZipFile(safety[0]).read("stray.txt")


# ---------------------------------------------------------------- the schedule

def test_nightly_schedule():
    at = datetime(2026, 11, 2, 2, 59)
    assert not backup.due({}, at)  # before 3 AM
    at = datetime(2026, 11, 2, 3, 5)
    assert backup.due({}, at)
    assert not backup.due({"last_ok": "2026-11-02T03:01:00"}, at)  # already done today
    assert backup.due({"last_ok": "2026-11-01T03:01:00"}, at)  # missed while the app was down: catch up
    failed = {"last_ok": "2026-11-01T03:01:00", "last_try": "2026-11-02T03:01:00"}
    assert not backup.due(failed, datetime(2026, 11, 2, 3, 30))  # retries hourly, not every few minutes
    assert backup.due(failed, datetime(2026, 11, 2, 4, 2))


def test_folder_id_from_a_pasted_address():
    assert backup.folder_id("https://drive.google.com/drive/u/0/folders/1vnPPpE6AluM9Hv75NZhlzRk74CioYyrB") \
        == "1vnPPpE6AluM9Hv75NZhlzRk74CioYyrB"
    assert backup.folder_id("https://drive.google.com/drive/folders/abc_D-1?usp=sharing") == "abc_D-1"
    assert backup.folder_id(" abc123 ") == "abc123"


# ------------------------------------------------------------------- Drive

class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def service_account():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return {"type": "service_account", "client_email": "backup@ct.iam.gserviceaccount.com",
            "private_key": pem, "private_key_id": "k1",
            "token_uri": "https://oauth2.googleapis.com/token"}


def test_drive_signs_in_uploads_lists_and_prunes(monkeypatch):
    from google.auth import jwt as gjwt

    calls = []
    files = [{"id": f"f{i}", "name": f"control-table-2026-10-{i:02d}-0300.zip"} for i in range(1, 5)]
    files.append({"id": "hand", "name": "before-restore-2026-10-01-1200.zip"})

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        if req.full_url.startswith("https://oauth2.googleapis.com/token"):
            form = dict(x.split("=", 1) for x in req.data.decode().split("&"))
            claims = gjwt.decode(form["assertion"].replace("%2E", "."), verify=False)
            assert claims["iss"] == "backup@ct.iam.gserviceaccount.com" and "auth/drive" in claims["scope"]
            return FakeResponse(json.dumps({"access_token": "tok", "expires_in": 3600}).encode())
        assert req.get_header("Authorization") == "Bearer tok"
        if req.get_method() == "GET":
            return FakeResponse(json.dumps({"files": files}).encode())
        return FakeResponse(b'{"id": "new"}' if req.get_method() == "POST" else b"")

    monkeypatch.setattr(backup.urllib.request, "urlopen", fake_urlopen)
    drive = backup.Drive(json.dumps(service_account()), "https://drive.google.com/drive/folders/FOLDER1")
    drive.upload("control-table-2026-11-02-0300.zip", b"ZIPDATA")
    upload = calls[-1]
    assert "uploadType=multipart" in upload.full_url and "supportsAllDrives=true" in upload.full_url
    assert b'"parents": ["FOLDER1"]' in upload.data and b"ZIPDATA" in upload.data
    assert drive.prune(keep=2) == 2
    deleted = [c.full_url.split("/files/")[1].split("?")[0] for c in calls if c.get_method() == "DELETE"]
    assert deleted == ["f3", "f4"]  # oldest nightly ones only; the hand-made one stays
    assert sum(c.full_url.startswith("https://oauth2") for c in calls) == 1  # the sign-in is reused


def test_drive_errors_say_what_to_fix(monkeypatch):
    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                     io.BytesIO(b'{"error": {"errors": [{"reason": "storageQuotaExceeded"}]}}'))

    monkeypatch.setattr(backup.urllib.request, "urlopen", refuse)
    drive = backup.Drive(json.dumps({"client_email": "b@x"}), "F")
    drive._token, drive._expires = "tok", 9e12
    with pytest.raises(backup.BackupError, match="shared drive"):
        drive.upload("a.zip", b"x")
    with pytest.raises(backup.BackupError, match="isn't a service account key"):
        backup.Drive("not json", "F")


# ------------------------------------------------------------ run and fallback

class FakeDrive:
    def __init__(self, fail=None):
        self.fail, self.uploaded = fail, []

    def upload(self, name, data):
        if self.fail:
            raise backup.BackupError(self.fail)
        self.uploaded.append(name)

    def prune(self):
        return 0


class FakeMailer:
    to = "office@example.com"

    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    def send(self, subject, text, attachment=None):
        if self.fail:
            raise backup.BackupError("Gmail is down")
        self.sent.append((subject, attachment[0] if attachment else None))


def test_backup_goes_to_drive(tmp_path):
    make_data(tmp_path)
    drive, mail = FakeDrive(), FakeMailer()
    status = backup.run(tmp_path, drive=drive, mailer=mail)
    assert status["last_where"] == "Google Drive" and not status["error"] and drive.uploaded and not mail.sent
    assert backup.load_status(tmp_path)["last_ok"] == status["last_ok"]


def test_drive_failure_falls_back_to_email(tmp_path):
    make_data(tmp_path)
    mail = FakeMailer()
    status = backup.run(tmp_path, drive=FakeDrive(fail="Drive is down"), mailer=mail)
    assert status["last_where"] == "email" and "Drive is down" in status["warning"]
    assert mail.sent[0][1].startswith("control-table-")


def test_total_failure_is_reported_and_warned_once_a_day(tmp_path):
    make_data(tmp_path)

    class HalfMailer(FakeMailer):  # can send a warning but the attachment fails
        def send(self, subject, text, attachment=None):
            if attachment:
                raise backup.BackupError("too big")
            self.sent.append(subject)

    mail = HalfMailer()
    status = backup.run(tmp_path, drive=FakeDrive(fail="Drive is down"), mailer=mail)
    assert "Drive is down" in status["error"] and not status.get("last_ok")
    backup.run(tmp_path, drive=FakeDrive(fail="Drive is down"), mailer=mail)
    assert mail.sent == ["Control Table backup FAILED"]  # one warning, not one per try


def test_nothing_set_up(tmp_path):
    make_data(tmp_path)
    status = backup.run(tmp_path)
    assert "aren't set up" in status["error"]


# --------------------------------------------------------------- web pages

def test_backup_pages_are_for_the_director_only(app):
    viewer, anon, admin = login(app, "viewpw123"), app.test_client(), login(app)
    assert anon.get("/admin/backups").status_code == 302
    assert viewer.get("/admin/backups").status_code == 200
    for client in (viewer, anon):
        assert client.get("/admin/backup.zip").status_code == 302
        client.post("/admin/backups/restore", data={"confirm": "RESTORE"})
    r = admin.get("/admin/backup.zip")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store" and r.data[:2] == b"PK"
    assert "Backups" in admin.get("/admin").get_data(as_text=True)


def test_restore_from_an_uploaded_file(app):
    admin = login(app)
    admin.post("/admin/new", data={"name": "Keep Me", "kind": "chip"})
    saved = admin.get("/admin/backup.zip").data
    admin.post("/admin/new", data={"name": "Made Later", "kind": "chip"})
    r = admin.post("/admin/backups/restore", data={"confirm": "nope", "file": (io.BytesIO(saved), "b.zip")},
                   content_type="multipart/form-data")
    assert app.config["hub"].get("made-later") is not None  # not confirmed: nothing changed
    admin.post("/admin/backups/restore", data={"confirm": "RESTORE", "file": (io.BytesIO(saved), "b.zip")},
               content_type="multipart/form-data")
    hub = app.config["hub"]
    assert hub.get("keep-me") is not None and hub.get("made-later") is None
    page = admin.get("/admin/backups").get_data(as_text=True)
    assert "Undo a restore" in page and "before-restore-" in page
    assert r.status_code == 302
