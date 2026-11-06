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
JOIN_URL = "https://join.poolplayers.com/"  # APA's sign-up page: shown as a JOIN button
LEAGUE_URL = "https://firstcoast.poolplayers.com/home"
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


YES_VALUES = ("yes", "y", "true", "x", "1", "eligible")
QUALIFIED_VALUES = ("qualified",)  # already earned their spot (Singles Boards): can't enter this qualifier again
NOT_ELIGIBLE_VALUES = ("not eligible", "not eligible yet", "ineligible")  # a plain No means "not on the list"
ESTABLISHED_MATCHES = 10  # APA: a player is established in a format after 10 lifetime matches
# The office's words for why someone isn't eligible (e.g. Jack & Jill): "Needs 20", "Not Active-Needs 20",
# "Not Active in Format". "Active" means on a current team roster in the event's format.
NEEDS = re.compile(r"^needs (\d+)$")
INACTIVE_NEEDS = re.compile(r"^not active needs (\d+)$")
INACTIVE_IN_FORMAT = "not active in format"


def _list_word(value: str) -> str:
    """'Not Active-Needs 20' -> 'not active needs 20': lower case, dashes and extra spaces dropped."""
    return re.sub(r"\s+", " ", re.sub(r"[-–—:,]", " ", value or "")).strip().lower()


def list_value(rec: dict | None, r: dict) -> str:
    """From the event's list: 'yes' (on it: Yes or Eligible), 'push' (a one-time grace), 'qualified' (already
    qualified, so not eligible), 'not_eligible' (Not Eligible: not 10 lifetime matches in the format),
    'needs:N' (Needs N lifetime matches), 'inactive_needs:N' (not on a roster in the format, and needs N),
    'inactive_format' (has the matches but isn't on a roster in the format) or ''."""
    if rec is None:
        return ""
    if rec["member"] in r["list_members"]:
        return "yes"
    if r["list_column"]:
        value = ((rec.get("extra") or {}).get(r["list_column"], "") or "").strip().lower()
        if value in YES_VALUES:
            return "yes"
        if value == "push":
            return "push"
        if value in QUALIFIED_VALUES:
            return "qualified"
        if value in NOT_ELIGIBLE_VALUES:
            return "not_eligible"
        word = _list_word(value)
        if m := NEEDS.match(word):
            return f"needs:{m.group(1)}"
        if m := INACTIVE_NEEDS.match(word):
            return f"inactive_needs:{m.group(1)}"
        if word == INACTIVE_IN_FORMAT:
            return "inactive_format"
    return ""


def _not_established(rec: dict, r: dict, game: str, label: str) -> str:
    """Why a player marked Not Eligible isn't: lifetime matches in the event's format, with their count."""
    fmt = r["matches_game"] if r["matches_game"] in ("8", "9") else (game if game in ("8", "9") else "")
    need = r["min_matches"] or ESTABLISHED_MATCHES
    m8, m9 = rec.get("mp8") or 0, rec.get("mp9") or 0
    have = {"8": m8, "9": m9}.get(fmt, max(m8, m9))
    name = f" {fmt}-Ball" if fmt else ""
    if have >= need:  # the list is behind the match counts
        return (f"The {label} list marks you Not Eligible ({need} lifetime{name} matches needed), but the player "
                f"list shows {have}. Ask the league office to update it.")
    return f"Not eligible yet: {label} needs {need} lifetime{name} matches. You have {have}: {need - have} more to go."


def _office_word(value: str, rec: dict, r: dict, game: str, label: str, no: list, review: list) -> None:
    """Needs N / Not Active-Needs N / Not Active in Format: why not, with the player's own count."""
    fmt = r["matches_game"] if r["matches_game"] in ("8", "9") else (game if game in ("8", "9") else "8")
    have = rec.get(f"mp{fmt}") or 0
    roster = f"a current {fmt}-Ball team roster"
    join = f" Join a {fmt}-Ball team to play: {LEAGUE_URL}"
    if value == "inactive_format":
        no.append(f"Not eligible: {label} needs you on {roster}. You have {have} lifetime {fmt}-Ball matches, so "
                  f"joining a {fmt}-Ball team is all that's left.{join}")
        return
    kind, need = value.split(":")
    need = int(need)
    if kind == "inactive_needs":
        more = f" You have {have}: {need - have} more to go." if have < need else ""
        no.append(f"Not eligible: {label} needs you on {roster} and {need} lifetime {fmt}-Ball matches.{more}{join}")
        return
    if have >= need:  # the list is behind the match counts: the office takes a look, it's not a refusal
        review.append(f"The {label} list says Needs {need}, but the player list shows {have} lifetime {fmt}-Ball "
                      "matches: check and update the list")
        return
    no.append(f"Not eligible yet: {label} needs {need} lifetime {fmt}-Ball matches. "
              f"You have {have}: {need - have} more to go.")


