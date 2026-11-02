"""Player history: every finished tournament's results, kept per player (by member number).

When an event finishes, each player with a member number gets a row: the event, date, team and
partners, finish, win-loss record and payout. The rows live in data/players/history.json, apart
from the event, so deleting or resetting an event later doesn't erase anyone's history. Test events
are skipped. Players registered by name only aren't recorded (a name can't be tied to a person).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .engine import money


class History:
    def __init__(self, root: str | Path):
        self.path = Path(root) / "players" / "history.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"recorded": [], "rows": {}}

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    def recorded(self, event_id: str) -> bool:
        return event_id in self._load()["recorded"]

    def for_member(self, member: str) -> list[dict]:
        return sorted(self._load()["rows"].get(member, []), key=lambda r: r.get("date") or "", reverse=True)

    def record(self, event_id: str, kind: str, t) -> int:
        """Write a finished event's results into each player's history (once). Returns how many players."""
        if t.status != "finished" or t.settings.test_event:
            return 0
        rows = results(event_id, kind, t)
        with self.lock:
            data = self._load()
            if event_id in data["recorded"]:
                return 0
            for member, row in rows:
                mine = [r for r in data["rows"].get(member, []) if r["event"] != event_id]
                data["rows"][member] = mine + [row]
            data["recorded"].append(event_id)
            self._save(data)
        return len(rows)


def results(event_id: str, kind: str, t) -> list[tuple[str, dict]]:
    """(member, row) for every player with a member number in a finished event."""
    out = []
    groups = t.teams if kind == "chip" else t.entries
    places = t.places() if kind != "chip" else {}
    for g in groups.values():
        if g.status in ("no_show", "waitlisted"):
            continue
        if kind == "chip":
            champion = any(e.team_id == g.id and e.reason.startswith("Last team standing") for e in t.ledger)
            finish = "Last team standing" if champion else (
                f"Cashed in {money(g.payout_cents)}" if g.payout_cents else "Out")
            wins, losses = g.wins, g.losses
            first = champion
        else:
            from .bracket import place_text
            place = places.get(g.id)
            finish = place_text(place) or "Played"
            if g.division and place:
                finish = f"{finish} in {g.division}"
            if place and place[0] == 1 and place[1] == 1:
                finish = f"{g.division} champion" if g.division else "Champion"
            played = t.matches_for(g.id)
            wins = sum(1 for m in played if m.winner == g.id)
            losses = len(played) - wins
            first = bool(place) and place == (1, 1)
        for p in g.players:
            if not p.member:
                continue
            partners = [x.name for x in g.players if x is not p]
            out.append((p.member, {"event": event_id, "name": t.settings.name, "date": t.settings.date,
                                   "team": g.name if partners else "", "partners": partners, "finish": finish,
                                   "title": first, "wins": wins, "losses": losses,
                                   "team_payout_cents": g.payout_cents}))
    return out


def summary(rows: list[dict]) -> dict:
    return {"events": len(rows), "titles": sum(1 for r in rows if r["title"]),
            "won_cents": sum(r["team_payout_cents"] for r in rows),
            "wins": sum(r["wins"] for r in rows), "losses": sum(r["losses"] for r in rows)}
