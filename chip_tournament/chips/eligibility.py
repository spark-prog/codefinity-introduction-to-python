"""Eligibility: who may play in an event, checked against the league's player list.

An event's rules are a small dict in its settings (`eligibility`), set by the director or
filled from the Bonus Event form's requirement list. Each check gives a plain reason and how
to fix it ("Needs 6 lifetime matches. You have 4: 2 more to go."). Anything the player list
can't answer (a requirement Control Table doesn't track, a player not in the list) isn't a
refusal: it goes to the director's Pending verification box for review.
"""

from __future__ import annotations

import re
from datetime import datetime

from .players import basis_label, event_skill

EITHER = "either"
GENDERS = {"": "Open to everyone", "women": "Women only", "men": "Men only", "mixed": "Jack & Jill (one man, one woman)"}


def default_rules() -> dict:
    return {"active": False, "min_matches": 0, "matches_game": EITHER, "played_session": False, "played_since": "",
            "active_in": "",
            "skill_min": 0, "skill_max": 0, "gender": "", "list_label": "", "list_column": "",
            "list_members": [], "review": []}


def rules_of(settings) -> dict:
    return {**default_rules(), **(getattr(settings, "eligibility", None) or {})}


def any_rules(r: dict) -> bool:
    d = default_rules()
    return any(r.get(k) != d[k] for k in d)


def describe(r: dict, session_start: str = "") -> list[str]:
    """The event's rules in plain words, for the event page and settings."""
    out = []
    if r["active"]:
        out.append("Active APA member in good standing (not Past Due or Inactive)")
    if r["min_matches"]:
        fmt = {"8": " in 8-Ball", "9": " in 9-Ball"}.get(r["matches_game"], " (8-Ball or 9-Ball)")
        out.append(f"At least {r['min_matches']} lifetime matches{fmt}")
    if r["played_session"]:
        since = r["played_since"] or session_start
        out.append(f"Played at least one match since {_nice(since)}" if since else "Played at least one match this session")
    if r["active_in"]:
        out.append(f"Active in {r['active_in']}-Ball (has a {r['active_in']}-Ball skill level)")
    if r["skill_min"] or r["skill_max"]:
        lo, hi = r["skill_min"] or 1, r["skill_max"] or 9
        out.append(f"Skill level {lo}–{hi}" if lo != hi else f"Skill level {lo}")
    if r["gender"]:
        out.append(GENDERS[r["gender"]])
    if r["list_label"] or r["list_column"] or r["list_members"]:
        out.append(f"On the {r['list_label'] or 'eligible'} list")
    out += [f"{x} (checked by the league office)" for x in r["review"]]
    return out


def _nice(day: str) -> str:
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
        return f"{d:%b} {d.day}"
    except ValueError:
        return day


def check_player(rec: dict | None, r: dict, session_start: str, game: str = "8") -> tuple[list[str], list[str]]:
    """(refusals, review notes) for one player. Refusals say what's wrong and how to fix it."""
    if not any_rules(r):
        return [], []
    if rec is None:
        return [], ["Not in the player list, so eligibility can't be checked"]
    no, review = [], []
    status = (rec.get("status") or "").strip().lower()
    if r["active"]:
        if status == "past due":
            no.append("Membership is Past Due. Pay your dues with the league office to play.")
        elif status == "inactive":
            no.append("Not active on a team (Inactive). Join a team roster this session to play.")
        elif status != "active":
            review.append(f"Member status is '{rec.get('status') or 'blank'}'")
    if r["min_matches"]:
        m8, m9 = rec.get("mp8") or 0, rec.get("mp9") or 0
        have = {"8": m8, "9": m9}.get(r["matches_game"], max(m8, m9))
        if have < r["min_matches"]:
            fmt = {"8": " 8-Ball", "9": " 9-Ball"}.get(r["matches_game"], "")
            left = r["min_matches"] - have
            no.append(f"Needs {r['min_matches']} lifetime{fmt} matches. You have {have}: {left} more to go.")
    if r["played_session"]:
        since = r["played_since"] or session_start
        last = max(rec.get("last8") or "", rec.get("last9") or "")
        if not last or last < since:
            no.append(f"Hasn't played a match since {_nice(since)}. Play one league match to qualify.")
    if r["active_in"] and rec.get(f"sl{r['active_in']}") is None:
        no.append(f"Not active in {r['active_in']}-Ball (no {r['active_in']}-Ball skill level).")
    if r["skill_min"] or r["skill_max"]:
        sl, how = event_skill(rec, game)
        if sl is None:
            review.append(f"No established {basis_label(game)} skill level ({how})")
        elif (r["skill_min"] and sl < r["skill_min"]) or (r["skill_max"] and sl > r["skill_max"]):
            lo, hi = r["skill_min"] or 1, r["skill_max"] or 9
            no.append(f"This event is for skill level {lo}–{hi}; your {basis_label(game)} skill level is {sl}.")
    if r["gender"] in ("women", "men"):
        want = "F" if r["gender"] == "women" else "M"
        g = (rec.get("gender") or "").strip().upper()[:1]
        if not g:
            review.append("Gender is blank in the player list")
        elif g != want:
            no.append(f"This event is {GENDERS[r['gender']].lower()}.")
    if r["list_label"] or r["list_column"] or r["list_members"]:
        label = r["list_label"] or "eligible"
        on_list = rec["member"] in r["list_members"]
        if not on_list and r["list_column"]:
            value = (rec.get("extra") or {}).get(r["list_column"], "")
            on_list = value.strip().lower() in ("yes", "y", "true", "x", "1")
        if not on_list:
            if r["list_members"] or r["list_column"]:
                no.append(f"Not on the {label} list for this event. Ask the league office if you think you should be.")
            else:
                review.append(f"The {label} list hasn't been set up yet")
    review += [f"{x}: checked by the league office" for x in r["review"]]
    return no, review


