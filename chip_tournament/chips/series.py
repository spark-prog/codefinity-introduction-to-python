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
            by: str, to_event: str = "", to_name: str = "") -> dict:
        rec = {"series": series, "member": member, "name": name, "from_event": from_event, "from_name": from_name,
               "from_date": from_date, "by": by, "at": datetime.now().isoformat(timespec="seconds"),
               "to_event": to_event, "to_name": to_name}
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


NO_PUSHING = "none"  # settings.push_to when the director turned pushing off


def target(hub, event_id: str, settings) -> tuple[str, str] | None:
    """(event id, name) players on this event's list can push their eligibility to, or None.
    The director picks the event (settings.push_to); older events without a pick use their series name and
    the next event in that series by date."""
    pick = getattr(settings, "push_to", "")
    if pick == NO_PUSHING:
        return None
    if pick:
        found = None if pick == event_id else hub.get(pick)
        if found is None:
            return None
        t = found.load()
        return None if t.status == "finished" else (pick, t.settings.name)
    series = getattr(settings, "series", "")
    nxt = next_edition(hub, series, event_id, getattr(settings, "date", "")) if series else None
    if nxt:
        return nxt, hub.get(nxt).load().settings.name
    return None


def pushed_into(hub, pushes: Pushes, event_id: str, series: str, member: str) -> dict | None:
    """The push that makes this player eligible for this event (pushed here from another one), if any."""
    if not member:
        return None
    for p in pushes.all():
        if p["member"] != member or p["from_event"] == event_id:
            continue
        if p.get("to_event"):
            if p["to_event"] == event_id:
                return p
        elif series and key(p["series"]) == key(series) \
                and next_edition(hub, p["series"], p["from_event"], p["from_date"]) == event_id:  # older pushes
            return p
    return None


def lands_in(hub, p: dict) -> str | None:
    """Where a push takes the player: its event, or (older pushes) the next one in its series."""
    return p.get("to_event") or next_edition(hub, p["series"], p["from_event"], p["from_date"])
