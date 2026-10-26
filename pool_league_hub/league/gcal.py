"""Two-way sync with the league's Google Calendar.

Out to Google (the hub owns these): calendar events, each location's league nights (one
weekly repeating event per night, between the season dates) and host blackouts. Anything
the hub changes is marked (core.mark) and sent on the next sync; deletions are queued in
gcal.deleted. Every event the hub writes carries its hub id in Google's private extended
properties, so the hub knows it again when it comes back.

In from Google: events added in Google become hub events of the "From Google Calendar"
type (at a host location when the Google event's location names one, otherwise
league-wide); changes made in Google to a hub event's title, date, time or notes come
back into the hub, and an event deleted in Google is deleted in the hub. League nights
and blackouts belong to the hub and the host: changes made to them in Google are put back,
except a single week of a league night moved or cancelled in Google (for a holiday), which
stays as Google has it.
If an event was changed in both places since the last sync, the hub's version wins.

The hub signs in to Google as a service account that the league's calendar is shared
with ("Make changes to events"). See the README for setting it up.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .core import ALL, DAYS, FROM_GOOGLE, TYPES, League, mark, parse_clock

MARKER = "Pool League Hub"  # the line that ends the description the hub writes
EVENT_HOURS = 5  # the hub has only a start time; Google needs an end
IMPORT_DAYS_BACK = 30  # events added in Google before this are history, not imported
RRULE_DAYS = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"]


class SyncTokenExpired(Exception):
    """Google forgot where the last sync left off; list everything again."""


class NotFound(Exception):
    """The event is gone from Google."""


class GoogleCalendar:
    """The few Google Calendar API calls the sync uses, signed in as a service account."""

    SCOPE = "https://www.googleapis.com/auth/calendar.events"

    def __init__(self, calendar_id: str, service_account_info: dict):
        from google.auth.transport.requests import AuthorizedSession  # only needed when sync is on
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_info(service_account_info, scopes=[self.SCOPE])
        self.http = AuthorizedSession(creds)
        self.url = f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id, safe='')}/events"

    def _check(self, r):
        if r.status_code in (404, 410):
            raise NotFound()
        r.raise_for_status()
        return r.json() if r.content else {}

    def list_changes(self, sync_token: str) -> tuple[list[dict], str]:
        """Everything changed since sync_token (or every event, without one), and the next token."""
        items, params = [], {"singleEvents": "false", "showDeleted": "true", "maxResults": 250}
        if sync_token:
            params["syncToken"] = sync_token
        while True:
            r = self.http.get(self.url, params=params, timeout=30)
            if r.status_code == 410:
                raise SyncTokenExpired()
            data = self._check(r)
            items += data.get("items", [])
            if data.get("nextPageToken"):
                params["pageToken"] = data["nextPageToken"]
            else:
                return items, data.get("nextSyncToken", "")

    def insert(self, body: dict) -> dict:
        return self._check(self.http.post(self.url, json=body, timeout=30))

    def update(self, event_id: str, body: dict) -> dict:
        """Change only the fields the hub writes (PATCH), so reminders or guests added in Google stay."""
        return self._check(self.http.patch(f"{self.url}/{quote(event_id, safe='')}", json=body, timeout=30))

    def delete(self, event_id: str) -> None:
        try:
            self._check(self.http.delete(f"{self.url}/{quote(event_id, safe='')}", timeout=30))
        except NotFound:
            pass  # already gone


def client_from_env(env) -> GoogleCalendar | None:
    """The Google Calendar client from GOOGLE_CALENDAR_ID and the service account key, or None when not set up."""
    calendar_id = env.get("GOOGLE_CALENDAR_ID", "").strip()
    key = env.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not key and env.get("GOOGLE_SERVICE_ACCOUNT_FILE"):
        with open(env["GOOGLE_SERVICE_ACCOUNT_FILE"]) as f:
            key = f.read()
    if not calendar_id or not key:
        return None
    return GoogleCalendar(calendar_id, json.loads(key))


# ------------------------------------------------------------- hub rows -> Google events

def when(day: str, time_text: str, tz: str, hours: int = EVENT_HOURS) -> tuple[dict, dict]:
    """Google start and end for a day and a time like '7:00 PM' (all day when there's no time)."""
    clock = parse_clock(time_text)
    if clock is None:
        return all_day(day, day)
    start = datetime.fromisoformat(day).replace(hour=clock[0], minute=clock[1])
    fmt = "%Y-%m-%dT%H:%M:%S"
    # the other kind of time is cleared (null), so an update can switch between all-day and timed
    return ({"dateTime": start.strftime(fmt), "timeZone": tz, "date": None},
            {"dateTime": (start + timedelta(hours=hours)).strftime(fmt), "timeZone": tz, "date": None})


def all_day(first: str, last: str) -> tuple[dict, dict]:
    end = (date.fromisoformat(last) + timedelta(days=1)).isoformat()  # Google's end day is the day after
    return {"date": first, "dateTime": None, "timeZone": None}, {"date": end, "dateTime": None, "timeZone": None}


def place(s: dict, loc_id: str) -> str:
    loc = League.location(s, loc_id)
    return ", ".join(x for x in (loc["name"], loc["address"]) if x) if loc else ""


def tagged(kind: str, hub_id: str, body: dict) -> dict:
    body["extendedProperties"] = {"private": {"hub": kind, "hub_id": hub_id}}
    return body


def event_body(s: dict, e: dict, tz: str) -> dict:
    start, end = when(e["date"], e["time"], tz)
    about = [e["notes"], f"Run by {e['director']}" if e["director"] else "",
             f"{TYPES[e['type']][0]} · {MARKER}"]
    return tagged("event", e["id"], {"summary": e["title"], "location": place(s, e["loc"]),
                                     "description": "\n\n".join(x for x in about if x), "start": start, "end": end})


def night_body(s: dict, loc: dict, i: int, tz: str, today: str) -> dict | None:
    """One weekly repeating event for a location's league night, or None when the season is over."""
    night, season = loc["nights"][i], s["settings"]
    first = date.fromisoformat(season["season_start"] or today)
    first += timedelta(days=(night["dow"] - (first.weekday() + 1) % 7) % 7)
    rule = f"RRULE:FREQ=WEEKLY;BYDAY={RRULE_DAYS[night['dow']]}"
    if season["season_end"]:
        if first.isoformat() > season["season_end"]:
            return None
        rule += f";UNTIL={season['season_end'].replace('-', '')}T235959Z"
    start, end = when(first.isoformat(), night["time"], tz)
    return tagged("night", f"{loc['id']}:{i}", {
        "summary": f"{night['label']} · {loc['name']}", "location": place(s, loc["id"]), "start": start, "end": end,
        "recurrence": [rule], "description": f"Weekly league night, {DAYS[night['dow']]}s · {MARKER}"})


def blackout_body(s: dict, b: dict) -> dict:
    loc = League.location(s, b["loc"])
    start, end = all_day(b["start"], b["end"])
    return tagged("blackout", b["id"], {
        "summary": f"{loc['name'] if loc else 'Host'} closed: {b['reason']}", "location": place(s, b["loc"]),
        "start": start, "end": end, "transparency": "transparent", "description": f"Host blackout · {MARKER}"})


# ------------------------------------------------------------- Google events -> hub fields

def from_google(item: dict, tz: str) -> dict | None:
    """The hub's date, time, title and notes for a Google event (None if it has no start)."""
    start = item.get("start") or {}
    if start.get("dateTime"):
        dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
        day, time = dt.date().isoformat(), dt.strftime("%I:%M %p").lstrip("0")
    elif start.get("date"):
        day, time = start["date"], "All day"
    else:
        return None
    notes = (item.get("description") or "").split(f" · {MARKER}")[0]
    # drop the lines the hub adds (the type, "Run by ...") so notes don't grow on every round trip
    parts = [p for p in notes.split("\n\n") if p and not p.startswith("Run by ") and p not in
             (label for label, _ in TYPES.values())]
    return {"date": day, "time": time, "title": (item.get("summary") or "").strip() or "(No title)",
            "notes": "\n\n".join(parts).strip()}


def guess_location(s: dict, item: dict) -> str:
    text = f"{item.get('location', '')} {item.get('summary', '')}".lower()
    hits = [l["id"] for l in s["locations"] if l["name"] and l["name"].lower() in text]
    return hits[0] if len(hits) == 1 else ALL


class Sync:
    def __init__(self, league: League, client, tz: str = "America/New_York"):
        self.league, self.client, self.tz = league, client, tz
        self.lock = threading.Lock()  # one sync at a time
        self.wake = threading.Event()

    def run(self) -> dict:
        """Pull Google's changes, then send the hub's. Returns what happened (also kept in gcal)."""
        with self.lock:
            report = {"pulled": 0, "sent": 0, "deleted": 0, "error": ""}
            try:
                report["pulled"] = self.pull()
                report["sent"] = self.push()
                report["deleted"] = self.push_deletes()
            except Exception as exc:  # keep the hub working; show the problem in Setup
                report["error"] = f"{type(exc).__name__}: {exc}"[:300]
            with self.league.change() as s:
                s["gcal"]["last_sync"] = datetime.now().isoformat(timespec="seconds")
                s["gcal"]["error"] = report["error"]
            return report

    # ----------------------------------------------------- in from Google

    def pull(self) -> int:
        token = self.league.load()["gcal"]["sync_token"]
        try:
            items, next_token = self.client.list_changes(token)
        except SyncTokenExpired:
            items, next_token = self.client.list_changes("")
        changed = 0
        with self.league.change() as s:
            for item in items:
                changed += self.apply(s, item)
            s["gcal"]["sync_token"] = next_token
        return changed

    def apply(self, s: dict, item: dict) -> int:
        """One changed Google event into the hub. Returns 1 if the hub changed."""
        gid = item.get("id", "")
        hub = (item.get("extendedProperties") or {}).get("private", {})
        cancelled = item.get("status") == "cancelled"

        # League nights and blackouts: the hub's version goes back to Google (made again if deleted there).
        # A single week of a league night moved or cancelled in Google (a holiday, say) stays as Google has it.
        if item.get("recurringEventId") in {n.get("google_id") for l in s["locations"] for n in l["nights"]} - {""}:
            return 0
        for loc in s["locations"]:
            for night in loc["nights"]:
                if night.get("google_id") == gid:
                    return self.restore(loc, night, item, cancelled)
        for b in s["blackouts"]:
            if b.get("google_id") == gid:
                return self.restore(b, b, item, cancelled)
        if hub.get("hub") in ("night", "blackout"):
            if not cancelled:
                s["gcal"]["deleted"].append(gid)  # left over from something the hub has since removed
            return 0

        event = next((e for e in s["events"] if e.get("google_id") == gid), None)
        if event is None and hub.get("hub") == "event" and not cancelled:
            event = next((e for e in s["events"] if e["id"] == hub.get("hub_id") and not e.get("google_id")), None)
        if cancelled:
            if event is None:
                return 0
            League.unschedule(s, event)
            s["events"].remove(event)
            return 1
        fields = from_google(item, self.tz)
        if fields is None:
            return 0
        if event is not None:
            event["google_id"] = gid
            if item.get("etag") and item.get("etag") == event.get("etag"):
                return 0  # the hub's own change coming back
            event["etag"] = item.get("etag", "")
            if event.get("sync") or all(event[k] == v for k, v in fields.items()):
                return 0  # unchanged, or changed in the hub since: the hub's version wins and goes out next
            event.update(fields)
            return 1
        if hub.get("hub") == "event":
            s["gcal"]["deleted"].append(gid)  # a copy of an event the hub has since deleted
            return 0
        if fields["date"] < (date.fromisoformat(self.league.today()) - timedelta(days=IMPORT_DAYS_BACK)).isoformat():
            return 0
        if item.get("recurrence"):
            fields["notes"] = (fields["notes"] + "\n\nRepeats in Google Calendar; the hub shows the first date.").strip()
        event = {"id": League._id("ev", s["events"]), **fields, "loc": guess_location(s, item), "type": FROM_GOOGLE,
                 "director": "", "request": "", "google_id": gid}
        mark(event)  # sent back once, tagged with its hub id
        s["events"].append(event)
        return 1

    @staticmethod
    def restore(owner: dict, row: dict, item: dict, cancelled: bool) -> int:
        """Google changed a league night or blackout; unless it's the hub's own write, send the hub's back."""
        if cancelled:
            row["google_id"] = ""
        elif item.get("etag") and item.get("etag") == row.get("etag"):
            return 0
        if not owner.get("sync"):
            mark(owner)
        return 0

    # ----------------------------------------------------- out to Google

    def jobs(self, s: dict) -> list[tuple]:
        """(where the row is, its sync mark, Google id, body) for everything marked."""
        out, today = [], self.league.today()
        for e in s["events"]:
            if e.get("sync"):
                out.append((("events", e["id"], None), e["sync"], e.get("google_id", ""), event_body(s, e, self.tz)))
        for b in s["blackouts"]:
            if b.get("sync"):
                out.append((("blackouts", b["id"], None), b["sync"], b.get("google_id", ""), blackout_body(s, b)))
        for loc in s["locations"]:
            if loc.get("sync"):
                for i, n in enumerate(loc["nights"]):
                    out.append((("locations", loc["id"], i), loc["sync"], n.get("google_id", ""),
                                night_body(s, loc, i, self.tz, today)))
                if not loc["nights"]:
                    out.append((("locations", loc["id"], None), loc["sync"], "", None))
        return out

    def push(self) -> int:
        done = []  # (kind, row id, night index, sync mark, Google id, etag)
        for (kind, row_id, night), sync_mark, gid, body in self.jobs(self.league.load()):
            result = {"id": "", "etag": ""}
            if body is None:  # nothing to show in Google (e.g. a league night after the season)
                if gid:
                    self.client.delete(gid)
            else:
                try:
                    result = self.client.update(gid, body) if gid else self.client.insert(body)
                except NotFound:
                    result = self.client.insert(body)  # deleted in Google: make it again
            done.append((kind, row_id, night, sync_mark, result["id"], result.get("etag", "")))
        with self.league.change() as s:
            for kind, row_id, night, _, gid, etag in done:
                row = next((r for r in s[kind] if r["id"] == row_id), None)
                if kind == "locations" and row is not None and night is not None:
                    row = row["nights"][night] if night < len(row["nights"]) else None
                elif kind == "locations":
                    continue  # a location with no league nights: nothing to remember
                if row is None:
                    if gid:
                        s["gcal"]["deleted"].append(gid)  # removed in the hub while it was being sent
                    continue
                row["google_id"], row["etag"] = gid, etag
            for kind, row_id, _, sync_mark, _, _ in done:
                row = next((r for r in s[kind] if r["id"] == row_id), None)
                if row is not None and row.get("sync") == sync_mark:
                    row.pop("sync")  # not changed again while it was being sent
        return len(done)

    def push_deletes(self) -> int:
        gone = list(self.league.load()["gcal"]["deleted"])
        for gid in gone:
            self.client.delete(gid)
        with self.league.change() as s:
            s["gcal"]["deleted"] = [g for g in s["gcal"]["deleted"] if g not in gone]
        return len(gone)

    def loop(self, minutes: int, log) -> None:
        """Sync every few minutes, and soon after any change in the hub."""
        while True:
            report = self.run()
            if report["error"]:
                log(report["error"])
            self.wake.wait(timeout=minutes * 60)
            if self.wake.is_set():
                self.wake.clear()
                self.wake.wait(timeout=5)  # let a burst of changes settle
                self.wake.clear()
