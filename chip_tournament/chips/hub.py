"""Control Table hub: several tournaments at once, each in its own folder.

data/
  events.json           the list of tournaments (id, kind, when created)
  events/<id>/          one tournament: tournament.json, history.json, texts.log, archives
  rules.json, secret_key, background.*   shared by every tournament

A data folder from the single-tournament app (tournament.json at the top) is
moved into events/<id>/ the first time the hub opens it, so nothing is lost.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .alerts import SmsSender
from .bracket import BracketTournament
from .engine import Tournament, TournamentError
from .storage import Store

CHIP, BRACKET = "chip", "bracket"
KINDS = {CHIP: Tournament, BRACKET: BracketTournament}
LEGACY_FILES = ("tournament.json", "history.json", "texts.log", "import.csv")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug[:40].strip("-") or "tournament"


@dataclass
class Event:
    id: str
    kind: str
    dir: Path
    store: Store
    sms: SmsSender
    created: str = ""

    def load(self):
        return self.store.load()


class Hub:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "events.json"
        self.lock = threading.RLock()
        self._events: dict[str, Event] = {}
        self._migrate_legacy()

    # ------------------------------------------------------------- index

    def _index(self) -> list[dict]:
        if not self.index_path.exists():
            return []
        return json.loads(self.index_path.read_text())

    def _save_index(self, rows: list[dict]) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=1))
        tmp.replace(self.index_path)

    def _event(self, row: dict) -> Event:
        with self.lock:  # one Store (and one lock) per tournament, even when two requests arrive at once
            ev = self._events.get(row["id"])
            if ev is None:
                folder = self.root / "events" / row["id"]
                ev = Event(id=row["id"], kind=row["kind"], dir=folder, store=Store(folder, KINDS[row["kind"]]),
                           sms=SmsSender(folder / "texts.log"), created=row.get("created", ""))
                self._events[row["id"]] = ev
            return ev

    def get(self, event_id: str) -> Event | None:
        for row in self._index():
            if row["id"] == event_id:
                return self._event(row)
        return None

    def events(self) -> list[Event]:
        return [self._event(row) for row in self._index()]

    def create(self, name: str, kind: str, **settings) -> Event:
        if kind not in KINDS:
            raise TournamentError("Pick a tournament type.")
        name = (name or "").strip()
        if not name:
            raise TournamentError("Give the tournament a name.")
        with self.lock:
            rows = self._index()
            taken = {r["id"] for r in rows}
            base = slugify(name)
            event_id, n = base, 2
            while event_id in taken:
                event_id, n = f"{base}-{n}", n + 1
            rows.append({"id": event_id, "kind": kind, "created": datetime.now().isoformat(timespec="seconds")})
            ev = self._event(rows[-1])
            t = ev.store.load()
            t.settings.name = name
            for key, value in settings.items():
                if value not in (None, "") and hasattr(t.settings, key):
                    setattr(t.settings, key, value)
            ev.store.save(t)
            self._save_index(rows)
            return ev

    def delete(self, event_id: str) -> bool:
        with self.lock:
            rows = self._index()
            keep = [r for r in rows if r["id"] != event_id]
            if len(keep) == len(rows):
                return False
            self._save_index(keep)
            self._events.pop(event_id, None)
            shutil.rmtree(self.root / "events" / event_id, ignore_errors=True)
            return True

    def is_migrated(self, event_id: str) -> bool:
        """The tournament that came from the data folder before Control Table hosted several."""
        return any(r["id"] == event_id and r.get("migrated") for r in self._index())

    def find_team(self, team_id: str) -> Event | None:
        """The tournament a team (or bracket entry) id belongs to, newest first."""
        for ev in reversed(self.events()):
            t = ev.load()
            if team_id in getattr(t, "teams", {}) or team_id in getattr(t, "entries", {}):
                return ev
        return None

    def summaries(self) -> list[dict]:
        """Every tournament with what the hub pages show, soonest first, finished last."""
        out = []
        for ev in self.events():
            t = ev.load()
            people = getattr(t, "teams", None) if ev.kind == CHIP else getattr(t, "entries", None)
            field = t.field_teams if ev.kind == CHIP else t.field_entries
            fmt = "Chip tournament" if ev.kind == CHIP else t.format_name
            out.append({"event": ev, "t": t, "format": fmt, "count": len(field), "registered": len(people or {})})
        order = {"running": 0, "setup": 1, "finished": 2}
        return sorted(out, key=lambda r: (order.get(r["t"].status, 3), r["t"].settings.date or "9999",
                                          r["event"].created))

    # --------------------------------------------------------- migration

    def _migrate_legacy(self) -> None:
        """Move a single-tournament data folder into the hub as its first tournament."""
        if self.index_path.exists():
            return
        legacy = [self.root / f for f in LEGACY_FILES if (self.root / f).exists()]
        archives = list(self.root.glob("tournament-*.json")) + list(self.root.glob("texts-*.log"))
        if not legacy and not archives:
            self._save_index([])
            return
        name = "Chip Tournament"
        if (self.root / "tournament.json").exists():
            try:
                name = json.loads((self.root / "tournament.json").read_text()).get("settings", {}).get("name") or name
            except ValueError:
                pass
        event_id = slugify(name)
        folder = self.root / "events" / event_id
        folder.mkdir(parents=True, exist_ok=True)
        for path in legacy + archives:
            shutil.move(str(path), folder / path.name)
        self._save_index([{"id": event_id, "kind": CHIP, "created": datetime.now().isoformat(timespec="seconds"),
                           "migrated": True}])
