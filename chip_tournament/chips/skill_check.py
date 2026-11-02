"""The director's skill level check: what players entered vs. the league's player list.

Each registered player is found in the player list by member number, or (for players who
registered without logging in) by a unique name match. Their entered skill levels are then
compared with the list's. Single-skill events use the event's format (8-Ball or 9-Ball) with
the established/converted rule; skill-group events compare both levels.
"""

from __future__ import annotations

from .players import PlayerStore, skill_for

OK, DIFFERS, MISSING, NOT_FOUND, AMBIGUOUS, REVIEW = "ok", "differs", "missing", "not_found", "ambiguous", "review"
LABELS = {OK: "Matches", DIFFERS: "Differs from the list", MISSING: "No skill level entered",
          NOT_FOUND: "Not in the player list", AMBIGUOUS: "More than one player with this name", REVIEW: "Needs review"}


def _key(name: str) -> str:
    return " ".join((name or "").lower().replace(".", "").split())


def name_index(store: PlayerStore) -> dict[str, list[dict]]:
    """'ann lee' (and 'nickname lee') -> the players with that name."""
    index: dict[str, list[dict]] = {}
    for rec in store.records().values():
        keys = {_key(f"{rec['first']} {rec['last']}")}
        if rec.get("nickname"):
            keys.add(_key(f"{rec['nickname']} {rec['last']}"))
        for k in keys:
            index.setdefault(k, []).append(rec)
    return index


def check_player(p, store: PlayerStore, index: dict, game: str, two_levels: bool) -> dict:
    """One registered player: who they are in the list, what they entered, what the list says, and the status."""
    entered = {"skill": p.skill, "skill8": p.skill_8, "skill9": p.skill_9}
    row = {"name": p.name, "member": p.member, "matched_by": "", "entered": entered, "list": None, "how": ""}
    rec = store.get(p.member) if p.member else None
    if rec is not None:
        row["matched_by"] = "member number"
    else:
        hits = index.get(_key(p.name), [])
        if len(hits) > 1:
            return {**row, "status": AMBIGUOUS}
        if not hits:
            return {**row, "status": NOT_FOUND}
        rec, row["matched_by"], row["member"] = hits[0], "name", hits[0]["member"]
    if two_levels:
        want = {"skill8": rec.get("sl8") or 0, "skill9": rec.get("sl9") or 0}
        row["list"] = want
        if not (p.skill_8 or p.skill_9):
            return {**row, "status": MISSING}
        same = p.skill_8 == want["skill8"] and p.skill_9 == want["skill9"]
        return {**row, "status": OK if same else DIFFERS}
    level, how = skill_for(rec, game)
    row["list"], row["how"] = {"skill": level}, how
    if level is None:
        return {**row, "status": REVIEW}
    if not p.skill:
        return {**row, "status": MISSING}
    return {**row, "status": OK if p.skill == level else DIFFERS}


def check_event(t, store: PlayerStore, two_levels: bool) -> list[dict]:
    """Every team or entry, with a row per player and whether anything needs a look."""
    index = name_index(store)
    game = getattr(t.settings, "skill_game", "8")
    groups = t.teams.values() if hasattr(t, "teams") else t.entries.values()
    out = []
    for g in groups:
        if getattr(g, "status", "") == "no_show":
            continue
        rows = [check_player(p, store, index, game, two_levels) for p in g.players]
        out.append({"id": g.id, "name": g.name, "status": g.status, "players": rows,
                    "fixable": any(r["status"] in (DIFFERS, MISSING) and r["list"] for r in rows),
                    "issues": [r for r in rows if r["status"] != OK]})
    return sorted(out, key=lambda x: (not x["issues"], x["name"].lower()))


def fixed_players(group, rows: list[dict], two_levels: bool):
    """Copies of the players with the list's skill levels (and member numbers found by name)."""
    import copy

    players = [copy.copy(p) for p in group.players]
    for p, r in zip(players, rows):
        if r["status"] not in (DIFFERS, MISSING, OK) or not r["list"]:
            continue
        if not p.member and r["member"]:
            p.member = r["member"]
        if two_levels:
            p.skill_8, p.skill_9 = r["list"]["skill8"], r["list"]["skill9"]
            p.skill = max(p.skill_8, p.skill_9)
        else:
            p.skill = r["list"]["skill"]
    return players
