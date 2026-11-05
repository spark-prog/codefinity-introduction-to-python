"""The player store: every APA player from the office's player Sheet, kept apart from the tournaments.

The office keeps a Google Sheet (tab "Current Player Records") clean by hand. Control Table
reads it once a day (about 4 AM), or when the director presses Sync now, as the service account
(the same Google login as backups). Each sync is checked before it's used:

- the required columns must be there; rows with a bad member number (always 8 digits
  starting with 322), an impossible skill level or a repeated member number are skipped
  and listed;
- a sync that would remove more than 5% of the players at once is held for the director
  to approve, so a bad edit can't wipe the list.

Only the columns Control Table needs are stored. Home phone, birth date and session
defense are dropped as the rows are read. The store lives in data/players/, outside
every tournament's undo history (backups include it).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from .backup import BackupError, Drive

TAB = "Current Player Records"
MEMBER_RE = re.compile(r"322\d{5}")
HOLD_SHARE = 0.05  # hold a sync that removes more than this share of the players
SYNC_HOUR = 4  # the daily sync runs after this hour (after the 3 AM backup)
RETRY_MINUTES = 60  # a failed daily sync tries again this often
STALE_DAYS = 8
LOG_KEEP = 60

# Sheet column -> stored field. Columns not listed here (home phone, birth date, session defense) are never kept.
COLUMNS = {
    "member number": "member",
    "first name": "first",
    "nickname": "nickname",
    "last name": "last",
    "9-ball skill level": "sl9",
    "8-ball skill level": "sl8",
    "total matches played 9-ball": "mp9",
    "total matches played 8-ball": "mp8",
    "last date played 9-ball": "last9",
    "last date played 8-ball": "last8",
    "member status": "status",
    "cell phone": "cell",
    "date started": "started",
    "division rep": "rep",
    "emailaddress": "email",
    "gender": "gender",
}
DROPPED = ("home phone", "birth date", "session defense")  # personal or unneeded: never kept
SESSION_START = "2026-09-08"  # the current session's first night, until the director changes it
REQUIRED = ("member number", "first name", "last name", "8-ball skill level", "9-ball skill level", "emailaddress")
LABELS = {"sl8": "8-Ball SL", "sl9": "9-Ball SL", "mp8": "8-Ball matches", "mp9": "9-Ball matches",
          "last8": "last 8-Ball", "last9": "last 9-Ball", "status": "status", "email": "email", "cell": "cell",
          "first": "first name", "last": "last name", "nickname": "nickname", "gender": "gender", "rep": "division rep",
          "started": "date started"}


class PlayerError(Exception):
    pass


def _key(header: str) -> str:
    return re.sub(r"\s+", " ", (header or "").strip().lower().replace("–", "-"))


def _date(value: str) -> str:
    value = (value or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _num(value: str) -> int | None:
    value = (value or "").replace(",", "").strip()
    if not value:
        return None
    try:
        n = float(value)
    except ValueError:
        return -1  # not a number: reported as a problem
    return int(n) if n == int(n) else -1


def parse_rows(rows: list[list[str]]) -> tuple[dict[str, dict], list[str]]:
    """The Sheet's rows (header first) -> ({member number: record}, problems)."""
    if not rows:
        raise PlayerError("The sheet is empty.")
    headers = [_key(h) for h in rows[0]]
    missing = [h for h in REQUIRED if h not in headers]
    if missing:
        raise PlayerError("The sheet is missing these columns: " + ", ".join(m.title().replace("Emailaddress", "EmailAddress")
                                                                          for m in missing)
                          + ". Nothing was changed.")
    where = {COLUMNS[h]: i for i, h in enumerate(headers) if h in COLUMNS}
    # Any other column the office adds (like a Yes/No "Masters" column for an eligible list) is kept by its name.
    extras = {rows[0][i].strip(): i for i, h in enumerate(headers) if h and h not in COLUMNS and h not in DROPPED}
    records: dict[str, dict] = {}
    problems: list[str] = []
    emails: dict[str, str] = {}
    for n, row in enumerate(rows[1:], start=2):
        cells = {f: (row[i] if i < len(row) else "") for f, i in where.items()}
        if not any((v or "").strip() for v in cells.values()):
            continue  # blank row
        member = re.sub(r"\D", "", cells.get("member", ""))
        name = f"{cells.get('first', '').strip()} {cells.get('last', '').strip()}".strip() or "(no name)"
        if not MEMBER_RE.fullmatch(member):
            problems.append(f"Row {n} ({name}): member number '{cells.get('member', '').strip()}' isn't 8 digits "
                            "starting with 322. Skipped.")
            continue
        if member in records:
            problems.append(f"Row {n} ({name}): member number {member} is already on row "
                            f"{records[member]['row']}. Skipped.")
            continue
        rec = {"member": member, "row": n,
               "extra": {name: (row[i] if i < len(row) else "").strip() for name, i in extras.items()}}
        for f in ("first", "nickname", "last", "status", "rep", "gender"):
            rec[f] = (cells.get(f) or "").strip()
        rec["email"] = (cells.get("email") or "").strip().lower()
        rec["cell"] = re.sub(r"\D", "", cells.get("cell") or "")[-10:]
        for f in ("last9", "last8", "started"):
            rec[f] = _date(cells.get(f, ""))
        bad = []
        for f, lo, hi in (("sl8", 2, 7), ("sl9", 1, 9)):
            v = _num(cells.get(f, ""))
            if v == 0:
                v = None  # 0 = not active in that format
            elif v is not None and not lo <= v <= hi:
                bad.append(f"{LABELS[f]} '{cells.get(f, '').strip()}' (must be {lo}–{hi})")
                v = None
            rec[f] = v
        for f in ("mp8", "mp9"):
            v = _num(cells.get(f, ""))
            if v is not None and v < 0:
                bad.append(f"{LABELS[f]} '{cells.get(f, '').strip()}'")
                v = None
            rec[f] = v
        if bad:
            problems.append(f"Row {n} ({name}): " + "; ".join(bad) + ". Kept, with those left blank.")
        if rec["email"]:
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", rec["email"]):
                problems.append(f"Row {n} ({name}): email '{rec['email']}' doesn't look right. Kept without it.")
                rec["email"] = ""
            elif rec["email"] in emails:
                problems.append(f"Row {n} ({name}): email {rec['email']} is also on member {emails[rec['email']]}. "
                                "Emails should be unique; login won't work for either until it's fixed.")
            else:
                emails[rec["email"]] = member
        records[member] = rec
    if not records:
        raise PlayerError("No usable player rows were found. Nothing was changed.")
    return records, problems


