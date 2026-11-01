"""Backups of the whole data folder: every tournament, the shared rules and settings.

A backup is one zip of the data folder. Each night it goes to a Google shared drive
(GOOGLE_SERVICE_ACCOUNT_JSON + BACKUP_DRIVE_FOLDER_ID); if Drive fails, or isn't set
up, it's emailed through the Gmail sender (GMAIL_ADDRESS + GMAIL_APP_PASSWORD) to
BACKUP_EMAIL instead. The director can also back up now, download a copy, and
restore any backup.

The server's own secret key is never backed up or restored, so a restore never
signs anyone out or makes old links invalid.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import smtplib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

MANIFEST = "control-table-backup.json"
STATUS_FILE = "backup_status.json"
SAFETY_DIR = "restore-safety"  # a copy of the data taken just before each restore
SKIP = {"secret_key", STATUS_FILE, SAFETY_DIR}  # never backed up, never replaced by a restore
KEEP_ON_DRIVE = 60  # nightly backups kept in the shared drive (about two months)
KEEP_SAFETY = 5
BACKUP_HOUR = 3  # the nightly backup runs after 3 AM local time (TZ setting)
RETRY_MINUTES = 60
EMAIL_LIMIT = 20 * 1024 * 1024  # Gmail refuses attachments over 25 MB

_run_lock = threading.Lock()


class BackupError(Exception):
    pass


# ------------------------------------------------------------------ the zip

def backup_name(at: datetime | None = None, prefix: str = "control-table") -> str:
    return f"{prefix}-{(at or datetime.now()):%Y-%m-%d-%H%M}.zip"


def _files(root: Path):
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] in SKIP or path.suffix == ".tmp" or not path.is_file():
            continue
        yield path, rel.as_posix()


def make_zip(root: str | os.PathLike) -> bytes:
    """Zip the data folder (everything but the server's secret key and backup bookkeeping)."""
    root = Path(root)
    buf = io.BytesIO()
    events = 0
    try:
        events = len(json.loads((root / "events.json").read_text()))
    except (OSError, ValueError):
        pass
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST, json.dumps({"app": "Control Table", "made": datetime.now().isoformat(timespec="seconds"),
                                         "events": events}))
        for path, name in _files(root):
            z.write(path, name)
    return buf.getvalue()


