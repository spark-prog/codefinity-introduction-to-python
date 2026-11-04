"""Calendar files and event reminders.

* Calendar: every event with a date and start time has an .ics file (Apple Calendar, Outlook, most phones) and a
  Google Calendar link. Times are the venue's (the server's TZ setting), so they show right on players' phones.
* Reminders: registered players are emailed (and, if the event turns it on, texted) the day before and 2 hours
  before the start. Each reminder goes out once per event; a player who registers after the day-before one gets
  only the 2-hour one. If the app was down when one was due, it catches up only while it's still useful: never
  after the start, and never the day-before one inside the last 2 hours.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .engine import start_of

DAY_BEFORE, TWO_HOURS = "day", "2h"
LEADS = {DAY_BEFORE: timedelta(hours=24), TWO_HOURS: timedelta(hours=2)}
EVENT_HOURS = 4  # how long a calendar entry lasts (events have no set end)
IN_FIELD = ("registered", "active")  # entries/teams that get reminders (not waitlisted, no-shows or withdrawn)


def due(s, now: datetime | None = None) -> str | None:
    """The reminder to send now for these settings (DAY_BEFORE, TWO_HOURS), or None."""
    start = start_of(s)
    now = now or datetime.now()
    if not start or now >= start or not getattr(s, "reminders", True) or s.test_event:
        return None
    sent = s.reminders_sent or {}
    if now >= start - LEADS[TWO_HOURS]:
        return None if TWO_HOURS in sent else TWO_HOURS
    if now >= start - LEADS[DAY_BEFORE] and DAY_BEFORE not in sent:
        return DAY_BEFORE
    return None


def recipients(t) -> list[tuple[object, object]]:
    """(entry or team, player) for everyone in the field who should be reminded."""
    groups = getattr(t, "entries", None) or getattr(t, "teams", None) or {}
    out = []
    for grp in groups.values():
        if grp.status not in IN_FIELD or getattr(grp, "withdrawn", False):
            continue
        for p in grp.players:
            out.append((grp, p))
    return out


def site_root(t) -> str:
    """The site's address (https://controltable.app): the SITE_URL setting, else the one the event saved. An event
    saves its own page address (https://.../t/<event>), so that part is taken off."""
    root = (os.environ.get("SITE_URL", "").strip() or getattr(t, "site_url", "") or "").rstrip("/")
    return root.split("/t/", 1)[0] if "/t/" in root else root


def when_line(s) -> str:
    start = start_of(s)
    day = start.strftime("%a, %b %-d") if start else s.date
    line = f"{day} at {s.start_time}"
    if s.checkin_by:
        line += f" (check in by {s.checkin_by})"
    return line


def where_line(s) -> str:
    return ", ".join(x for x in (s.venue, s.address) if x) or "See the event page"


def day_word(s, now: datetime | None = None) -> str:
    """'today' or 'tomorrow' from the calendar date, not from which reminder it is: a day-before reminder that
    goes out on the event's own date (an event set up that morning) says today."""
    start = start_of(s)
    now = now or datetime.now()
    if start and start.date() == now.date():
        return "today"
    if start and (start.date() - now.date()).days == 1:
        return "tomorrow"
    return f"on {start.strftime('%a, %b %-d')}" if start else "coming up"


def email_text(t, event_id: str, kind: str, grp, which: str, now: datetime | None = None) -> tuple[str, str]:
    """(subject, body) of a reminder email for one entry or team."""
    s = t.settings
    start = start_of(s)
    day = day_word(s, now)
    soon = f"{day} at {s.start_time}" if day == "today" else day
    subject = f"Reminder: {s.name} is {soon}"
    root = site_root(t)
    page = f"{root}/t/{event_id}/{'entry' if kind == 'bracket' else 'team'}/{grp.id}" if root else ""
    lines = [f"This is a reminder that you're registered for {s.name}"
             + (f" ({grp.name})" if len(grp.players) > 1 or grp.name != grp.players[0].name else "") + ".", "",
             f"When: {when_line(s)}", f"Where: {where_line(s)}"]
    if page:
        lines += ["", f"Your page (table calls, scores and your PIN): {page}",
                  f"Add it to your calendar: {root}/t/{event_id}/calendar.ics"]
    lines += ["", "Can't make it? Unregister on your page so someone on the waiting list gets your spot."
              if page else "Can't make it? Let the tournament director know so someone on the waiting list gets your spot."]
    if start and which == TWO_HOURS:
        lines += ["", "See you soon, and good luck!"]
    return subject, "\n".join(lines)


def text_body(t, which: str, now: datetime | None = None) -> str:
    s = t.settings
    body = f"Reminder: {s.name} is {day_word(s, now)} at {s.start_time}"
    if s.venue:
        body += f" at {s.venue}"
    body += "."
    if s.checkin_by:
        body += f" Check in by {s.checkin_by}."
    return body


# ------------------------------------------------------------------ calendar

def _ics_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """iCalendar lines are at most 75 octets: longer ones continue on the next line after a space."""
    out, raw = [], line.encode()
    while len(raw) > 75:
        cut = 75
        while (raw[cut] & 0xC0) == 0x80:  # don't split a multi-byte character
            cut -= 1
        out.append(raw[:cut].decode())
        raw = b" " + raw[cut:]
    out.append(raw.decode())
    return "\r\n".join(out)


def ics(t, event_id: str, url: str = "") -> str | None:
    """The event as an iCalendar file, or None without a date and start time."""
    s = t.settings
    start = start_of(s)
    if not start:
        return None
    end = start + timedelta(hours=EVENT_HOURS)
    tz = os.environ.get("TZ", "").strip()
    fmt = "%Y%m%dT%H%M%S"
    stamp = datetime.now(timezone.utc).strftime(fmt) + "Z"
    when = (lambda d: f";TZID={tz}:{d.strftime(fmt)}") if tz else (lambda d: f":{d.strftime(fmt)}")
    details = [x for x in (f"Check in by {s.checkin_by}." if s.checkin_by else "", s.details or "", url) if x]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Control Table//Events//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "BEGIN:VEVENT", f"UID:{event_id}-{s.date}@controltable",
             f"DTSTAMP:{stamp}", f"DTSTART{when(start)}", f"DTEND{when(end)}",
             f"SUMMARY:{_ics_escape(s.name)}", f"LOCATION:{_ics_escape(where_line(s) if s.venue or s.address else '')}",
             f"DESCRIPTION:{_ics_escape(chr(10).join(details))}"]
    if url:
        lines.append(f"URL:{url}")
    lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_escape(s.name)}", "TRIGGER:-PT2H", "END:VALARM",
              "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"


def google_link(t, url: str = "") -> str:
    s = t.settings
    start = start_of(s)
    if not start:
        return ""
    fmt = "%Y%m%dT%H%M%S"
    params = {"action": "TEMPLATE", "text": s.name,
              "dates": f"{start.strftime(fmt)}/{(start + timedelta(hours=EVENT_HOURS)).strftime(fmt)}",
              "details": "\n".join(x for x in (f"Check in by {s.checkin_by}." if s.checkin_by else "", url) if x),
              "location": where_line(s) if s.venue or s.address else ""}
    tz = os.environ.get("TZ", "").strip()
    if tz:
        params["ctz"] = tz
    return "https://calendar.google.com/calendar/render?" + urlencode(params)