def check_team(recs: list[dict | None], r: dict) -> list[str]:
    """Rules about the whole team (Jack & Jill: one man and one woman)."""
    if r.get("gender") != "mixed" or len(recs) != 2 or any(x is None for x in recs):
        return []
    genders = sorted((x.get("gender") or "").strip().upper()[:1] for x in recs)
    if "" in genders:
        return []
    return [] if genders == ["F", "M"] else ["This is a Jack & Jill event: a team is one man and one woman."]


# ------------------------------------------------------------- from the Bonus Event form

_LIST_WORDS = ("masters", "ladies", "captain", "co-captain", "division rep", "invited", "listed as eligible")


def from_bonus_event(doc: dict) -> tuple[dict, list[str]]:
    """The form's requirement list -> (rules, notes for the director)."""
    r, notes = default_rules(), []
    elig = doc.get("eligibility") or {}
    for item in elig.get("requirements") or []:
        if not isinstance(item, dict) or (item.get("selection") or "").strip().lower() != "required":
            continue
        req = (item.get("requirement") or "").strip()
        low = req.lower()
        m = re.search(r"at least (\d+) lifetime matches", low)
        if "good standing" in low or "active in this league area" in low:
            r["active"] = True
        elif m:
            r["min_matches"] = max(r["min_matches"], int(m.group(1)))
            r["matches_game"] = "8" if "8-ball" in low and "9-ball" not in low else (
                "9" if "9-ball" in low and "8-ball" not in low else EITHER)
        elif "current session" in low:
            r["played_session"] = True
        elif "8-ball roster" in low:
            r["active_in"] = "8"
        elif "9-ball roster" in low:
            r["active_in"] = "9"
        elif any(w in low for w in _LIST_WORDS):
            r["list_label"] = r["list_label"] or req.replace("Must be ", "").replace("Active on a ", "").strip()
            notes.append(f"'{req}' needs an eligible list: pick a sheet column or paste member numbers in Settings.")
        else:
            r["review"].append(req)
    gender = (elig.get("gender_requirement") or "").lower()
    if "jack" in gender or ("male" in gender and "female" in gender and "open" not in gender):
        r["gender"] = "mixed"
    elif any(w in gender for w in ("women", "ladies", "female only", "women only")):
        r["gender"] = "women"
    limit = (elig.get("skill_level_limit") or "").lower()
    if "singles" in limit:
        hi = re.search(r"maximum (?:skill level )?(\d)", limit)
        lo = re.search(r"minimum (?:skill level )?(\d)", limit)
        r["skill_max"] = int(hi.group(1)) if hi else 0
        r["skill_min"] = int(lo.group(1)) if lo else 0
    return r, notes


def _day(value: str) -> str:
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def parse_form(f, current: dict) -> dict:
    """The director's Eligibility settings."""
    def num(key, lo, hi):
        v = (f.get(key) or "0").strip() or "0"
        if not v.isdigit() or not lo <= int(v) <= hi:
            from .engine import TournamentError
            raise TournamentError(f"Eligibility: {key.replace('elig_', '').replace('_', ' ')} must be {lo}–{hi}.")
        return int(v)

    members = sorted(set(re.findall(r"322\d{5}", re.sub(r"(?<=\d)[ -](?=\d)", "", f.get("elig_list_members", "")))))
    return {
        "active": f.get("elig_active") == "1",
        "min_matches": num("elig_min_matches", 0, 999),
        "matches_game": f.get("elig_matches_game") if f.get("elig_matches_game") in ("8", "9") else EITHER,
        "played_session": f.get("elig_played_session") == "1",
        "played_since": _day(f.get("elig_played_since", "")),
        "active_in": f.get("elig_active_in") if f.get("elig_active_in") in ("8", "9") else "",
        "skill_min": num("elig_skill_min", 0, 9), "skill_max": num("elig_skill_max", 0, 9),
        "gender": f.get("elig_gender") if f.get("elig_gender") in GENDERS else "",
        "list_label": (f.get("elig_list_label") or "").strip()[:60],
        "list_column": (f.get("elig_list_column") or "").strip(),
        "list_members": members,
        "review": [x.strip() for x in (f.get("elig_review") or "").splitlines() if x.strip()],
    }
