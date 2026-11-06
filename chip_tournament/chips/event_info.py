"""What a player needs to know about an event at a glance: where it stands, what it costs, who can play, how full it is.

Used by the All Events cards and the top of every event page, so both say the same thing.
"""

from __future__ import annotations

from datetime import datetime

from . import eligibility, schedule
from .bracket import entries_label
from .engine import TEAM_SIZE, money, start_of


def is_bracket(t) -> bool:
    return getattr(t, "kind", "chip") == "bracket"


def status(t) -> dict:
    """Where the event stands: {'key', 'label', 'detail'}. The key picks the color (live, open, before, closed, finished)."""
    s = t.settings
    if t.status == "running":
        return {"key": "live", "label": "Live now", "detail": ""}
    if t.status == "finished":
        return {"key": "finished", "label": "Finished", "detail": ""}
    if not is_bracket(t) and (getattr(t, "entries_locked", False) or getattr(t, "last_call", False)):
        return {"key": "closed", "label": "Entries closed", "detail": ""}
    state, when = schedule.reg_window(s)
    if state == schedule.OPEN:
        return {"key": "open", "label": "Registration open",
                "detail": f"Closes {schedule.when_text(when)}" if when else ""}
    if state == schedule.BEFORE:
        return {"key": "before", "label": f"Registration opens {schedule.when_text(when)}", "detail": ""}
    return {"key": "closed", "label": "Registration closed",
            "detail": f"Closed {schedule.when_text(when)}" if state == schedule.AFTER and when else ""}


def fee(t, word: bool = True) -> str:
    """'Free', '$20 entry' or '$20 per team'. With word=False a single player's fee is just '$20'."""
    s = t.settings
    if not s.entry_fee_cents:
        return "Free"
    teams = not is_bracket(t) or s.team_size > 1
    return f"{money(s.entry_fee_cents)} per team" if teams else f"{money(s.entry_fee_cents)}{' entry' if word else ''}"


def spots(t) -> str:
    """'12 of 32 spots filled', 'Full · 3 waiting', or just the count when there's no limit."""
    s = t.settings
    bracket = is_bracket(t)
    count = len(t.field_entries if bracket else t.field_teams)
    noun = ("entry" if count == 1 else "entries") if bracket else ("team" if count == 1 else "teams")
    waiting = len(getattr(t, "waitlist", []) or [])
    if not s.field_cap:
        return f"{count} {noun}" + (f" · {waiting} waiting" if waiting else "")
    if count >= s.field_cap:
        return "Full" + (f" · {waiting} waiting" if waiting else "")
    return f"{count} of {s.field_cap} spots filled" + (f" · {waiting} waiting" if waiting else "")


def _group_text(groups: list[list[int]]) -> str:
    return ", ".join(str(lo) if lo == hi else f"{lo}–{hi}" for lo, hi in groups)


def _skill_clauses(t, group_detail: bool) -> list[str]:
    s = t.settings
    if not is_bracket(t):
        return [f"combined SL {s.registration_cap} or less to register"]
    bits = []
    if s.team_skill_cap and s.team_size > 1:
        bits.append(f"combined SL {s.team_skill_cap} or less")
    if s.max_player_skill:
        bits.append(f"no player above SL {s.max_player_skill}")
    if s.skill_groups:
        bits.append(f"skill groups SL {_group_text(s.skill_groups)}" if group_detail else "skill groups")
    return bits


def skill_rule(t) -> str:
    """One line on who can play by skill level: 'Doubles · combined SL 9 or less · no player above SL 6'."""
    s = t.settings
    parts = [entries_label(s.team_size).capitalize() if is_bracket(t) else f"Teams of {TEAM_SIZE}"]
    parts += _skill_clauses(t, group_detail=True) or ["all skill levels"]
    if eligibility.any_rules(eligibility.rules_of(s)):
        parts.append("eligibility rules apply")
    return " · ".join(parts)


def skill_short(t) -> str:
    """The skill rule without the team size, for the slim bar: 'combined SL 9 or less'."""
    return " · ".join(_skill_clauses(t, group_detail=False)) or "all skill levels"


def short_when(s) -> str:
    """'Sat, Nov 7 · 10:30 AM' (or just the date, or '' when there isn't one)."""
    start = start_of(s)
    if start:
        return f"{start:%a, %b} {start.day} · {start.strftime('%I:%M %p').lstrip('0')}"
    try:
        day = datetime.strptime((s.date or "").strip(), "%Y-%m-%d")
    except ValueError:
        return ""
    return f"{day:%a, %b} {day.day}"


def can_register(t) -> bool:
    """May a visitor register now? (The Register button, here and in the slim bar.)"""
    s = t.settings
    if is_bracket(t):
        return t.status == "setup" and not s.registration_shut
    return t.status != "finished" and not t.entries_locked and not t.last_call and not s.registration_shut


def can_alert(t) -> bool:
    """Is registration waiting to open at a set time (so 'Alert me' makes sense)?"""
    s = t.settings
    state, when = schedule.reg_window(s)
    return t.status == "setup" and state == schedule.BEFORE and when is not None and not getattr(t, "entries_locked", False)


def register_label(t) -> str:
    return "Register" if is_bracket(t) else "Register a team"


def tabs(t) -> list[dict]:
    """The tabs under an event's header: Details, Teams (or Players), Bracket (Live for chips), Rules."""
    s = t.settings
    bracket = is_bracket(t)
    count = len(t.field_entries if bracket else t.field_teams)
    people = "Players" if bracket and s.team_size == 1 else "Teams"
    return [{"key": "details", "label": "Details", "endpoint": "event_details", "count": None},
            {"key": "teams", "label": people, "endpoint": "event_teams", "count": count},
            {"key": "bracket", "label": "Bracket" if bracket else "Live", "endpoint": "event_bracket", "count": None},
            {"key": "rules", "label": "Rules", "endpoint": "rules_page", "count": None}]


def default_tab(t) -> str:
    """The tab an event opens on: who's registered while people are signing up, the bracket once it's running."""
    return "teams" if t.status == "setup" else "bracket"
