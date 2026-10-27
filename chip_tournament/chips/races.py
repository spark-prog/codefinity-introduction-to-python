"""Skill groups and rack race charts for bracket events.

A rack race chart gives each side its own number of racks to win, looked up
from both players' skill levels. Skill groups split a singles bracket event
into one bracket per skill range (each with its own champion), placing every
player by the higher of their 8-Ball and 9-Ball skill levels.
"""

from __future__ import annotations

import re

from .engine import TournamentError

# The league's rack race chart for singles: RACK_RACE_SINGLES[mine][theirs] = (my race, their race).
# Rows and columns are skill levels 1-9; None means the two levels never meet (SL 8 and 9 only
# play SL 5 and up). Used for 8-Ball and Saratoga, and for 9-Ball when it's played as a rack race.
_ROWS = """
1: 2/2 1/2 2/2 2/3 2/4 2/5 2/6 -   -
2: 2/1 3/3 2/3 2/4 2/5 2/6 2/7 -   -
3: 2/2 3/2 3/3 3/4 3/5 3/6 3/7 -   -
4: 3/2 4/2 4/3 4/4 4/5 4/6 3/6 -   -
5: 4/2 5/2 5/3 5/4 5/5 5/6 4/6 2/5 2/6
6: 5/2 6/2 6/3 6/4 6/5 5/5 5/6 3/5 3/6
7: 6/2 7/2 7/3 6/3 6/4 6/5 5/5 4/5 4/6
8: -   -   -   -   5/2 5/3 5/4 5/5 5/6
9: -   -   -   -   6/2 6/3 6/4 6/5 6/6
"""


def _parse_chart(text: str) -> dict[int, dict[int, tuple[int, int] | None]]:
    chart: dict[int, dict[int, tuple[int, int] | None]] = {}
    for line in text.strip().splitlines():
        row, cells = line.split(":")
        chart[int(row)] = {col: (None if cell == "-" else tuple(int(x) for x in cell.split("/")))
                           for col, cell in enumerate(cells.split(), 1)}
    return chart


RACK_RACE_SINGLES = _parse_chart(_ROWS)
CHART_LEVELS = range(1, 10)


def rack_race(mine: int, theirs: int) -> tuple[int, int]:
    """(my race, their race) from the singles rack race chart.

    Two levels the chart never pairs (an SL 8 or 9 against SL 1 to 4, only possible when small
    groups merge more than once) race as if the lower player were an SL 5.
    """
    if mine not in CHART_LEVELS or theirs not in CHART_LEVELS:
        raise TournamentError("Rack races need a skill level from 1 to 9 for both players.")
    cell = RACK_RACE_SINGLES[mine][theirs]
    if cell is None:
        if mine < theirs:
            cell = (RACK_RACE_SINGLES[5][theirs][0], RACK_RACE_SINGLES[5][theirs][1])
        else:
            cell = RACK_RACE_SINGLES[mine][5]
    return cell


# ------------------------------------------------------------------ skill groups

def group_label(lo: int, hi: int) -> str:
    return f"SL {lo}" if lo == hi else f"SL {lo}–{hi}"


def parse_skill_groups(text: str) -> list[list[int]]:
    """'1-3, 4, 5, 6, 7-9' -> [[1, 3], [4, 4], [5, 5], [6, 6], [7, 9]]; blank -> [] (one bracket)."""
    groups = []
    for part in [p.strip() for p in re.split(r"[,;\n]+", text or "") if p.strip()]:
        m = re.fullmatch(r"(?:SL\s*)?(\d)\s*(?:[-–to]+\s*(\d))?", part, re.IGNORECASE)
        if not m:
            raise TournamentError(f"Skill groups look like 1-3, 4, 5, 6, 7-9 ('{part}' isn't one).")
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if not 1 <= lo <= hi <= 9:
            raise TournamentError(f"'{part}': skill levels go from 1 to 9, low to high.")
        groups.append([lo, hi])
    groups.sort()
    for (_, a_hi), (b_lo, _) in zip(groups, groups[1:]):
        if b_lo <= a_hi:
            raise TournamentError("Skill groups can't overlap.")
    if len(groups) == 1:
        raise TournamentError("Use at least two skill groups, or leave it blank for one bracket.")
    return groups


def skill_groups_text(groups: list[list[int]]) -> str:
    return ", ".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in groups)


def group_for(groups: list[list[int]], skill: int) -> list[int] | None:
    return next((g for g in groups if g[0] <= skill <= g[1]), None)
