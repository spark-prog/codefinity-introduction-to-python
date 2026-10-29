"""Rules for bracket tournaments: single, double and modified single elimination,
and round robin groups with an optional playoff.

Like engine.py, this module has no web or storage code, so it can be tested on
its own. Money is kept in whole cents.

How brackets are built
----------------------
* Every match takes each side from a *source*: a fixed entry (or a bye), the
  winner or loser of an earlier match, or a finishing place in a round robin
  group. When a source is known the side fills in; when both sides are known
  the match is READY. A match with a bye on one side finishes by itself.
* Seeds follow the usual bracket order (1 v 16, 8 v 9, ...), so byes go to the
  top seeds. Seeding is a random draw at the start, or the order the director
  set on the Entries list.
* Single elimination: one loss and you're out.
* Double elimination: a first loss drops you to the losers side; a second loss
  puts you out. Losers drop in alternating order to avoid quick rematches.
  The losers-side champion meets the winners-side champion in the grand final;
  with "true double" on, a second (reset) final is played if the losers-side
  champion wins the first.
* Modified single elimination: double elimination until the final 4, 8 or 16
  are left (half from each side), then single elimination. This is the usual
  pool-room "lose once and you're on the one-loss side until the final 8".
* Round robin: groups (snake-seeded), everyone in a group plays everyone once.
  Standings go by wins, then game difference, then games won. Optionally the
  top N of each group advance to a single-elimination playoff, with group
  winners seeded first.

Running it
----------
* Ready matches are sent to free tables automatically (first ready, first
  called; the director can push a match to the front or pick a table). An
  entry is never called to two tables at once. A table can be taken out of
  service: its match moves to another free table, or back to the front of the
  queue with its score kept.
* Places are final as soon as no one still playing can finish at or below
  them; payouts (percent of the pot by place, ties split evenly) are texted to
  the entries as soon as they are final.

Skill groups and rack races (singles)
-------------------------------------
* With skill groups set (e.g. SL 1-3, 4, 5, 6, 7-9), each player goes in the
  group for their skill level (the higher of their 8-Ball and 9-Ball levels)
  and each group plays its own bracket with its own champion; the groups share
  the tables. At the draw, a group too small for the format joins whichever
  neighboring group has fewer players.
* With the rack race chart, each side of a match gets its own race from the
  two players' skill levels (see races.py).
"""

from __future__ import annotations

import random
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .engine import MAX_PLAYER_SKILL, Message, Player, TournamentError, money, now
from .races import group_for, group_label, rack_race

BYE = "bye"

SINGLE, DOUBLE, MODIFIED, ROUND_ROBIN = "single", "double", "modified", "round_robin"
FORMATS = [
    (DOUBLE, "Double elimination"),
    (SINGLE, "Single elimination"),
    (MODIFIED, "Modified single elimination"),
    (ROUND_ROBIN, "Round robin"),
]
FORMAT_NAMES = dict(FORMATS)
FINAL_CUTS = (4, 8, 16, 32)
STRAIGHT, RACK_CHART = "straight", "rack_chart"  # race methods

# Entry statuses
REGISTERED = "registered"
WAITLISTED = "waitlisted"
NO_SHOW = "no_show"
ACTIVE = "active"
ELIMINATED = "eliminated"
CHAMPION = "champion"

# Match statuses
PENDING = "pending"  # waiting for one or both sides
READY = "ready"  # both sides known, waiting for a table
CALLED = "called"  # on a table
DONE = "done"
VOID = "void"  # a reset final that wasn't needed

# Tournament statuses (same words as the chip engine)
SETUP = "setup"
RUNNING = "running"
FINISHED = "finished"

STAGE_NAMES = {"W": "Winners side", "L": "Losers side", "F": "Finals", "P": "Playoff", "G": "Groups"}


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def place_text(place: tuple[int, int] | None) -> str:
    if not place:
        return ""
    lo, hi = place
    return ordinal(lo) if lo == hi else f"{ordinal(lo)}–{ordinal(hi)}"


def bracket_order(size: int) -> list[int]:
    """Seed numbers in bracket position order: 8 -> [1, 8, 4, 5, 2, 7, 3, 6]."""
    order = [1]
    while len(order) < size:
        total = len(order) * 2 + 1
        order = [x for s in order for x in (s, total - s)]
    return order


def next_power_of_two(n: int) -> int:
    size = 2
    while size < n:
        size *= 2
    return size


def round_name(rounds_from_end: int) -> str:
    """0 = the final, 1 = semifinals, ..."""
    names = ["Final", "Semifinals", "Quarterfinals"]
    if rounds_from_end < len(names):
        return names[rounds_from_end]
    return f"Round of {2 ** (rounds_from_end + 1)}"


@dataclass
class Entry:
    id: str
    name: str
    players: list[Player] = field(default_factory=list)
    pin: str = ""
    status: str = REGISTERED
    seed: int | None = None
    checked_in_at: str | None = None
    registered_at: str = field(default_factory=now)
    withdrawn: bool = False
    out_level: int | None = None  # how far it got before it was knocked out
    out_match: str | None = None
    payout_cents: int = 0
    payout_texted: bool = False
    payout_texted_cents: int = 0  # the amount in that text, so a later correction can be texted
    division: str = ""  # skill group label ("SL 4"); "" when the event has one bracket

    @property
    def skill(self) -> int:
        """The entry's skill level for groups and races: its highest player's (0 if none given)."""
        return max((p.skill for p in self.players), default=0)

    @property
    def phones(self) -> list[str]:
        seen = []
        for number in [p.phone for p in self.players if p.sms_consent]:
            if number and number not in seen:
                seen.append(number)
        return seen

    @property
    def awaiting_texts(self) -> list[Player]:
        return [p for p in self.players if p.phone and not p.sms_consent]

    @property
    def partners(self) -> str:
        return " & ".join(p.name for p in self.players)


@dataclass
class BMatch:
    id: str
    num: int  # match number shown everywhere ("#12")
    stage: str  # "W", "L", "F", "P" or "G"
    round: int
    index: int
    level: int = 0  # for places: a loser knocked out at a higher level finishes higher
    knockout: bool = False  # the loser can be knocked out here
    reset: bool = False  # the second grand final, played only if the losers-side champion wins the first
    group: str = ""
    division: str = ""  # the skill group this match belongs to
    src_a: dict | None = None
    src_b: dict | None = None
    a: str | None = None
    b: str | None = None
    status: str = PENDING
    race_to: int = 0
    race_a: int = 0  # with a rack race chart: each side's own race (0 = both race to race_to)
    race_b: int = 0
    table: str | None = None
    score_a: int = 0
    score_b: int = 0
    winner: str | None = None
    loser: str | None = None
    forfeit: bool = False
    priority: bool = False
    ready_at: str | None = None
    called_at: str | None = None
    finished_at: str | None = None

    @property
    def is_bye(self) -> bool:
        return BYE in (self.a, self.b)


@dataclass
class BracketSettings:
    name: str = "Bracket Tournament"
    date: str = ""
    start_time: str = ""
    venue: str = ""
    address: str = ""
    details: str = ""
    checkin_by: str = ""
    format: str = DOUBLE
    team_size: int = 1  # 1 = singles, 2 = doubles / scotch doubles
    entry_fee_cents: int = 2000
    added_money_cents: int = 0
    race_winners: int = 5
    race_losers: int = 4
    race_finals: int = 5
    tables: list[str] = field(default_factory=lambda: ["1", "2", "3", "4"])
    tables_down: list[str] = field(default_factory=list)
    auto_call: bool = True
    field_cap: int = 0
    seeding: str = "random"  # "random" draw at the start, or "manual" (the order on the Entries list)
    final_cut: int = 8  # modified single elimination: single elimination from this many
    true_double: bool = True  # a reset grand final if the losers-side champion wins the first
    groups: int = 1  # round robin groups
    advance: int = 0  # per group into a playoff (0 = no playoff)
    payouts: list[int] = field(default_factory=lambda: [50, 30, 20])  # percent of the pot by place
    require_checkin: bool = False
    sms_name: str = ""
    theme: str = "electric"
    contact_phone: str = ""
    rule_set: str = ""  # id of the league rule set this event plays by
    event_rules: str = ""  # APA-approved rules for this tournament only
    registration_closed: bool = False  # director closed online registration (the director can still add entries)
    skill_groups: list[list[int]] = field(default_factory=list)  # [[1, 3], [4, 4], ...]; [] = one bracket
    race_method: str = STRAIGHT  # STRAIGHT: the race_* numbers; RACK_CHART: each side from the rack race chart
    match_minutes: int = 30  # amber after this
    long_match_minutes: int = 45  # red after this


