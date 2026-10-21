"""Core rules for a chip billiards tournament.

This module has no web or storage code in it, so the rules can be tested on
their own. Money is always kept in whole cents (int) to avoid rounding drift.

How the format works
--------------------
* The director sets a field cap. Teams that register once the field is full go
  on a waiting list and move into the field, in order, as spots open.
* Event check-in: the director checks teams in as they arrive at the venue.
  The tournament can't start while a team in the field isn't checked in; a
  team that doesn't show is marked a no-show, which frees its spot for the
  next team on the waiting list.
* Late entries are allowed after the start, and the money settings (entry fee,
  added money, chip brackets, registration cap) can still change, until every
  team still in has completed a match. Then both lock for the night.
* A team is two partners. Their combined skill level sets the team's starting
  chips (e.g. 6 or less = 5 chips, 7-8 = 4, 9-10 = 3, 11 = 2). Combined skill
  is capped at registration (10). If the same partners' skill levels go up,
  the team may rise to the top bracket (11) by the start. Swapping in a new
  partner counts as registering again, so the 10 cap applies, but the team
  keeps its spot in the field. Above the top bracket a team needs a new partner.
* Pairing is rolling. At the start every team is put on the winners list in
  random order. As soon as two teams on the same list can play each other they
  are paired and join the "waiting on a table" queue; tables take pairs from
  that queue, fewest rounds played first.
* No rematches unless it's the only option. A team arriving on a list is
  compared with the top team, then the second, and so on, and paired with the
  first one it hasn't played. If it has played them all it waits for the next
  arrival. A rematch (or a winners-vs-losers "cross" match) only happens when
  both teams have already played everyone still in, or when nothing is playing
  or queued that could bring a new opponent.
* A match is a race to N games. The winner keeps its chips and goes to the
  bottom of the WINNERS list. The loser gives one chip back to the main table
  and goes to the bottom of the LOSERS list.
* A team with zero chips is eliminated.
* Catch-up: a team that has played 2+ fewer matches than the leader (slow
  play, late entry, an opponent cashing in) jumps to the top of its list. If
  nobody else is waiting on its list it plays the top team of the other list.
  The director can also push any waiting team to the front ("Call next").

* Check-in: when called, each team taps in as it arrives at the table. Once
  both are there the match starts 2 minutes later. A team that hasn't checked
  in 5 minutes after being called gets a 15-minute grace period, then forfeits
  (loses the match and gives a chip back). If neither team shows, both forfeit.
* Pot = entry fees (+ any added money). Chip value = pot / chips still in play,
  so each chip lost to the table makes every remaining chip worth more.
* When cash-in opens, a team may cash in: it is paid chips x chip value and
  leaves the rotation.
* Last call: each team's next match is its last, and both teams are cashed in
  automatically when it ends. A match already on a table at last call finishes
  as normal, then those teams play one more (last) match.
"""

from __future__ import annotations

import random
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

# Team statuses
REGISTERED = "registered"
WAITLISTED = "waitlisted"
NO_SHOW = "no_show"
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

# [highest combined skill level, starting chips], lowest bracket first.
DEFAULT_CHIP_BRACKETS = [[6, 5], [8, 4], [10, 3], [11, 2]]
DEFAULT_REGISTRATION_CAP = 10
TEAM_SIZE = 2


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
    skill: int = 0


