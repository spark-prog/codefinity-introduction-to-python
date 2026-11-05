"""The director's spreadsheet of every team (or bracket entry), with PINs and contact details."""

from __future__ import annotations

import csv
import io

from .bracket import asks_two_levels
from .engine import money

# Spreadsheet apps run a cell that starts with one of these as a formula; a leading ' keeps it as text.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


class Pin(str):
    """A PIN written so spreadsheets keep its leading zeros (0307, not 307)."""


def _cell(value) -> str:
    if isinstance(value, Pin):
        return f'="{value}"' if value.isdigit() else str(value)
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def _players(players, size: int, two_levels: bool) -> tuple[list[str], list[str]]:
    header, row = [], []
    for i in range(size):
        p = players[i] if i < len(players) else None
        n = f"Player {i + 1}"
        header += [f"{n} name", f"{n} cell"]
        row += [p.name if p else "", p.phone if p else ""]
        if two_levels:
            header += [f"{n} 8-Ball SL", f"{n} 9-Ball SL"]
            row += [(p.skill_8 or "") if p else "", (p.skill_9 or "") if p else ""]
        header += [f"{n} skill", f"{n} texts on", f"{n} texts on since"]
        row += [(p.skill or "") if p else "", _yes(p.sms_consent) if p else "", (p.sms_consent_at or "") if p else ""]
    return header, row


def chip_teams(t) -> list[list[str]]:
    teams = sorted(t.teams.values(), key=lambda x: x.name.lower())
    size = max([2] + [len(x.players) for x in teams])
    header = ["Team", "PIN", "Status", "Where", "Waiting list #", "Checked in", "Registered", "Combined skill",
              "Starting chips", "Chips", "Wins", "Losses", "Value / paid", "Needs partner", "On partner list"]
    rows = []
    for team in teams:
        p_header, p_row = _players(team.players, size, False)
        value = t.payout_for(team) if team.status == "active" else team.payout_cents
        rows.append([team.name, Pin(team.pin), team.status.replace("_", " "), t.where(team), t.waitlist_position(team) or "",
                     team.checked_in_at or "", team.registered_at, team.combined_skill, team.starting_chips or "",
                     team.chips if t.status != "setup" else "", team.wins, team.losses, money(value),
                     _yes(team.needs_partner), _yes(team.list_seeking)] + p_row)
    return [header + (p_header if teams else _players([], size, False)[0])] + rows


def bracket_entries(t) -> list[list[str]]:
    entries = sorted(t.entries.values(), key=lambda x: x.name.lower())
    s = t.settings
    two_levels = asks_two_levels(s)
    header = ["Entry", "PIN", "Status", "Where", "Skill group", "Seed", "Waiting list #", "Checked in", "Registered",
              "Won"]
    rows = []
    for e in entries:
        _, p_row = _players(e.players, s.team_size, two_levels)
        rows.append([e.name, Pin(e.pin), e.status.replace("_", " "), t.where(e), e.division, e.seed or "",
                     t.waitlist_position(e) or "", e.checked_in_at or "", e.registered_at,
                     money(e.payout_cents) if e.payout_cents else ""] + p_row)
    return [header + _players([], s.team_size, two_levels)[0]] + rows


def to_csv(rows: list[list]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return "﻿" + out.getvalue()  # the BOM makes Excel read names with accents correctly


def mask_phones(rows: list[list], mask) -> list[list]:
    """The same sheet with every '… cell' column masked (for a guest TD's download)."""
    if not rows:
        return rows
    cols = [i for i, h in enumerate(rows[0]) if str(h).endswith(" cell")]
    return [rows[0]] + [[mask(v) if i in cols else v for i, v in enumerate(r)] for r in rows[1:]]
