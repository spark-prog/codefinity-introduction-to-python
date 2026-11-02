"""Create a tournament from the Bonus Event Approval form's export (Apps Script JSON).

The export ("control-table-bonus-event/v1") describes an approved event: date, venue,
format, fees, registration dates, rules and eligibility. `plan()` turns it into
Control Table settings plus a list of things the director still has to check; nothing
is created until the director confirms. The whole export is kept with the event so
later phases (eligibility) can use it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .bracket import DOUBLE, MAX_TEAM_SIZE, MODIFIED, RACK_CHART, ROUND_ROBIN, SINGLE, STRAIGHT, entries_label
from .engine import TournamentError
from .hub import BRACKET, CHIP

SCHEMA = "control-table-bonus-event/v1"
SAVED_AS = "bonus_event.json"


@dataclass
class Plan:
    kind: str
    name: str
    settings: dict
    summary: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    external_id: str = ""


def read(raw: bytes | str) -> dict:
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise TournamentError("That file isn't the Bonus Event export (it isn't JSON).")
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA:
        raise TournamentError(f"That file isn't a Bonus Event export ({SCHEMA}).")
    if not isinstance(doc.get("event"), dict) or not (doc["event"].get("name") or "").strip():
        raise TournamentError("The export has no event name.")
    return doc


def _time(value) -> str:
    """'9:00:00 PM' -> '9:00 PM'; '' when missing or unreadable."""
    value = (value or "").strip() if isinstance(value, str) else ""
    for fmt in ("%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).strftime("%I:%M %p").lstrip("0")
        except ValueError:
            continue
    return ""


def _day(value, end: bool = False) -> str:
    try:
        d = datetime.strptime((value or "").strip(), "%Y-%m-%d")
    except (ValueError, AttributeError):
        return ""
    return d.strftime("%Y-%m-%dT23:59" if end else "%Y-%m-%dT00:00")


def _nice_day(value: str) -> str:
    """'2026-10-31' -> 'Sat, Oct 31, 2026'."""
    d = datetime.strptime(value, "%Y-%m-%d")
    return f"{d:%a, %b} {d.day}, {d.year}"


def _int(value, default=None):
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return default


def _structure(text: str) -> tuple[str, str] | None:
    """('chip', '') or ('bracket', format) from 'Chips Tournament', 'Modified Single Elimination', ..."""
    t = (text or "").lower()
    if "chip" in t:
        return CHIP, ""
    if "round" in t and "robin" in t:
        return BRACKET, ROUND_ROBIN
    if "modified" in t:
        return BRACKET, MODIFIED
    if "double" in t and "elim" in t:
        return BRACKET, DOUBLE
    if "single" in t and "elim" in t:
        return BRACKET, SINGLE
    return None


def plan(doc: dict, rule_sets: list[dict] | None = None) -> Plan:
    ev, reg, fmt = doc.get("event") or {}, doc.get("registration") or {}, doc.get("format") or {}
    rules, elig, awards = doc.get("rules") or {}, doc.get("eligibility") or {}, doc.get("awards") or {}
    src = doc.get("source") or {}
    warnings: list[str] = []
    s: dict = {}

    kind_fmt = _structure(fmt.get("tournament_structure", ""))
    if kind_fmt is None:
        raise TournamentError(f"Control Table can't run a '{fmt.get('tournament_structure') or 'blank'}' structure yet. "
                              "Create the event by hand.")
    kind, bracket_format = kind_fmt
    name = ev["name"].strip()

    if (src.get("approval_status") or "").lower() != "approved":
        warnings.append(f"The form says this event is '{src.get('approval_status') or 'not approved'}', not Approved.")

    # When and where
    date = (ev.get("date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise TournamentError("The export has no event date.")
    s["date"], s["start_time"], s["venue"] = date, _time(ev.get("start_time")), (ev.get("venue") or "").strip()
    tz = (ev.get("timezone") or "").strip()
    if tz and os.environ.get("TZ") and tz != os.environ["TZ"]:
        warnings.append(f"The event is in {tz} but Control Table runs on {os.environ['TZ']} time: check the times.")

    # What players read on the event page
    details = [(ev.get("public_description") or "").strip()]
    practice = _time(ev.get("practice_tables_open"))
    if practice:
        details.append(f"Practice tables open at {practice}.")
    if ev.get("doors_open"):
        details.append(f"Doors open at {_time(ev['doors_open']) or ev['doors_open']}.")
    paid = [m["method"] for m in reg.get("payment_methods") or [] if isinstance(m, dict)
            and "not accepted" not in (m.get("availability") or "").lower()]
    if paid:
        details.append(f"Payment: {', '.join(paid)}.")
    checkin = [c.strip() for c in reg.get("check_in_requirements") or [] if isinstance(c, str) and c.strip()]
    if checkin:
        details.append("Check-in: " + " ".join(c.rstrip(".") + "." for c in checkin))
    if awards.get("gifts"):
        details.append(f"Prizes: {awards['gifts'].strip().rstrip('.')}.")
    s["details"] = "\n\n".join(d for d in details if d)

    # Check-in time ("30 minutes before start time")
    m = re.search(r"(\d+)\s*min", " ".join(checkin), re.IGNORECASE)
    if m and s["start_time"]:
        start = datetime.strptime(s["start_time"], "%I:%M %p")
        s["checkin_by"] = (start - timedelta(minutes=int(m.group(1)))).strftime("%I:%M %p").lstrip("0")

    # Registration
    s["registration_opens"], s["registration_closes"] = _day(reg.get("opens_on")), _day(reg.get("closes_on"), end=True)
    s["registration_closed"] = True  # nothing goes public until the director has checked it
    fee = reg.get("entry_fee")
    if isinstance(fee, (int, float)) and fee >= 0:
        s["entry_fee_cents"] = round(fee * 100)
    else:
        warnings.append(f"Couldn't read the entry fee ('{reg.get('entry_fee_raw') or 'blank'}'): set it.")
    cap = _int(reg.get("maximum_entries"))
    if cap:
        s["field_cap"] = cap

    from .eligibility import any_rules, from_bonus_event
    who_can_play, elig_notes = from_bonus_event(doc)
    if any_rules(who_can_play):
        s["eligibility"] = who_can_play
    warnings += elig_notes
    basis = (elig.get("skill_level_basis") or "").lower()
    if "high" in basis and "7" in basis:
        s["skill_game"] = "high7"
    elif "high" in basis:
        s["skill_game"] = "high"
    elif "9-ball" in basis or "9 ball" in basis:
        s["skill_game"] = "9"
    elif "8-ball" in basis or "8 ball" in basis:
        s["skill_game"] = "8"

    # Format
    per = _int(fmt.get("players_per_entry"), 2 if kind == CHIP else 1)
    racks = _int(fmt.get("maximum_racks"))
    race = racks // 2 + 1 if racks else None  # "maximum 3 racks" = race to 2
    method = (fmt.get("race_method") or "").lower()
    tables = _int(fmt.get("available_tables"))
    if tables:
        s["tables"] = [str(i) for i in range(1, tables + 1)]
    if kind == CHIP:
        if per != 2:
            warnings.append(f"Chip tournaments are teams of 2; the form says {per} per entry.")
        if race:
            s["race_to"] = race
        m = re.search(r"combined skill level\s*(\d+)", elig.get("skill_level_limit") or "", re.IGNORECASE)
        if m:
            s["registration_cap"] = int(m.group(1))
    else:
        s["format"] = bracket_format
        if 1 <= per <= MAX_TEAM_SIZE:
            s["team_size"] = per
        elif per:
            warnings.append(f"The form says {per} players per entry; teams go up to {MAX_TEAM_SIZE}. Set it by hand.")
        m = re.search(r"combined skill level\s*(\d+)", elig.get("skill_level_limit") or "", re.IGNORECASE)
        if m and per and per > 1:
            s["team_skill_cap"] = int(m.group(1))
        if "chart" in method or "rack race" in method:
            s["race_method"] = RACK_CHART
        elif "ball" in method:
            warnings.append("Ball-count races aren't built yet: set the races by hand.")
        else:
            s["race_method"] = STRAIGHT
        if race and s.get("race_method") == STRAIGHT:
            s["race_winners"] = s["race_losers"] = s["race_finals"] = race
        if "skill" in (fmt.get("grouping_method") or "").lower():
            warnings.append("The form splits players into skill groups: enter them in Settings (e.g. 1-3, 4, 5, 6, 7-9).")
        if elig.get("skill_level_limit") and "not applicable" not in elig["skill_level_limit"].lower() \
                and "team_skill_cap" not in s:
            warnings.append(f"Skill limit '{elig['skill_level_limit']}': set it in Settings (highest skill level a "
                            "player can be, or the combined cap).")
        if awards.get("payout_model"):
            warnings.append(f"Payouts: the form says '{awards['payout_model']}'. Set the payout percentages.")
    if "ball" in method and kind == CHIP:
        warnings.append("Ball-count races aren't built yet: chips use a straight race.")

    # Rules
    game = (fmt.get("game_format") or "").strip()
    match = None
    if game:
        g = game.lower()
        match = next((r for r in rule_sets or [] if g in r["title"].lower() or r["title"].lower().removesuffix(" rules") == g),
                     None)
        if match:
            s["rule_set"] = match["id"]
        else:
            warnings.append(f"No rule set matches '{game}': pick one in Settings.")
    lines = []
    if rules.get("sudden_death"):
        lines.append(f"- {rules['sudden_death'].strip().rstrip('.')}.")
    if rules.get("coaching"):
        lines.append(f"- Coaching: {rules['coaching'].strip().rstrip('.')}.")
    if rules.get("electronics"):
        lines.append(f"- Electronics: {rules['electronics'].strip().rstrip('.')}.")
    tracked = [x["item"] for x in rules.get("scorekeeping") or [] if isinstance(x, dict)
               and "must be tracked" in (x.get("selection") or "").lower()]
    if tracked:
        lines.append(f"- Track: {', '.join(t.lower() for t in tracked)}.")
    if rules.get("additional_penalty_notes"):
        lines.append(f"- {rules['additional_penalty_notes'].strip().rstrip('.')}.")
    extra = (rules.get("additional_rules") or "").strip()
    s["event_rules"] = "\n".join(lines) + (f"\n\n{extra}" if extra else "")

    fmt_name = {CHIP: "Chip tournament"}.get(kind) or dict(
        [(DOUBLE, "Double elimination"), (SINGLE, "Single elimination"), (MODIFIED, "Modified single elimination"),
         (ROUND_ROBIN, "Round robin")])[bracket_format]
    summary = [
        ("Type", fmt_name + (f" · {game}" if game else "")),
        ("When", _nice_day(date) + (f" at {s['start_time']}" if s["start_time"] else "")
         + (f" (check in by {s['checkin_by']})" if s.get("checkin_by") else "")),
        ("Where", s["venue"] or "—"),
        ("Entry", f"${s['entry_fee_cents'] / 100:.2f}" if "entry_fee_cents" in s else "—"),
        ("Field cap", str(s.get("field_cap") or "No limit")),
        ("Tables", f"{len(s['tables'])} (1–{len(s['tables'])})" if s.get("tables") else "Not set"),
        ("Race", (f"Race to {race}" if race and s.get("race_method", STRAIGHT) == STRAIGHT
                  else "Rack race chart" if s.get("race_method") == RACK_CHART else "Not set")),
        ("Registration", " to ".join(x for x in (
            f"opens {_nice_day(s['registration_opens'][:10])}" if s["registration_opens"] else "",
            f"closes {_nice_day(s['registration_closes'][:10])} (end of day)" if s["registration_closes"] else "")
            if x) or "No dates"),
        ("Rules", match["title"] if match else "Not matched"),
    ]
    if s.get("eligibility"):
        from .eligibility import describe
        summary.append(("Who can play", "; ".join(describe(s["eligibility"]))))
    if kind == CHIP and "registration_cap" in s:
        summary.append(("Skill cap", f"Combined {s['registration_cap']}"))
    if kind == BRACKET and "team_size" in s:
        summary.append(("Entries", entries_label(s["team_size"]).capitalize()))
    if kind == BRACKET and s.get("team_skill_cap"):
        summary.append(("Skill cap", f"Combined {s['team_skill_cap']}"))
    return Plan(kind=kind, name=name, settings=s, summary=summary, warnings=warnings,
                external_id=str(src.get("external_id") or ""))


def apply(t, p: Plan) -> None:
    """Put the planned settings on a new tournament."""
    for key, value in p.settings.items():
        if hasattr(t.settings, key):
            setattr(t.settings, key, value)
    t.settings.bonus_event_id = p.external_id