@dataclass
class BracketTournament:
    kind = "bracket"

    settings: BracketSettings = field(default_factory=BracketSettings)
    entries: dict[str, Entry] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)  # entry ids in seed / sign-up order
    waitlist: list[str] = field(default_factory=list)
    groups: dict[str, list[str]] = field(default_factory=dict)
    matches: list[BMatch] = field(default_factory=list)
    status: str = SETUP
    site_url: str = ""
    pot_cents: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    outbox: list[Message] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ helpers

    def entry(self, entry_id: str) -> Entry:
        try:
            return self.entries[entry_id]
        except KeyError:
            raise TournamentError("Unknown entry.") from None

    def match(self, match_id: str) -> BMatch:
        for m in self.matches:
            if m.id == match_id:
                return m
        raise TournamentError("Unknown match.")

    def name_of(self, entry_id: str | None) -> str:
        if entry_id == BYE:
            return "BYE"
        if entry_id is None:
            return "TBD"
        return self.entries[entry_id].name if entry_id in self.entries else "?"

    @property
    def format_name(self) -> str:
        s = self.settings
        name = FORMAT_NAMES.get(s.format, s.format)
        if s.format == MODIFIED:
            name += f" (final {s.final_cut})"
        if s.format == ROUND_ROBIN and s.advance:
            name += f" + playoff (top {s.advance} per group)"
        return name

    @property
    def field_entries(self) -> list[Entry]:
        """Entries with a spot, in seed order."""
        return [self.entries[i] for i in self.order
                if i in self.entries and self.entries[i].status not in (WAITLISTED, NO_SHOW)]

    @property
    def listed_entries(self) -> list[Entry]:
        """Everyone for the director's list: the field in seed order, then the waiting list, then no-shows."""
        return (self.field_entries + [self.entries[i] for i in self.waitlist if i in self.entries]
                + [e for e in self.entries.values() if e.status == NO_SHOW])

    @property
    def field_full(self) -> bool:
        return bool(self.settings.field_cap) and len(self.field_entries) >= self.settings.field_cap

    def waitlist_position(self, entry: Entry) -> int | None:
        return self.waitlist.index(entry.id) + 1 if entry.id in self.waitlist else None

    @property
    def not_checked_in(self) -> list[Entry]:
        return [e for e in self.field_entries if not e.checked_in_at]

    def busy_tables(self) -> set[str]:
        return {m.table for m in self.matches if m.status == CALLED}

    def free_tables(self) -> list[str]:
        busy, down = self.busy_tables(), set(self.settings.tables_down)
        return [t for t in self.settings.tables if t not in busy and t not in down]

    def in_play(self) -> list[BMatch]:
        return sorted((m for m in self.matches if m.status == CALLED),
                      key=lambda m: (self._table_index(m.table), m.num))

    def _table_index(self, table: str | None) -> int:
        try:
            return self.settings.tables.index(table)
        except ValueError:
            return 999

    def ready_queue(self) -> list[BMatch]:
        """Matches waiting for a table, in the order they'll be called."""
        return sorted((m for m in self.matches if m.status == READY),
                      key=lambda m: (not m.priority, m.ready_at or "", m.num))

    def recent_results(self, limit: int = 12) -> list[BMatch]:
        done = [m for m in self.matches if m.status == DONE and not m.is_bye]
        return sorted(done, key=lambda m: m.finished_at or "", reverse=True)[:limit]

    def current_match_for(self, entry_id: str) -> BMatch | None:
        for status in (CALLED, READY):
            for m in sorted(self.matches, key=lambda m: m.num):
                if m.status == status and entry_id in (m.a, m.b):
                    return m
        return None

    def matches_for(self, entry_id: str) -> list[BMatch]:
        return [m for m in sorted(self.matches, key=lambda m: m.finished_at or "")
                if m.status == DONE and entry_id in (m.a, m.b) and not m.is_bye]

    def _say(self, text: str) -> None:
        self.log.append(f"{now()}  {text}")

    def _text(self, entry: Entry, body: str) -> None:
        if self.settings.sms_name:
            body = f"{self.settings.sms_name}: {body}"
        for number in entry.phones:
            self.outbox.append(Message(to=number, body=body, team_id=entry.id))

    def entry_link(self, entry: Entry) -> str:
        return f"{self.site_url}/entry/{entry.id}" if self.site_url else ""

    def _questions(self) -> str:
        phone = self.settings.contact_phone
        return f" Questions? Call/text {phone}" if phone else ""

    def _welcome(self, entry: Entry, opening: str) -> str:
        link = self.entry_link(entry)
        return (f"{opening}\nPIN: {entry.pin}"  # the PIN and link each get their own line
                + (f"\nYour page: {link}" if link else "")
                + "\nYou'll get tournament alerts: registration, table calls and results."
                  " Msg frequency varies. Msg & data rates may apply. Reply HELP for help, STOP to opt out."
                + self._questions())

    def opt_in(self, entry_id: str, index: int) -> Player:
        entry = self.entry(entry_id)
        if not 0 <= index < len(entry.players):
            raise TournamentError("That player isn't on this entry.")
        player = entry.players[index]
        if not player.phone:
            raise TournamentError(f"There's no cell number on file for {player.name}.")
        if not player.sms_consent:
            player.sms_consent, player.sms_consent_at = True, now()
            self._say(f"{player.name} ({entry.name}) opted in to text alerts.")
            body = self._welcome(entry, f"{player.name}, you're signed up for {entry.name}'s alerts for {self.settings.name}.")
            if self.settings.sms_name:
                body = f"{self.settings.sms_name}: {body}"
            self.outbox.append(Message(to=player.phone, body=body, team_id=entry.id))
        return player

    def broadcast(self, body: str) -> int:
        before = len(self.outbox)
        for entry in self.entries.values():
            if entry.status not in (NO_SHOW,):
                self._text(entry, body)
        return len(self.outbox) - before

    def text_pins(self) -> tuple[int, int]:
        """Resend each entry its own PIN and page link (opted-in players only).

        Returns (texts queued, entries with nobody signed up for texts)."""
        before, unreachable = len(self.outbox), 0
        for entry in self.entries.values():
            if entry.status not in (REGISTERED, WAITLISTED, ACTIVE):
                continue
            if not entry.phones:
                unreachable += 1
                continue
            link = self.entry_link(entry)
            self._text(entry, f"{entry.name}, here's your info for {self.settings.name}.\nPIN: {entry.pin}"
                       + (f"\nYour page: {link}" if link else "") + "\nReply STOP to opt out.")
        self._say(f"Resent PINs: {len(self.outbox) - before} text(s).")
        return len(self.outbox) - before, unreachable

    # ------------------------------------------------------------ registration

    def _check_players(self, players: list[Player]) -> None:
        size = self.settings.team_size
        if len(players) != size or any(not p.name.strip() for p in players):
            raise TournamentError("Enter a name for the player." if size == 1 else f"Enter all {size} player names.")
        for p in players:
            if p.skill_8 and not 2 <= p.skill_8 <= 7:
                raise TournamentError("8-Ball skill levels go from 2 to 7.")
            if p.skill_9 and not 1 <= p.skill_9 <= 9:
                raise TournamentError("9-Ball skill levels go from 1 to 9.")
            if p.skill and not 1 <= p.skill <= MAX_PLAYER_SKILL:
                raise TournamentError(f"Skill levels go from 1 to {MAX_PLAYER_SKILL}.")
        groups = self.settings.skill_groups
        if groups and size == 1 and players[0].skill and not group_for(groups, players[0].skill):
            raise TournamentError(f"There's no skill group for SL {players[0].skill} in this event.")

    @staticmethod
    def _clean(p: Player) -> Player:
        """A copy of a player from a form, with its skill set from the 8-Ball and 9-Ball levels if given."""
        skill = max(p.skill_8, p.skill_9) or p.skill
        return Player(name=p.name.strip(), phone=p.phone.strip(), skill=skill, skill_8=p.skill_8, skill_9=p.skill_9,
                      sms_consent=p.sms_consent, sms_consent_at=p.sms_consent_at)

    def preview_division(self, entry: Entry) -> str:
        """The skill group an entry would play in, before any small groups merge at the draw."""
        g = group_for(self.settings.skill_groups, entry.skill) if self.settings.skill_groups else None
        return group_label(*g) if g else ""

    def _unique_name(self, name: str, skip: str | None = None) -> None:
        key = " ".join(name.lower().split())
        for e in self.entries.values():
            if e.id != skip and e.status != NO_SHOW and " ".join(e.name.lower().split()) == key:
                raise TournamentError(f"{name} is already registered.")

    def _new_pin(self) -> str:
        used = {e.pin for e in self.entries.values()}
        while True:
            pin = f"{secrets.randbelow(10000):04d}"
            if pin not in used:
                return pin

    def register_entry(self, name: str, players: list[Player], by_director: bool = False) -> Entry:
        if self.status != SETUP:
            raise TournamentError("Registration is closed: the bracket is already drawn.")
        players = [self._clean(p) for p in players]
        self._check_players(players)
        name = (name or "").strip() or " & ".join(p.name for p in players)
        self._unique_name(name)
        entry = Entry(id=uuid.uuid4().hex[:8], name=name, players=players, pin=self._new_pin())
        entry.division = self.preview_division(entry)
        self.entries[entry.id] = entry
        self.order.append(entry.id)
        if self.field_full and not by_director:
            entry.status = WAITLISTED
            self.waitlist.append(entry.id)
            spot = f"You're #{len(self.waitlist)} on the waiting list; we'll text you if a spot opens."
        elif self.field_full:
            entry.status = WAITLISTED
            self.waitlist.append(entry.id)
            spot = f"The field is full, so you're #{len(self.waitlist)} on the waiting list."
        else:
            spot = "You're in."
        if entry.division:
            spot += f" Skill group: {entry.division}."
        self._say(f"Registered {name} ({entry.partners}){' on the waiting list' if entry.status == WAITLISTED else ''}.")
        for p in players:
            if p.sms_consent and p.phone:
                body = self._welcome(entry, f"{name} is registered for {self.settings.name}. {spot}")
                if self.settings.sms_name:
                    body = f"{self.settings.sms_name}: {body}"
                self.outbox.append(Message(to=p.phone, body=body, team_id=entry.id))
        return entry

    def update_entry(self, entry_id: str, name: str | None, players: list[Player]) -> Entry:
        entry = self.entry(entry_id)
        players = [self._clean(p) for p in players]
        for p in players:
            p.sms_consent, p.sms_consent_at = False, None
        self._check_players(players)
        if self.status != SETUP and [p.name.lower() for p in players] != [p.name.lower() for p in entry.players]:
            raise TournamentError("Players can't change once the bracket is drawn (only cell numbers and the entry name).")
        name = (name or "").strip() or entry.name
        self._unique_name(name, skip=entry.id)
        for old, new in zip(entry.players, players):
            if old.phone == new.phone:  # keep text consent for the same number
                new.sms_consent, new.sms_consent_at = old.sms_consent, old.sms_consent_at
        if self.status != SETUP and [p.skill for p in players] != [p.skill for p in entry.players]:
            raise TournamentError("Skill levels are set once the bracket is drawn.")
        entry.name, entry.players = name, players
        if self.status == SETUP:
            entry.division = self.preview_division(entry)
        self._say(f"Updated entry {name}.")
        return entry

    def _fill_from_waitlist(self) -> None:
        while self.waitlist and not self.field_full:
            entry = self.entries[self.waitlist.pop(0)]
            entry.status = REGISTERED
            self._say(f"{entry.name} moved from the waiting list into the field.")
            self._text(entry, f"Good news: a spot opened and {entry.name} is in the field for {self.settings.name}.")

    def set_field_cap(self, cap: int) -> None:
        if cap < 0:
            raise TournamentError("Field cap can't be negative.")
        if cap and cap < len(self.field_entries) and self.status == SETUP:
            raise TournamentError(f"There are already {len(self.field_entries)} entries in the field.")
        self.settings.field_cap = cap
        if self.status == SETUP:
            self._fill_from_waitlist()

    def remove_entry(self, entry_id: str) -> None:
        entry = self.entry(entry_id)
        if self.status != SETUP:
            raise TournamentError("Entries can't be removed after the draw. Use Withdraw instead.")
        del self.entries[entry_id]
        self.order = [i for i in self.order if i != entry_id]
        self.waitlist = [i for i in self.waitlist if i != entry_id]
        self._say(f"Removed {entry.name}.")
        self._fill_from_waitlist()

    def check_in_entry(self, entry_id: str) -> None:
        entry = self.entry(entry_id)
        if entry.status != REGISTERED:
            raise TournamentError(f"{entry.name} isn't in the field.")
        entry.checked_in_at = None if entry.checked_in_at else now()
        self._say(f"{'Checked in' if entry.checked_in_at else 'Undid check-in for'} {entry.name}.")

    def mark_no_show(self, entry_id: str) -> None:
        entry = self.entry(entry_id)
        if self.status != SETUP:
            raise TournamentError("No-shows can only be marked before the draw. Use Withdraw instead.")
        if entry.status == WAITLISTED:
            self.waitlist.remove(entry.id)
        entry.status, entry.checked_in_at = NO_SHOW, None
        self._say(f"{entry.name} marked as a no-show.")
        self._text(entry, f"{entry.name} was marked as a no-show for {self.settings.name}.{self._questions()}")
        self._fill_from_waitlist()

    def restore_entry(self, entry_id: str) -> None:
        entry = self.entry(entry_id)
        if self.status != SETUP or entry.status != NO_SHOW:
            raise TournamentError("Only a no-show can be restored, before the draw.")
        if self.field_full:
            entry.status = WAITLISTED
            self.waitlist.append(entry.id)
        else:
            entry.status = REGISTERED
        self._say(f"Restored {entry.name}.")

    def move_seed(self, entry_id: str, step: int) -> None:
        if self.status != SETUP:
            raise TournamentError("The order is set once the bracket is drawn.")
        ids = [e.id for e in self.field_entries]
        if entry_id not in ids:
            raise TournamentError("Only entries in the field can be moved.")
        i = ids.index(entry_id)
        j = max(0, min(len(ids) - 1, i + step))
        ids.insert(j, ids.pop(i))
        rest = [i for i in self.order if i not in ids]
        self.order = ids + rest

    def shuffle_order(self, rng: random.Random | None = None) -> None:
        if self.status != SETUP:
            raise TournamentError("The order is set once the bracket is drawn.")
        ids = [e.id for e in self.field_entries]
        (rng or random).shuffle(ids)
        self.order = ids + [i for i in self.order if i not in ids]
        self._say("Shuffled the entry order.")

    # ------------------------------------------------------------- the draw

    def check_format(self, count: int | None = None) -> None:
        """Raise if the settings can't make a bracket for this many entries."""
        s = self.settings
        n = len(self.field_entries) if count is None else count
        if n < 2:
            raise TournamentError("A bracket needs at least 2 entries.")
        if s.format not in FORMAT_NAMES:
            raise TournamentError("Pick a bracket format.")
        if s.format == MODIFIED:
            if s.final_cut not in FINAL_CUTS:
                raise TournamentError("The single-elimination final must be 4, 8, 16 or 32.")
            if n <= s.final_cut:
                raise TournamentError(f"Modified single elimination to a final {s.final_cut} needs more than "
                                      f"{s.final_cut} entries. Pick a smaller final, or double elimination.")
        if s.format == ROUND_ROBIN and s.skill_groups:
            raise TournamentError("Skill groups work with single, double and modified single elimination.")
        if (s.skill_groups or s.race_method == RACK_CHART) and s.team_size != 1:
            raise TournamentError("Skill groups and the rack race chart are for singles events.")
        if s.format == ROUND_ROBIN:
            if s.groups < 1:
                raise TournamentError("Round robin needs at least 1 group.")
            if n < 2 * s.groups:
                raise TournamentError(f"{s.groups} groups need at least {2 * s.groups} entries.")
            if s.advance and s.advance >= n // s.groups:
                raise TournamentError("Fewer entries must advance from each group than are in it.")
            if s.advance and s.groups * s.advance < 2:
                raise TournamentError("A playoff needs at least 2 entries advancing.")

    def start(self, rng: random.Random | None = None) -> None:
        if self.status != SETUP:
            raise TournamentError("The bracket is already drawn.")
        field_entries = self.field_entries
        if self.settings.require_checkin and self.not_checked_in:
            names = ", ".join(e.name for e in self.not_checked_in[:5])
            raise TournamentError(f"Check in or mark as no-shows first: {names}.")
        self.check_format(len(field_entries))
        ids = [e.id for e in field_entries]
        if self.settings.seeding == "random":
            (rng or random).shuffle(ids)
        needs_skill = self.settings.skill_groups or self.settings.race_method == RACK_CHART
        missing = [self.entries[i].name for i in ids if needs_skill and not self.entries[i].skill]
        if missing:
            raise TournamentError(f"Add a skill level first (Edit under Entries): {', '.join(missing[:5])}.")
        divisions = self._divisions(ids)  # checks every group can make a bracket before anything changes
        self.order = ids + [i for i in self.order if i not in ids]
        for n, entry_id in enumerate(ids, 1):
            entry = self.entries[entry_id]
            entry.seed, entry.status = n, ACTIVE
        for entry_id in list(self.waitlist):
            self._text(self.entries[entry_id], f"The bracket for {self.settings.name} is drawn and the field is set. "
                                               "Sorry we couldn't fit you in this time.")
        self.matches, self.groups = [], {}
        builder = {SINGLE: self._build_single_event, DOUBLE: self._build_double,
                   MODIFIED: self._build_double, ROUND_ROBIN: self._build_round_robin}[self.settings.format]
        for label, members in divisions:
            first = len(self.matches)
            for entry_id in members:
                self.entries[entry_id].division = label
            builder(members)
            for m in self.matches[first:]:
                m.division = label
        self.pot_cents = self.settings.entry_fee_cents * len(ids) + self.settings.added_money_cents
        self.status, self.started_at = RUNNING, now()
        self._say(f"Bracket drawn: {len(ids)} entries, {self.format_name}. Pot {money(self.pot_cents)}.")
        self._advance()

    def _fits(self, count: int) -> bool:
        try:
            self.check_format(count)
            return True
        except TournamentError:
            return False

    def _divisions(self, ids: list[str]) -> list[tuple[str, list[str]]]:
        """Split the field (in draw order) into skill groups, merging any group too small to play.

        A group too small for the format joins whichever neighboring group has fewer players
        (the lower one on a tie). Returns [(label, entry ids)] from the lowest group up.
        """
        groups = self.settings.skill_groups
        if not groups:
            return [("", ids)]
        buckets = [[lo, hi, []] for lo, hi in groups]
        for entry_id in ids:
            skill = self.entries[entry_id].skill
            bucket = next((b for b in buckets if b[0] <= skill <= b[1]), None)
            if bucket is None:
                raise TournamentError(f"{self.entries[entry_id].name} (SL {skill}) isn't in any skill group.")
            bucket[2].append(entry_id)
        buckets = [b for b in buckets if b[2]]
        while True:
            small = [b for b in buckets if not self._fits(len(b[2]))]
            if not small:
                break
            if len(buckets) == 1:
                self.check_format(len(buckets[0][2]))  # raises with the reason
            b = min(small, key=lambda x: (len(x[2]), x[0]))
            i = buckets.index(b)
            neighbors = [buckets[j] for j in (i - 1, i + 1) if 0 <= j < len(buckets)]
            into = min(neighbors, key=lambda x: (len(x[2]), x[0]))
            into[0], into[1] = min(into[0], b[0]), max(into[1], b[1])
            into[2] = [x for x in ids if x in set(into[2]) | set(b[2])]  # keep the draw order
            buckets.remove(b)
            self._say(f"Skill group {group_label(b[0], b[1])} ({len(b[2])}) was too small, so it joined the next group "
                      f"to make {group_label(into[0], into[1])}.")
        return [(group_label(lo, hi), members) for lo, hi, members in buckets]

    @property
    def division_names(self) -> list[str]:
        """The skill groups in play (lowest first); [""] for an event with one bracket."""
        if self.status == SETUP:
            if not self.settings.skill_groups:
                return [""]
            return [group_label(lo, hi) for lo, hi in self.settings.skill_groups]
        names = []
        for m in self.matches:
            if m.division not in names:
                names.append(m.division)
        return names or [""]

    def division_entries(self, division: str) -> list[Entry]:
        return [e for e in self.entries.values() if e.division == division and e.seed is not None]

    def division_done(self, division: str) -> bool:
        ms = [m for m in self.matches if m.division == division]
        return bool(ms) and all(m.status in (DONE, VOID) for m in ms)

    def _new(self, stage: str, rnd: int, index: int, src_a: dict, src_b: dict, *, level: int = 0,
             knockout: bool = False, group: str = "", reset: bool = False) -> BMatch:
        m = BMatch(id=uuid.uuid4().hex[:8], num=len(self.matches) + 1, stage=stage, round=rnd, index=index,
                   level=level, knockout=knockout, group=group, reset=reset, src_a=src_a, src_b=src_b)
        for side, src in (("a", src_a), ("b", src_b)):
            if src and "entry" in src:
                setattr(m, side, src["entry"])
        self.matches.append(m)
        return m

    @staticmethod
    def _seeded_slots(ids: list[str]) -> list[dict]:
        size = next_power_of_two(len(ids))
        return [{"entry": ids[s - 1] if s <= len(ids) else BYE} for s in bracket_order(size)]

    def _build_knockout(self, slots: list[dict], stage: str, level_offset: int = 0) -> BMatch:
        """Single elimination from first-round slots (a power of two). Returns the final."""
        prev = [self._new(stage, 1, i, slots[2 * i], slots[2 * i + 1], level=level_offset + 1, knockout=True)
                for i in range(len(slots) // 2)]
        rnd = 1
        while len(prev) > 1:
            rnd += 1
            prev = [self._new(stage, rnd, i, {"match": prev[2 * i].id, "take": "winner"},
                              {"match": prev[2 * i + 1].id, "take": "winner"}, level=level_offset + rnd, knockout=True)
                    for i in range(len(prev) // 2)]
        return prev[0]

    def _build_single_event(self, ids: list[str]) -> None:
        self._build_knockout(self._seeded_slots(ids), "W")

    def _build_double(self, ids: list[str]) -> None:
        s = self.settings
        slots = self._seeded_slots(ids)
        size = len(slots)
        k = size.bit_length() - 1
        cut = s.final_cut if s.format == MODIFIED else 0
        w_rounds = k if not cut else (2 * size // cut).bit_length() - 1
        winners: dict[int, list[BMatch]] = {1: [self._new("W", 1, i, slots[2 * i], slots[2 * i + 1])
                                                for i in range(size // 2)]}
        for r in range(2, w_rounds + 1):
            prev = winners[r - 1]
            winners[r] = [self._new("W", r, i, {"match": prev[2 * i].id, "take": "winner"},
                                    {"match": prev[2 * i + 1].id, "take": "winner"}) for i in range(len(prev) // 2)]

        def win(m): return {"match": m.id, "take": "winner"}
        def lose(m): return {"match": m.id, "take": "loser"}

        last_l = 2 * (w_rounds - 1)
        losers: dict[int, list[BMatch]] = {}
        if last_l >= 1:
            w1 = winners[1]
            losers[1] = [self._new("L", 1, i, lose(w1[2 * i]), lose(w1[2 * i + 1]), level=1, knockout=True)
                         for i in range(len(w1) // 2)]
            for j in range(1, w_rounds):
                prev = losers[2 * j - 1]
                drops = winners[j + 1][::-1] if j % 2 == 1 else winners[j + 1]
                losers[2 * j] = [self._new("L", 2 * j, i, win(prev[i]), lose(drops[i]), level=2 * j, knockout=True)
                                 for i in range(len(prev))]
                if 2 * j + 1 <= last_l:
                    prev = losers[2 * j]
                    losers[2 * j + 1] = [self._new("L", 2 * j + 1, i, win(prev[2 * i]), win(prev[2 * i + 1]),
                                                   level=2 * j + 1, knockout=True) for i in range(len(prev) // 2)]

        if cut:  # modified: half from each side, then single elimination
            w_q = [win(m) for m in winners[w_rounds]]
            l_q = [win(m) for m in losers[last_l]]
            slots = []
            for i in range(len(w_q)):
                slots += [w_q[i], l_q[len(l_q) - 1 - i]]
            self._build_knockout(slots, "P", level_offset=last_l)
            return

        w_final = winners[w_rounds][0]
        l_champ = win(losers[last_l][0]) if last_l else lose(winners[1][0])
        gf = self._new("F", 1, 0, win(w_final), l_champ, level=last_l + 1, knockout=True)
        if s.true_double:
            self._new("F", 2, 0, lose(gf), win(gf), level=last_l + 1, knockout=True, reset=True)

    def _build_round_robin(self, ids: list[str]) -> None:
        s = self.settings
        count = max(1, min(s.groups, len(ids) // 2))
        labels = [chr(ord("A") + i) for i in range(count)]
        self.groups = {g: [] for g in labels}
        for n, entry_id in enumerate(ids):  # snake seeding: A B C C B A A B C ...
            lap, pos = divmod(n, count)
            self.groups[labels[pos if lap % 2 == 0 else count - 1 - pos]].append(entry_id)
        rounds_by_group = {}
        for g, members in self.groups.items():
            ring = members + ([BYE] if len(members) % 2 else [])
            n = len(ring)
            rounds = []
            for r in range(n - 1):
                rounds.append([(ring[i], ring[n - 1 - i]) for i in range(n // 2)
                               if BYE not in (ring[i], ring[n - 1 - i])])
                ring = [ring[0], ring[-1]] + ring[1:-1]
            rounds_by_group[g] = rounds
        most = max(len(r) for r in rounds_by_group.values())
        for r in range(most):  # round by round across groups, so every group moves along together
            for g in labels:
                for i, (x, y) in enumerate(rounds_by_group[g][r] if r < len(rounds_by_group[g]) else []):
                    self._new("G", r + 1, i, {"entry": x}, {"entry": y}, group=g)
        if s.advance:
            ranked = []
            for rank in range(1, s.advance + 1):
                row = [{"group": g, "rank": rank} for g in labels]
                ranked += row if rank % 2 == 1 else row[::-1]
            size = next_power_of_two(len(ranked))
            slots = [ranked[n - 1] if n <= len(ranked) else {"entry": BYE} for n in bracket_order(size)]
            self._build_knockout(slots, "P")

    # ------------------------------------------------------- moving it along

    def _consumers(self) -> dict[tuple[str, str], list[BMatch]]:
        out: dict[tuple[str, str], list[BMatch]] = {}
        for m in self.matches:
            for src in (m.src_a, m.src_b):
                if src and "match" in src:
                    out.setdefault((src["match"], src["take"]), []).append(m)
        return out

    def group_complete(self, group: str) -> bool:
        return all(m.status in (DONE, VOID) for m in self.matches if m.stage == "G" and m.group == group)

    def _source_value(self, src: dict | None) -> str | None:
        if not src:
            return None
        if "entry" in src:
            return src["entry"]
        if "match" in src:
            m = self.match(src["match"])
            if m.status == DONE:
                return m.winner if src["take"] == "winner" else m.loser
            return None
        if "group" in src:
            if not self.group_complete(src["group"]):
                return None
            table = self.group_standings(src["group"])
            return table[src["rank"] - 1]["entry"].id if src["rank"] <= len(table) else BYE
        return None

    def _race_for(self, m: BMatch) -> int:
        s = self.settings
        if m.stage == "F":
            return s.race_finals
        if m.stage == "L":
            return s.race_losers
        if m.stage in ("W", "P"):
            consumers = self._consumers()
            is_final = not consumers.get((m.id, "winner")) and not consumers.get((m.id, "loser"))
            return s.race_finals if is_final else s.race_winners
        return s.race_winners

    def _set_race(self, m: BMatch) -> None:
        m.race_to = self._race_for(m)
        m.race_a = m.race_b = 0
        if self.settings.race_method == RACK_CHART and m.a not in (None, BYE) and m.b not in (None, BYE):
            m.race_a, m.race_b = rack_race(self.entries[m.a].skill, self.entries[m.b].skill)
            m.race_to = max(m.race_a, m.race_b)

    def race_of(self, m: BMatch, side: str) -> int:
        """Racks side 'a' or 'b' needs to win the match."""
        return getattr(m, f"race_{side}") or m.race_to

    def race_text(self, m: BMatch) -> str:
        """'race to 3', or with a rack race chart 'Ann 3 · Bob 2'."""
        if not (m.race_a or m.race_b):
            return f"race to {m.race_to}"
        return f"{self.name_of(m.a)} races to {m.race_a} · {self.name_of(m.b)} to {m.race_b}"

    def _resolve(self) -> None:
        changed = True
        while changed:
            changed = False
            for m in self.matches:
                if m.status != PENDING:
                    continue
                if m.reset:
                    first = self.match(m.src_b["match"])
                    if first.status == DONE and first.winner == first.a:  # winners-side champion won: no reset
                        m.status, m.finished_at, changed = VOID, now(), True
                        continue
                for side in ("a", "b"):
                    if getattr(m, side) is None:
                        value = self._source_value(getattr(m, f"src_{side}"))
                        if value is not None:
                            setattr(m, side, value)
                            changed = True
                if m.a is None or m.b is None:
                    continue
                changed = True
                if m.is_bye:  # a bye: the other side goes through (two byes make a bye)
                    m.winner, m.loser = (m.b if m.a == BYE else m.a), BYE
                    m.status, m.finished_at = DONE, now()
                    continue
                m.status, m.ready_at = READY, now()
                self._set_race(m)
                withdrawn = [x for x in (m.a, m.b) if self.entries[x].withdrawn]
                if withdrawn:  # a withdrawn entry forfeits whatever it has left
                    winner = m.b if withdrawn[0] == m.a and len(withdrawn) == 1 else m.a
                    self._finish(m, winner, forfeit=True, quiet=True)

    def _finish(self, m: BMatch, winner: str, forfeit: bool = False, quiet: bool = False) -> None:
        m.winner, m.loser = winner, (m.b if winner == m.a else m.a)
        m.forfeit, m.status, m.finished_at = forfeit, DONE, now()
        if forfeit and m.score_a < self.race_of(m, "a") and m.score_b < self.race_of(m, "b"):
            if winner == m.a:
                m.score_a = self.race_of(m, "a")
            else:
                m.score_b = self.race_of(m, "b")
        if not quiet:
            self._say(f"#{m.num}: {self.name_of(m.winner)} beat {self.name_of(m.loser)} "
                      f"{m.score_a}-{m.score_b}{' (forfeit)' if forfeit else ''}.")

    def _loser_out(self, m: BMatch, consumers) -> bool:
        if not m.knockout:
            return False
        return not any(c.status != VOID for c in consumers.get((m.id, "loser"), []))

    def _settle(self) -> None:
        """Work out who is still in, places and payouts from the matches (safe to repeat)."""
        consumers = self._consumers()
        for e in self.entries.values():
            if e.status in (ACTIVE, ELIMINATED, CHAMPION):
                e.status, e.out_level, e.out_match = ACTIVE, None, None
        for m in sorted(self.matches, key=lambda m: m.finished_at or ""):
            if m.status == DONE and m.loser not in (None, BYE) and self._loser_out(m, consumers):
                e = self.entries[m.loser]
                e.status, e.out_level, e.out_match = ELIMINATED, m.level, m.id
        if self.settings.format == ROUND_ROBIN and self.settings.advance:
            for g in self.groups:
                if self.group_complete(g):
                    for row in self.group_standings(g)[self.settings.advance:]:
                        e = row["entry"]
                        if e.status == ACTIVE:
                            e.status, e.out_level = ELIMINATED, 0
        if all(m.status in (DONE, VOID) for m in self.matches) and self.status == RUNNING:
            self.status, self.finished_at = FINISHED, now()
            self._say("Tournament finished.")
        elif self.status == FINISHED and not all(m.status in (DONE, VOID) for m in self.matches):
            self.status, self.finished_at = RUNNING, None
        places = self.places()
        if self.status in (RUNNING, FINISHED):
            for division in self.division_names:
                if not self.division_done(division):
                    continue
                for e in self.division_entries(division):
                    if places.get(e.id, (0, 0))[0] == 1:
                        e.status = CHAMPION
                    # Everyone else is done too (a round robin without a playoff knocks nobody out).
                    elif e.status == ACTIVE:
                        e.status = ELIMINATED
        self._pay(places)

    def division_pot_cents(self, division: str) -> int:
        """A skill group's share of the pot, by its number of entries (the whole pot with one bracket)."""
        seeded = [e for e in self.entries.values() if e.seed is not None]
        if not seeded or len(self.division_names) == 1:
            return self.pot_cents
        return self.pot_cents * len(self.division_entries(division)) // len(seeded)

    def _pay(self, places: dict[str, tuple[int, int]]) -> None:
        pcts = self.settings.payouts
        for e in self.entries.values():
            e.payout_cents = 0
        for division in self.division_names:
            pot = self.division_pot_cents(division)
            ids = {e.id for e in self.division_entries(division)}
            here = {i: p for i, p in places.items() if i in ids}
            done = self.division_done(division)
            paid = []
            for entry_id, (lo, hi) in here.items():
                share = sum(pcts[lo - 1:hi]) / (hi - lo + 1)
                if share:
                    self.entries[entry_id].payout_cents = int(pot * share / 100)
                    paid.append(entry_id)
            champ = next((i for i, (lo, _) in here.items() if lo == 1), None)
            if done and paid and champ:  # rounding pennies go to the winner
                used = sum(pcts[:max(hi for _, hi in here.values())])
                target = round(pot * used / 100)
                self.entries[champ].payout_cents += target - sum(self.entries[i].payout_cents for i in paid)
            for entry_id, (lo, hi) in here.items():
                e = self.entries[entry_id]
                if e.payout_cents and not e.payout_texted and (done or lo > 1):
                    e.payout_texted, e.payout_texted_cents = True, e.payout_cents
                    where = f" in {division}" if division else ""
                    self._text(e, f"{e.name} finished {place_text((lo, hi))}{where} and won {money(e.payout_cents)}. "
                                  "Collect it at the tournament desk. Congratulations!")
        # A result fixed after payout texts went out: tell each player their corrected amount.
        for e in self.entries.values():
            if e.payout_texted and not e.payout_texted_cents:  # saved before amounts were remembered
                e.payout_texted_cents = e.payout_cents
            if e.payout_texted and e.payout_cents != e.payout_texted_cents:
                where = f"finished {place_text(places[e.id])}" if e.id in places else "is no longer in the money"
                e.payout_texted_cents = e.payout_cents
                self._text(e, f"Correction: {e.name} {where}"
                              + (f" and won {money(e.payout_cents)}." if e.payout_cents else ".")
                              + " See the tournament desk with any questions.")

    def places(self) -> dict[str, tuple[int, int]]:
        """Final places so far: entry id -> (first, last) place it shares (within its skill group)."""
        field_ids = [e.id for e in self.entries.values() if e.seed is not None]
        if self.status == SETUP or not field_ids:
            return {}
        out: dict[str, tuple[int, int]] = {}
        if self.settings.format == ROUND_ROBIN:
            out.update(self._group_places(field_ids))
            if not self.settings.advance or not all(self.group_complete(g) for g in self.groups):
                return out
            pool = [i for i in field_ids if self.entries[i].out_level != 0]
            out.update(self._knockout_places(pool, self.matches))
            return out
        for division in self.division_names:
            pool = [i for i in field_ids if self.entries[i].division == division]
            out.update(self._knockout_places(pool, [m for m in self.matches if m.division == division]))
        return out

    def _knockout_places(self, pool: list[str], matches: list[BMatch]) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        knocked = [m for m in matches if m.knockout and m.status != VOID]
        levels = sorted({self.entries[i].out_level for i in pool if self.entries[i].out_level})
        for level in levels:
            if not all(m.status == DONE for m in knocked if m.level <= level):
                break
            out_here = [i for i in pool if self.entries[i].out_level == level]
            lo = 1 + sum(1 for i in pool if not (self.entries[i].out_level and self.entries[i].out_level <= level))
            for i in out_here:
                out[i] = (lo, lo + len(out_here) - 1)
        if matches and all(m.status in (DONE, VOID) for m in matches):
            alive = [i for i in pool if not self.entries[i].out_level]
            if len(alive) == 1:
                out[alive[0]] = (1, 1)
        return out

    def _group_places(self, field_ids: list[str]) -> dict[str, tuple[int, int]]:
        """Round robin places for entries that don't go on to a playoff."""
        if not all(self.group_complete(g) for g in self.groups):
            return {}
        rows = []
        for g in self.groups:
            for rank, row in enumerate(self.group_standings(g), 1):
                rows.append((rank, -row["wins"], -(row["for"] - row["against"]), -row["for"], row["entry"].seed or 0,
                             row["entry"].id))
        rows.sort()
        skip = self.settings.advance * len(self.groups) if self.settings.advance else 0
        out = {}
        place = skip
        for rank, *_rest, entry_id in rows:
            if self.settings.advance and rank <= self.settings.advance:
                continue
            place += 1
            out[entry_id] = (place, place)
        return out

    def group_standings(self, group: str) -> list[dict]:
        rows = {i: {"entry": self.entries[i], "played": 0, "wins": 0, "losses": 0, "for": 0, "against": 0}
                for i in self.groups.get(group, [])}
        for m in self.matches:
            if m.stage != "G" or m.group != group or m.status != DONE:
                continue
            for side, other in (("a", "b"), ("b", "a")):
                row = rows[getattr(m, side)]
                row["played"] += 1
                row["for"] += getattr(m, f"score_{side}")
                row["against"] += getattr(m, f"score_{other}")
                if m.winner == getattr(m, side):
                    row["wins"] += 1
                else:
                    row["losses"] += 1
        return sorted(rows.values(), key=lambda r: (-r["wins"], -(r["for"] - r["against"]), -r["for"],
                                                     r["entry"].seed or 0))

    def _call_ready(self) -> None:
        if not self.settings.auto_call:
            return
        busy = {x for m in self.matches if m.status == CALLED for x in (m.a, m.b)}
        free = self.free_tables()
        for m in self.ready_queue():
            if not free:
                break
            if m.a in busy or m.b in busy:
                continue
            self._call(m, free.pop(0))
            busy |= {m.a, m.b}

    def _call(self, m: BMatch, table: str) -> None:
        m.status, m.table, m.called_at, m.priority = CALLED, table, now(), False
        label = self.match_label(m)
        self._say(f"#{m.num} called to table {table}: {self.name_of(m.a)} vs {self.name_of(m.b)}.")
        for me, them, mine, theirs in ((m.a, m.b, "a", "b"), (m.b, m.a, "b", "a")):
            race = (f"race to {m.race_to}" if not m.race_a else
                    f"you race to {self.race_of(m, mine)}, they race to {self.race_of(m, theirs)}")
            self._text(self.entries[me], f"Table {table}: {self.name_of(me)} vs {self.name_of(them)}, {race} "
                                         f"({label}). Good luck!")

    def _advance(self) -> None:
        self._resolve()
        self._settle()
        if self.status == RUNNING:
            self._call_ready()

    # --------------------------------------------------------------- labels

    def match_label(self, m: BMatch) -> str:
        if m.stage == "G":
            return f"Group {m.group}, round {m.round}"
        label = f"{self.round_title(m.stage, m.round, m.division)}, match #{m.num}"
        return f"{m.division} · {label}" if m.division else label

    def stage_rounds(self, stage: str, division: str = "") -> int:
        return max((m.round for m in self.matches if m.stage == stage and m.division == division), default=0)

    def round_title(self, stage: str, rnd: int, division: str = "") -> str:
        last = self.stage_rounds(stage, division)
        if stage == "W" and self.settings.format == SINGLE:
            return round_name(last - rnd)
        if stage == "P":
            return round_name(last - rnd)
        if stage == "W":
            return "Winners final" if rnd == last and self.settings.format == DOUBLE else f"Winners round {rnd}"
        if stage == "L":
            return "Losers final" if rnd == last and self.settings.format == DOUBLE else f"Losers round {rnd}"
        if stage == "F":
            return "Grand final" if rnd == 1 else "Grand final (reset)"
        return f"Round {rnd}"

    def stage_title(self, stage: str) -> str:
        if stage == "W" and self.settings.format == SINGLE:
            return "Bracket"
        if stage == "P" and self.settings.format == MODIFIED:
            return f"Final {self.settings.final_cut} (single elimination)"
        return STAGE_NAMES.get(stage, stage)

    def slot_label(self, m: BMatch, side: str) -> str:
        value = getattr(m, side)
        if value is not None:
            return self.name_of(value)
        src = getattr(m, f"src_{side}") or {}
        if "match" in src:
            num = self.match(src["match"]).num
            return f"{'Winner' if src['take'] == 'winner' else 'Loser'} of #{num}"
        if "group" in src:
            return f"Group {src['group']} {ordinal(src['rank'])}"
        return "TBD"

    def bracket(self, division: str = "") -> list[dict]:
        """The elimination stages (of one skill group), as columns of rounds, for drawing the bracket."""
        out = []
        for stage in ("W", "L", "P", "F"):
            ms = [m for m in self.matches if m.stage == stage and m.division == division]
            if not ms:
                continue
            rounds = []
            for r in range(1, self.stage_rounds(stage, division) + 1):
                rounds.append({"title": self.round_title(stage, r, division),
                               "matches": sorted((m for m in ms if m.round == r), key=lambda m: m.index)})
            out.append({"stage": stage, "title": self.stage_title(stage), "rounds": rounds})
        return out

    def next_for(self, entry_id: str) -> dict:
        """Where an entry goes from its current match: {'win': text, 'lose': text}."""
        m = self.current_match_for(entry_id)
        if m is None:
            return {}
        consumers = self._consumers()
        out = {}
        for take, key in (("winner", "win"), ("loser", "lose")):
            nxt = [c for c in consumers.get((m.id, take), []) if c.status != VOID]
            if nxt:
                c = nxt[0]
                out[key] = f"{self.round_title(c.stage, c.round, c.division)} (#{c.num})" if c.stage != "G" else ""
            elif m.stage == "G":
                out[key] = ""
            else:
                champ = f"{m.division} champion" if m.division else "Champion"
                out[key] = champ if take == "winner" else "Out"
        return out

    # ---------------------------------------------------------------- tables

    def score(self, match_id: str, side: str, delta: int, by: str | None = None) -> BMatch:
        m = self.match(match_id)
        if m.status != CALLED:
            raise TournamentError("That match isn't on a table.")
        if side not in ("a", "b"):
            raise TournamentError("Pick a side.")
        attr, other_side = f"score_{side}", "b" if side == "a" else "a"
        value = getattr(m, attr) + delta
        if value < 0 or value > self.race_of(m, side):
            raise TournamentError(f"Scores go from 0 to {self.race_of(m, side)}.")
        other = getattr(m, f"score_{other_side}")
        if delta > 0 and other >= self.race_of(m, other_side):
            raise TournamentError("The other side already reached the race. Submit the result or press −.")
        setattr(m, attr, value)
        if by:
            self._say(f"#{m.num} score {m.score_a}-{m.score_b} (entered by {by}).")
        return m

    def race_winner(self, m: BMatch) -> str | None:
        if m.score_a >= self.race_of(m, "a"):
            return m.a
        if m.score_b >= self.race_of(m, "b"):
            return m.b
        return None

    def submit_result(self, match_id: str) -> BMatch:
        m = self.match(match_id)
        winner = self.race_winner(m)
        if winner is None:
            raise TournamentError("Neither side has reached its race yet.")
        return self.record_result(match_id, winner)

    def record_result(self, match_id: str, winner_id: str, forfeit: bool = False) -> BMatch:
        m = self.match(match_id)
        if m.status not in (CALLED, READY):
            raise TournamentError("That match isn't being played.")
        if winner_id not in (m.a, m.b):
            raise TournamentError("Pick one of the two sides.")
        if m.status == READY and not forfeit:
            raise TournamentError("That match hasn't been called to a table. Record it as a forfeit, or call it first.")
        self._finish(m, winner_id, forfeit)
        self._resolve()  # first, so an unneeded reset final is voided before we tell the loser where it stands
        consumers = self._consumers()
        winner, loser = self.entries[m.winner], self.entries[m.loser]
        score = f"{max(m.score_a, m.score_b)}-{min(m.score_a, m.score_b)}"
        champion = not consumers.get((m.id, "winner")) and not any(
            c.status != VOID for c in consumers.get((m.id, "loser"), [])) and m.stage != "G"
        self._text(winner, f"{winner.name} beat {loser.name} {score}{' (forfeit)' if forfeit else ''}. "
                           + ((f"{winner.name} is the {m.division} champion!" if m.division else "") if champion
                              else "Stay close: we'll text you when your next table is ready."))
        if m.stage == "G":
            self._text(loser, f"{loser.name} lost to {winner.name} {score}. Watch for your next table call.")
        elif self._loser_out(m, consumers):
            self._text(loser, f"{loser.name} lost to {winner.name} {score} and is out. Thanks for playing!")
        elif m.stage == "F":
            self._text(loser, f"{loser.name} lost to {winner.name} {score}. One more final decides it: "
                              "watch for your table call.")
        else:
            self._text(loser, f"{loser.name} lost to {winner.name} {score} and moves to the losers side. "
                              "We'll text you when your next table is ready.")
        self._settle()
        if self.status == RUNNING:
            self._call_ready()
        return m

    def fix_result(self, match_id: str) -> BMatch:
        """Swap the winner of a finished match while nothing that depends on it has started."""
        m = self.match(match_id)
        if m.status != DONE or m.is_bye:
            raise TournamentError("Only a finished match can be fixed.")
        deps = self._dependents(m)
        started = [d for d in deps if d.status in (CALLED, DONE) and not d.is_bye]
        if started:
            raise TournamentError(f"Match #{started[0].num} has already used this result. Use Undo, or fix that match first.")
        m.winner, m.loser = m.loser, m.winner
        m.score_a, m.score_b = m.score_b, m.score_a
        if m.race_a or m.race_b:  # uneven races: the new winner reached its own race, the loser fell short of its
            won, lost = ("a", "b") if m.winner == m.a else ("b", "a")
            setattr(m, f"score_{won}", self.race_of(m, won))
            setattr(m, f"score_{lost}", min(getattr(m, f"score_{lost}"), self.race_of(m, lost) - 1))
        self._reset_dependents(m)
        self._say(f"Fixed #{m.num}: {self.name_of(m.winner)} won.")
        for x in (m.winner, m.loser):
            self._text(self.entries[x], f"Correction for match #{m.num}: {self.name_of(m.winner)} won "
                                        f"{max(m.score_a, m.score_b)}-{min(m.score_a, m.score_b)}.")
        self._advance()
        return m

    def _dependents(self, m: BMatch) -> list[BMatch]:
        """Matches that take a side from m, directly or through byes and resets."""
        found, todo = [], [m]
        while todo:
            cur = todo.pop()
            for other in self.matches:
                for src in (other.src_a, other.src_b):
                    if not src:
                        continue
                    hit = src.get("match") == cur.id or (cur.stage == "G" and src.get("group") == cur.group)
                    if hit and other not in found:
                        found.append(other)
                        if other.is_bye or other.status == VOID or other.reset:
                            todo.append(other)
        return found

    def _reset_dependents(self, m: BMatch) -> None:
        for d in self._dependents(m):
            for side in ("a", "b"):
                src = getattr(d, f"src_{side}") or {}
                if "entry" not in src:
                    setattr(d, side, None)
            d.status, d.winner, d.loser, d.ready_at, d.finished_at, d.table = PENDING, None, None, None, None, None
            d.score_a = d.score_b = 0

    def call_to_table(self, match_id: str, table: str) -> BMatch:
        m = self.match(match_id)
        if table not in self.settings.tables:
            raise TournamentError("Unknown table.")
        if table in self.settings.tables_down:
            raise TournamentError(f"Table {table} is out of service.")
        if table in self.busy_tables() and m.table != table:
            raise TournamentError(f"Table {table} already has a match on it.")
        if m.status == READY:
            busy = {x for o in self.matches if o.status == CALLED for x in (o.a, o.b)}
            if busy & {m.a, m.b}:
                raise TournamentError("One of those entries is playing on another table right now.")
            self._call(m, table)
        elif m.status == CALLED:
            old, m.table = m.table, table
            self._say(f"#{m.num} moved from table {old} to table {table}.")
            for x in (m.a, m.b):
                self._text(self.entries[x], f"Table change: your match (#{m.num}) is now on table {table}.")
        else:
            raise TournamentError("That match isn't ready to play.")
        self._call_ready()
        return m

    def prioritize(self, match_id: str) -> BMatch:
        m = self.match(match_id)
        if m.status != READY:
            raise TournamentError("Only a match waiting for a table can be moved to the front.")
        m.priority = True
        self._call_ready()
        return m

    def set_table_down(self, table: str, down: bool) -> None:
        s = self.settings
        if table not in s.tables:
            raise TournamentError("Unknown table.")
        if down and table not in s.tables_down:
            s.tables_down.append(table)
            self._say(f"Table {table} is out of service.")
            for m in self.matches:
                if m.status == CALLED and m.table == table:
                    free = self.free_tables()
                    if free:
                        self.call_to_table(m.id, free[0])
                    else:  # back to the front of the line, score kept
                        m.status, m.table, m.priority = READY, None, True
                        for x in (m.a, m.b):
                            self._text(self.entries[x], f"Table {table} is out of service. Your match (#{m.num}) "
                                                        "goes next when a table opens; the score is kept.")
        elif not down and table in s.tables_down:
            s.tables_down.remove(table)
            self._say(f"Table {table} is back in service.")
        if self.status == RUNNING:
            self._call_ready()

    def call_waiting(self) -> None:
        """Call whatever fits on the free tables now (used after turning auto-call back on)."""
        auto, self.settings.auto_call = self.settings.auto_call, True
        try:
            self._call_ready()
        finally:
            self.settings.auto_call = auto

    def withdraw(self, entry_id: str) -> None:
        entry = self.entry(entry_id)
        if self.status != RUNNING or entry.status != ACTIVE:
            raise TournamentError("Only an entry that's still playing can withdraw.")
        m = self.current_match_for(entry_id)
        if m and m.status == CALLED and (m.score_a or m.score_b):
            raise TournamentError(f"{entry.name} is mid-match. Record the result (or a forfeit) first.")
        entry.withdrawn = True
        self._say(f"{entry.name} withdrew; its remaining matches are forfeits.")
        for m in self.matches:
            if m.status in (READY, CALLED) and entry_id in (m.a, m.b):
                self._finish(m, m.b if m.a == entry_id else m.a, forfeit=True)
        self._advance()

    def match_minutes_played(self, m: BMatch) -> float | None:
        if not m.called_at:
            return None
        return (datetime.now() - datetime.fromisoformat(m.called_at)).total_seconds() / 60

    def long_matches(self) -> list[BMatch]:
        return [m for m in self.in_play() if (self.match_minutes_played(m) or 0) >= self.settings.long_match_minutes]

    def set_money(self, fee: int, added: int) -> None:
        if self.status != SETUP and (fee, added) != (self.settings.entry_fee_cents, self.settings.added_money_cents):
            raise TournamentError("The entry fee and added money are set once the bracket is drawn.")
        if fee < 0 or added < 0:
            raise TournamentError("Money can't be negative.")
        self.settings.entry_fee_cents, self.settings.added_money_cents = fee, added

    def set_payouts(self, percents: list[int]) -> None:
        if any(p < 0 for p in percents) or sum(percents) > 100:
            raise TournamentError("Payouts are percents of the pot and can't add up to more than 100.")
        if self.status != SETUP and any(e.payout_texted for e in self.entries.values()) \
                and percents != self.settings.payouts:
            raise TournamentError("Payouts can't change once entries have been told what they won.")
        self.settings.payouts = percents
        if self.status != SETUP:
            self._settle()

    def update_races(self) -> None:
        """New race settings apply to matches that haven't been called yet."""
        for m in self.matches:
            if m.status == READY:
                self._set_race(m)

    @property
    def projected_pot_cents(self) -> int:
        if self.status != SETUP:
            return self.pot_cents
        return self.settings.entry_fee_cents * len(self.field_entries) + self.settings.added_money_cents

    def payout_table(self) -> list[tuple[str, int]]:
        pot = self.projected_pot_cents
        return [(ordinal(i), int(pot * p / 100)) for i, p in enumerate(self.settings.payouts, 1) if p]

    def results(self) -> list[dict]:
        """Final and current standings for the public page."""
        places = self.places()
        rows = []
        for e in self.entries.values():
            if e.seed is None:
                continue
            rows.append({"entry": e, "place": places.get(e.id), "place_text": place_text(places.get(e.id)),
                         "division": e.division})
        order = {d: n for n, d in enumerate(self.division_names)}
        rows.sort(key=lambda r: (order.get(r["division"], 99), r["place"] or (999, 999), r["entry"].status != ACTIVE,
                                 r["entry"].seed))
        return rows

    # ----------------------------------------------------------- serialization

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> BracketTournament:
        data = dict(data)
        t = cls(
            settings=BracketSettings(**data.pop("settings", {})),
            entries={k: Entry(**{**v, "players": [Player(**p) for p in v.get("players", [])]})
                     for k, v in data.pop("entries", {}).items()},
            matches=[BMatch(**m) for m in data.pop("matches", [])],
            outbox=[Message(**m) for m in data.pop("outbox", [])],
        )
        for key, value in data.items():
            setattr(t, key, value)
        return t
