# Chip Tournament Manager

A web app for running a billiards league **chip tournament**: team registration, tournament details and location, roster updates, scorekeeping, winners/losers-list pairing, chip tracking, payouts and text alerts.

## Run it

```bash
cd chip_tournament
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ADMIN_PASSWORD=choose-one python app.py
```

Open http://localhost:5000. Players use **Home**, **Board** and **Register**. The tournament director uses **Director** (password from `ADMIN_PASSWORD`; it's `admin` if you don't set one).

Run the tests with `python -m pytest`.

## Pages

| Page | Who | What it does |
|---|---|---|
| `/` | everyone | Tournament details, venue and map link, how it works, pot, chip value, tables in play, winners/losers lists, standings |
| `/board` | TV at the venue | Same live info; refreshes every 15 seconds |
| `/register` | teams | Register a team of two partners: names, skill levels, cell numbers. Shows the combined skill and starting chips as you type. Gives (and texts) a 4-digit team PIN |
| `/team/<id>` | a team | Status, chips, where to play now, what its chips would cash in for, results |
| `/team/<id>/roster` | a team (with PIN) | Update partners, skill levels (before the start), team name and cell numbers |
| `/admin` | director | Settings, start, scoring, cash-in, last call, chip adjustments, texts, undo, chip ledger and activity log |

## How the rules work in the app

1. **Teams and starting chips.** A team is two partners. Their skill levels are added together, and the combined skill sets the starting chips:

   | Combined skill level | Starting chips |
   |---|---|
   | 6 or less | 5 |
   | 7–8 | 4 |
   | 9–10 | 3 |
   | 11 | 2 |
   | 12 or more | needs a new partner |

   Combined skill can't be over **10 at registration**. If a partner's skill goes up afterward, the team updates it on its roster page, up to **11**. A team over 11 can't be saved, and the tournament won't start until every team is 11 or less. Partners and skill levels lock once the tournament starts. The director can change the brackets and the registration cap in Settings until the start.
2. **Round 1** is a random draw. Matches go to free tables in order. If there are more matches than tables, the rest wait and get called automatically as tables free up.
3. **Scoring.** Tap `+`/`−` for each game. When a team reaches the race, it wins automatically (or tap **Winner**).
4. **After a match:** the winner keeps its chips and goes to the bottom of the **winners list**. The loser gives one chip back to the main table and goes to the bottom of the **losers list**. A team with 0 chips is **eliminated** and leaves the rotation.
5. **The next round** starts when every match in the round is finished: winner 1 vs winner 2, winner 3 vs winner 4, …, loser 1 vs loser 2, …
6. **Pot and chip value.** Pot = entry fee × teams + added money. Chip value = pot ÷ chips still in play, so it goes up every time a chip goes back to the table.
7. **Cash-in.** When the director opens cash-in, every team gets a text with the chip value and what its chips are worth. A team can cash in at the desk (the director clicks **Cash in**) for chips × chip value, or keep playing. Cashing in doesn't change what the other teams' chips are worth.
8. **Last call** stops all matches and cashes in every remaining team at the current chip value. The last team paid gets whatever is left in the pot, so every cent is paid out.
9. If only one team has chips left, it wins the rest of the pot automatically.

### Decisions the rules didn't specify (easy to change in `chips/engine.py`)

- **Odd number of teams on a list:** the leftover winner plays the leftover loser (a "cross" match). If only one team is left over, it gets a bye and keeps its chips.
- **Cashing in during a match:** not allowed once a game has been scored. If the match hasn't started, it's cancelled and the opponent waits for the next round.
- **Late entries** (after the start) pay the entry fee, get chips for their skill level and join the bottom of the winners list.
- **Entry fee, added money, chip brackets and the registration cap** lock when the tournament starts.
- **Skill updates** are entered by the team on its roster page (or by the director) before the start, and each change is written to the activity log (e.g. "Combined skill 10 → 11"). The 10 cap applies only to new registrations, so a team that swaps partners after registering can go up to 11.
- **Cash-in and last-call times** show to players and on the director's buttons, but the director clicks the buttons, so no team gets cashed out by surprise mid-match.

## Chip tracking

Every chip movement (starting chips, losses, cash-ins, manual adjustments) goes in the **chip ledger** with a reason and the chip value afterward. The director page shows a running check: chips issued = chips in play + chips returned + chips cashed in, and money collected = paid out + pot left. **Adjust** fixes a mistake (it needs a reason), and **Undo** reverses the last action of any kind (up to 100 steps).

## Text alerts

Texts go to the captain's cell and any player who added a cell number. A team gets a text when it registers (with its PIN), is called to a table, wins or loses a round, gets a bye, is eliminated, or cashes in, and when cash-in opens or last call happens. The director can also text every team from the dashboard.

To send real texts, create a [Twilio](https://www.twilio.com) account and set:

```bash
export TWILIO_ACCOUNT_SID=ACxxxxxxxx
export TWILIO_AUTH_TOKEN=xxxxxxxx
export TWILIO_FROM_NUMBER=+15551234567
```

Without these, texts are only logged (see **Recent texts** on the director page), so you can test without paying for SMS. US carriers require A2P 10DLC registration for business texting through Twilio.

## Data

Everything is saved in `chip_tournament/data/` (`tournament.json`, the undo history and `texts.log`). **Archive and start over** on the director page renames the old file and starts a fresh tournament. Set `CHIPS_DATA_DIR` to store the data somewhere else.

## Code layout

- `chips/engine.py`: all tournament rules (no web code), tested in `tests/test_engine.py`
- `chips/storage.py`: JSON save/load and undo
- `chips/alerts.py`: Twilio texts with a log-only fallback
- `app.py`: Flask routes, with `templates/` and `static/` for the pages
