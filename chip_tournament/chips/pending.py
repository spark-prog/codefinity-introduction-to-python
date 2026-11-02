"""Pending verification: member numbers the director still has to check.

A registration keeps its spot while a member number is pending (not in the player list, or
in it under a different name). The director verifies it as entered, corrects it to a number
in the list, or removes the registration (the team is texted why).
"""

from __future__ import annotations

from .engine import TournamentError


def groups(t) -> dict:
    return t.teams if hasattr(t, "teams") else t.entries


def pending(t) -> list[tuple]:
    """(team or entry, player index, player) for every player waiting on the director."""
    return [(g, i, p) for g in groups(t).values() if g.status != "no_show"
            for i, p in enumerate(g.players) if p.pending]


def _find(t, gid: str, i: int):
    g = groups(t).get(gid)
    if g is None or not 0 <= i < len(g.players) or not g.players[i].pending:
        raise TournamentError("That player isn't waiting for verification any more.")
    return g, g.players[i]


def verify(t, gid: str, i: int, member: str | None = None, name: str | None = None) -> str:
    """Accept the number (as entered, or corrected to `member`, with the list's `name`)."""
    g, p = _find(t, gid, i)
    if member and member != p.member:
        other = t.registered_as(member, skip=gid)
        if other is not None:
            raise TournamentError(f"{member} is already registered ({other.name}).")
        if any(o is not p and o.member == member for o in g.players):
            raise TournamentError(f"{member} is already on {g.name}.")
        old = p.member
        p.member = member
        if name:
            p.name = name
        t._say(f"Director corrected {p.name}'s member number on {g.name}: {old} → {member}.")
    else:
        typed = p.name
        if name and name != p.name:  # the player list's name for that number (they typed "Angel": Angelica McCain)
            p.name = name
        t._say(f"Director verified {p.name} ({p.member}) on {g.name}: {p.pending}"
               + (f" (registered as {typed})." if p.name != typed else "."))
    p.pending = ""
    return p.name


def remove(t, gid: str) -> str:
    """Take out a registration whose member number couldn't be verified, and tell the team why."""
    g = groups(t).get(gid)
    if g is None:
        raise TournamentError("That registration isn't there any more.")
    body = (f"{g.name}: the league office couldn't verify a member number on your registration for "
            f"{t.settings.name}, so it was removed. Questions? Contact the league office.")
    if hasattr(t, "teams"):
        t.remove_team(gid)
        t._text_team(g, body)
    else:
        t.remove_entry(gid)
        t._text(g, body)
    t._say(f"Removed {g.name}: member number not verified.")
    return g.name


# ------------------------------------------------------------ director's exceptions

def allow_twice(t, member: str, note: str) -> None:
    """Director: this member number may be on more than one team in this event (with a reason)."""
    from datetime import datetime

    note = (note or "").strip()
    if not note:
        raise TournamentError("Add a note saying why (it's kept with the exception).")
    if any(x["member"] == member for x in t.settings.member_exceptions):
        raise TournamentError(f"{member} already has an exception.")
    t.settings.member_exceptions.append({"member": member, "note": note,
                                         "at": datetime.now().isoformat(timespec="seconds")})
    t._say(f"Director allowed {member} on more than one team: {note}")


def remove_exception(t, member: str) -> None:
    before = len(t.settings.member_exceptions)
    t.settings.member_exceptions = [x for x in t.settings.member_exceptions if x["member"] != member]
    if len(t.settings.member_exceptions) == before:
        raise TournamentError("That exception isn't there any more.")
    t._say(f"Director removed the exception for {member} (registrations already made stay).")


def on_teams(t, member: str) -> list[str]:
    """The teams or entries a member number is on in this event."""
    return [g.name for g in groups(t).values() if g.status != "no_show" and any(p.member == member for p in g.players)]
