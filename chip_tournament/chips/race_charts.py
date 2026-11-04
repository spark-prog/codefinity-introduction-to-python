"""The league's race charts: rack races (8-Ball and Saratoga) and ball counts (9-Ball).

Two versions of each chart live in race_charts/: standard.json (the APA charts) and adjusted.json (the
league's shortened charts). An event picks a race type and a version; this module turns two entries into
each side's race:

* Rack race, singles: both players' 8-Ball skill levels give two rack targets (6 vs 5 = 5/4).
* Rack race, doubles: both teams' combined 8-Ball skill levels ("6 or less" up to the chart's top).
* Rack race, 3-player teams: both teams' combined skill-level brackets (13 or below, 14-16, 17-20, 21-23);
  mixed games, each rack is 1 point. Only the adjusted file has this chart, so both versions use it.
* Ball count, singles: each player's own 9-Ball skill level gives their points target (SL 5 = 38).
* Ball count, doubles: each team's combined 9-Ball skill level ("4 or less" up to 12).

A player with no 8-Ball skill level races by their 9-Ball level, mapped to the 8-Ball range (1 -> 2,
8 and 9 -> 7). Anything off a chart is raced at its nearest square and the match carries a note for the
director, who can change any match's race.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .engine import TournamentError

RACK_RACE, BALL_COUNT = "rack_race", "ball_count"  # race methods that use these charts
STANDARD, ADJUSTED = "standard", "adjusted"
CHART_METHODS = (RACK_RACE, BALL_COUNT)
VERSIONS = ((STANDARD, "Standard"), (ADJUSTED, "Adjusted (shorter)"))
OVER_CAP_RACKS, OVER_CAP_POINTS = 1, 4  # a team over the skill cap races this much longer

_DIR = Path(__file__).with_name("race_charts")


@lru_cache(maxsize=None)
def chart(version: str) -> dict:
    if version not in (STANDARD, ADJUSTED):
        raise TournamentError("Pick the standard or adjusted race chart.")
    data = json.loads((_DIR / f"{version}.json").read_text())
    check(data)
    return data


def check(data: dict) -> None:
    """Rack race charts must mirror: 6 vs 5 = 5/4 means 5 vs 6 = 4/5. Catches typos in a chart file."""
    tables = [data.get("eight_ball", {}).get(k, {}) for k in ("singles", "doubles")]
    tables.append(data.get("team_3_player", {}).get("races", {}))
    for t in tables:
        for a, row in t.items():
            for b, cell in row.items():
                back = t.get(b, {}).get(a)
                if not back or (cell["you"], cell["opponent"]) != (back["opponent"], back["you"]):
                    raise TournamentError(f"Race chart typo: {a} vs {b} doesn't match {b} vs {a}.")


# ------------------------------------------------------------------ skill levels

def _fallback_map() -> dict[int, int]:
    return {int(k): v for k, v in chart(STANDARD)["_rules"]["eight_ball_sl_fallback"]["map"].items()}


def eight_ball_equivalent(nine_ball_level: int) -> int:
    """A 9-Ball level as an 8-Ball level (1 -> 2, 8 and 9 -> 7), the way the rack race charts convert it."""
    return _fallback_map().get(nine_ball_level, chart(STANDARD)["_rules"]["eight_ball_sl_range"][1])


def eight_ball_level(p, skill_game: str = "8") -> tuple[int, str]:
    """(the player's 8-Ball skill level for a rack race, a note when it was worked out from 9-Ball)."""
    lo, hi = chart(STANDARD)["_rules"]["eight_ball_sl_range"]
    if p.skill_8:
        return min(max(p.skill_8, lo), hi), ""
    nine = p.skill_9 or (p.skill if skill_game == "9" else 0)
    if nine:
        sl = _fallback_map().get(nine, hi)
        return sl, f"{p.name} has no 8-Ball skill level: raced as an 8-Ball {sl} from 9-Ball {nine}"
    if p.skill:
        return min(max(p.skill, lo), hi), ""
    return 0, ""


def nine_ball_level(p, skill_game: str = "8") -> int:
    """The player's 9-Ball skill level for a ball count (0 = not known)."""
    if p.skill_9:
        return p.skill_9
    if p.skill and not p.skill_8 and skill_game != "8":
        return p.skill
    return 0


def missing_levels(method: str, players, skill_game: str = "8") -> list[str]:
    """Players who can't be raced by this chart: no usable skill level."""
    if method == BALL_COUNT:
        return [p.name for p in players if not nine_ball_level(p, skill_game)]
    if method == RACK_RACE:
        return [p.name for p in players if not eight_ball_level(p, skill_game)[0]]
    return []


# ------------------------------------------------------------------ chart lookups

def _bucket(total: int, keys: list[str], what: str, notes: list[str]) -> str:
    """The chart key for a combined skill level: 'N_or_less' covers the low end; above the top is the top."""
    low = next(k for k in keys if k.endswith("_or_less"))
    floor = int(low.split("_")[0])
    top = max(int(k) for k in keys if k.isdigit())
    if total <= floor:
        return low
    if total > top:
        notes.append(f"{what}: combined skill level {total} is over the chart's {top}, raced as {top}")
        return str(top)
    return str(total)


def _team3_bracket(total: int, brackets: dict, what: str, notes: list[str]) -> str:
    for key, (lo, hi) in brackets.items():
        if lo <= total <= hi:
            return key
    top = max(brackets, key=lambda k: brackets[k][1])
    notes.append(f"{what}: combined skill level {total} is over the chart's {brackets[top][1]}, raced as {top}")
    return top


def races(method: str, version: str, team_size: int, a_players, b_players, skill_game: str = "8",
          a_name: str = "A", b_name: str = "B") -> tuple[int, int, list[str]]:
    """(side a's race, side b's race, notes for the director) from the chart."""
    c, notes = chart(version), []
    if method == RACK_RACE:
        if team_size >= 3:
            t3 = (c.get("team_3_player") or chart(ADJUSTED)["team_3_player"])
            ka = _team3_bracket(sum(p.skill for p in a_players), t3["brackets"], a_name, notes)
            kb = _team3_bracket(sum(p.skill for p in b_players), t3["brackets"], b_name, notes)
            cell = t3["races"][ka][kb]
            return cell["you"], cell["opponent"], notes
        la, lb = [], []
        for players, out in ((a_players, la), (b_players, lb)):
            for p in players:
                sl, note = eight_ball_level(p, skill_game)
                out.append(sl)
                if note:
                    notes.append(note)
        if team_size == 1:
            table = c["eight_ball"]["singles"]
            sa, sb = la[0], lb[0]
            cell = table.get(str(sa), {}).get(str(sb))
            if cell is None:  # the adjusted chart only pairs equal or neighboring levels (groups are set first)
                hi, lo = max(sa, sb), min(sa, sb)
                near = hi - 1
                while str(near) not in table.get(str(hi), {}) and near > lo:
                    near -= 1
                pair = (str(hi), str(near)) if sa > sb else (str(near), str(hi))
                cell = table[pair[0]][pair[1]]
                notes.append(f"SL {sa} vs SL {sb} isn't on the {version} chart: raced as SL {pair[0]} vs SL {pair[1]}")
            return cell["you"], cell["opponent"], notes
        table = c["eight_ball"]["doubles"]
        ka = _bucket(sum(la), list(table), a_name, notes)
        kb = _bucket(sum(lb), list(table), b_name, notes)
        cell = table[ka][kb]
        return cell["you"], cell["opponent"], notes
    if method == BALL_COUNT:
        if team_size == 1:
            table = c["nine_ball"]["singles"]
            return table[str(nine_ball_level(a_players[0], skill_game))], \
                table[str(nine_ball_level(b_players[0], skill_game))], notes
        table = c["nine_ball"]["doubles"]
        ka = _bucket(sum(nine_ball_level(p, skill_game) for p in a_players), list(table), a_name, notes)
        kb = _bucket(sum(nine_ball_level(p, skill_game) for p in b_players), list(table), b_name, notes)
        return table[ka], table[kb], notes
    raise TournamentError("That race type doesn't use a chart.")


def supports(method: str, team_size: int) -> str:
    """Why this race type can't be used with this many players per entry ('' = it can)."""
    if method == RACK_RACE and team_size > 3:
        return "The rack race charts cover singles, doubles and 3-player teams."
    if method == BALL_COUNT and team_size > 2:
        return "The ball count charts cover singles and doubles."
    return ""


def method_label(method: str, version: str = STANDARD) -> str:
    v = dict(VERSIONS).get(version, "Standard").split(" ")[0].lower()
    return {RACK_RACE: f"Rack race chart ({v})", BALL_COUNT: f"Ball count chart ({v})"}.get(method, "")
