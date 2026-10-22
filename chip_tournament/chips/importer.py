"""Bulk-import teams from a CSV file, e.g. Google Form responses downloaded from Google Sheets.

The director maps the form's columns to team fields; every row then goes
through the normal registration rules (skill cap, field cap and waiting list,
duplicate names), in file order, so the earliest sign-ups get the field spots.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from .engine import TEAM_SIZE, Player, Tournament, TournamentError

# Fields a column can be mapped to, in the order shown on the page.
FIELDS = {
    "team": "Team name",
    "name1": "Partner 1 name",
    "skill1": "Partner 1 skill level",
    "phone1": "Partner 1 cell",
    "name2": "Partner 2 name",
    "skill2": "Partner 2 skill level",
    "phone2": "Partner 2 cell",
    "combined": "Combined skill (only if the form didn't ask each partner)",
}


@dataclass
class Row:
    line: int  # row number in the spreadsheet (header is row 1)
    team_name: str = ""
    players: list[Player] = field(default_factory=list)
    error: str = ""
    result: str = ""  # "added", "waiting list #3", or why it was skipped
    note: str = ""


def read_csv(text: str) -> tuple[list[str], list[list[str]]]:
    rows = [r for r in csv.reader(io.StringIO(text.lstrip("﻿"))) if any(c.strip() for c in r)]
    if not rows:
        raise TournamentError("That file is empty.")
    return [h.strip() for h in rows[0]], rows[1:]


def _slot(header: str) -> int | None:
    """Which partner a column is about: 1, 2 or None (unclear)."""
    h = header.lower()
    if re.search(r"\b(2|two|second|2nd)\b", h) or "partner's" in h:
        return 2
    if re.search(r"\b(1|one|first|1st)\b", h) or "captain" in h or "your" in h:
        return 1
    if "partner" in h:  # "Partner name" next to "Your name"
        return 2
    return None


def guess_mapping(headers: list[str]) -> dict[str, int]:
    """Best guess of which column holds each field (column index), from the headers."""
    mapping: dict[str, int] = {}
    loose: dict[str, list[int]] = {"name": [], "skill": [], "phone": []}

    for i, raw in enumerate(headers):
        h = raw.lower()
        if "timestamp" in h or "email" in h:
            continue
        if "team" in h and "name" in h or h.strip() == "team":
            mapping.setdefault("team", i)
            continue
        if "combined" in h or "total" in h:
            mapping.setdefault("combined", i)
            continue
        if any(w in h for w in ("phone", "cell", "mobile", "text")):
            kind = "phone"
        elif "skill" in h or re.search(r"\bsl\b", h) or "rating" in h or "level" in h:
            kind = "skill"
        elif "name" in h or "player" in h or "partner" in h:
            kind = "name"
        else:
            continue
        slot = _slot(raw)
        key = f"{kind}{slot}" if slot else None
        if key and key not in mapping:
            mapping[key] = i
        else:
            loose[kind].append(i)

    # Columns that don't say which partner: fill partner 1, then partner 2.
    for kind, cols in loose.items():
        for i in cols:
            for slot in (1, 2):
                if f"{kind}{slot}" not in mapping:
                    mapping[f"{kind}{slot}"] = i
                    break
    return mapping


def _skill(value: str) -> int | None:
    m = re.search(r"\d+", value or "")
    return int(m.group()) if m else None


def build_rows(rows: list[list[str]], mapping: dict[str, int]) -> list[Row]:
    def cell(r: list[str], key: str) -> str:
        i = mapping.get(key)
        return r[i].strip() if i is not None and i < len(r) else ""

    out = []
    for n, r in enumerate(rows, start=2):
        row = Row(line=n)
        names = [cell(r, f"name{s}") for s in (1, 2)]
        skills = [_skill(cell(r, f"skill{s}")) for s in (1, 2)]
        phones = [cell(r, f"phone{s}") for s in (1, 2)]
        # Signed up alone (no partner name, skill or cell): holds a spot until a partner is added.
        solo = bool(names[0]) and not names[1] and skills[1] is None and not phones[1]
        if solo:
            if skills[0] is None and "combined" in mapping:
                skills[0] = _skill(cell(r, "combined"))
            row.team_name = cell(r, "team") or names[0]
            row.players = [Player(name=names[0], phone=phones[0], skill=skills[0] or 0)]
            row.note = "No partner yet: holds a spot until a partner is added."
            if skills[0] is None:
                row.error = "Missing or unreadable skill level."
            out.append(row)
            continue
        if None in skills and "combined" in mapping:
            combined = _skill(cell(r, "combined"))
            if combined:
                skills = [combined // 2, combined - combined // 2]
                row.note = f"Split combined {combined} as {skills[0]} + {skills[1]}; fix on the roster page if needed."
        row.team_name = cell(r, "team") or (" & ".join(n for n in names if n))
        row.players = [Player(name=names[s], phone=phones[s], skill=skills[s] or 0) for s in range(TEAM_SIZE)]
        if not all(names):
            row.error = "Missing a partner's name."
        elif None in skills:
            row.error = "Missing or unreadable skill level."
        out.append(row)
    return out


def apply(t: Tournament, rows: list[Row], send_texts: bool = True) -> tuple[int, int]:
    """Register each row in order. Returns (added, waitlisted); each row's result says what happened."""
    added = waitlisted = 0
    for row in rows:
        if row.error:
            row.result = f"Skipped: {row.error}"
            continue
        texts_before = len(t.outbox)
        try:
            team = t.register_team(row.team_name, row.players)
        except TournamentError as exc:
            row.result = f"Skipped: {exc}"
            continue
        if not send_texts:
            del t.outbox[texts_before:]
        if team.status == "waitlisted":
            waitlisted += 1
            row.result = f"Waiting list #{t.waitlist_position(team)}"
        else:
            added += 1
            row.result = "Added" + (" (needs a partner)" if team.needs_partner else
                                    f" ({team.chips} chips)" if team.chips else f" ({t.chips_preview(team)} chips)")
    return added, waitlisted


def preview(t: Tournament, rows: list[Row]) -> tuple[int, int]:
    """Dry run on a copy, so nothing is saved and no texts are sent."""
    return apply(Tournament.from_dict(t.to_dict()), rows, send_texts=False)