def check_player(rec: dict | None, r: dict, session_start: str, game: str = "8",
                 pushed: bool = False) -> tuple[list[str], list[str]]:
    """(refusals, review notes) for one player. Refusals say what's wrong and how to fix it.
    `pushed`: the player pushed their eligibility here from the series' previous edition (on the list)."""
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
            no.append(f"Not eligible: not active in First Coast APA. Join to play: {JOIN_URL} "
                      f"(or find a team at {LEAGUE_URL}).")
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
        value = "yes" if pushed else list_value(rec, r)
        if value == "qualified":
            no.append(f"Already qualified for {r['list_label'] or 'this event'}: you've earned your spot, so you "
                      "can't enter this qualifier again. Ask the league office if that's not right.")
        elif value == "not_eligible":
            if not any("lifetime" in x for x in no):  # the event's own match rule already said it
                no.append(_not_established(rec, r, game, r["list_label"] or "this event"))
        elif value.startswith(("needs:", "inactive_needs:")) or value == "inactive_format":
            _office_word(value, rec, r, game, r["list_label"] or "this event", no, review)
        elif value not in ("yes", "push"):
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


# ------------------------------------------------------------ decisions (Phase 7)

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def audit(t, what: str, member: str = "", name: str = "", note: str = "") -> None:
    """Every eligibility decision, with when and why (also in the event log)."""
    t.settings.elig_audit.append({"at": _now(), "what": what, "member": member, "name": name, "note": note})
    t._say(f"Eligibility: {what}{' ' + name if name else ''}{f' ({member})' if member else ''}{': ' + note if note else ''}.")


def override_for(t, member: str) -> dict | None:
    return next((x for x in t.settings.elig_overrides if x["member"] == member), None)


def add_override(t, member: str, name: str, note: str) -> None:
    from .engine import TournamentError

    note = (note or "").strip()
    if not note:
        raise TournamentError("Add a note saying why (it's kept with the override).")
    if override_for(t, member):
        raise TournamentError(f"{name or member} already has an override.")
    t.settings.elig_overrides.append({"member": member, "name": name, "note": note, "at": _now()})
    audit(t, "override added for", member, name, note)
    for g in (t.teams if hasattr(t, "teams") else t.entries).values():  # it settles any eligibility question
        for p in g.players:
            if p.member == member and p.pending.startswith(("Eligibility", "Not eligible")):
                p.pending = ""


def remove_override(t, member: str) -> None:
    from .engine import TournamentError

    x = override_for(t, member)
    if x is None:
        raise TournamentError("That override isn't there any more.")
    t.settings.elig_overrides.remove(x)
    audit(t, "override removed for", member, x.get("name", ""))


def request_review(t, member: str, name: str, reasons: list[str], message: str) -> dict:
    """A refused player asks the league office to take another look (one open request each)."""
    from .engine import TournamentError

    if any(r["member"] == member and r["status"] == "open" for r in t.settings.review_requests):
        raise TournamentError("Your request is already with the league office. You'll get an email when they decide.")
    req = {"id": f"r{len(t.settings.review_requests) + 1}", "member": member, "name": name, "reasons": reasons,
           "message": (message or "").strip()[:500], "at": _now(), "status": "open", "decided_at": "", "note": ""}
    t.settings.review_requests.append(req)
    audit(t, "review requested by", member, name, req["message"])
    return req


def decide_review(t, req_id: str, approve: bool, note: str) -> dict:
    from .engine import TournamentError

    req = next((r for r in t.settings.review_requests if r["id"] == req_id), None)
    if req is None or req["status"] != "open":
        raise TournamentError("That request has already been decided.")
    note = (note or "").strip()
    if approve:
        if not override_for(t, req["member"]):
            add_override(t, req["member"], req["name"], note or "Approved on review")
    elif not note:
        raise TournamentError("Add a note saying why (the player sees it in the email).")
    req.update(status="approved" if approve else "declined", decided_at=_now(), note=note)
    audit(t, f"review {req['status']} for", req["member"], req["name"], note)
    return req


def open_reviews(t) -> list[dict]:
    return [r for r in t.settings.review_requests if r["status"] == "open"]


def lock(t, store, session_start: str, pushed=lambda member: False) -> int:
    """Registration closed: check everyone once, keep what it found, and stop later sheet syncs changing it.
    Anyone not eligible goes to Pending verification for the director (nobody is removed). Returns how many."""
    rules = rules_of(t.settings)
    game = getattr(t.settings, "skill_game", "8")
    snapshot, flagged = {}, 0
    for g in (t.teams if hasattr(t, "teams") else t.entries).values():
        if g.status == "no_show":
            continue
        for p in g.players:
            if not p.member:
                continue
            if override_for(t, p.member):
                snapshot[p.member] = {"no": [], "review": [], "override": True}
                continue
            no, review = check_player(store.get(p.member), rules, session_start, game, pushed(p.member))
            snapshot[p.member] = {"no": no, "review": review}
            if (no or review) and not p.pending:
                p.pending = ("Not eligible when registration closed: " + " ".join(no)) if no else \
                    ("Eligibility: " + "; ".join(review))
                flagged += 1
    t.settings.elig_snapshot, t.settings.elig_locked_at = snapshot, _now()
    audit(t, "locked", note=f"{len(snapshot)} player(s) checked, {flagged} sent to Pending verification")
    return flagged


def unlock(t) -> None:
    t.settings.elig_snapshot, t.settings.elig_locked_at = {}, ""
    audit(t, "unlocked")


def lock_due(t, at: datetime | None = None) -> bool:
    """Lock once registration has closed (its closing time passed) or the event has started."""
    from .schedule import parse_when

    if t.settings.elig_locked_at or not any_rules(rules_of(t.settings)):
        return False
    if t.status != "setup":
        return True
    try:
        closes = parse_when(t.settings.registration_closes)
    except Exception:
        return False
    return bool(closes) and (at or datetime.now()) >= closes
