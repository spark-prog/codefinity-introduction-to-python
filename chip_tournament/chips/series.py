"""Recurring invite-only events (a 'series', like MVP every session or Movin-On-Up every year), and
pushing eligibility to the next edition.

A player eligible for an edition through its list (Yes in the sheet column) who can't make it can push
their eligibility once: they're taken out of this edition and are eligible for the next edition of the
series (the next event in it by date), on a one-time grace. A player on that grace (Push in the sheet, or
pushed here in the app) can't push again. Pushes are kept league-wide in data/players/pushes.json.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path


class Pushes:
    def __init__(self, root: str | Path):
        self.path = Path(root) / "players" / "pushes.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def all(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []

    def add(self, series: str, member: str, name: str, from_event: str, from_name: str, from_date: str,
            by: str) -> dict:
        rec = {"series": series, "member": member, "name": name, "from_event": from_event, "from_name": from_name,
               "from_date": from_date, "by": by, "at": datetime.now().isoformat(timespec="seconds")}
        with self.lock:
            rows = self.all() + [rec]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows, indent=1))
            tmp.replace(self.path)
        return rec

    def from_event(self, event_id: str) -> list[dict]:
        return [p for p in self.all() if p["from_event"] == event_id]


def key(series: str) -> str:
    return " ".join((series or "").lower().split())


def next_edition(hub, series: str, from_event: str, from_date: str) -> str | None:
    """The next event in the series after an edition (by date), or None if it isn't created yet."""
    later = []
    for ev, t in hub.loaded():
        if ev.id == from_event:
            continue
        s = t.settings
        if key(getattr(s, "series", "")) == key(series) and s.date and s.date > (from_date or ""):
            later.append((s.date, ev.id))
    return min(later)[1] if later else None


def pushed_into(hub, pushes: Pushes, event_id: str, series: str, member: str) -> dict | None:
    """The push that makes this player eligible for this edition (pushed from the previous one), if any."""
    if not series or not member:
        return None
    for p in pushes.all():
        if p["member"] == member and key(p["series"]) == key(series) and p["from_event"] != event_id \
                and next_edition(hub, p["series"], p["from_event"], p["from_date"]) == event_id:
            return p
    return None
