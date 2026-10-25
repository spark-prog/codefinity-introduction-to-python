"""Pool League Hub: the league calendar, host locations and player match-ups.

Who does what:
  league office   the master calendar, confirming host requests, player sign-ups, setup
  host location   its own schedule, blackout dates, requesting weekend events, its public page
  division rep    claims requested events from the request board to run them
  player          a "looking for a team" listing and an inbox of team invites
  team captain    a team with open spots, browsing players and sending invites

Everything is kept in league.json in the data folder.
League nights repeat every week at each location (between the season dates, when set);
other events are single dates. A host's blackout dates block new events there, and
anything already on the calendar that lands on one is flagged as a conflict.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
GENERAL, LEAGUE_NIGHT = "general", "league"
TYPES = {  # event type -> (label, pool-ball number for its color)
    GENERAL: ("League-wide date", 1),
    LEAGUE_NIGHT: ("League night", 2),
    "singles_tourney": ("Singles tournament", 3),
    "doubles_tourney": ("Doubles tournament", 4),
    "singles_board": ("Singles board", 5),
}
REQUEST_TYPES = ["singles_board", "doubles_tourney", "singles_tourney"]
FORMATS = ["8-Ball", "9-Ball", "Scotch Doubles"]
LEVELS = ["Beginner", "Intermediate", "Advanced"]
ALL = "all"  # an event for every location (the general calendar)
OPEN, CLAIMED, SCHEDULED = "open", "claimed", "scheduled"
LOCATION_FIELDS = ("name", "area", "address", "phone", "email", "hours", "tables", "rates", "menu", "specials")


class LeagueError(ValueError):
    """A rule the league hub enforces; the message is shown to the person."""


def parse_day(text: str, what: str = "date") -> str:
    text = (text or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise LeagueError(f"Pick a {what}.") from None


def day_label(iso: str, long: bool = False) -> str:
    """'2026-11-07' -> 'Sat, Nov 7' (or 'Saturday, November 7')."""
    d = date.fromisoformat(iso)
    return f"{d:%A, %B} {d.day}" if long else f"{d:%a, %b} {d.day}"


def same_contact(a: str, b: str) -> bool:
    """A phone number matches by its digits, an email ignoring capitals."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if "@" in a or "@" in b:
        return bool(a) and a == b
    da, db = re.sub(r"\D", "", a)[-10:], re.sub(r"\D", "", b)[-10:]
    return len(da) >= 7 and da == db


def new_pin(digits: int = 4) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(digits))


def lines(text: str) -> list[str]:
    return [x.strip() for x in (text or "").splitlines() if x.strip()]


def label_rows(text: str) -> list[tuple[str, str]]:
    """'Tue: $3 domestics' lines -> [('Tue', '$3 domestics')]; lines without a short label get ''."""
    out = []
    for line in lines(text):
        head, sep, rest = line.partition(":")
        out.append((head.strip(), rest.strip()) if sep and 0 < len(head) < 20 else ("", line))
    return out


def empty() -> dict:
    return {"settings": {"name": "Pool League Hub", "season_start": "", "season_end": ""},
            "locations": [], "reps": [], "events": [], "blackouts": [], "requests": [],
            "signups": [], "players": [], "teams": [], "invites": []}


