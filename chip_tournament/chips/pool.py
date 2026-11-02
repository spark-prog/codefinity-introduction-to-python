"""Shared tables: several events at one venue (Mini-Mania) using one set of tables.

A pool lists its tables and its events. Every event in the pool uses the pool's tables, and before
an event hands out a table it's told which tables are in use by the other events, and which players
are at a table in another event (they aren't set or called until that match ends). Changes to pooled
events are made one at a time (one lock per pool), so two events can never take the same table. When
a table frees up, the other events get a turn at it straight away.

Pools are kept in data/pools.json (included in backups).
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from .engine import TournamentError, start_of

DEFAULT_HOURS = 4  # how long a mini-tournament takes

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def lock_for(pool_id: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(pool_id, threading.RLock())


class Pools:
    def __init__(self, root: str | Path):
        self.path = Path(root) / "pools.json"
        self.lock = threading.RLock()

    def all(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []

    def _save(self, rows: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=1))
        tmp.replace(self.path)

    def get(self, pool_id: str) -> dict | None:
        return next((p for p in self.all() if p["id"] == pool_id), None)

    def of_event(self, event_id: str) -> dict | None:
        return next((p for p in self.all() if event_id in p["events"]), None)

    def save(self, pool_id: str | None, name: str, tables: list[str], events: list[str],
             hours: float = DEFAULT_HOURS) -> dict:
        name = " ".join((name or "").split())
        if not name:
            raise TournamentError("Give the shared tables a name (like Mini-Mania).")
        if not tables:
            raise TournamentError("List the venue's tables.")
        with self.lock:
            rows = self.all()
            for p in rows:  # an event is in one pool at most
                if p["id"] != pool_id:
                    taken = set(p["events"]) & set(events)
                    if taken:
                        raise TournamentError(f"{', '.join(sorted(taken))} already shares tables in {p['name']}.")
            if pool_id:
                row = next((p for p in rows if p["id"] == pool_id), None)
                if row is None:
                    raise TournamentError("Those shared tables aren't there any more.")
            else:
                base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "tables"
                pool_id, n = base, 2
                while any(p["id"] == pool_id for p in rows):
                    pool_id, n = f"{base}-{n}", n + 1
                row = {"id": pool_id}
                rows.append(row)
            row.update(name=name, tables=tables, events=events, hours=hours)
            self._save(rows)
            return row

    def delete(self, pool_id: str) -> None:
        with self.lock:
            self._save([p for p in self.all() if p["id"] != pool_id])


def busy_elsewhere(hub, pool: dict, event_id: str) -> tuple[set[str], set[str]]:
    """(tables in use, members at a table) across the pool's other events."""
    tables, members = set(), set()
    for other in pool["events"]:
        if other == event_id:
            continue
        ev = hub.get(other)
        if ev is None:
            continue
        t = ev.load()
        if t.status != "running":
            continue
        for m in t.matches:
            if m.status in ("called", "waiting", "ready") and m.table:
                tables.add(m.table)
            if m.status == "called":
                sides = (m.team_a, m.team_b) if hasattr(m, "team_a") else (m.a, m.b)
                people = t.teams if hasattr(t, "teams") else t.entries
                for sid in sides:
                    if sid in people:
                        members |= {p.member for p in people[sid].players if p.member}
    return tables, members


def tables_in_use(t) -> set[str]:
    if t.status != "running":
        return set()
    return {m.table for m in t.matches if m.status in ("called", "waiting", "ready") and m.table}


def check_no_clash(hub, events: list[str]) -> None:
    """Events joining one pool mustn't already have the same table in use."""
    seen: dict[str, str] = {}
    for eid in events:
        ev = hub.get(eid)
        if ev is None:
            continue
        t = ev.load()
        for table in sorted(tables_in_use(t)):
            if table in seen:
                raise TournamentError(
                    f"Table {table} is in use in both {seen[table]} and {t.settings.name}. Move one of those "
                    "matches to another table (or finish it), then share the tables.")
            seen[table] = t.settings.name


def hours_of(pool: dict) -> float:
    return float(pool.get("hours", DEFAULT_HOURS) or 0)


def _when(start) -> str:
    return f"{start:%a} at {start:%I:%M %p}".replace(" at 0", " at ")


def check_times(hub, pools: Pools, event_id: str, t, players) -> None:
    """A player can't enter a shared-tables event that starts within a tournament's length (the pool's
    hours, 4 by default) of another shared-tables event they're already in, across every pool (Mini-Mania's
    8-foot and 9-foot tables are two pools, one day)."""
    mine = pools.of_event(event_id)
    start = start_of(t.settings)
    members = {p.member: p.name for p in players if p.member}
    if mine is None or start is None or not members:
        return
    for pool in pools.all():
        hours = max(hours_of(mine), hours_of(pool))
        if hours <= 0:
            continue
        for other in pool["events"]:
            ev = hub.get(other) if other != event_id else None
            if ev is None:
                continue
            o = ev.load()
            when = start_of(o.settings)
            if o.status == "finished" or when is None or abs((when - start).total_seconds()) >= hours * 3600:
                continue
            for member, name in members.items():
                if o.registered_as(member) is not None:
                    h = f"{hours:g} hour{'s' if hours != 1 else ''}"
                    raise TournamentError(
                        f"{name} is already in {o.settings.name}, which starts {_when(when)}. Each tournament "
                        f"takes about {h}, so a player can't enter two that start within {h} of each other.")


def inform(t, hub, pool: dict | None, event_id: str) -> None:
    """Tell a loaded tournament what the rest of its pool is using."""
    if pool is None:
        t.elsewhere_tables, t.elsewhere_members = set(), set()
        return
    t.elsewhere_tables, t.elsewhere_members = busy_elsewhere(hub, pool, event_id)


def waiting_for_table(t) -> bool:
    if t.status != "running":
        return False
    if hasattr(t, "table_queue"):
        return bool(t.table_queue())
    return bool(t.ready_queue())


def give_tables(t) -> None:
    """Hand out free tables to an event's waiting matches (the engines' own rules)."""
    if hasattr(t, "table_queue"):
        t._call_waiting()
    else:
        t._call_ready()
