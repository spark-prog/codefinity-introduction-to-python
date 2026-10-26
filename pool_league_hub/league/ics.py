"""Calendar feeds (iCalendar) that hosts and reps subscribe to from Google Calendar or a phone.

A host's feed has its league nights, its events, league-wide dates and its blackouts; the
league feed has everything. Google Calendar: Other calendars → + → From URL. Google checks
a subscribed feed every few hours, so changes show up there later, not right away.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .core import ALL, DAYS, TYPES, League, parse_clock
from .gcal import EVENT_HOURS, RRULE_DAYS


def esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    """Lines longer than 75 bytes continue on the next line after a space."""
    out, raw = [], line.encode()
    while len(raw) > 75:
        cut = 75
        while raw[cut] & 0xC0 == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(raw[:cut].decode())
        raw = b" " + raw[cut:]
    out.append(raw.decode())
    return "\r\n".join(out)


def times(day: str, time_text: str, tz: str) -> list[str]:
    clock = parse_clock(time_text)
    d = date.fromisoformat(day)
    if clock is None:
        return [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"]
    start = datetime(d.year, d.month, d.day, *clock)
    return [f"DTSTART;TZID={tz}:{start:%Y%m%dT%H%M%S}",
            f"DTEND;TZID={tz}:{start + timedelta(hours=EVENT_HOURS):%Y%m%dT%H%M%S}"]


def feed(league: League, s: dict, tz: str, loc_id: str | None = None) -> str:
    """The calendar for one location (its own plus league-wide dates), or for the whole league."""
    today = league.today()
    since = (date.fromisoformat(today) - timedelta(days=60)).isoformat()
    name = League.location_name(s, loc_id) if loc_id else s["settings"]["name"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Pool League Hub//EN", "CALSCALE:GREGORIAN",
             f"X-WR-CALNAME:{esc(name)} · league schedule", f"X-WR-TIMEZONE:{tz}"]

    def vevent(uid: str, summary: str, when: list[str], where: str = "", about: str = "", extra=()):
        lines.extend(["BEGIN:VEVENT", f"UID:{uid}@pool-league-hub", f"DTSTAMP:{stamp}", *when,
                      f"SUMMARY:{esc(summary)}", *extra])
        if where:
            lines.append(f"LOCATION:{esc(where)}")
        if about:
            lines.append(f"DESCRIPTION:{esc(about)}")
        lines.append("END:VEVENT")

    def place(lid: str) -> str:
        loc = League.location(s, lid)
        return ", ".join(x for x in (loc["name"], loc["address"]) if x) if loc else ""

    for e in s["events"]:
        if e["date"] >= since and (not loc_id or e["loc"] in (loc_id, ALL)):
            about = "\n".join(x for x in (TYPES[e["type"]][0], f"Run by {e['director']}" if e["director"] else "",
                                          e["notes"]) if x)
            vevent(e["id"], e["title"], times(e["date"], e["time"], tz), place(e["loc"]), about)
    season = s["settings"]
    for loc in s["locations"]:
        if loc_id and loc["id"] != loc_id:
            continue
        for i, n in enumerate(loc["nights"]):
            first = date.fromisoformat(season["season_start"] or since)
            first += timedelta(days=(n["dow"] - (first.weekday() + 1) % 7) % 7)
            rule = f"RRULE:FREQ=WEEKLY;BYDAY={RRULE_DAYS[n['dow']]}"
            if season["season_end"]:
                rule += f";UNTIL={season['season_end'].replace('-', '')}T235959Z"
            summary = n["label"] if loc_id else f"{n['label']} · {loc['name']}"
            vevent(f"night-{loc['id']}-{i}", summary, times(first.isoformat(), n["time"], tz), place(loc["id"]),
                   f"Weekly league night, {DAYS[n['dow']]}s", [rule])
    for b in s["blackouts"]:
        if b["end"] >= since and (not loc_id or b["loc"] == loc_id):
            end = date.fromisoformat(b["end"]) + timedelta(days=1)
            vevent(b["id"], f"{League.location_name(s, b['loc'])} closed: {b['reason']}",
                   [f"DTSTART;VALUE=DATE:{b['start'].replace('-', '')}", f"DTEND;VALUE=DATE:{end:%Y%m%d}"],
                   place(b["loc"]), "Host blackout", ["TRANSP:TRANSPARENT"])
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in lines) + "\r\n"