def describe(data: bytes) -> dict:
    """Check a backup zip and say what's in it. Raises BackupError if it isn't one."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise BackupError("That file isn't a Control Table backup (not a zip file).")
    names = z.namelist()
    if MANIFEST not in names or "events.json" not in names:
        raise BackupError("That zip isn't a Control Table backup.")
    for name in names:
        parts = Path(name).parts
        if name.startswith(("/", "\\")) or ".." in parts or ":" in name or (parts and parts[0] in SKIP):
            raise BackupError("That backup has a file in a place it shouldn't be, so it wasn't restored.")
    try:
        manifest = json.loads(z.read(MANIFEST))
        index = json.loads(z.read("events.json"))
    except ValueError:
        raise BackupError("That backup is damaged (its contents can't be read).")
    if not isinstance(index, list):
        raise BackupError("That backup is damaged (its list of tournaments can't be read).")
    for row in index:
        if not isinstance(row, dict) or not re.fullmatch(r"[a-z0-9-]+", str(row.get("id", ""))):
            raise BackupError("That backup is damaged (a tournament in it has a bad name).")
    return {"made": manifest.get("made", ""), "events": len(index), "files": len(names) - 1}


def restore_zip(root: str | os.PathLike, data: bytes, hub=None) -> dict:
    """Replace the data folder with a backup. A copy of the current data is kept first."""
    root = Path(root)
    info = describe(data)
    lock = hub.lock if hub is not None else threading.RLock()
    with lock:
        stores = [ev.store for ev in hub.events()] if hub is not None else []
        for s in stores:  # nothing changes mid-restore
            s.lock.acquire()
        try:
            safety = root / SAFETY_DIR
            safety.mkdir(exist_ok=True)
            (safety / backup_name(prefix="before-restore")).write_bytes(make_zip(root))
            for old in sorted(safety.glob("before-restore-*.zip"))[:-KEEP_SAFETY]:
                old.unlink()
            for path in root.iterdir():
                if path.name in SKIP:
                    continue
                shutil.rmtree(path) if path.is_dir() else path.unlink()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for name in z.namelist():
                    if name == MANIFEST or name.endswith("/"):
                        continue
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(z.read(name))
            if hub is not None:
                hub._events.clear()  # every tournament is read fresh from the restored files
        finally:
            for s in stores:
                s.lock.release()
    return info


# ------------------------------------------------------------- Google Drive

def folder_id(value: str) -> str:
    """The folder id, even when a whole Drive address was pasted."""
    value = (value or "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", value) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", value)
    return m.group(1) if m else value


class Drive:
    """The few Google Drive calls backups need, as the service account."""

    API = "https://www.googleapis.com/drive/v3/files"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
    SCOPE = "https://www.googleapis.com/auth/drive"

    def __init__(self, account_json: str, folder: str):
        try:
            self.info = json.loads(account_json)
            self.email = self.info["client_email"]
        except (ValueError, KeyError, TypeError):
            raise BackupError("GOOGLE_SERVICE_ACCOUNT_JSON isn't a service account key. Paste the whole key file.")
        self.folder = folder_id(folder)
        self._token, self._expires = "", 0.0

    @classmethod
    def from_env(cls) -> "Drive | None":
        key, folder = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""), os.environ.get("BACKUP_DRIVE_FOLDER_ID", "")
        return cls(key, folder) if key.strip() and folder.strip() else None

    def token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        from google.auth import crypt, jwt  # only needed when Drive is set up

        now = int(time.time())
        token_uri = self.info.get("token_uri", "https://oauth2.googleapis.com/token")
        try:
            signer = crypt.RSASigner.from_service_account_info(self.info)
        except Exception:
            raise BackupError("The service account key can't be read. Paste the whole key file again.")
        assertion = jwt.encode(signer, {"iss": self.email, "scope": self.SCOPE, "aud": token_uri,
                                        "iat": now, "exp": now + 3600})
        body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                       "assertion": assertion if isinstance(assertion, str) else assertion.decode()})
        data = self._request(urllib.request.Request(token_uri, data=body.encode()), auth=False)
        self._token, self._expires = data["access_token"], now + int(data.get("expires_in", 3600))
        return self._token

    def _request(self, req: urllib.request.Request, auth: bool = True, raw: bool = False):
        if auth:
            req.add_header("Authorization", f"Bearer {self.token()}")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raise BackupError(self._explain(exc.code, exc.read().decode("utf-8", "replace")))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BackupError(f"Couldn't reach Google Drive ({exc}).")
        if raw:
            return body
        return json.loads(body) if body else {}

    def _explain(self, code: int, text: str) -> str:
        if "storageQuotaExceeded" in text or "do not have storage quota" in text:
            return ("Google won't let the app save into My Drive. Use a folder in a shared drive, "
                    "with the service account added as Content manager.")
        if code == 404 or "notFound" in text:
            return (f"Google Drive can't find the backup folder. Check BACKUP_DRIVE_FOLDER_ID, and add "
                    f"{self.email} to the shared drive as Content manager.")
        if code in (401, 403) and ("invalid_grant" in text or "unauthorized" in text.lower()):
            return "Google refused the service account key. Make a new key and paste it into Render."
        if code == 403:
            return (f"Google Drive refused access. Add {self.email} to the shared drive as Content manager, "
                    "and make sure the Google Drive API is turned on for the project.")
        return f"Google Drive error {code}: {text[:200]}"

    def upload(self, name: str, data: bytes) -> dict:
        boundary = uuid.uuid4().hex
        meta = json.dumps({"name": name, "parents": [self.folder]})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: application/zip\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        url = f"{self.UPLOAD}?uploadType=multipart&supportsAllDrives=true&fields=id,name,size,createdTime"
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        return self._request(req)

    def list(self) -> list[dict]:
        """Backups in the folder, newest first."""
        q = f"'{self.folder}' in parents and trashed = false and name contains '.zip'"
        params = urllib.parse.urlencode({"q": q, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
                                         "corpora": "allDrives", "orderBy": "createdTime desc", "pageSize": "200",
                                         "fields": "files(id,name,size,createdTime)"})
        return self._request(urllib.request.Request(f"{self.API}?{params}")).get("files", [])

    def download(self, file_id: str) -> bytes:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", file_id or ""):
            raise BackupError("Unknown backup.")
        url = f"{self.API}/{file_id}?alt=media&supportsAllDrives=true"
        return self._request(urllib.request.Request(url), raw=True)

    def delete(self, file_id: str) -> None:
        self._request(urllib.request.Request(f"{self.API}/{file_id}?supportsAllDrives=true", method="DELETE"))

    def prune(self, keep: int = KEEP_ON_DRIVE) -> int:
        """Delete nightly backups beyond the newest `keep` (backups saved by hand are kept)."""
        nightly = [f for f in self.list() if f["name"].startswith("control-table-")]
        for f in nightly[keep:]:
            self.delete(f["id"])
        return max(0, len(nightly) - keep)


# ------------------------------------------------------------------ email

class Mailer:
    def __init__(self, address: str, password: str, to: str):
        self.address, self.password, self.to = address, password.replace(" ", ""), to

    @classmethod
    def from_env(cls) -> "Mailer | None":
        address, password = os.environ.get("GMAIL_ADDRESS", "").strip(), os.environ.get("GMAIL_APP_PASSWORD", "")
        to = os.environ.get("BACKUP_EMAIL", "").strip() or address
        return cls(address, password, to) if address and password.strip() else None

    def send(self, subject: str, text: str, attachment: tuple[str, bytes] | None = None) -> None:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, f"Control Table <{self.address}>", self.to
        msg.set_content(text)
        if attachment:
            msg.add_attachment(attachment[1], maintype="application", subtype="zip", filename=attachment[0])
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
                smtp.login(self.address, self.password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise BackupError(f"Gmail couldn't send the email ({exc}).")


# ------------------------------------------------------- status and schedule

def load_status(root: str | os.PathLike) -> dict:
    try:
        return json.loads((Path(root) / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return {}


def _save_status(root: Path, status: dict) -> None:
    tmp = root / (STATUS_FILE + ".tmp")
    tmp.write_text(json.dumps(status, indent=1))
    tmp.replace(root / STATUS_FILE)


def configured() -> dict:
    """Which backup routes are set up, for the director page."""
    drive_error = ""
    try:
        drive = Drive.from_env()
    except BackupError as exc:
        drive, drive_error = None, str(exc)
    mailer = Mailer.from_env()
    return {"drive": drive is not None, "drive_email": drive.email if drive else "", "drive_error": drive_error,
            "email": mailer is not None,
            "email_to": mailer.to if mailer else ""}


def due(status: dict, at: datetime | None = None) -> bool:
    """Whether the nightly backup should run now: once a day after BACKUP_HOUR, retrying hourly if it failed."""
    at = at or datetime.now()
    if at.hour < BACKUP_HOUR:
        return False
    last_ok = status.get("last_ok")
    if last_ok and datetime.fromisoformat(last_ok).date() >= at.date():
        return False
    last_try = status.get("last_try")
    return not last_try or at - datetime.fromisoformat(last_try) >= timedelta(minutes=RETRY_MINUTES)


def run(root: str | os.PathLike, reason: str = "nightly", drive: Drive | None = None,
        mailer: Mailer | None = None) -> dict:
    """Make a backup and send it to Drive (or by email if Drive fails). Returns the new status."""
    root = Path(root)
    with _run_lock:
        status = load_status(root)
        now = datetime.now()
        status["last_try"] = now.isoformat(timespec="seconds")
        try:
            drive = drive if drive is not None else Drive.from_env()
        except BackupError as exc:
            drive, drive_error = None, str(exc)
        else:
            drive_error = "" if drive else "Google Drive isn't set up."
        mailer = mailer if mailer is not None else Mailer.from_env()
        name = backup_name(now)
        data = make_zip(root)
        where, problems = "", []
        if drive is not None:
            try:
                drive.upload(name, data)
                where = "Google Drive"
                try:
                    drive.prune()
                except BackupError as exc:  # the backup is safe; old ones just weren't cleared
                    problems.append(f"Old backups weren't cleared: {exc}")
            except BackupError as exc:
                drive_error = str(exc)
        if not where and mailer is not None:
            try:
                if len(data) <= EMAIL_LIMIT:
                    mailer.send(f"Control Table backup {now:%b %-d, %Y}",
                                f"Google Drive didn't take tonight's backup ({drive_error}), so it's attached here "
                                "instead. Keep this email: the attachment can be restored from the director page.\n\n"
                                "It holds player names, phone numbers and team PINs.",
                                (name, data))
                    where = "email"
            except BackupError as exc:
                problems.append(str(exc))
        if where:
            status.update(last_ok=status["last_try"], last_where=where, last_name=name, last_size=len(data),
                          last_reason=reason)
            status["warning"] = f"Saved by email because Google Drive failed: {drive_error}" if where == "email" else ""
            status["warning"] = "; ".join(x for x in [status["warning"]] + problems if x)
            status["error"] = ""
        else:
            if drive is None and mailer is None:
                status["error"] = drive_error if drive_error != "Google Drive isn't set up." else \
                    "Backups aren't set up: add the Google Drive settings in Render."
            else:
                too_big = ["The backup is too big to email."] if mailer is not None and len(data) > EMAIL_LIMIT else []
                status["error"] = " ".join(x for x in [drive_error] + problems + too_big if x)
            _warn(root, status, mailer, now)
        _save_status(root, status)
        return status


def _warn(root: Path, status: dict, mailer: Mailer | None, now: datetime) -> None:
    """Email the director once a day when the backup keeps failing."""
    if mailer is None or status.get("warned") == now.date().isoformat():
        return
    try:
        mailer.send("Control Table backup FAILED",
                    f"Tonight's backup didn't save: {status['error']}\n\nOpen the director page to try again "
                    "or download a copy by hand.")
        status["warned"] = now.date().isoformat()
    except BackupError:
        pass


def is_set_up() -> bool:
    c = configured()
    return c["drive"] or Mailer.from_env() is not None


def nightly_loop(root: str | os.PathLike, log=None, sleep: float = 300) -> None:
    """Runs in the background: checks every few minutes and backs up once a night."""
    while True:
        try:
            if is_set_up() and due(load_status(root)):
                run(root)
        except Exception as exc:  # the loop must never stop
            if log:
                log("nightly backup failed: %s", exc)
        time.sleep(sleep)