ESTABLISHED = 10  # APA: a player is established in a format after this many matches in it
RANGES = {"8": (2, 7), "9": (1, 9)}


def established(rec: dict, game: str) -> bool:
    """Established in 8-Ball ('8') or 9-Ball ('9'): 10 or more matches played in it."""
    return (rec.get(f"mp{game}") or 0) >= ESTABLISHED


def skill_for(rec: dict, game: str) -> tuple[int | None, str]:
    """The skill level a player plays at in 8-Ball ('8') or 9-Ball ('9'), and how it was found.

    - their own skill level in that format: (sl, "rated")
    - not active in it, but established (10+ matches) in the other format: they convert at their
      current skill level there: (sl, "converted from 9-Ball"). 8-Ball only has 2 to 7, so a 9-Ball
      SL 1 converts to 2 and an SL 8 or 9 to 7: (7, "converted from 9-Ball SL 9")
    - otherwise: (None, "needs review")
    """
    own = rec.get(f"sl{game}")
    if own is not None:
        return own, "rated"
    other = "9" if game == "8" else "8"
    sl = rec.get(f"sl{other}")
    if sl is not None and established(rec, other):
        lo, hi = RANGES[game]
        played = min(max(sl, lo), hi)
        return played, f"converted from {other}-Ball" + (f" SL {sl}" if played != sl else "")
    return None, "needs review"


