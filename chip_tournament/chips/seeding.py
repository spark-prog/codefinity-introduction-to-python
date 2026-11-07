"""Seed a singles bracket by skill level.

The order is set for the event's game (8-Ball or 9-Ball) and goes best first. Skill groups are separate brackets,
so each one takes its own players from this order, best first: the SL 2-3 group seeds all its 2s and 3s from
highest to lowest.

  1. The player's skill level in the event's game, highest first.
  2. Their level in the other game, highest first (5/6 above 5/5 above 5/4), but only if they played that game in
     the 4 months before the event. Otherwise it isn't counted and they rank as if both levels were the same.
  3. First name, A to Z.

Win percentage isn't used until the player sheet has it.

A player with no level in the event's game ranks by their other level: a 9-Ball level is converted the way the
rack race charts do (see race_charts.py); a 9-Ball event uses the 8-Ball level as it is.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime

from . import race_charts

RECENT_MONTHS = 4
GAMES = {"8": "8-Ball", "9": "9-Ball"}


def game_for(settings) -> str:
    """The game an event is played in ("8" or "9"): a ball count is 9-Ball; else the name, else the skill-level
    setting. The director can change it on the Seed by skill button."""
    name = (settings.name or "").lower().replace("-", " ")
    if getattr(settings, "race_method", "") == "ball_count":
        return "9"
    if "9 ball" in name or "9ball" in name:
        return "9"
    if "8 ball" in name or "8ball" in name:
        return "8"
    return "9" if settings.skill_game == "9" else "8"


def event_day(settings) -> date:
    try:
        return datetime.strptime((settings.date or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return date.today()


def months_before(day: date, months: int) -> date:
    """The same day of the month `months` calendar months earlier (the last day of that month if it's shorter)."""
    y, m = divmod(day.year * 12 + day.month - 1 - months, 12)
    m += 1
    return date(y, m, min(day.day, calendar.monthrange(y, m)[1]))


def played_since(last_played: str, cutoff: date) -> bool:
    try:
        return datetime.strptime(last_played or "", "%Y-%m-%d").date() >= cutoff
    except ValueError:
        return False


def _day(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%b %-d")
    except ValueError:
        return ""


def levels(player, record: dict | None, game: str, day: date) -> tuple[int, int, str]:
    """(level to rank by in this game, the other game's level to rank by next, a short note for the director)."""
    other_game = "9" if game == "8" else "8"
    mine, other = ((player.skill_8, player.skill_9) if game == "8" else (player.skill_9, player.skill_8))
    here, there = GAMES[game], GAMES[other_game]
    if not mine:
        if other:  # no level in this game: rank by the other one
            primary = race_charts.eight_ball_equivalent(other) if game == "8" else other
            return primary, primary, f"No {here} level: ranked by {there} {other}"
        return player.skill, player.skill, f"{here} {player.skill}" if player.skill else "No skill level"
    if not other:
        return mine, mine, f"{here} {mine}"
    last = (record or {}).get(f"last{other_game}", "")
    if played_since(last, months_before(day, RECENT_MONTHS)):
        return mine, other, f"{here} {mine} · {there} {other} (played {_day(last)})"
    why = f"last played {_day(last)}" if last else "no play on record"
    return mine, mine, f"{here} {mine} · {there} {other} not counted ({why})"


def rank(t, entry_ids: list[str], game: str, record_for) -> list[tuple[str, str]]:
    """[(entry id, note)] best seed first. `record_for(member)` gives a player-sheet record or None."""
    day = event_day(t.settings)
    rows = []
    for n, entry_id in enumerate(entry_ids):
        entry = t.entries[entry_id]
        player = entry.players[0]
        record = record_for(player.member) if player.member else None
        primary, other, note = levels(player, record, game, day)
        first = (player.name.split() or [""])[0].lower()
        rows.append(((-primary, -other, first, player.name.lower(), n), entry_id, note))
    rows.sort(key=lambda r: r[0])
    return [(entry_id, note) for _, entry_id, note in rows]