class League:
    def __init__(self, data_dir: str | Path, today=None):
        self.path = Path(data_dir) / "league.json"
        self.lock = threading.RLock()
        self._today = today  # tests pass a fixed day

    def today(self) -> str:
        return (self._today() if callable(self._today) else self._today or date.today()).isoformat()

    # ------------------------------------------------------------- storage

    def load(self) -> dict:
        data = empty()
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            data.update(saved)
            data["settings"] = {**empty()["settings"], **saved.get("settings", {})}
        return data

    @contextmanager
    def change(self):
        """Load, change, save; nothing is saved if a rule error is raised."""
        with self.lock:
            s = self.load()
            yield s
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(s, indent=1))
            tmp.replace(self.path)

    @staticmethod
    def _id(prefix: str, rows: list[dict]) -> str:
        used = {r["id"] for r in rows}
        while True:
            new = prefix + secrets.token_hex(3)
            if new not in used:
                return new

    @staticmethod
    def find(s: dict, kind: str, row_id: str) -> dict:
        row = next((r for r in s[kind] if r["id"] == row_id), None)
        if row is None:
            raise LeagueError("That's no longer here. Reload the page.")
        return row

    # ------------------------------------------------------------- reading

    @staticmethod
    def location(s: dict, loc_id: str) -> dict | None:
        return next((l for l in s["locations"] if l["id"] == loc_id), None)

    @classmethod
    def location_name(cls, s: dict, loc_id: str) -> str:
        if loc_id == ALL:
            return "All locations"
        loc = cls.location(s, loc_id)
        return loc["name"] if loc else "A closed location"

    @staticmethod
    def blackout_on(s: dict, loc_id: str, day: str) -> dict | None:
        return next((b for b in s["blackouts"] if b["loc"] == loc_id and b["start"] <= day <= b["end"]), None)

    def league_nights(self, s: dict, start: str, end: str, loc_id: str | None = None) -> list[dict]:
        """Each location's weekly league nights between two days, within the season (when set)."""
        season = s["settings"]
        start = max(start, season["season_start"] or start)
        end = min(end, season["season_end"] or end)
        out = []
        if start > end:
            return out
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        for loc in s["locations"]:
            if loc_id and loc["id"] != loc_id:
                continue
            for night in loc.get("nights", []):
                d = first + timedelta(days=(night["dow"] - (first.weekday() + 1) % 7) % 7)
                while d <= last:
                    out.append({"id": f"night-{loc['id']}-{d.isoformat()}", "date": d.isoformat(), "loc": loc["id"],
                                "type": LEAGUE_NIGHT, "title": night["label"], "time": night["time"], "weekly": True})
                    d += timedelta(days=7)
        return out

    def events_between(self, s: dict, start: str, end: str, loc_id: str | None = None) -> list[dict]:
        """Calendar events and league nights between two days (for one location: its own plus league-wide)."""
        evs = [e for e in s["events"] if start <= e["date"] <= end and (not loc_id or e["loc"] in (loc_id, ALL))]
        evs += self.league_nights(s, start, end, loc_id)
        return sorted(evs, key=lambda e: (e["date"], e["loc"] != ALL, e.get("time", "")))

    def conflicts(self, s: dict) -> list[tuple[dict, dict]]:
        """Upcoming events at a location on one of its blackout dates: (event, blackout)."""
        if not s["blackouts"]:
            return []
        today = self.today()
        last = max(b["end"] for b in s["blackouts"])
        out = []
        for e in self.events_between(s, today, max(today, last)):
            b = e["loc"] != ALL and self.blackout_on(s, e["loc"], e["date"])
            if b:
                out.append((e, b))
        return out

    @staticmethod
    def open_teams(s: dict, loc_id: str | None = None) -> list[dict]:
        return [t for t in s["teams"] if t["spots"] > 0 and (not loc_id or t["loc"] == loc_id)]

    @staticmethod
    def fit(player: dict, team: dict) -> int:
        """How well a player suits a team: 0-3 for night, location and format."""
        return (team["night"] in player["nights"]) + (team["loc"] in player["locs"]) + (team["format"] in player["formats"])

    # ------------------------------------------------------------- league office

    def save_settings(self, name: str, season_start: str, season_end: str) -> None:
        start = parse_day(season_start, "season start") if season_start.strip() else ""
        end = parse_day(season_end, "season end") if season_end.strip() else ""
        if start and end and end < start:
            raise LeagueError("The season ends before it starts.")
        with self.change() as s:
            s["settings"].update(name=name.strip() or "Pool League Hub", season_start=start, season_end=end)

    def save_location(self, loc_id: str | None, fields: dict, nights: list[dict] | None = None) -> dict:
        """Add a location (loc_id None) or update one. Hosts change only the page fields, not nights."""
        if (loc_id is None or "name" in fields) and not (fields.get("name") or "").strip():
            raise LeagueError("Give the location a name.")
        with self.change() as s:
            if loc_id is None:
                loc = {"id": self._id("loc", s["locations"]), "pin": new_pin(6), "nights": [],
                       **{k: "" for k in LOCATION_FIELDS}}
                s["locations"].append(loc)
            else:
                loc = self.find(s, "locations", loc_id)
            for k in LOCATION_FIELDS:
                if k in fields:
                    loc[k] = (fields[k] or "").replace("\r\n", "\n").strip()
            if nights is not None:
                loc["nights"] = sorted(nights, key=lambda n: n["dow"])
            return loc

    @staticmethod
    def parse_nights(text: str) -> list[dict]:
        """'Tue 7:00 PM 8-Ball Open · Division 1' lines -> weekly league nights."""
        out = []
        for line in lines(text):
            m = re.match(r"^(sun|mon|tue|wed|thu|fri|sat)\w*\.?\s+(\d{1,2}(?::\d\d)?\s*[ap]\.?m\.?)\s*[-–,:]?\s*(.*)$", line, re.I)
            if not m:
                raise LeagueError(f"Write each league night as 'Tue 7:00 PM 8-Ball Division 1', not '{line}'.")
            day, time, label = m.groups()
            time = re.sub(r"\s*([ap])\.?m\.?$", lambda x: f" {x.group(1).upper()}M", time.strip(), flags=re.I)
            out.append({"dow": [d.lower() for d in DAYS].index(day.lower()[:3]), "time": time, "label": label.strip() or "League night"})
        return out

    def delete_location(self, loc_id: str) -> None:
        with self.change() as s:
            self.find(s, "locations", loc_id)
            team_ids = {t["id"] for t in s["teams"] if t["loc"] == loc_id}
            for kind in ("events", "blackouts", "requests", "signups", "teams"):
                s[kind] = [r for r in s[kind] if r["loc"] != loc_id]
            s["invites"] = [i for i in s["invites"] if i["team"] not in team_ids]
            for p in s["players"]:
                p["locs"] = [x for x in p["locs"] if x != loc_id]
            s["locations"] = [l for l in s["locations"] if l["id"] != loc_id]

    def add_rep(self, name: str, division: str) -> dict:
        if not name.strip():
            raise LeagueError("Give the rep's name.")
        with self.change() as s:
            rep = {"id": self._id("rep", s["reps"]), "name": name.strip(), "division": division.strip(), "pin": new_pin(6)}
            s["reps"].append(rep)
            return rep

    def delete_rep(self, rep_id: str) -> None:
        with self.change() as s:
            self.find(s, "reps", rep_id)
            for r in s["requests"]:
                if r.get("rep") == rep_id and r["status"] == CLAIMED:
                    r["status"], r["rep"] = OPEN, ""  # back on the board for another rep
            s["reps"] = [r for r in s["reps"] if r["id"] != rep_id]

    def reset_pin(self, kind: str, row_id: str) -> str:
        with self.change() as s:
            row = self.find(s, kind, row_id)
            row["pin"] = new_pin(6 if kind in ("locations", "reps") else 4)
            return row["pin"]

    def add_event(self, f: dict) -> dict:
        kind = f.get("type", "")
        if kind not in TYPES or kind == LEAGUE_NIGHT:
            raise LeagueError("Pick an event type.")
        loc = ALL if kind == GENERAL else f.get("loc", "")
        title = (f.get("title") or "").strip()
        if not title:
            raise LeagueError("Give the event a title.")
        day = parse_day(f.get("date", ""))
        with self.change() as s:
            if loc != ALL and not self.location(s, loc):
                raise LeagueError("Pick a host location, or make it a league-wide date.")
            b = loc != ALL and self.blackout_on(s, loc, day)
            if b:
                raise LeagueError(f"{self.location_name(s, loc)} is unavailable {day_label(day)} ({b['reason']}). "
                                  "Pick another date or ask the host to lift the blackout.")
            event = {"id": self._id("ev", s["events"]), "date": day, "loc": loc, "type": kind, "title": title,
                     "time": (f.get("time") or "").strip(), "director": (f.get("director") or "").strip(),
                     "notes": (f.get("notes") or "").strip(), "request": ""}
            if f.get("request"):
                req = self.find(s, "requests", f["request"])
                if req["status"] != CLAIMED:
                    raise LeagueError("That request isn't claimed by a rep yet.")
                req["status"], req["date"], event["request"] = SCHEDULED, day, req["id"]
            s["events"].append(event)
            return event

    def delete_event(self, event_id: str) -> None:
        with self.change() as s:
            event = self.find(s, "events", event_id)
            if event.get("request"):
                req = next((r for r in s["requests"] if r["id"] == event["request"]), None)
                if req:
                    req["status"] = CLAIMED if req.get("rep") else OPEN  # back to the board, still with its rep
            s["events"].remove(event)

    def decline_request(self, req_id: str) -> None:
        with self.change() as s:
            req = self.find(s, "requests", req_id)
            if req["status"] == SCHEDULED:
                raise LeagueError("It's on the calendar already; delete the event instead.")
            s["requests"].remove(req)

    def mark_connected(self, signup_id: str) -> None:
        with self.change() as s:
            self.find(s, "signups", signup_id)["status"] = "connected"

    def delete_row(self, kind: str, row_id: str) -> None:
        """Remove a player listing, team or sign-up (with any invites it had)."""
        with self.change() as s:
            self.find(s, kind, row_id)
            s[kind] = [r for r in s[kind] if r["id"] != row_id]
            if kind in ("players", "teams"):
                key = "player" if kind == "players" else "team"
                s["invites"] = [i for i in s["invites"] if i[key] != row_id]

    # ------------------------------------------------------------- host location

    def add_blackout(self, loc_id: str, start: str, end: str, reason: str) -> int:
        """Block days at a location. Returns how many scheduled events now conflict."""
        start = parse_day(start, "first day")
        end = parse_day(end, "last day") if (end or "").strip() else start
        if end < start:
            raise LeagueError("The last day is before the first day.")
        if start < self.today():
            raise LeagueError("Blackout dates can't be in the past.")
        if not reason.strip():
            raise LeagueError("Give a reason (the league office sees it).")
        with self.change() as s:
            self.find(s, "locations", loc_id)
            s["blackouts"].append({"id": self._id("bk", s["blackouts"]), "loc": loc_id, "start": start, "end": end,
                                   "reason": reason.strip()})
            return sum(e["loc"] == loc_id for e in self.events_between(s, start, end, loc_id))

    def delete_blackout(self, loc_id: str, blackout_id: str) -> None:
        with self.change() as s:
            b = self.find(s, "blackouts", blackout_id)
            if b["loc"] != loc_id:
                raise LeagueError("That blackout belongs to another location.")
            s["blackouts"].remove(b)

    def add_request(self, loc_id: str, kind: str, day: str, alt: str, notes: str) -> dict:
        if kind not in REQUEST_TYPES:
            raise LeagueError("Pick the kind of event.")
        day = parse_day(day, "preferred date")
        alt = parse_day(alt, "backup date") if (alt or "").strip() else ""
        if day < self.today() or (alt and alt < self.today()):
            raise LeagueError("Pick dates from today on.")
        with self.change() as s:
            self.find(s, "locations", loc_id)
            b = self.blackout_on(s, loc_id, day)
            if b:
                raise LeagueError(f"You have {day_label(day)} blacked out ({b['reason']}). "
                                  "Pick another date or remove the blackout first.")
            req = {"id": self._id("rq", s["requests"]), "loc": loc_id, "type": kind, "date": day, "alt": alt,
                   "notes": notes.strip(), "status": OPEN, "rep": "", "created": self.today()}
            s["requests"].append(req)
            return req

    # ------------------------------------------------------------- division rep

    def claim(self, req_id: str, rep_id: str) -> None:
        with self.change() as s:
            req = self.find(s, "requests", req_id)
            if req["status"] != OPEN:
                raise LeagueError("Another rep already claimed that one.")
            req["status"], req["rep"] = CLAIMED, rep_id

    def release(self, req_id: str, rep_id: str) -> None:
        with self.change() as s:
            req = self.find(s, "requests", req_id)
            if req["status"] != CLAIMED or req["rep"] != rep_id:
                raise LeagueError("Only the rep who claimed it can release it, before it's on the calendar.")
            req["status"], req["rep"] = OPEN, ""

    # ------------------------------------------------------------- players and captains

    @staticmethod
    def _picked(values, allowed) -> list[str]:
        return [v for v in allowed if v in set(values or [])]

    def sign_up(self, loc_id: str, f: dict) -> None:
        """A player asks the league office to connect them with a team at a location."""
        name, contact = (f.get("name") or "").strip(), (f.get("contact") or "").strip()
        if not name or not contact:
            raise LeagueError("Give your name and a phone or email so the league office can reach you.")
        with self.change() as s:
            self.find(s, "locations", loc_id)
            s["signups"].append({"id": self._id("su", s["signups"]), "created": self.today(), "name": name,
                                 "contact": contact, "loc": loc_id, "formats": self._picked(f.get("formats"), FORMATS),
                                 "nights": self._picked(f.get("nights"), DAYS), "note": (f.get("note") or "").strip(),
                                 "status": "new"})

    def save_player(self, player_id: str | None, f: dict) -> dict:
        first, contact = (f.get("first") or "").strip(), (f.get("contact") or "").strip()
        if not first:
            raise LeagueError("Give your first name.")
        with self.change() as s:
            if player_id is None:
                if not contact:
                    raise LeagueError("Give a phone or email. Only you and the league office see it.")
                if any(same_contact(p["contact"], contact) for p in s["players"]):
                    raise LeagueError("There's already a listing with that phone or email. Sign in to change it.")
                p = {"id": self._id("pl", s["players"]), "pin": new_pin(), "created": self.today()}
                s["players"].append(p)
            else:
                p = self.find(s, "players", player_id)
                if contact and any(same_contact(o["contact"], contact) for o in s["players"] if o["id"] != p["id"]):
                    raise LeagueError("Another listing already uses that phone or email.")
            if contact:
                p["contact"] = contact
            p.update(first=first, last=(f.get("last") or "").strip()[:3], area=(f.get("area") or "").strip(),
                     level=f.get("level") if f.get("level") in LEVELS else LEVELS[0],
                     formats=self._picked(f.get("formats"), FORMATS), nights=self._picked(f.get("nights"), DAYS),
                     locs=[l["id"] for l in s["locations"] if l["id"] in set(f.get("locs") or [])],
                     about=(f.get("about") or "").strip(), visible=bool(f.get("visible")))
            return p

    def save_team(self, team_id: str | None, f: dict) -> dict:
        name, captain, contact = ((f.get(k) or "").strip() for k in ("name", "captain", "contact"))
        spots = (f.get("spots") or "").strip()
        if not name or not captain or not contact:
            raise LeagueError("Give the team name, the captain's name and a phone or email.")
        if not spots.isdigit() or int(spots) > 8:
            raise LeagueError("Open spots is a number from 0 to 8.")
        if f.get("night") not in DAYS or f.get("format") not in FORMATS:
            raise LeagueError("Pick the night and format the team plays.")
        with self.change() as s:
            if not self.location(s, f.get("loc", "")):
                raise LeagueError("Pick the location the team plays out of.")
            if team_id is None:
                if any(same_contact(t["contact"], contact) for t in s["teams"]):
                    raise LeagueError("There's already a team with that captain phone or email. Sign in to change it.")
                t = {"id": self._id("tm", s["teams"]), "pin": new_pin(), "created": self.today()}
                s["teams"].append(t)
            else:
                t = self.find(s, "teams", team_id)
                if any(same_contact(o["contact"], contact) for o in s["teams"] if o["id"] != t["id"]):
                    raise LeagueError("Another team already uses that captain phone or email.")
            t.update(name=name, captain=captain, contact=contact, loc=f["loc"], night=f["night"],
                     format=f["format"], spots=int(spots))
            return t

    def invite(self, team_id: str, player_id: str, message: str) -> None:
        if not message.strip():
            raise LeagueError("Write a short message to the player.")
        with self.change() as s:
            team, player = self.find(s, "teams", team_id), self.find(s, "players", player_id)
            if not player["visible"]:
                raise LeagueError("That player isn't looking right now.")
            if any(i["team"] == team["id"] and i["player"] == player["id"] for i in s["invites"]):
                raise LeagueError(f"You already sent {player['first']} your team info.")
            s["invites"].append({"id": self._id("iv", s["invites"]), "team": team["id"], "player": player["id"],
                                 "created": self.today(), "message": message.strip(), "status": "new"})

    def answer_invite(self, player_id: str, invite_id: str, status: str) -> None:
        if status not in ("seen", "declined"):
            raise LeagueError("Pick an answer.")
        with self.change() as s:
            inv = self.find(s, "invites", invite_id)
            if inv["player"] != player_id:
                raise LeagueError("That invite is for someone else.")
            inv["status"] = status

    def sign_in(self, kind: str, contact: str, pin: str) -> dict | None:
        """A player listing or team whose phone/email and PIN match."""
        for row in self.load()[kind]:
            if same_contact(row["contact"], contact) and secrets.compare_digest(pin.strip(), row["pin"]):
                return row
        return None


def month_grid(month: str) -> list[list[str]]:
    """'2026-11' -> weeks (Sunday first) of ISO days covering that month."""
    first = datetime.strptime(month, "%Y-%m").date()
    start = first - timedelta(days=(first.weekday() + 1) % 7)
    weeks, d = [], start
    while True:
        weeks.append([(d + timedelta(days=i)).isoformat() for i in range(7)])
        d += timedelta(days=7)
        if d.month != first.month:
            return weeks