@dataclass
class Team:
    id: str
    name: str
    players: list[Player] = field(default_factory=list)
    pin: str = ""
    chips: int = 0
    starting_chips: int = 0
    status: str = REGISTERED
    payout_cents: int = 0
    wins: int = 0
    losses: int = 0
    priority: bool = False  # director pressed "Call next"
    checked_in_at: str | None = None  # arrived at the venue (event check-in)
    queued_at: str | None = None  # when it joined its current list
    # Set at last call: matches this team still plays before it is cashed in.
    matches_left: int | None = None
    registered_at: str = field(default_factory=now)

    @property
    def phones(self) -> list[str]:
        seen = []
        for number in [p.phone for p in self.players]:
            if number and number not in seen:
                seen.append(number)
        return seen

    @property
    def matches_played(self) -> int:
        return self.wins + self.losses

    @property
    def combined_skill(self) -> int:
        return sum(p.skill for p in self.players)

    @property
    def partners(self) -> str:
        return " & ".join(p.name for p in self.players)


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
    arrived_a: str | None = None  # when each team checked in at the table
    arrived_b: str | None = None
    start_at: str | None = None  # set once both teams are at the table
    grace_a: str | None = None  # when each team's grace period began
    grace_b: str | None = None
    forfeit: str | None = None  # "a", "b" or "both"
    priority: bool = False  # director pressed "Call next" for this pair
    rematch: bool = False  # these teams had already played each other
    clock_paused: bool = False  # no automatic forfeit (set after an Undo restores an overdue match)


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
    warmup_minutes: int = 2  # from both teams arriving to the match start
    checkin_minutes: int = 5  # to reach the table after being called
    grace_minutes: int = 15  # extra time before a no-show forfeits
    match_minutes: int = 15  # expected match length (amber after this)
    long_match_minutes: int = 20  # red flag this long after the match start
    catchup_matches: int = 2  # this many matches behind the leader = jump the queue
    chip_brackets: list[list[int]] = field(default_factory=lambda: [list(b) for b in DEFAULT_CHIP_BRACKETS])
    registration_cap: int = DEFAULT_REGISTRATION_CAP
    field_cap: int = 0  # most teams in the field; 0 = no limit set
    tables: list[str] = field(default_factory=lambda: ["1", "2", "3", "4"])
    cash_in_time: str = ""
    last_call_time: str = ""
    checkin_by: str = ""  # event check-in time shown to players, e.g. "6:30 PM"
    require_checkin: bool = True  # every team in the field must be checked in to start
    sms_name: str = ""  # starts every text, e.g. "First Coast APA"
    contact_phone: str = ""  # added to the registration text: "Questions? Call/text ..."


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
    waitlist: list[str] = field(default_factory=list)  # team ids, first in line first
    pot_cents: int = 0
    total_collected_cents: int = 0
    cash_in_open: bool = False
    entries_locked: bool = False  # set once every team has completed a match
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
        """Chips given back to the main table from losses and forfeits."""
        return sum(-e.change for e in self.ledger if e.reason.startswith(("Lost", "Forfeited")))

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

    @property
    def field_teams(self) -> list[Team]:
        """Teams with a spot in the event (not on the waiting list, not no-shows)."""
        return [t for t in self.teams.values() if t.status not in (WAITLISTED, NO_SHOW)]

    @property
    def field_full(self) -> bool:
        return bool(self.settings.field_cap) and len(self.field_teams) >= self.settings.field_cap

    def waitlist_position(self, team: Team) -> int | None:
        return self.waitlist.index(team.id) + 1 if team.id in self.waitlist else None

    def standings(self) -> list[Team]:
        order = {ACTIVE: 0, CASHED_OUT: 1, ELIMINATED: 2, REGISTERED: 3, WAITLISTED: 4, NO_SHOW: 5}
        return sorted(
            self.teams.values(),
            key=lambda t: (order.get(t.status, 9), -t.chips, -t.payout_cents, t.name.lower()),
        )

    def _say(self, text: str) -> None:
        self.log.append(f"{now()}  {text}")

    def _text_team(self, team: Team, body: str, prefix: bool = True) -> None:
        if prefix and self.settings.sms_name:
            body = f"{self.settings.sms_name}: {body}"
        for number in team.phones:
            self.outbox.append(Message(to=number, body=body, team_id=team.id))

    def _questions(self) -> str:
        phone = self.settings.contact_phone
        return f" Questions? Call/text {phone}" if phone else ""

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

    @property
    def max_skill(self) -> int:
        """The highest combined skill level that can play."""
        return max(top for top, _ in self.settings.chip_brackets)

    def chips_for_skill(self, combined_skill: int) -> int:
        for top, chips in sorted(self.settings.chip_brackets):
            if combined_skill <= top:
                return chips
        raise TournamentError(
            f"Combined skill level {combined_skill} is over {self.max_skill}. This team needs a new partner."
        )

    def chips_preview(self, team: Team) -> int | None:
        """Starting chips for a team, or None if its combined skill is too high."""
        try:
            return self.chips_for_skill(team.combined_skill)
        except TournamentError:
            return None

    def bracket_labels(self) -> list[tuple[str, int]]:
        """[("6 or less", 5), ("7–8", 4), ...] for showing the chip table."""
        labels, low = [], None
        for top, chips in sorted(self.settings.chip_brackets):
            if low is None:
                label = f"{top} or less"
            elif low == top:
                label = str(top)
            else:
                label = f"{low}–{top}"
            labels.append((label, chips))
            low = top + 1
        return labels

    def _check_players(self, players: list[Player]) -> None:
        if len(players) != TEAM_SIZE:
            raise TournamentError(f"A team needs exactly {TEAM_SIZE} partners.")
        for p in players:
            if not p.name.strip():
                raise TournamentError("Enter both partners' names.")
            if p.skill < 1:
                raise TournamentError(f"Enter a skill level for {p.name}.")
        if not any(p.phone for p in players):
            raise TournamentError("Enter at least one cell number for text alerts.")

    def register_team(self, name: str, players: list[Player]) -> Team:
        name = name.strip()
        if not name:
            raise TournamentError("Team name is required.")
        if any(t.name.lower() == name.lower() for t in self.teams.values()):
            raise TournamentError(f"A team named '{name}' is already registered.")
        if self.status == FINISHED:
            raise TournamentError("This tournament is over.")
        if self.last_call:
            raise TournamentError("Registration is closed after last call.")
        if self.entries_locked:
            raise TournamentError("Registration is closed: every team has already played a match.")
        self._check_players(players)
        combined = sum(p.skill for p in players)
        if combined > self.settings.registration_cap:
            raise TournamentError(
                f"Combined skill level {combined} is over the registration cap of "
                f"{self.settings.registration_cap}. Register with a different partner."
            )
        self.chips_for_skill(combined)

        full = self.field_full
        team = Team(
            id=uuid.uuid4().hex[:8],
            name=name,
            players=players,
            pin=f"{secrets.randbelow(10000):04d}",
        )
        self.teams[team.id] = team
        if full:
            team.status = WAITLISTED
            self.waitlist.append(team.id)
            spot = len(self.waitlist)
            self._say(f"{team.name} ({team.partners}, combined skill {combined}) is #{spot} on the waiting list.")
            self._text_team(
                team,
                f"{self.settings.name} is full. {team.name} is #{spot} on the waiting list; we'll text you "
                f"if a spot opens. Your team PIN for roster changes is {team.pin}. Reply STOP to opt out.{self._questions()}",
            )
            return team
        self._say(f"Registered {team.name}: {team.partners} (combined skill {combined}).")
        self._text_team(
            team,
            f"{team.name} is registered for {self.settings.name}. "
            f"Your team PIN for roster changes is {team.pin}. Reply STOP to opt out.{self._questions()}",
        )
        if self.status == RUNNING:
            self._join_running(team)
        return team

    def _fill_from_waitlist(self) -> None:
        """Move waiting teams into the field, in order, while there is room."""
        if self.status == FINISHED or self.last_call or self.entries_locked:
            return
        while self.waitlist and not self.field_full:
            team = self.teams[self.waitlist.pop(0)]
            team.status = REGISTERED
            self._say(f"{team.name} moved from the waiting list into the field.")
            self._text_team(team, f"Good news: a spot opened and {team.name} is now in the field for {self.settings.name}.")
            if self.status == RUNNING:
                self._join_running(team)

    def set_field_cap(self, cap: int) -> None:
        if cap < 0:
            raise TournamentError("The field cap can't be negative.")
        in_field = len(self.field_teams)
        if cap and cap < in_field:
            raise TournamentError(f"{in_field} teams are already in the field, so the cap can't be lower than that.")
        if cap != self.settings.field_cap:
            self.settings.field_cap = cap
            self._say(f"Field cap set to {cap or 'no limit'}.")
            self._fill_from_waitlist()

    def _join_running(self, team: Team) -> None:
        """Late entry: pay in, get chips, and wait on the winners list."""
        self._activate(team)
        self._enqueue(team.id, self.winners_list)
        self._say(f"{team.name} entered late with {team.chips} chips.")
        self._advance()

    def _activate(self, team: Team) -> None:
        fee = self.settings.entry_fee_cents
        self.pot_cents += fee
        self.total_collected_cents += fee
        team.status = ACTIVE
        team.starting_chips = self.chips_for_skill(team.combined_skill)
        self._chip_change(team, team.starting_chips, f"Starting chips (combined skill {team.combined_skill})")

    @staticmethod
    def _names(players: list[Player]) -> set[str]:
        return {" ".join(p.name.lower().split()) for p in players}

    def update_roster(self, team_id: str, players: list[Player], name: str | None = None) -> Team:
        """Change partners, phones or skill levels.

        With the same two partners, skill levels can rise after registration
        (up to the top chip bracket). Swapping in a new partner counts as a new
        registration, so the registration cap applies, but the team keeps its
        place (in the field or on the waiting list). Partners and skill levels
        lock once the team has been given its chips.
        """
        team = self.team(team_id)
        self._check_players(players)
        old_skill, new_skill = team.combined_skill, sum(p.skill for p in players)
        swap = self._names(players) != self._names(team.players)
        if team.status not in (REGISTERED, WAITLISTED) and [(p.name, p.skill) for p in players] != [
            (p.name, p.skill) for p in team.players
        ]:
            raise TournamentError("Partners and skill levels are locked once the tournament starts.")
        if swap and new_skill > self.settings.registration_cap:
            raise TournamentError(
                f"A partner swap counts as a new registration, so combined skill can't be over "
                f"{self.settings.registration_cap}. With the new partner it's {new_skill}."
            )
        self.chips_for_skill(new_skill)
        if name is not None and name.strip() and name.strip() != team.name:
            if any(t.name.lower() == name.strip().lower() for t in self.teams.values() if t.id != team_id):
                raise TournamentError(f"A team named '{name.strip()}' is already registered.")
            team.name = name.strip()
        team.players = players
        change = f" Combined skill {old_skill} → {new_skill}." if new_skill != old_skill else ""
        if swap:
            change = f" Partner swap, now {team.partners}." + change
        self._say(f"Roster updated for {team.name}.{change}")
        return team

    def remove_team(self, team_id: str) -> None:
        team = self.team(team_id)
        if team.status == WAITLISTED:
            self.waitlist.remove(team_id)
            del self.teams[team_id]
            self._say(f"Removed {team.name} from the waiting list.")
            return
        if team.status not in (REGISTERED, NO_SHOW):
            raise TournamentError("Teams in the field can only be removed before the tournament starts.")
        del self.teams[team_id]
        self._say(f"Removed {team.name}.")
        self._fill_from_waitlist()

    # ------------------------------------------------------------ event check-in

    @property
    def not_checked_in(self) -> list[Team]:
        return [t for t in self.field_teams if t.status == REGISTERED and not t.checked_in_at]

    def check_in_team(self, team_id: str) -> None:
        """The team has arrived at the venue."""
        team = self.team(team_id)
        if team.status not in (REGISTERED, WAITLISTED):
            raise TournamentError(f"{team.name} can't be checked in ({team.status.replace('_', ' ')}).")
        if team.checked_in_at:
            raise TournamentError(f"{team.name} is already checked in.")
        team.checked_in_at = now()
        self._say(f"{team.name} checked in at the venue.")

    def mark_no_show(self, team_id: str) -> None:
        """Take a team that didn't arrive out of the field; the waiting list moves up."""
        team = self.team(team_id)
        if self.status != SETUP:
            raise TournamentError("No-shows are marked before the tournament starts.")
        if team.status not in (REGISTERED, WAITLISTED):
            raise TournamentError(f"{team.name} can't be marked a no-show.")
        if team.id in self.waitlist:
            self.waitlist.remove(team.id)
        team.status = NO_SHOW
        team.checked_in_at = None
        self._say(f"{team.name} marked as a no-show.")
        self._text_team(team, f"{team.name} was marked as a no-show and removed from {self.settings.name}. "
                              "If this is a mistake, see the tournament desk.")
        self._fill_from_waitlist()

    def no_show_everyone_not_checked_in(self) -> int:
        teams = self.not_checked_in
        for team in teams:
            self.mark_no_show(team.id)
        return len(teams)

    def restore_team(self, team_id: str) -> None:
        """Undo a no-show: back into the field if there's room, otherwise the end of the waiting list.

        After the start, a restored team joins as a late entry (pays in, gets its chips).
        """
        team = self.team(team_id)
        if team.status != NO_SHOW:
            raise TournamentError(f"{team.name} isn't a no-show.")
        if self.status == FINISHED or self.last_call:
            raise TournamentError("Teams can't be added after last call.")
        if self.entries_locked:
            raise TournamentError("Entries are locked: every team has already played a match.")
        if self.field_full:
            team.status = WAITLISTED
            self.waitlist.append(team.id)
            self._say(f"{team.name} restored to the waiting list (#{len(self.waitlist)}); the field is full.")
            return
        team.status = REGISTERED
        team.checked_in_at = now()  # restoring means they're here
        self._say(f"{team.name} restored to the field.")
        if self.status == RUNNING:
            self._join_running(team)

    # ----------------------------------------------------------------- running

    def start(self, rng: random.Random | None = None) -> None:
        if self.status != SETUP:
            raise TournamentError("The tournament has already started.")
        teams = self.field_teams
        if len(teams) < 2:
            raise TournamentError("At least two teams are needed to start.")
        if not self.settings.tables:
            raise TournamentError("Add at least one table before starting.")
        if self.settings.require_checkin and self.not_checked_in:
            raise TournamentError(
                "These teams haven't checked in: " + ", ".join(t.name for t in self.not_checked_in)
                + ". Check them in or mark them as no-shows before starting."
            )
        too_high = [f"{t.name} ({t.combined_skill})" for t in teams if self.chips_preview(t) is None]
        if too_high:
            raise TournamentError(
                f"Combined skill over {self.max_skill}, so these teams need a new partner "
                f"(or must be removed) before the start: {', '.join(too_high)}."
            )

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
        for team_id in order:
            self._enqueue(team_id, self.winners_list)
        self._advance()

    # ------------------------------------------------------------ rolling pairing

    def _enqueue(self, team_id: str, lst: list[str], front: bool = False) -> None:
        for other in (self.winners_list, self.losers_list):
            if team_id in other:
                other.remove(team_id)
        lst.insert(0, team_id) if front else lst.append(team_id)
        self.teams[team_id].queued_at = now()

    @property
    def matches_completed(self) -> int:
        return sum(1 for m in self.matches if m.status == DONE)

    def is_behind(self, team: Team) -> bool:
        """Has this team played `catchup_matches` or more fewer matches than the leader?"""
        active = self.active_teams
        if team.status != ACTIVE or not active:
            return False
        lead = max(t.matches_played for t in active)
        return lead - team.matches_played >= max(1, self.settings.catchup_matches)

    def _urgency(self, team_id: str) -> int:
        team = self.teams[team_id]
        return 0 if team.priority else 1 if self.is_behind(team) else 2

    def queue_order(self, lst: list[str]) -> list[str]:
        """Teams on a list that aren't paired yet, in the order they're considered.

        "Call next" teams first, then teams behind on matches (fewest played
        first), then everyone else in the order they arrived.
        """
        busy = {x for m in self.open_matches() for x in (m.team_a, m.team_b)}
        waiting = [t for t in lst if t in self.teams and self.teams[t].status == ACTIVE and t not in busy]
        return sorted(waiting, key=lambda t: (self._urgency(t), self.teams[t].matches_played if self._urgency(t) < 2 else 0))

    def played_pairs(self) -> set[frozenset]:
        """Pairs of teams that have actually played each other (forfeits don't count)."""
        return {frozenset((m.team_a, m.team_b)) for m in self.matches
                if m.status == DONE and m.team_b and not m.forfeit}

    def opponents_of(self, team_id: str) -> set[str]:
        return {x for pair in self.played_pairs() if team_id in pair for x in pair if x != team_id}

    def table_queue(self) -> list[Match]:
        """Pairs waiting for a table, in the order they'll be called."""
        waiting = [m for m in self.matches if m.status == WAITING]
        return sorted(waiting, key=lambda m: (
            0 if m.priority else 1,
            min(self.teams[m.team_a].matches_played, self.teams[m.team_b].matches_played),
            self.teams[m.team_a].matches_played + self.teams[m.team_b].matches_played,
            m.round,
        ))

    def _find_pair(self, level: int) -> tuple[str, str, str] | None:
        """The next pair to set, or None.

        level 0: same list, new opponents only (a rematch is allowed only if both
                 teams have already played everyone still in). A team behind on
                 matches may also take a new opponent from the other list.
        level 1: also winners-vs-losers with new opponents     } only when nothing is
        level 2: also rematches on the same list               } playing or queued, so
        level 3: also rematches across the lists               } waiting can't help
        """
        played = self.played_pairs()
        active = {t.id for t in self.active_teams}

        def exhausted(x: str) -> bool:  # has played every other team still in
            return all(frozenset((x, y)) in played for y in active if y != x)

        def ok(a: str, b: str, allow_rematch: bool) -> bool:
            return (frozenset((a, b)) not in played or allow_rematch
                    or (exhausted(a) and exhausted(b)))

        def same_list(lst: list[str], allow_rematch: bool):
            # Each team, in list order, takes the highest team above it that it may play.
            for j in range(1, len(lst)):
                for i in range(j):
                    if ok(lst[i], lst[j], allow_rematch):
                        return lst[i], lst[j]
            return None

        def cross(a_list: list[str], b_list: list[str], allow_rematch: bool, only_urgent: bool):
            for a in a_list:
                if only_urgent and self._urgency(a) == 2:
                    continue
                for b in b_list:
                    if ok(a, b, allow_rematch):
                        return a, b
            return None

        w, l = self.queue_order(self.winners_list), self.queue_order(self.losers_list)
        choices = []
        for lst, label in ((w, "winners"), (l, "losers")):
            pair = same_list(lst, allow_rematch=level >= 2)
            if pair:
                choices.append((*pair, label))
        if not choices:
            urgent_only = level == 0
            if level == 0 or level in (1, 3):
                for a_list, b_list in ((w, l), (l, w)):
                    pair = cross(a_list, b_list, allow_rematch=level >= 3, only_urgent=urgent_only)
                    if pair:
                        a, b = pair
                        choices.append((a, b, "cross") if a in w else (b, a, "cross"))
        if not choices and level == 0:
            # Director override ("Call next"): pair the team now, with a new opponent
            # from either list if one is waiting, otherwise a rematch.
            for a in [x for x in w + l if self.teams[x].priority]:
                others = [x for x in w + l if x != a]
                fresh = [x for x in others if frozenset((a, x)) not in played]
                b = (fresh or others or [None])[0]
                if b:
                    same = (a in w) == (b in w)
                    label = ("winners" if a in w else "losers") if same else "cross"
                    choices.append((a, b, label) if a in w or not same else (b, a, label))
                    break
        if not choices:
            return None

        def key(choice):
            a, b, _ = choice
            return (min(self._urgency(a), self._urgency(b)),
                    min(self.teams[a].queued_at or "", self.teams[b].queued_at or ""))

        return min(choices, key=key)

    def _form_pairs(self) -> None:
        """Pair every team that can be paired and add the pairs to the table queue."""
        while True:
            # Rematches and cross matches are a last resort: only when nothing that
            # is playing or queued could bring a new opponent onto the lists.
            nothing_coming = not self.open_matches()
            pair = None
            for level in (0, 1, 2, 3) if nothing_coming else (0,):
                pair = self._find_pair(level)
                if pair:
                    break
            if not pair:
                return
            self._queue_pair(*pair)

    def _queue_pair(self, a_id: str, b_id: str, bracket: str) -> Match:
        a, b = self.teams[a_id], self.teams[b_id]
        if a.matches_played == 0 and b.matches_played == 0:
            bracket = "start"
        rematch = frozenset((a_id, b_id)) in self.played_pairs()
        for team in (a, b):
            for lst in (self.winners_list, self.losers_list):
                if team.id in lst:
                    lst.remove(team.id)
        self.round += 1
        m = Match(id=uuid.uuid4().hex[:8], round=self.round, team_a=a_id, team_b=b_id, bracket=bracket,
                  priority=a.priority or b.priority, rematch=rematch)
        a.priority = b.priority = False
        self.matches.append(m)
        note = " (rematch: the only option left)" if rematch else ""
        self._say(f"Match #{m.round} set: {a.name} vs {b.name} ({bracket}){note}. Waiting for a table.")
        return m

    def _call_waiting(self) -> None:
        """Send queued pairs to free tables."""
        free = self.free_tables()
        for m in self.table_queue():
            if not free:
                break
            m.table = free.pop(0)
            m.status = CALLED
            m.called_at = now()
            a, b = self.teams[m.team_a], self.teams[m.team_b]
            self._say(f"Match #{m.round}: called {a.name} vs {b.name} to table {m.table}.")

            def side(t: Team) -> str:
                return f"{t.name} with combined skill {t.combined_skill} currently holding {t.chips} chip{'s' if t.chips != 1 else ''}"

            body = f"New match: Table {m.table}: {side(a)} VS {side(b)}. Good Luck!"
            for team in (a, b):
                self._text_team(team, body, prefix=False)

    def call_next(self, team_id: str) -> None:
        """Director override: this team's pair (or the team, if not paired yet) goes first."""
        team = self.team(team_id)
        if self.status != RUNNING or team.status != ACTIVE:
            raise TournamentError(f"{team.name} isn't waiting to play.")
        m = self.current_match_for(team_id)
        if m is not None and m.status == CALLED:
            raise TournamentError(f"{team.name} is already on a table.")
        if m is not None:
            m.priority = True
        else:
            if team.id not in self.winners_list and team.id not in self.losers_list:
                self._enqueue(team.id, self.winners_list)
            team.priority = True
        self._say(f"Director moved {team.name} to the front of the line.")
        self._advance()

    def score(self, match_id: str, side: str, delta: int, by: str | None = None) -> Match:
        m = self.match(match_id)
        if m.status != CALLED:
            raise TournamentError("Only a match at a table can be scored.")
        if by:  # entered by a player from their team page
            name = self.name_of(m.team_a if side == "a" else m.team_b)
            self._say(f"{by} entered {'+1' if delta > 0 else '-1'} for {name} on table {m.table}.")
        stamp = now()
        for s_ in ("a", "b"):  # a scored game means both teams are at the table
            if not getattr(m, "arrived_" + s_):
                setattr(m, "arrived_" + s_, stamp)
        m.start_at = m.start_at or stamp
        attr = "score_a" if side == "a" else "score_b"
        value = max(0, min(self.settings.race_to, getattr(m, attr) + delta))
        setattr(m, attr, value)
        if value >= self.settings.race_to:
            self.record_result(match_id, m.team_a if side == "a" else m.team_b)
        return m

    def record_result(self, match_id: str, winner_id: str, forfeit: bool = False) -> Match:
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
        how = " by forfeit" if forfeit else ""
        if forfeit:
            m.forfeit = "a" if loser_id == m.team_a else "b"

        self._chip_change(loser, -1, f"Lost match #{m.round} to {winner.name}{how}")
        self._say(
            f"{winner.name} beat {loser.name}{how} on table {m.table}. {loser.name} gives a chip back "
            f"({loser.chips} left). Chip value now {money(self.chip_value_cents)}."
        )
        if forfeit:
            self._text_team(loser, f"{loser.name}: you didn't check in at table {m.table} in time and forfeited your match.")
        self._after_loss(loser, m)
        if not self._played_last_match(winner):
            self._enqueue(winner_id, self.winners_list)
            self._text_team(winner, f"{winner.name}: {'your opponent forfeited, ' if forfeit else 'nice win! '}"
                                    f"You keep your {winner.chips} chip(s).")

        self._advance()
        return m

    def _after_loss(self, loser: Team, m: Match) -> None:
        if loser.chips <= 0:
            loser.status = ELIMINATED
            self._say(f"{loser.name} is out of chips and eliminated.")
            self._text_team(loser, f"{loser.name}: you're out of chips and eliminated. Thanks for playing!")
        elif not self._played_last_match(loser):
            self._enqueue(loser.id, self.losers_list)
            self._text_team(loser, f"{loser.name}: you lost that match and have {loser.chips} chip(s) left.")

    def _double_forfeit(self, m: Match) -> None:
        """Neither team showed: both give a chip back and go to the losers list."""
        teams = [self.teams[m.team_a], self.teams[m.team_b]]
        others = [x for x in self.active_teams if x.id not in (m.team_a, m.team_b)]
        if not others and all(team.chips <= 1 for team in teams):
            # Forfeiting would leave nobody holding chips and the pot unpaid,
            # so the last two teams are cashed in at the current value instead.
            m.status = CANCELLED
            m.finished_at = now()
            self._say(f"Neither {teams[0].name} nor {teams[1].name} checked in for the final match; both are cashed in.")
            for team in sorted(teams, key=lambda x: x.chips):
                payout = self._cash_out(team, "No-show in the final match")
                self._text_team(team, f"{team.name}: you didn't check in for your match and have been cashed in for "
                                      f"{money(payout)}. See the desk to collect.")
            self._check_finished()
            return
        m.status = DONE
        m.forfeit = "both"
        m.finished_at = now()
        for team in teams:
            team.losses += 1
            self._chip_change(team, -1, f"Forfeited match #{m.round} (no-show)")
        self._say(f"Neither {teams[0].name} nor {teams[1].name} checked in at table {m.table}; both forfeit a chip.")
        for team in teams:
            self._text_team(team, f"{team.name}: you didn't check in at table {m.table} in time and forfeited your match.")
            self._after_loss(team, m)
        self._advance()

    # ----------------------------------------------------------------- check-in

    def checkin_deadline(self, m: Match) -> datetime:
        return datetime.fromisoformat(m.called_at) + timedelta(minutes=self.settings.checkin_minutes)

    def forfeit_deadline(self, m: Match) -> datetime:
        return self.checkin_deadline(m) + timedelta(minutes=self.settings.grace_minutes)

    def check_in(self, match_id: str, side: str, at: datetime | None = None) -> Match:
        """A team has arrived at its table."""
        m = self.match(match_id)
        if m.status != CALLED:
            raise TournamentError("That match is not at a table.")
        attr = "arrived_a" if side == "a" else "arrived_b"
        team = self.teams[m.team_a if side == "a" else m.team_b]
        if getattr(m, attr):
            raise TournamentError(f"{team.name} is already checked in.")
        at = at or datetime.now()
        setattr(m, attr, at.isoformat(timespec="seconds"))
        late = " (during grace)" if getattr(m, "grace_" + side) else ""
        self._say(f"{team.name} checked in at table {m.table}{late}.")
        if m.arrived_a and m.arrived_b and not m.start_at:
            start = max(datetime.fromisoformat(m.arrived_a), datetime.fromisoformat(m.arrived_b))
            start += timedelta(minutes=self.settings.warmup_minutes)
            m.start_at = start.isoformat(timespec="seconds")
            when = start.strftime("%I:%M %p").lstrip("0")
            self._say(f"Table {m.table}: both teams are here; the match starts at {when}.")
            for tid in (m.team_a, m.team_b):
                self._text_team(self.teams[tid], f"Both teams are at table {m.table}. Your match starts at {when}.")
        return m

    def pause_overdue_forfeits(self, at: datetime | None = None) -> int:
        """Stop the clock re-forfeiting matches whose deadline has already passed.

        Used after Undo: otherwise an undone forfeit would be applied again
        within seconds. The director then taps Arrived or picks a winner.
        """
        at = at or datetime.now()
        paused = 0
        for m in self.matches:
            if (m.status == CALLED and m.called_at and not m.clock_paused
                    and not (m.arrived_a and m.arrived_b) and at >= self.forfeit_deadline(m)):
                m.clock_paused = True
                paused += 1
        return paused

    def check_clock(self, at: datetime | None = None) -> int:
        """Start grace periods and record forfeits that are due. Returns how many things changed."""
        if self.status != RUNNING:
            return 0
        at = at or datetime.now()
        changes = 0
        for m in [m for m in self.matches if m.status == CALLED]:
            if m.status != CALLED or not m.called_at or m.score_a or m.score_b or m.clock_paused:
                continue
            missing = [side for side in ("a", "b") if not getattr(m, "arrived_" + side)]
            if not missing:
                continue
            if at >= self.forfeit_deadline(m):
                if len(missing) == 2:
                    self._double_forfeit(m)
                else:
                    self.record_result(m.id, m.team_b if missing[0] == "a" else m.team_a, forfeit=True)
                changes += 1
                continue
            if at >= self.checkin_deadline(m):
                for side in missing:
                    if getattr(m, "grace_" + side):
                        continue
                    setattr(m, "grace_" + side, self.checkin_deadline(m).isoformat(timespec="seconds"))
                    team = self.teams[m.team_a if side == "a" else m.team_b]
                    when = self.forfeit_deadline(m).strftime("%I:%M %p").lstrip("0")
                    self._say(f"{team.name} hasn't checked in at table {m.table}; grace period until {when}.")
                    self._text_team(
                        team,
                        f"{team.name}: you were called to TABLE {m.table} {self.settings.checkin_minutes} min ago "
                        f"and haven't checked in. Get there by {when} or you forfeit and lose a chip.",
                    )
                    changes += 1
        return changes

    def _advance(self) -> None:
        """Pair whoever can be paired, then send queued pairs to free tables."""
        if self.status != RUNNING or self._check_finished():
            return
        self._maybe_lock_entries()
        # Safety net: every active team is either on a table or on a list.
        busy = {x for m in self.open_matches() for x in (m.team_a, m.team_b)}
        for team in self.active_teams:
            if team.id not in busy and team.id not in self.winners_list and team.id not in self.losers_list:
                self._enqueue(team.id, self.winners_list)
        self._form_pairs()
        self._call_waiting()

    @property
    def teams_yet_to_play(self) -> list[Team]:
        return [t for t in self.active_teams if t.matches_played == 0]

    def _maybe_lock_entries(self) -> None:
        """Once every team still in has completed a match, close entries and lock the money."""
        if self.entries_locked or self.status != RUNNING or not self.active_teams or self.teams_yet_to_play:
            return
        self.entries_locked = True
        self._say("Every team has completed a match: registration and money settings are now locked.")
        for team_id in self.waitlist:
            self._text_team(self.teams[team_id], f"{self.teams[team_id].name}: the field for "
                                                 f"{self.settings.name} is now set, so we couldn't fit you in tonight. Sorry!")

    def set_money(self, entry_fee_cents: int, added_money_cents: int,
                  chip_brackets: list[list[int]], registration_cap: int) -> None:
        """Change the entry fee, added money, chip brackets and registration cap.

        Allowed until entries lock. After the start, the pot and every team's
        chips are adjusted to match, and each adjustment is logged.
        """
        if self.entries_locked:
            raise TournamentError("Money settings are locked: every team has already played a match.")
        s = self.settings
        if entry_fee_cents < 0 or added_money_cents < 0:
            raise TournamentError("Amounts can't be negative.")
        old_brackets, s.chip_brackets = s.chip_brackets, chip_brackets
        if registration_cap > self.max_skill:
            raise TournamentError("The registration cap can't be higher than the top chip bracket.")
        if self.status == SETUP:
            s.entry_fee_cents, s.added_money_cents, s.registration_cap = entry_fee_cents, added_money_cents, registration_cap
            return

        # Running: adjust what's already been handed out. (On any error nothing is saved.)
        entered = [t for t in self.teams.values() if t.starting_chips]
        money_changed = entry_fee_cents != s.entry_fee_cents or added_money_cents != s.added_money_cents
        if money_changed and any(t.status == CASHED_OUT for t in entered):
            raise TournamentError("The entry fee and added money can't change after a team has cashed in.")
        chip_changes = []
        for team in entered:
            new = self.chips_for_skill(team.combined_skill)
            change = new - team.starting_chips
            if not change:
                continue
            if team.status != ACTIVE:
                raise TournamentError(f"{team.name} is already out, so the chip brackets can't change its chips.")
            if team.chips + change < 1:
                raise TournamentError(f"{team.name} would have no chips left with those chip brackets.")
            chip_changes.append((team, new, change))
        for team, new, change in chip_changes:
            team.starting_chips = new
            self._chip_change(team, change, f"Chip brackets changed: starting chips now {new}")
        if chip_brackets != old_brackets:
            self._say(f"Chip brackets changed; {len(chip_changes)} team(s) adjusted.")
        fee_change = entry_fee_cents - s.entry_fee_cents
        if fee_change:
            self.pot_cents += fee_change * len(entered)
            self.total_collected_cents += fee_change * len(entered)
            self._say(f"Entry fee changed to {money(entry_fee_cents)}: "
                      f"{'collect' if fee_change > 0 else 'refund'} {money(abs(fee_change))} per team that has paid.")
        added_change = added_money_cents - s.added_money_cents
        if added_change:
            self.pot_cents += added_change
            self._say(f"Added money changed to {money(added_money_cents)}.")
        s.entry_fee_cents, s.added_money_cents, s.registration_cap = entry_fee_cents, added_money_cents, registration_cap

    def match_minutes_played(self, m: Match, at: datetime | None = None) -> float | None:
        """Minutes since the match started (None before it starts)."""
        if m.status != CALLED or not m.start_at:
            return None
        elapsed = ((at or datetime.now()) - datetime.fromisoformat(m.start_at)).total_seconds() / 60
        return elapsed if elapsed >= 0 else None

    def long_matches(self, at: datetime | None = None) -> list[Match]:
        """Matches on a table at least `long_match_minutes` past their start."""
        return [m for m in self.open_matches()
                if (self.match_minutes_played(m, at) or 0) >= self.settings.long_match_minutes]

    def _check_finished(self) -> bool:
        active = self.active_teams
        if self.status != RUNNING or len(active) > 1:
            return False
        if len(active) == 1:
            champ = active[0]
            if self.last_call:
                self._cash_out(champ, "Last call (no opponents left)")
                self._text_team(champ, f"{champ.name}: no opponents left after last call. "
                                       f"You've been cashed in for {money(champ.payout_cents)}. See the desk to collect.")
            else:
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
                # Not the opponent's fault: back to the front of the list it came from.
                last = [x for x in self.matches if x.status == DONE and other in (x.team_a, x.team_b)]
                lost_last = bool(last) and (last[-1].loser == other or last[-1].forfeit == "both")
                self._enqueue(other, self.losers_list if lost_last else self.winners_list, front=True)
                self._text_team(
                    self.teams[other],
                    f"{self.teams[other].name}: your opponent left the tournament. You're first in line for the next table.",
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

    def _played_last_match(self, team: Team) -> bool:
        """After last call, count down a team's matches and cash it in after its last."""
        if team.matches_left is None:
            return False
        team.matches_left -= 1
        if team.matches_left > 0:
            self._text_team(team, f"LAST CALL: {team.name}, your next match is your last.")
            return False
        payout = self._cash_out(team, "Last call")
        self._text_team(
            team,
            f"{team.name}: that was your last match. You've been cashed in for {money(payout)} "
            f"({money(self.chip_value_cents)} a chip). See the desk to collect.",
        )
        return True

    def call_last_call(self) -> None:
        """Every team's next match is its last; teams are cashed in after it.

        A match already on a table finishes first, so those teams play one more.
        """
        if self.status != RUNNING:
            raise TournamentError("The tournament is not running.")
        if self.last_call:
            raise TournamentError("Last call has already been called.")
        self.last_call = True
        self.cash_in_open = True
        self._say("LAST CALL. Each team's next match is its last; teams are cashed in when it ends.")
        for team in self.active_teams:
            m = self.current_match_for(team.id)
            if m is not None and m.status == CALLED:
                team.matches_left = 2
                self._text_team(
                    team,
                    f"LAST CALL: {team.name}, finish your match on table {m.table}. "
                    "Your next match after it is your last, then you're cashed in automatically.",
                )
            else:
                team.matches_left = 1
                self._text_team(
                    team,
                    f"LAST CALL: {team.name}, your next match is your last. When it's over you're cashed in "
                    f"automatically. Chips are worth {money(self.chip_value_cents)} each right now.",
                )

    def cash_out_everyone(self) -> None:
        """End the tournament now: stop all matches and cash in every team."""
        if self.status != RUNNING:
            raise TournamentError("The tournament is not running.")
        self.last_call = True
        self.cash_in_open = True
        for m in self.open_matches():
            m.status = CANCELLED
            m.finished_at = now()
        self.winners_list, self.losers_list = [], []
        self._say(f"Tournament ended early. All teams cash in at {money(self.chip_value_cents)} per chip.")
        for team in sorted(self.active_teams, key=lambda t: t.chips):
            # The last team takes whatever is left, so rounding never loses a cent.
            payout = self._cash_out(team, "Tournament ended")
            self._text_team(team, f"The tournament has ended. {team.name} cashed in {money(payout)}. See the desk to collect.")
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
