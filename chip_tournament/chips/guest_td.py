"""Guest tournament directors: division reps who run one event of their own.

The league director keeps a short list of members who may act as guest TDs. A guest TD signs
in with their own player login, accepts the confidentiality agreement (the director's wording,
versioned), and asks for an event. The event stays hidden, sends no texts and keeps
registration closed until the director approves it. A guest TD can then run that event only,
never sees a full phone number, and loses access when the event finishes, when the director
removes them, or when the agreement changes until they accept the new version.

Everything lives in data/guest_tds.json, apart from the tournaments.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

PENDING, APPROVED, DECLINED = "pending", "approved", "declined"
FRESH_HOURS = 16  # a guest TD signs in again at least this often to keep TD access

# Blocked even inside a guest TD's own event: league-wide settings, wiping history, bulk imports.
GUEST_BLOCKED = frozenset({
    "admin_background",       # the photo behind every page of the site
    "admin_reset",            # archives the tournament and starts over
    "admin_delete_archives",  # deletes past tournaments for good
    "admin_import",           # spreadsheet import of many players at once
})


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def mask_phone(phone: str) -> str:
    """'904-555-1234' -> '•••-•••-1234'. Enough to tell two players apart, not enough to call."""
    digits = "".join(c for c in phone or "" if c.isdigit())
    return f"•••-•••-{digits[-4:]}" if digits else ""


class GuestError(Exception):
    pass


class GuestTDs:
    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "guest_tds.json"
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ storage

    def load(self) -> dict:
        data = {"guests": {}, "events": {}, "agreement": {"text": "", "version": 0, "published_at": ""}}
        if self.path.exists():
            data.update(json.loads(self.path.read_text()))
        return data

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    # ------------------------------------------------------------------ the list

    def guest(self, member: str) -> dict | None:
        return self.load()["guests"].get(member or "")

    def add(self, member: str, name: str) -> None:
        with self.lock:
            data = self.load()
            if member in data["guests"]:
                raise GuestError(f"{name} is already a guest TD.")
            data["guests"][member] = {"name": name, "added_at": now(), "accepted": None}
            self._save(data)

    def remove(self, member: str) -> str:
        """Take a member off the list. Their events stay (the director runs them) but they lose access."""
        with self.lock:
            data = self.load()
            rec = data["guests"].pop(member, None)
            if rec is None:
                raise GuestError("That member isn't a guest TD.")
            self._save(data)
            return rec["name"]

    # ------------------------------------------------------------------ the agreement

    def agreement(self) -> dict:
        return self.load()["agreement"]

    def publish_agreement(self, text: str) -> int:
        """Save the director's wording as a new version. Everyone must accept it again."""
        text = (text or "").replace("\r\n", "\n").strip()
        if len(text) < 200:
            raise GuestError("The agreement looks too short to publish. Paste the full wording.")
        with self.lock:
            data = self.load()
            if text == data["agreement"]["text"]:
                raise GuestError("That's the same wording as the published version, so nothing changed.")
            version = data["agreement"]["version"] + 1
            data["agreement"] = {"text": text, "version": version, "published_at": now()}
            self._save(data)
            return version

    def accept(self, member: str, typed_name: str) -> None:
        typed_name = " ".join((typed_name or "").split())
        with self.lock:
            data = self.load()
            rec, agreement = data["guests"].get(member), data["agreement"]
            if rec is None:
                raise GuestError("You're not on the guest TD list. Ask the league office.")
            if not agreement["version"]:
                raise GuestError("The agreement isn't ready yet. The league office will let you know.")
            if len(typed_name) < 3:
                raise GuestError("Type your full name to accept.")
            rec["accepted"] = {"version": agreement["version"], "at": now(), "typed_name": typed_name}
            self._save(data)

    def has_accepted(self, member: str) -> bool:
        data = self.load()
        rec, version = data["guests"].get(member or ""), data["agreement"]["version"]
        return bool(rec and version and rec.get("accepted") and rec["accepted"]["version"] == version)

    # ------------------------------------------------------------------ events

    def request_event(self, member: str, event_id: str) -> None:
        with self.lock:
            data = self.load()
            data["events"][event_id] = {"member": member, "status": PENDING, "requested_at": now(), "decided_at": ""}
            self._save(data)

    def event(self, event_id: str) -> dict | None:
        return self.load()["events"].get(event_id)

    def decide(self, event_id: str, status: str) -> dict:
        if status not in (APPROVED, DECLINED):
            raise GuestError("Approve or decline.")
        with self.lock:
            data = self.load()
            rec = data["events"].get(event_id)
            if rec is None:
                raise GuestError("That isn't a guest TD's event.")
            if rec["status"] != PENDING:
                raise GuestError(f"That event was already {rec['status']}.")
            rec["status"], rec["decided_at"] = status, now()
            self._save(data)
            return rec

    def forget_event(self, event_id: str) -> None:
        with self.lock:
            data = self.load()
            if data["events"].pop(event_id, None) is not None:
                self._save(data)

    def events_of(self, member: str) -> dict[str, dict]:
        return {eid: rec for eid, rec in self.load()["events"].items() if rec["member"] == member}

    def can_run(self, member: str, event_id: str) -> bool:
        """On the list, accepted the current agreement, and it's their event (pending or approved)."""
        rec = self.event(event_id)
        return bool(rec and rec["member"] == member and rec["status"] in (PENDING, APPROVED)
                    and self.guest(member) and self.has_accepted(member))


class BroadcastLog:
    """Every free-form text sent to an event's players: when, by whom, to how many, and what it said."""

    def __init__(self, event_dir: str | Path):
        self.path = Path(event_dir) / "broadcasts.json"

    def all(self) -> list[dict]:
        return json.loads(self.path.read_text()) if self.path.exists() else []

    def add(self, by: str, body: str, count: int) -> None:
        rows = self.all()
        rows.append({"at": now(), "by": by, "count": count, "body": body})
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=1))
        tmp.replace(self.path)
