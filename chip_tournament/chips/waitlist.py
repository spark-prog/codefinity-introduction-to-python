"""Automatic waiting list: an open spot is offered to the next team, who has hours to accept.

When a spot opens before the start, the next team on the waiting list is texted an offer with a
deadline (the event's 'offer hours', 12 by default, but never past 30 minutes before the start).
Accepting puts them in the field; declining takes them off the waiting list; letting it run out
moves them to the end of the waiting list and the spot is offered to the next team. Too close to the
start, or with offer hours set to 0, the next team simply moves in, as before.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .engine import TournamentError

LAST_CALL_BEFORE_START = timedelta(minutes=30)
TOO_SHORT = timedelta(minutes=15)


def start_of(settings) -> datetime | None:
    try:
        day = datetime.strptime((settings.date or "").strip(), "%Y-%m-%d")
    except ValueError:
        return None
    text = " ".join((settings.start_time or "").upper().replace(".", "").split())
    for fmt in ("%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H:%M"):
        try:
            t = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return day.replace(hour=t.hour, minute=t.minute)
    return day  # a date with no readable time: midnight, to be safe


def deadline(t, at: datetime | None = None) -> datetime | None:
    """When an offer made now runs out, or None if the next team should just move in."""
    hours = getattr(t.settings, "waitlist_offer_hours", 12)
    if not hours or t.status != "setup":
        return None
    at = at or datetime.now()
    until = at + timedelta(hours=hours)
    start = start_of(t.settings)
    if start is not None:
        until = min(until, start - LAST_CALL_BEFORE_START)
    return until if until - at >= TOO_SHORT else None


def when_text(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{d.strftime('%I:%M %p').lstrip('0')} {d:%a, %b} {d.day}"


def groups(t) -> dict:
    return t.teams if hasattr(t, "teams") else t.entries


def offered(t) -> list:
    return [groups(t)[i] for i in t.waitlist if groups(t)[i].offer_until]


def make_offer(t, g, until: datetime, link: str) -> None:
    g.offer_until = until.isoformat(timespec="minutes")
    t._say(f"Offered {g.name} the open spot until {when_text(g.offer_until)}.")
    body = (f"A spot opened in {t.settings.name}! {g.name} has until {when_text(g.offer_until)} to take it."
            + (f" Accept or decline here: {link}" if link else " Open your team page to accept or decline."))
    (t._text_team if hasattr(t, "teams") else t._text)(g, body)


def accept(t, gid: str, at: datetime | None = None) -> str:
    g = groups(t).get(gid)
    if g is None or not g.offer_until:
        raise TournamentError("There's no open spot waiting for you right now.")
    if datetime.fromisoformat(g.offer_until) <= (at or datetime.now()):
        raise TournamentError("That offer ran out, so the spot went to the next team. You're still on the waiting list.")
    g.offer_until = ""
    t.waitlist.remove(gid)
    t._promote(g)
    return g.name


def decline(t, gid: str) -> str:
    g = groups(t).get(gid)
    if g is None or not g.offer_until:
        raise TournamentError("There's no open spot waiting for you right now.")
    name = g.name
    g.offer_until = ""
    t._say(f"{name} turned down the open spot and left the waiting list.")
    (t.remove_team if hasattr(t, "teams") else t.remove_entry)(gid)
    t._fill_from_waitlist()  # the spot is offered to the next team
    return name


def expire(t, at: datetime | None = None) -> int:
    """Offers that ran out: to the end of the waiting list, and the spot is offered to the next team."""
    at = at or datetime.now()
    late = [g for g in offered(t) if datetime.fromisoformat(g.offer_until) <= at]
    for g in late:
        g.offer_until = ""
        t.waitlist.remove(g.id)
        t.waitlist.append(g.id)
        t._say(f"{g.name} didn't take the open spot in time: back to the end of the waiting list.")
        (t._text_team if hasattr(t, "teams") else t._text)(
            g, f"{g.name}: the offer for {t.settings.name} ran out, so the spot went to the next team. "
               "You're still on the waiting list.")
    if late:
        t._fill_from_waitlist()
    return len(late)
