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


GAMES = {"8": "8-Ball", "9": "9-Ball", "other": "Other"}


def entry_kind(t) -> str:
    """'Singles', 'Doubles' or 'Teams'. Chip tournaments are teams of two, so Doubles."""
    if not is_bracket(t):
        return "Doubles"
    size = t.settings.team_size
    return "Singles" if size == 1 else "Doubles" if size == 2 else "Teams"


def game_key(t) -> str:
    """'8', '9' or 'other'. The director's choice; an older event without one is guessed from its name and race."""
    s = t.settings
    if s.game in GAMES:
        return s.game
    from . import seeding
    return seeding.game_for(s)


def event_types(t) -> list[str]:
    """['Qualifier', 'Bonus Tournament'] as chosen (either, both or neither). Older events: from the name and the
    Bonus Event form they came from, until the director chooses."""
    s = t.settings
    if s.categories_set:
        qualifier, bonus = s.is_qualifier, s.is_bonus
    else:
        qualifier, bonus = "qualifier" in (s.name or "").lower(), bool(s.bonus_event_id)
    return (["Qualifier"] if qualifier else []) + (["Bonus Tournament"] if bonus else [])


def categories(t) -> dict:
    """What kind of event this is: {'entry', 'game', 'game_key', 'types', 'confirmed'}."""
    key = game_key(t)
    return {"entry": entry_kind(t), "game": GAMES[key], "game_key": key, "types": event_types(t),
            "confirmed": bool(t.settings.categories_set)}


def tag_line(t) -> str:
    """'9-Ball · Doubles · Qualifier' (the small line on cards and event pages)."""
    c = categories(t)
    return " · ".join([c["game"], c["entry"], *c["types"]])


def parse_categories(form, need_entry: bool = False) -> dict:
    """The game and event-type boxes from a form, as settings. Raises if the game wasn't chosen.
    With need_entry, also the players-per-entry choice: adds team_size ('1', '2' or 'teams' with team_n)."""
    from .engine import TournamentError
    game = (form.get("game") or "").strip()
    if game not in GAMES:
        raise TournamentError("Choose the game: 8-Ball, 9-Ball or Other.")
    out = {"game": game, "is_qualifier": form.get("qualifier") == "1", "is_bonus": form.get("bonus") == "1",
           "categories_set": True}
    if need_entry:
        entry = (form.get("entry") or "").strip()
        if entry == "1":
            out["team_size"] = 1
        elif entry == "2":
            out["team_size"] = 2
        elif entry == "teams":
            n = (form.get("team_n") or "").strip()
            if not n.isdigit() or not 3 <= int(n) <= 10:
                raise TournamentError("Teams need 3 or more players: enter how many.")
            out["team_size"] = int(n)
        else:
            raise TournamentError("Choose singles, doubles or teams.")
    return out


def play_phrase(t) -> str:
    """The partner question, answered: 'Play solo', 'Bring a partner' or 'Team of 4'."""
    kind = entry_kind(t)
    return "Play solo" if kind == "Singles" else "Bring a partner" if kind == "Doubles" else f"Team of {t.settings.team_size}"


def access_phrase(t) -> str:
    """'Open to anyone' or 'Eligibility required' (the player's own answer is shown separately)."""
    return "Eligibility required" if eligibility.any_rules(eligibility.rules_of(t.settings)) else "Open to anyone"


def skill_phrase(t) -> str:
    """'Combined SL 10 or less', 'No player above SL 6', 'Skill groups SL 1–3, 4–9', or 'All skill levels'."""
    bits = _skill_clauses(t, group_detail=True)
    text = " · ".join(bits) if bits else "all skill levels"
    return text[:1].upper() + text[1:]


def _short_dt(when: datetime) -> str:
    return f"{when:%a} {when.strftime('%I %p').lstrip('0')}" if when.minute == 0 else \
        f"{when:%a} {when.strftime('%I:%M %p').lstrip('0')}"


def fill(t) -> dict:
    """How full the field is, for the bar on a card: {'filled', 'left', 'pct', 'tone', 'note'}.
    tone is '' (plenty of room), 'low' (about a fifth left or less) or 'full'."""
    s = t.settings
    bracket = is_bracket(t)
    count = len(t.field_entries if bracket else t.field_teams)
    noun = ("player" if bracket and s.team_size == 1 else "team" if not bracket or s.team_size > 1 else "entry")
    noun = noun if count == 1 else noun + "s"
    waiting = len(getattr(t, "waitlist", []) or [])
    state, when = schedule.reg_window(s)
    note = ""
    if waiting:
        note = f"{waiting} on the waiting list"
    elif t.status == "setup" and state == schedule.OPEN and when:
        note = f"Closes {_short_dt(when)}"
    elif state == schedule.BEFORE and when:
        note = f"Opens {_short_dt(when)}"
    if not s.field_cap:
        return {"filled": f"{count} {noun}", "left": f"{count} {noun}", "pct": 0, "tone": "", "note": note, "capped": False}
    left = max(0, s.field_cap - count)
    tone = "full" if not left else "low" if left <= max(1, -(-s.field_cap // 5)) else ""
    return {"filled": f"{count} of {s.field_cap} {noun}", "pct": min(100, round(100 * count / s.field_cap)),
            "left": "Full" if not left else f"{left} spot{'s' if left != 1 else ''} left", "tone": tone, "note": note,
            "capped": True}


def partner_link_wanted(t) -> bool:
    """Show 'Need a partner?' on a doubles event while people can still sign up."""
    return t.status == "setup" and not t.settings.registration_shut and entry_kind(t) == "Doubles"


def partners_label(t) -> str:
    """The Partners & waitlist tab's name, or '' when it has nothing to say: only while people can still sign up,
    and only for team events (partner finding) or when somebody is on the waiting list."""
    s = t.settings
    doubles = not is_bracket(t) or s.team_size > 1
    waiting = bool(getattr(t, "waitlist", None))
    if t.status != "setup" or not (doubles or waiting):
        return ""
    if doubles and (waiting or getattr(s, "field_cap", 0)):
        return "Partners & waitlist"
    return "Partners" if doubles else "Waitlist"


def tabs(t) -> list[dict]:
    """The tabs under an event's header: Details, Teams (or Players), Partners & waitlist (only when it applies),
    Bracket (Live for chips), Rules."""
    s = t.settings
    bracket = is_bracket(t)
    count = len(t.field_entries if bracket else t.field_teams)
    people = "Players" if bracket and s.team_size == 1 else "Teams"
    extra = partners_label(t)
    waiting = len(getattr(t, "waitlist", None) or [])
    return [{"key": "details", "label": "Details", "endpoint": "event_details", "count": None},
            {"key": "teams", "label": people, "endpoint": "event_teams", "count": count},
            *([{"key": "partners", "label": extra, "endpoint": "event_partners", "count": waiting or None}] if extra else []),
            {"key": "bracket", "label": "Bracket" if bracket else "Live", "endpoint": "event_bracket", "count": None},
            {"key": "rules", "label": "Rules", "endpoint": "rules_page", "count": None}]


def default_tab(t) -> str:
    """The tab an event opens on: who's registered while people are signing up, the bracket once it's running."""
    return "teams" if t.status == "setup" else "bracket"
