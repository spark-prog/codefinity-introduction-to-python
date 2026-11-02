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
        t._say(f"Director verified {p.name} ({p.member}) on {g.name}: {p.pending}.")
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
