"""'Alert me' before registration opens: a player taps a button and is emailed (and texted, if they agreed and we
have their cell number) 5 minutes before online registration opens.

Kept in the event's settings as {member number: {"at", "texts", "phone"}}. Each alert goes out once. If the app was
down at the time, it still goes out until registration has been open for 30 minutes, then it's dropped as stale.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import schedule

LEAD = timedelta(minutes=5)
STALE = timedelta(minutes=30)


def wanted(s, member: str) -> dict | None:
    return (getattr(s, "open_alerts", None) or {}).get(member)


def can_ask(s, at: datetime | None = None) -> bool:
    """Is registration still to open at a set time (so an alert means something)?"""
    return schedule.reg_window(s, at)[0] == schedule.BEFORE


def add(s, member: str, texts: bool, phone: str, at: datetime | None = None) -> dict:
    """Sign a member up. `texts` is their yes to text alerts for this event, taken with the button."""
    rec = {"at": (at or datetime.now()).isoformat(timespec="seconds"), "texts": bool(texts and phone),
           "phone": phone if texts else ""}
    s.open_alerts = dict(getattr(s, "open_alerts", None) or {})
    s.open_alerts[member] = rec
    return rec


def remove(s, member: str) -> None:
    s.open_alerts = {k: v for k, v in (getattr(s, "open_alerts", None) or {}).items() if k != member}


def due(s, at: datetime | None = None) -> list[str]:
    """Members to alert now: from 5 minutes before registration opens until 30 minutes after."""
    at = at or datetime.now()
    waiting = {k: v for k, v in (getattr(s, "open_alerts", None) or {}).items() if not v.get("sent")}
    if not waiting or s.registration_closed or s.test_event:
        return []
    try:
        opens = schedule.parse_when(s.registration_opens)
    except Exception:
        return []
    if opens is None or not opens - LEAD <= at < opens + STALE:
        return []
    return list(waiting)


def _time(s) -> str:
    opens = schedule.parse_when(s.registration_opens)
    return schedule.when_text(opens).split(" at ")[-1] if opens else "soon"


def email_text(t, link: str = "") -> tuple[str, str]:
    """(subject, body) of the email. `link` is the registration page address, if the site's address is known."""
    s = t.settings
    lines = [f"Registration for {s.name} opens at {_time(s)}.", ""]
    if link:
        lines += [f"Register here: {link}", ""]
    lines.append("You asked us to alert you. This is the only message you'll get about it.")
    return f"Registration for {s.name} opens in 5 minutes", "\n".join(lines)


def text_body(t, link: str = "") -> str:
    s = t.settings
    return f"{s.name}: registration opens at {_time(s)}." + (f" Register: {link}" if link else "")