SKILL_BASES = {"8": "8-Ball skill levels", "9": "9-Ball skill levels",
               "high": "Highest of 8-Ball & 9-Ball (no cap)", "high7": "Highest of 8-Ball & 9-Ball (cap at 7)"}


def basis_label(basis: str) -> str:
    """How an event's skill level is described in messages: '8-Ball', '9-Ball' or 'highest'."""
    return {"8": "8-Ball", "9": "9-Ball"}.get(basis, "highest")


def level_from(sl8: int, sl9: int, basis: str) -> int:
    """The one skill level a player plays at, from their 8-Ball and 9-Ball levels, by the event's basis
    ("Skill levels from the player list"): '8', '9', 'high' or 'high7'. 0 if neither is known.

    '8' uses the 8-Ball level; with none, the 9-Ball level converts (1 -> 2, 8 or 9 -> 7). '9' uses the
    9-Ball level; with none, the 8-Ball level. 'high' is the higher of the two; 'high7' the same, capped at 7.
    """
    sl8, sl9 = sl8 or 0, sl9 or 0
    if basis == "8":
        return sl8 or (min(max(sl9, 2), 7) if sl9 else 0)
    if basis == "9":
        return sl9 or sl8
    top = max(sl8, sl9)
    return min(top, 7) if basis == "high7" else top


def event_skill(rec: dict, basis: str) -> tuple[int | None, str]:
    """The skill level a player plays at in an event, by its basis: '8', '9' (with the conversion rule),
    'high' (the higher of their 8-Ball and 9-Ball levels) or 'high7' (the higher, capped at 7)."""
    if basis in ("8", "9"):
        return skill_for(rec, basis)
    levels = [x for x in (rec.get("sl8"), rec.get("sl9")) if x is not None]
    if not levels:
        return None, "needs review"
    top = max(levels)
    if basis == "high7" and top > 7:
        return 7, f"highest of 8-Ball & 9-Ball, capped at 7 (SL {top})"
    return top, "highest of 8-Ball & 9-Ball"


def rows_from_csv(raw: bytes) -> list[list[str]]:
    """A CSV download of the sheet (File > Download > CSV), in UTF-8 or Windows encoding."""
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return [row for row in csv.reader(io.StringIO(text))]


