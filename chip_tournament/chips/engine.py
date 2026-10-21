"""Core rules for a chip billiards tournament.

This module has no web or storage code in it, so the rules can be tested on
their own. Money is always kept in whole cents (int) to avoid rounding drift.

How the format works
--------------------
* Each team starts with a number of chips based on its skill level.
* Round 1: teams are paired at random and called to a table.
* A match is a race to N games. The winner keeps its chips and goes to the
  bottom of the WINNERS list. The loser gives one chip back to the main table
  and goes to the bottom of the LOSERS list.
* A team with zero chips is eliminated.
* When every match in the round is done, the next round pairs the lists in
  order: winner 1 vs winner 2, winner 3 vs winner 4, loser 1 vs loser 2, ...
* Pot = entry fees (+ any added money). Chip value = pot / chips still in play,
  so each chip lost to the table makes every remaining chip worth more.
* When cash-in opens, a team may cash in: it is paid chips x chip value and
  leaves the rotation. At last call every remaining team is cashed in.
"""

from __future__ import annotations

import random
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

# Team statuses
REGISTERED = "registered"
ACTIVE = "active"
ELIMINATED = "eliminated"
CASHED_OUT = "cashed_out"

# Match statuses
WAITING = "waiting"  # paired, waiting for a free table
CALLED = "called"  # called to a table / in play
DONE = "done"
CANCELLED = "cancelled"

# Tournament statuses
SETUP = "setup"
RUNNING = "running"
FINISHED = "finished"

DEFAULT_SKILL_CHIPS = {"1": 5, "2": 4, "3": 3, "4": 2, "5": 1}


class TournamentError(Exception):
    """Raised when an action breaks the tournament rules."""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def money(cents: float) -> str:
    return f"${cents / 100:,.2f}"


@dataclass
class Player:
    name: str
    phone: str = ""


@dataclass
class Team:
    id: str
    name: str
    skill_level: str
    captain: str
    phone: str
    players: list[Player] = field(default_factory=list)
    pin: str = ""
    chips: int = 0
    starting_chips: int = 0
    status: str = REGISTERED
    payout_cents: int = 0
    wins: int = 0
    losses: int = 0
    registered_at: str = field(default_factory=now)

    @property
    def phones(self) -> list[str]:
        seen = []
        for number in [self.phone] + [p.phone for p in self.players]:
            if number and number not in seen:
                seen.append(number)
        return seen


@dataclass
class Match:
    id: str
    round: int
    team_a: str
    team_b: str | None  # None means a bye
    bracket: str  # "start", "winners", "losers" or "cross"
    status: str = WAITING
    table: str | None = None
    score_a: int = 0
    score_b: int = 0
    winner: str | None = None
    loser: str | None = None
    called_at: str | None = None
    finished_at: str | None = None


@dataclass
class Settings:
    name: str = "Chip Tournament"
    date: str = ""
    start_time: str = ""
    venue: str = ""
    address: str = ""
    details: str = ""
    entry_fee_cents: int = 2000
    added_money_cents: int = 0
    race_to: int = 3
    skill_chips: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SKILL_CHIPS))
    tables: list[str] = field(default_factory=lambda: ["1", "2", "3", "4"])
    cash_in_time: str = ""
    last_call_time: str = ""


@dataclass
class LedgerEntry:
    at: str
    team_id: str
    team_name: str
    change: int
    chips_after: int
    reason: str
    chip_value_cents: float


@dataclass
class Message:
    to: str
    body: str
    team_id: str | None = None


