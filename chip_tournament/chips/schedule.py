"""Registration windows: online registration can open and close on its own at set times."""

from __future__ import annotations

from datetime import datetime

from .engine import TournamentError

OPEN, BEFORE, AFTER, CLOSED = "open", "before", "after", "closed"


def parse_when(value: str) -> datetime | None:
    """'2026-11-07T18:00' (a datetime-local field, venue time) -> datetime; blank -> None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        raise TournamentError(f"'{value}' isn't a date and time.")
    if not 2000 <= when.year <= 2100:
        raise TournamentError("Enter a real date for registration.")
    return when.replace(tzinfo=None)


def reg_window(s, at: datetime | None = None) -> tuple[str, datetime | None]:
    """Where registration stands: (OPEN, closes-or-None), (BEFORE, opens), (AFTER, closed-at) or (CLOSED, None).

    The director's Close registration box wins over the times."""
    at = at or datetime.now()
    if s.registration_closed:
        return CLOSED, None
    try:
        opens, closes = parse_when(s.registration_opens), parse_when(s.registration_closes)
    except TournamentError:
        return OPEN, None
    if opens and at < opens:
        return BEFORE, opens
    if closes and at >= closes:
        return AFTER, closes
    return OPEN, closes


def when_text(when: datetime) -> str:
    """'Fri, Nov 7 at 6:00 PM'."""
    return f"{when:%a, %b} {when.day} at {when.strftime('%I:%M %p').lstrip('0')}"


def shut_message(s, at: datetime | None = None) -> str:
    state, when = reg_window(s, at)
    if state == BEFORE:
        return f"Registration opens {when_text(when)}."
    if state == AFTER:
        return f"Registration closed {when_text(when)}."
    return "Registration is closed for now."


def check_times(opens: str, closes: str) -> tuple[str, str]:
    """Validate the director's times; returns them tidied ('' for blank)."""
    a, b = parse_when(opens), parse_when(closes)
    if a and b and b <= a:
        raise TournamentError("Registration has to close after it opens.")
    fmt = "%Y-%m-%dT%H:%M"
    return (a.strftime(fmt) if a else "", b.strftime(fmt) if b else "")