def sheet_id(value: str) -> str:
    value = (value or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        return value
    raise PlayerError("That isn't a Google Sheet link. Copy the address of the sheet from your browser.")


class SheetReader:
    """Reads the player tab as the service account."""

    def __init__(self, drive: Drive):
        self.drive = drive

    @classmethod
    def from_env(cls) -> "SheetReader | None":
        key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if not key.strip():
            return None
        try:
            return cls(Drive(key, ""))
        except BackupError as exc:
            raise PlayerError(str(exc))

    def rows(self, sid: str, tab: str = TAB) -> list[list[str]]:
        rng = urllib.parse.quote(f"'{tab}'", safe="")
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{rng}?valueRenderOption=FORMATTED_VALUE"
        try:
            return self.drive._request(urllib.request.Request(url)).get("values", [])
        except BackupError as exc:
            text = str(exc)
            if "Unable to parse range" in text:
                raise PlayerError(f"The sheet has no tab named '{tab}'.")
            if "can't find" in text or "404" in text or "refused access" in text or "403" in text:
                raise PlayerError(f"Google won't let Control Table open the sheet. Keep it in the shared drive (or share "
                                  f"it with {self.drive.email} as Viewer), and make sure the Google Sheets API is on.")
            raise PlayerError(text.replace("Google Drive", "Google Sheets"))

    def modified(self, sid: str) -> str:
        """When the sheet was last edited (ISO, UTC), or '' if Drive won't say."""
        url = f"{Drive.API}/{sid}?supportsAllDrives=true&fields=modifiedTime"
        try:
            return self.drive._request(urllib.request.Request(url)).get("modifiedTime", "")
        except BackupError:
            return ""


# ---------------------------------------------------------------- the store

class PlayerStore:
    def __init__(self, root: str | os.PathLike):
        self.dir = Path(root) / "players"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def _read(self, name: str, default):
        try:
            return json.loads((self.dir / name).read_text())
        except (OSError, ValueError):
            return default

    def _write(self, name: str, data) -> None:
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.dir / name)

    # settings ------------------------------------------------------------
    def config(self) -> dict:
        return {"sheet": "", "tab": TAB, "session_start": SESSION_START, "session_name": "Fall",
                **self._read("config.json", {})}

    def set_session_start(self, value: str, name: str = "") -> str:
        try:
            day = datetime.strptime((value or "").strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            raise PlayerError("Enter the session's start date.")
        name = name if name in ("Spring", "Summer", "Fall") else self.config()["session_name"]
        with self.lock:
            self._write("config.json", {**self.config(), "session_start": day, "session_name": name})
        return day

    def extra_columns(self) -> list[str]:
        """Extra sheet columns (for eligible lists), from the first player that has them."""
        return sorted(next((r.get("extra", {}) for r in self.records().values() if r.get("extra")), {}))

    def set_sheet(self, link: str) -> str:
        sid = sheet_id(link) if (link or "").strip() else ""
        with self.lock:
            self._write("config.json", {**self.config(), "sheet": sid})
        return sid

    # reading -------------------------------------------------------------
    def data(self) -> dict:
        return self._read("players.json", {"records": {}})

    def records(self) -> dict[str, dict]:
        return self.data().get("records", {})

    def get(self, member: str) -> dict | None:
        return self.records().get(re.sub(r"\D", "", member or ""))

    def by_email(self, email: str) -> dict | None:
        email = (email or "").strip().lower()
        hits = [r for r in self.records().values() if email and r.get("email") == email]
        return hits[0] if len(hits) == 1 else None

    def search(self, q: str, limit: int = 50) -> list[dict]:
        q = (q or "").strip().lower()
        if not q:
            return []
        digits = re.sub(r"\D", "", q)
        out = []
        for r in self.records().values():
            name = f"{r['first']} {r.get('nickname', '')} {r['last']}".lower()
            if (digits and len(digits) >= 3 and digits in r["member"]) or all(w in name for w in q.split()) \
                    or (q in (r.get("email") or "")):
                out.append(r)
        return sorted(out, key=lambda r: (r["last"].lower(), r["first"].lower()))[:limit]

    def log(self) -> list[dict]:
        return self._read("sync_log.json", [])

    def held(self) -> dict | None:
        return self._read("held.json", None)

    def status(self, at: datetime | None = None) -> dict:
        d, at = self.data(), at or datetime.now()
        synced = d.get("synced_at", "")
        stale = not synced or at - datetime.fromisoformat(synced) > timedelta(days=STALE_DAYS)
        return {"count": len(d.get("records", {})), "synced_at": synced, "sheet_modified": d.get("sheet_modified", ""),
                "source": d.get("source", ""), "problems": d.get("problems", []), "stale": stale,
                "held": self.held(), "config": self.config(), "error": self.last_error()}

    # changing ------------------------------------------------------------
    @staticmethod
    def diff(old: dict, new: dict) -> dict:
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        updated = {}
        for m in set(old) & set(new):
            changed = [LABELS[f] for f in LABELS if old[m].get(f) != new[m].get(f)]
            if changed:
                updated[m] = changed
        return {"added": added, "removed": removed, "updated": updated}

    def _label(self, rec: dict) -> str:
        return f"{rec['first']} {rec['last']} ({rec['member']})"

    def apply(self, records: dict, problems: list[str], source: str, by: str, sheet_modified: str = "",
              force: bool = False) -> dict:
        """Use a checked set of players. Returns the log entry (status 'synced', 'held' or 'no changes')."""
        with self.lock:
            old = self.records()
            d = self.diff(old, records)
            now = datetime.now().isoformat(timespec="seconds")
            entry = {"at": now, "by": by, "source": source, "players": len(records), "added": len(d["added"]),
                     "updated": len(d["updated"]), "removed": len(d["removed"]), "problems": len(problems),
                     "names": {"added": [self._label(records[m]) for m in d["added"][:25]],
                               "removed": [self._label(old[m]) for m in d["removed"][:25]],
                               "updated": [f"{self._label(records[m])}: {', '.join(f)}"
                                           for m, f in list(d["updated"].items())[:25]]}}
            if old and not force and len(d["removed"]) > max(2, len(old) * HOLD_SHARE):
                entry["status"] = "held"
                self._write("held.json", {"records": records, "problems": problems, "source": source,
                                          "sheet_modified": sheet_modified, "entry": entry})
            else:
                entry["status"] = "synced" if any(d.values()) or not old else "no changes"
                self._write("players.json", {"records": records, "synced_at": now, "source": source,
                                             "sheet_modified": sheet_modified, "problems": problems})
                (self.dir / "held.json").unlink(missing_ok=True)
            quiet = entry["status"] == "no changes" and by == "automatic"
            log = self.log()
            if not quiet or not log or log[0].get("problems") != entry["problems"]:
                self._write("sync_log.json", ([entry] + log)[:LOG_KEEP])
            elif quiet:  # keep the data date fresh without filling the log on every check
                log[0]["checked_at"] = now
                self._write("sync_log.json", log)
            return entry

    def approve_held(self, by: str) -> dict:
        held = self.held()
        if not held:
            raise PlayerError("There's no held sync to approve.")
        return self.apply(held["records"], held["problems"], held["source"], by, held.get("sheet_modified", ""),
                          force=True)

    def discard_held(self) -> None:
        (self.dir / "held.json").unlink(missing_ok=True)

    # syncing ---------------------------------------------------------------
    def sync_sheet(self, by: str, reader: SheetReader | None = None) -> dict:
        cfg = self.config()
        if not cfg["sheet"]:
            raise PlayerError("Link the player sheet first.")
        now = datetime.now().isoformat(timespec="seconds")
        self._write("last_try.json", now)
        try:
            reader = reader or SheetReader.from_env()
            if reader is None:
                raise PlayerError("GOOGLE_SERVICE_ACCOUNT_JSON isn't set in Render, so the sheet can't be read.")
            rows = reader.rows(cfg["sheet"], cfg["tab"])
            records, problems = parse_rows(rows)
            entry = self.apply(records, problems, "Google Sheet", by, reader.modified(cfg["sheet"]))
        except PlayerError as exc:
            self._write("last_error.json", {"at": now, "error": str(exc)})
            raise
        (self.dir / "last_error.json").unlink(missing_ok=True)
        return entry

    def last_error(self) -> dict | None:
        return self._read("last_error.json", None)

    def sync_csv(self, raw: bytes, by: str) -> dict:
        records, problems = parse_rows(rows_from_csv(raw))
        return self.apply(records, problems, "CSV upload", by)

    def due(self, at: datetime | None = None) -> bool:
        if not self.config()["sheet"] or self.held():
            return False
        at = at or datetime.now()
        if at.hour < SYNC_HOUR:
            return False
        log = self.log()
        done = max([self.data().get("synced_at", "")] + [x.get("checked_at") or x["at"] for x in log[:1]])
        if done and datetime.fromisoformat(done).date() >= at.date():
            return False  # already read today (by the daily sync or Sync now)
        last = self._read("last_try.json", "")
        return not last or at - datetime.fromisoformat(last) >= timedelta(minutes=RETRY_MINUTES)


def sync_loop(store: PlayerStore, log=None, sleep: float = 300) -> None:
    """Background: read the sheet once a day when it's linked (checks every few minutes whether it's due)."""
    import time

    while True:
        try:
            if store.due():
                store.sync_sheet("automatic")
        except Exception as exc:  # a bad sync must never stop the loop; the last good copy stays in use
            if log:
                log("player sheet sync failed: %s", exc)
        time.sleep(sleep)