@dataclass
class Tournament:
    settings: Settings = field(default_factory=Settings)
    teams: dict[str, Team] = field(default_factory=dict)
    matches: list[Match] = field(default_factory=list)
    status: str = SETUP
    round: int = 0
    winners_list: list[str] = field(default_factory=list)
    losers_list: list[str] = field(default_factory=list)
    pot_cents: int = 0
    total_collected_cents: int = 0
    cash_in_open: bool = False
    last_call: bool = False
    ledger: list[LedgerEntry] = field(default_factory=list)
    # Texts produced by the rules; the app sends them and clears this list.
    outbox: list[Message] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ helpers

    def team(self, team_id: str) -> Team:
        try:
            return self.teams[team_id]
        except KeyError:
            raise TournamentError("Unknown team.") from None

    def match(self, match_id: str) -> Match:
        for m in self.matches:
            if m.id == match_id:
                return m
        raise TournamentError("Unknown match.")

    def name_of(self, team_id: str | None) -> str:
        if team_id is None:
            return "BYE"
        return self.teams[team_id].name if team_id in self.teams else "?"

    @property
    def active_teams(self) -> list[Team]:
        return [t for t in self.teams.values() if t.status == ACTIVE]

    @property
    def chips_in_play(self) -> int:
        return sum(t.chips for t in self.active_teams)

    @property
    def total_chips_issued(self) -> int:
        return sum(t.starting_chips for t in self.teams.values())

    @property
    def chips_returned(self) -> int:
        """Chips given back to the main table from losses."""
        return sum(-e.change for e in self.ledger if e.reason.startswith("Lost"))

    @property
    def chip_value_cents(self) -> float:
        chips = self.chips_in_play
        return self.pot_cents / chips if chips else 0.0

    def payout_for(self, team: Team) -> int:
        """What this team would be paid if it cashed in right now."""
        chips = self.chips_in_play
        if team.status != ACTIVE or not chips:
            return 0
        if len(self.active_teams) == 1:
            return self.pot_cents
        return round(self.pot_cents * team.chips / chips)

    def open_matches(self) -> list[Match]:
        return [m for m in self.matches if m.status in (WAITING, CALLED)]

    def current_match_for(self, team_id: str) -> Match | None:
        for m in self.open_matches():
            if team_id in (m.team_a, m.team_b):
                return m
        return None

    def free_tables(self) -> list[str]:
        busy = {m.table for m in self.matches if m.status == CALLED}
        return [t for t in self.settings.tables if t not in busy]

    def standings(self) -> list[Team]:
        order = {ACTIVE: 0, CASHED_OUT: 1, ELIMINATED: 2, REGISTERED: 3}
        return sorted(
            self.teams.values(),
            key=lambda t: (order.get(t.status, 9), -t.chips, -t.payout_cents, t.name.lower()),
        )

    def _say(self, text: str) -> None:
        self.log.append(f"{now()}  {text}")

    def _text_team(self, team: Team, body: str) -> None:
        for number in team.phones:
            self.outbox.append(Message(to=number, body=body, team_id=team.id))

    def _text_active(self, body: str) -> None:
        for team in self.active_teams:
            self._text_team(team, body)

    def _chip_change(self, team: Team, change: int, reason: str) -> None:
        team.chips += change
        self.ledger.append(
            LedgerEntry(
                at=now(),
                team_id=team.id,
                team_name=team.name,
                change=change,
                chips_after=team.chips,
                reason=reason,
                chip_value_cents=self.chip_value_cents,
            )
        )

    # ------------------------------------------------------------- registration

    def chips_for_skill(self, skill_level: str) -> int:
        try:
            return int(self.settings.skill_chips[str(skill_level)])
        except KeyError:
            raise TournamentError(f"No chip count is set for skill level {skill_level}.") from None

    def register_team(
        self,
        name: str,
        skill_level: str,
        captain: str,
        phone: str,
        players: list[Player] | None = None,
    ) -> Team:
        name = name.strip()
        if not name:
            raise TournamentError("Team name is required.")
        if any(t.name.lower() == name.lower() for t in self.teams.values()):
            raise TournamentError(f"A team named '{name}' is already registered.")
        if self.status == FINISHED:
            raise TournamentError("This tournament is over.")
        self.chips_for_skill(skill_level)  # validates the skill level

        team = Team(
            id=uuid.uuid4().hex[:8],
            name=name,
            skill_level=str(skill_level),
            captain=captain.strip(),
            phone=phone.strip(),
            players=players or [],
            pin=f"{secrets.randbelow(10000):04d}",
        )
        self.teams[team.id] = team
        self._say(f"Registered {team.name} (skill {team.skill_level}).")
        self._text_team(
            team,
            f"{team.name} is registered for {self.settings.name}. "
            f"Your team PIN for roster changes is {team.pin}.",
        )
        if self.status == RUNNING:
            self._join_running(team)
        return team

    def _join_running(self, team: Team) -> None:
        """Late entry: pay in, get chips, and wait on the winners list."""
        self._activate(team)
        self.winners_list.append(team.id)
        self._say(f"{team.name} entered late with {team.chips} chips.")

    def _activate(self, team: Team) -> None:
        fee = self.settings.entry_fee_cents
        self.pot_cents += fee
        self.total_collected_cents += fee
        team.status = ACTIVE
        team.starting_chips = self.chips_for_skill(team.skill_level)
        self._chip_change(team, team.starting_chips, f"Starting chips (skill {team.skill_level})")

    def update_roster(
        self,
        team_id: str,
        captain: str,
        phone: str,
        players: list[Player],
        name: str | None = None,
    ) -> Team:
        team = self.team(team_id)
        if name is not None and name.strip() and name.strip() != team.name:
            if any(t.name.lower() == name.strip().lower() for t in self.teams.values() if t.id != team_id):
                raise TournamentError(f"A team named '{name.strip()}' is already registered.")
            team.name = name.strip()
        team.captain = captain.strip()
        team.phone = phone.strip()
        team.players = players
        self._say(f"Roster updated for {team.name}.")
        return team

    def set_skill_level(self, team_id: str, skill_level: str) -> None:
        team = self.team(team_id)
        if team.status != REGISTERED:
            raise TournamentError("Skill level can only change before the tournament starts.")
        self.chips_for_skill(skill_level)
        team.skill_level = str(skill_level)

    def remove_team(self, team_id: str) -> None:
        team = self.team(team_id)
        if team.status != REGISTERED:
            raise TournamentError("Teams can only be removed before the tournament starts.")
        del self.teams[team_id]
        self._say(f"Removed {team.name}.")

    # ----------------------------------------------------------------- running

    def start(self, rng: random.Random | None = None) -> None:
        if self.status != SETUP:
            raise TournamentError("The tournament has already started.")
        teams = list(self.teams.values())
        if len(teams) < 2:
            raise TournamentError("At least two teams are needed to start.")
        if not self.settings.tables:
            raise TournamentError("Add at least one table before starting.")

        self.status = RUNNING
        self.pot_cents = self.settings.added_money_cents
        self.total_collected_cents = 0
        for team in teams:
            self._activate(team)

        self._say(
            f"Tournament started: {len(teams)} teams, {self.chips_in_play} chips, "
            f"pot {money(self.pot_cents)}, chip value {money(self.chip_value_cents)}."
        )
        order = [t.id for t in teams]
        (rng or random.SystemRandom()).shuffle(order)
        self.round = 1
        self._pair(order, [], start=True)
        self._call_matches()

    def _pair(self, winners: list[str], losers: list[str], start: bool = False) -> None:
        """Create the matches for self.round from the two ordered lists."""
        pairs: list[tuple[str, str | None, str]] = []
        label = "start" if start else "winners"
        for i in range(0, len(winners) - 1, 2):
            pairs.append((winners[i], winners[i + 1], label))
        for i in range(0, len(losers) - 1, 2):
            pairs.append((losers[i], losers[i + 1], "losers"))

        odd_winner = winners[-1] if len(winners) % 2 else None
        odd_loser = losers[-1] if len(losers) % 2 else None
        if odd_winner and odd_loser:
            # One team left on each list: they play each other.
            pairs.append((odd_winner, odd_loser, "cross"))
        elif odd_winner or odd_loser:
            pairs.append((odd_winner or odd_loser, None, label if odd_winner else "losers"))

        for a, b, bracket in pairs:
            m = Match(id=uuid.uuid4().hex[:8], round=self.round, team_a=a, team_b=b, bracket=bracket)
            self.matches.append(m)
            if b is None:
                self._finish_bye(m)
            else:
                self._say(f"Round {self.round}: {self.name_of(a)} vs {self.name_of(b)} ({bracket}).")

    def _finish_bye(self, m: Match) -> None:
        m.status = DONE
        m.winner = m.team_a
        m.finished_at = now()
        team = self.teams[m.team_a]
        target = self.losers_list if m.bracket == "losers" else self.winners_list
        target.append(team.id)
        self._say(f"Round {self.round}: {team.name} has a bye and keeps its chips.")
        self._text_team(team, f"{team.name}: you have a bye in round {self.round}. Stay close for the next round.")

    def _call_matches(self) -> None:
        """Send waiting matches to free tables, in the order they were paired."""
        free = self.free_tables()
        for m in self.matches:
            if not free:
                break
            if m.status != WAITING:
                continue
            m.table = free.pop(0)
            m.status = CALLED
            m.called_at = now()
            a, b = self.teams[m.team_a], self.teams[m.team_b]
            self._say(f"Called {a.name} vs {b.name} to table {m.table}.")
            for team, opp in ((a, b), (b, a)):
                self._text_team(
                    team,
                    f"{team.name}: report to TABLE {m.table} now vs {opp.name}. "
                    f"Race to {self.settings.race_to}. You have {team.chips} chip(s).",
                )

    def score(self, match_id: str, side: str, delta: int) -> Match:
        m = self.match(match_id)
        if m.status != CALLED:
            raise TournamentError("Only a match at a table can be scored.")
        attr = "score_a" if side == "a" else "score_b"
        value = max(0, min(self.settings.race_to, getattr(m, attr) + delta))
        setattr(m, attr, value)
        if value >= self.settings.race_to:
            self.record_result(match_id, m.team_a if side == "a" else m.team_b)
        return m

    def record_result(self, match_id: str, winner_id: str) -> Match:
        m = self.match(match_id)
        if m.status != CALLED:
            raise TournamentError("That match is not at a table.")
        if winner_id not in (m.team_a, m.team_b):
            raise TournamentError("The winner must be one of the two teams in the match.")
        loser_id = m.team_b if winner_id == m.team_a else m.team_a
        winner, loser = self.teams[winner_id], self.teams[loser_id]

        m.status = DONE
        m.winner, m.loser = winner_id, loser_id
        m.finished_at = now()
        winner.wins += 1
        loser.losses += 1

        self._chip_change(loser, -1, f"Lost round {m.round} to {winner.name}")
        self.winners_list.append(winner_id)
        self._say(
            f"{winner.name} beat {loser.name} on table {m.table}. {loser.name} gives a chip back "
            f"({loser.chips} left). Chip value now {money(self.chip_value_cents)}."
        )
        if loser.chips <= 0:
            loser.status = ELIMINATED
            self._say(f"{loser.name} is out of chips and eliminated.")
            self._text_team(loser, f"{loser.name}: you're out of chips and eliminated. Thanks for playing!")
        else:
            self.losers_list.append(loser_id)
            self._text_team(loser, f"{loser.name}: you lost round {m.round} and have {loser.chips} chip(s) left.")
        self._text_team(winner, f"{winner.name}: nice win! You keep your {winner.chips} chip(s).")

        self._advance()
        return m

    def _advance(self) -> None:
        """Fill free tables and start the next round when this one is complete."""
        if self._check_finished():
            return
        if any(m.round == self.round and m.status in (WAITING, CALLED) for m in self.matches):
            self._call_matches()
            return
        winners = [t for t in self.winners_list if self.teams[t].status == ACTIVE]
        losers = [t for t in self.losers_list if self.teams[t].status == ACTIVE]
        # Safety net: an active team should always be on a list by now.
        winners += [t.id for t in self.active_teams if t.id not in winners and t.id not in losers]
        self.winners_list, self.losers_list = [], []
        self.round += 1
        self._say(f"Round {self.round} pairings are set.")
        self._pair(winners, losers)
        # A round of nothing but a bye ends immediately, so go round again.
        if not any(m.round == self.round and m.status == WAITING for m in self.matches):
            self._advance()
            return
        self._call_matches()

    def _check_finished(self) -> bool:
        active = self.active_teams
        if self.status != RUNNING or len(active) > 1:
            return False
        if len(active) == 1:
            champ = active[0]
            self._cash_out(champ, "Last team standing")
            self._text_team(champ, f"{champ.name}: you're the last team standing! Payout {money(champ.payout_cents)}.")
        self.status = FINISHED
        self._say("Tournament finished.")
        return True

    # ---------------------------------------------------------------- payouts

    def _cash_out(self, team: Team, reason: str) -> int:
        payout = self.payout_for(team)
        chips = team.chips
        team.payout_cents += payout
        self.pot_cents -= payout
        self._chip_change(team, -chips, f"{reason}: cashed in {chips} chip(s) for {money(payout)}")
        team.status = CASHED_OUT
        self._say(f"{team.name} cashed in {chips} chip(s) for {money(payout)}.")
        return payout

    def _withdraw_from_lists(self, team: Team) -> None:
        """Take a team out of the rotation, cancelling a match that hasn't started."""
        m = self.current_match_for(team.id)
        if m is not None:
            if m.score_a or m.score_b:
                raise TournamentError(f"{team.name} has a match in progress. Finish it before cashing in.")
            m.status = CANCELLED
            m.finished_at = now()
            other = m.team_b if m.team_a == team.id else m.team_a
            if other is not None:
                # The opponent waits for the next round as if it had a bye.
                target = self.losers_list if m.bracket == "losers" else self.winners_list
                target.append(other)
                self._text_team(
                    self.teams[other],
                    f"{self.teams[other].name}: your opponent cashed in. You'll be paired next round.",
                )
        for lst in (self.winners_list, self.losers_list):
            if team.id in lst:
                lst.remove(team.id)

    def open_cash_in(self) -> None:
        if self.status != RUNNING:
            raise TournamentError("The tournament is not running.")
        self.cash_in_open = True
        self._say(f"Cash-in is open. Chip value {money(self.chip_value_cents)}.")
        for team in self.active_teams:
            self._text_team(
                team,
                f"Cash-in is OPEN. Chips are worth {money(self.chip_value_cents)} each; "
                f"{team.name}'s {team.chips} chip(s) = {money(self.payout_for(team))}. "
                "See the desk to cash in, or keep playing.",
            )

    def cash_in(self, team_id: str) -> int:
        team = self.team(team_id)
        if self.status != RUNNING:
            raise TournamentError("The tournament is not running.")
        if not self.cash_in_open:
            raise TournamentError("Cash-in is not open yet.")
        if team.status != ACTIVE:
            raise TournamentError(f"{team.name} is not active.")
        self._withdraw_from_lists(team)
        payout = self._cash_out(team, "Cash-in")
        self._text_team(team, f"{team.name}: you cashed in for {money(payout)}. Thanks for playing!")
        self._advance()
        return payout

    def call_last_call(self) -> None:
        """Everyone still in must cash in now at the current chip value."""
        if self.status != RUNNING:
            raise TournamentError("The tournament is not running.")
        self.last_call = True
        self.cash_in_open = True
        for m in self.open_matches():
            m.status = CANCELLED
            m.finished_at = now()
        self.winners_list, self.losers_list = [], []
        self._say(f"LAST CALL. All teams cash in at {money(self.chip_value_cents)} per chip.")
        teams = sorted(self.active_teams, key=lambda t: t.chips)
        for team in teams:
            # The last team takes whatever is left, so rounding never loses a cent.
            payout = self._cash_out(team, "Last call")
            self._text_team(team, f"LAST CALL: {team.name} cashed in {money(payout)}. See the desk to collect.")
        self.status = FINISHED
        self._say("Tournament finished.")

    def adjust_chips(self, team_id: str, change: int, reason: str) -> None:
        """Manual correction by the tournament director."""
        team = self.team(team_id)
        if team.status != ACTIVE:
            raise TournamentError("Only active teams' chips can be adjusted.")
        if change == 0:
            raise TournamentError("Enter a non-zero change.")
        if team.chips + change < 0:
            raise TournamentError("A team can't have fewer than zero chips.")
        self._chip_change(team, change, f"Adjustment: {reason or 'no reason given'}")
        self._say(f"Adjusted {team.name} by {change:+d} chip(s): {reason}.")
        if team.chips == 0:
            team.status = ELIMINATED
            self._withdraw_from_lists(team)
            self._say(f"{team.name} is out of chips and eliminated.")
            self._advance()

    def broadcast(self, body: str) -> int:
        before = len(self.outbox)
        teams = self.active_teams if self.status == RUNNING else list(self.teams.values())
        for team in teams:
            self._text_team(team, body)
        return len(self.outbox) - before

    # ----------------------------------------------------------- serialization

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Tournament:
        data = dict(data)
        t = cls(
            settings=Settings(**data.pop("settings", {})),
            teams={
                k: Team(**{**v, "players": [Player(**p) for p in v.get("players", [])]})
                for k, v in data.pop("teams", {}).items()
            },
            matches=[Match(**m) for m in data.pop("matches", [])],
            ledger=[LedgerEntry(**e) for e in data.pop("ledger", [])],
            outbox=[Message(**m) for m in data.pop("outbox", [])],
        )
        for key, value in data.items():
            setattr(t, key, value)
        return t
