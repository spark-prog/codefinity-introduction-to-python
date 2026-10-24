# Chip Tournament Manager

A web app for running a billiards league **chip tournament**: team registration, tournament details and location, roster updates, scorekeeping, winners/losers-list pairing, chip tracking, payouts and text alerts.

## Run it

**To put it online for the league, follow [DEPLOY.md](DEPLOY.md).**

To try it on your own computer:

```bash
cd chip_tournament
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
ADMIN_PASSWORD=choose-one python app.py
```

Open http://localhost:5000. Players use **Home**, **Board** and **Register**. The tournament director uses **Director** (password from `ADMIN_PASSWORD`; it's `admin` if you don't set one, which is only allowed when running locally).

Run the tests with `python -m pytest`.

## Pages

New director? Read the **Director's guide** in the app (`/guide`, linked from the Director page). It covers tournament night step by step and can be printed.

| Page | Who | What it does |
|---|---|---|
| `/` | everyone | Tournament details, venue and map link, how it works, pot, chip value, tables in play, winners/losers lists, standings |
| `/board` | TV at the venue | Same live info; refreshes every 15 seconds |
| `/register` | teams | Register a team of two partners: names, skill levels, cell numbers. Shows the combined skill and starting chips as you type. Gives (and texts) a 4-digit team PIN |
| `/team/<id>` | a team | Status, chips, where to play now, what its chips would cash in for, results. With its PIN, the team checks in at its table and keeps score here |
| `/team/<id>/roster` | a team (with PIN) | Update partners, skill levels (before the start), team name and cell numbers |
| `/admin/import` | director | Import teams from a CSV file, such as Google Form responses (before the start) |
| `/rules` | everyone | League rule sets (8-Ball, 9-Ball, Fast 8, Fast Saratoga…) as tabs, with a search box that finds and highlights matching rules across every game |
| `/admin/rules` | director | Add, edit, reorder and delete rule sets by pasting plain text |
| `/guide` | director | Step-by-step guide for tournament night (printable) |
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

   Combined skill can't be over **10 at registration**. If the same two partners' skill levels go up afterward, the team updates them on its roster page, up to **11**. **Swapping in a new partner counts as registering again**, so the new pair must be 10 or less, but the team keeps its spot in the field (or its place on the waiting list). A team over 11 can't be saved, and the tournament won't start until every team is 11 or less. Partners and skill levels lock once the tournament starts. The director can change the brackets and the registration cap in Settings until the start.

   **Field cap and waiting list.** The director sets the field cap (the most teams) in Settings. Once the field is full, new registrations go on a **waiting list** in order, and each team is texted its place. When a team in the field is removed before the start, or the director raises the cap, the next waiting team moves into the field and gets a text. Waiting-list teams don't play or pay into the pot unless they get a spot. A cap of 0 means no limit, and the cap can't be set below the number of teams already in the field.
2. **Pairing is rolling.** At the start every team goes on the winners list in random order. A team joining a list is compared with the top team on it, then the second, and so on, and is paired with the first one it **hasn't played yet**. Pairs go into a **Teams waiting on a table** queue, and free tables take pairs fewest rounds played first.
   - **No rematches unless it's the only option:** a team that has played everyone waiting on its list holds for the next arrival, even if a table is free. A rematch happens only when both teams have played everyone still in, or when nothing is playing or queued that could bring a new opponent. Forfeits don't count as having played. The table-call text reads: "New match: Table 3: Sharks with combined skill 9 currently holding 4 chips VS Rail Riders with combined skill 8 currently holding 3 chips. Good Luck!"
3. **Check-in at the table.** Teams check themselves in: on its own team page (after entering its PIN once), a team that has been called sees a **We're at the table** button. A team can only check itself in. The director's card also has an **Arrived** button for each team. When both teams have checked in, the match start time is set **2 minutes after the second arrival**, and both teams get a text with the start time. The card, the board and the team's page show the start time with a live countdown.
   - A team that hasn't checked in **5 minutes** after being called starts a **15-minute grace period**. It gets a text saying when it will forfeit, and its card turns red.
   - If it still hasn't checked in when the grace period ends, it **forfeits**: it loses the match and gives a chip back, and the opponent wins. If **neither** team checks in, both forfeit a chip and both go to the losers list.
   - A background timer checks every 15 seconds, so grace texts and forfeits happen on time even if no one is looking at the screen. The director page reloads itself when a deadline passes, and an automatic forfeit can be reversed with **Undo**.
   - Entering a score counts as both teams being at the table, so a match being played can't be forfeited by mistake.
   - The 2 / 5 / 15 minutes can be changed in Settings.
4. **Scoring.** Teams keep score on their team pages: either team at the table can tap `+`/`−` for either side, and every score a team enters is written to the activity log. When a team reaches the race, a pop-up says "[Team] won the match" with **Submit** and **Go back** (Go back removes the last game). Nothing is recorded until someone submits. After that only the director can change the result. The director can also score from the card, tap **Submit result**, or tap **Winner** to end a match early (the same pop-up confirms it).
   - **Fix result.** Under **Recent results** on the director page, **Fix result** swaps the winner of a finished match if the wrong team was recorded: chips, records and places in line are corrected, any pairing made since is redone, and both teams get a correction text. It works while neither team has started a later match and neither team's chips have changed since (for example with **Adjust**); otherwise use Undo or **Adjust**. After last call, a team brought back still gets only its last match. **End now** records any match that already reached the race before cashing everyone in.
5. **After a match:** the winner keeps its chips and goes to the bottom of the **winners list**. The loser gives one chip back to the main table and goes to the bottom of the **losers list**. A team with 0 chips is **eliminated** and leaves the rotation.
6. **Catch-up and Call next.** A team 2 or more matches behind the leader (late entry, an opponent cashing in) is tagged *catching up* and moves to the top of its list. If nobody else is waiting on its list, it plays the top team of the other list. The director can press **Call next** on a queued pair to send it to the next free table. Pressing it on a team still on a list pairs that team right away (a new opponent if one is waiting anywhere, otherwise a rematch). The 2-match gap is a setting.
   - **Long matches:** each match card shows how long it has been playing. It turns amber after the expected length (15 min) and red 20 minutes after the start, and a "Running long" banner lists those tables. Both times are settings.
7. **Pot and chip value.** Pot = entry fee × teams + added money. Chip value = pot ÷ chips still in play, so it goes up every time a chip goes back to the table.
8. **Cash-in.** When the director opens cash-in, every team gets a text with the chip value and what its chips are worth. A team can cash in at the desk (the director clicks **Cash in**) for chips × chip value, or keep playing. Cashing in doesn't change what the other teams' chips are worth.
9. **Last call** tells every team that its next match is its last. When that match ends, both teams are cashed in automatically: the loser gives its chip back first, then each team is paid its chips × the chip value.
   - A match already **on a table** when last call happens finishes as normal. Those teams then play one more match, which is their last.
   - Teams **waiting** in line play one more match, then cash in.
   - Pairing keeps working the same way (winners vs winners, losers vs losers) until every team has played its last match. A team left with no opponent is cashed in right away.
   - Every team gets a text at last call, and the director's team list shows "last match" or "2 matches left" for each team. Registration closes at last call.
   - **End now — cash everyone out** is a separate emergency button (for example, if the bar is closing). It stops every match and cashes everyone in at once. The last team paid gets whatever is left in the pot, so every cent is paid out.
10. If only one team has chips left, it wins the rest of the pot automatically.

### Decisions the rules didn't specify (easy to change in `chips/engine.py`)

- **Winners vs losers ("cross") matches:** only when teams are stuck on opposite lists with nothing else playing or queued, or for a team catching up. There are no byes.
- **Last two teams both no-show** with 1 chip each: instead of both forfeiting their last chip (which would leave nobody to pay), both are cashed in at the current chip value.
- **Cashing in during a match:** not allowed once a game has been scored. If the match hasn't started, it's cancelled and the opponent goes back to the front of the line.
- **Late entries and money settings lock together**, once every team still in has completed a match. Until then:
  - **Late entries:** teams can register late (or be restored from no-show). They pay the entry fee, get chips for their skill level and join the winners list.
  - **Money settings:** the entry fee, added money, chip brackets and registration cap can still change. After the start, a change adjusts the pot (fee × teams that have paid) and every team's chips (by the difference in starting chips), and each adjustment is logged. The fee and added money can't change once any team has cashed in.
  - **At the lock:** registration closes, and teams still on the waiting list get a text that the field is set.
- **Table-call texts** start with "New match:" and no league name. Every other text starts with the sender name.
- **Skill updates** are entered by the team on its roster page (or by the director) before the start, and each change is written to the activity log (e.g. "Partner swap, now Ann & Cy. Combined skill 9 → 10").
- **What counts as a swap:** the app compares partner names (ignoring capitals and extra spaces). Fixing a typo in a partner's name also counts as a swap, so the director should fix name typos for a team that's at 11.
- **Cash-in and last-call times** show to players and on the director's buttons, but the director clicks the buttons.
- **"Started" at last call** means the match has been called to a table. Matches still waiting for a table count as not started.

## Registering without a partner

A player who doesn't have a partner yet can register alone to **hold a real spot** in the field (or on the waiting list if it's full). They tick **"I don't have a partner yet"** on the Register page.

- **Skill check.** Skill levels go from 1 to 9. A player registering alone can be up to the registration cap minus 1 (9 with a cap of 10), so a partner can still fit.
- **Adding the partner.** The team adds its partner on its roster page with its PIN. That counts as a new registration, so the pair must be at or under the registration cap. The team keeps its spot and gets a text with its starting chips.
- **Deadline: 24 hours before the start** (from the date and start time in Settings). At the deadline, teams in the field that still have no partner move to the end of the waiting list and get a text. If the director presses **Undo** on that move, it stays undone: the director page shows "Partner deadline paused" with a **Move them to the waiting list now** button, and changing the date or start time turns the automatic move back on. Saving Settings with a date that puts the deadline in the past shows a warning first.
- **Check-in.** A team without a partner can't be checked in at the desk until it has one, and a partnerless no-show can't be restored into a running tournament.
- **Fixing a name.** On the roster page, editing a partner's name shows "Same person: I'm only fixing the spelling". Tick it to keep that player's cell number and text alerts (otherwise a new name counts as a new partner). Their spots go to the waiting list in order. Teams without a partner can't move into the field after the deadline. If they add a partner later, they move in when a spot opens. Registering alone closes at the deadline.
- **Public "Looking for a partner" list.** A player registering alone can tick a box to be listed on the Register page. The list shows only their name and skill level (never phone numbers). It disappears once they have a partner.
- **Director.** The director page shows the partner deadline and which teams still need a partner. A **Pair** button next to each one puts two players who registered alone on one team. The team with the better spot is kept, and the other spot opens up for the waiting list. The tournament can't start while a team in the field has no partner.
- **Google Form import.** A row with Partner 1 filled in and Partner 2 left blank (name, skill and cell) is imported as a player holding a spot.

## Event check-in and no-shows

Set **Check in at the desk by** (e.g. 6:30 PM) in Settings. It shows on the home page, the registration page and each team's page. On tournament day:
- Press **Check in** next to each team as it arrives. The time is recorded, and the director page counts how many are checked in. To find a team fast, type part of its name, a player's name or the last 4 digits of a cell in the search box above the team list. Tick **Not checked in only** to see who's still missing. The search stays in place after each check-in, so you can type the next team.
- The buttons across the top of the director page (Summary, Tables, Teams & chips, Texts & log, Settings, New tournament) jump straight to each section and stay on screen as you scroll.
- Press **No-show** for a team that doesn't come, or **Mark all not checked in as no-shows** once check-in closes. The team is taken out of the field and texted, and the next team on the waiting list moves in (and is texted). **Restore** undoes a no-show. If a no-show team walks in after the start, **Arrived late: restore** adds it as a late entry (it pays the fee and gets its chips), up until last call.
- **Start tournament** stays disabled until every team in the field is checked in or marked a no-show. That requirement can be turned off in Settings.
- No-shows don't appear on public pages and don't pay into the pot.

## Importing teams from a Google Form

On the director page (before the start), click **Import teams from a spreadsheet (CSV)**.

1. In Google Forms, open **Responses → View in Sheets**, then in the sheet choose **File → Download → Comma-separated values (.csv)**. Upload that file, or paste the rows.
2. The app guesses which column is the team name, each partner's name, skill level and cell. Fix any guesses that are wrong.
   - A team name column is optional. Without one, teams are named after their partners.
   - If the form only asked for a combined skill, map that instead. It's split between the partners, and teams can correct it on their roster page.
   - A row with no partner 2 (name, skill and cell all blank) is a player registering alone. It holds a spot until a partner is added.
3. Check the preview, which shows every row's result before anything is saved: added (with its chips), waiting list, or skipped and why.

Rows are added top to bottom (sign-up order), so the earliest teams get the field spots and the rest fill the waiting list in order. Every row goes through the normal registration rules. Teams already registered are skipped, so importing an updated sheet again only adds the new sign-ups. You can choose whether each imported team gets its PIN by text.

## Rules page

Players open **Rules** in the menu: each game is a tab, and the search box looks through every rule set at once, showing only the matching rules with the words highlighted.

Directors edit the rules from the director page (**Edit rules**). Paste each game's rules as plain text. Each line becomes a paragraph, and a few simple marks add structure: `# Heading`, `## Smaller heading`, `- bullet`, `1. numbered step`, `**bold**`. Rules are saved in `rules.json` in the data folder, separate from the tournament, so starting a new tournament keeps them.

## Header

The APA Pool Leagues – First Coast logo sits in the top-left corner and opens the league website (https://firstcoast.poolplayers.com/) in a new tab. Next to it, the house icon and event name go back to this app's home page. The logo file is `static/img/apa-first-coast-logo.png` (white on transparent).

## Themes

Each tournament has its own color theme: **Director → Settings → Theme**. Picking one previews it on the director page right away; **Save settings** applies it to every page, including the board. Everyday themes: Electric blue (default), Neutral, Mustard yellow and Neon green. Holiday themes: New Year's, Valentine's Day, Mardi Gras, St. Patrick's Day, Easter / spring, Patriotic (Memorial Day, July 4th, Labor Day, Veterans Day), Halloween, Thanksgiving / fall, Christmas and Hanukkah. The default background picks up the theme's color; an uploaded venue photo keeps its own colors. Themes are defined as `[data-theme="..."]` blocks at the end of `static/style.css` and listed in `chips/themes.py`.

## Link previews and icon

When someone shares a link to the app (in a text, Facebook, etc.), the preview shows the **Control Table** image (`static/img/share.jpg`, 1200×630) with the event name, date and venue. The browser-tab icon is a blue **CT** tile (`static/favicon.ico`, also served at `/favicon.ico`, plus `img/favicon-32.png` and `img/favicon.png`). The phone home-screen icon (`apple-touch-icon.png`) is a square crop of the Control Table artwork. To change the preview image, replace `share.jpg` with another 1200×630 image.

## Background photo

Every page shows a faded photo behind the content. To use a photo of your own venue, go to the director page, **Settings → Background photo**, and upload a wide JPG or PNG (up to 10 MB). It's stored with the tournament data (not in the code), and **Use the default photo** switches back. The default (`static/img/billiard-room.jpg`) is the Control Table artwork, blurred into a soft blue glow.

## Privacy

Phone numbers appear only on director pages. On a team's roster page the cell boxes are blank and show just the last 4 digits; leaving a box blank keeps the number on file.

## Chip tracking

Every chip movement (starting chips, losses, cash-ins, manual adjustments) goes in the **chip ledger** with a reason and the chip value afterward. The director page shows a running check: chips issued = chips in play + chips returned + chips cashed in, and money collected = paid out + pot left. **Adjust** fixes a mistake (it needs a reason), and **Undo** reverses the last action of any kind (up to 40 steps). It asks first, and if anything else happened in the meantime (a player scoring, for example) it does nothing and shows the new latest action instead.

## Text alerts

Texts go only to players who agreed to them, each for their own number: the person registering ticks the text-alert box on the registration form, and a partner turns on alerts on the team page with the team PIN (the director can't opt anyone in). Changing a cell number turns alerts off for that number until it's opted in again. Registering also requires agreeing to the [Terms & Conditions](/terms) and [Privacy Policy](/privacy), which name Maximum English LLC dba First Coast APA (set `LEGAL_NAME`, `BRAND_NAME`, `SUPPORT_EMAIL` and `SUPPORT_PHONE` to change them). The first text each player gets is a welcome with the team PIN, a link to the team page, message frequency, rates, and HELP/STOP instructions.

A team gets a text when it registers (with its PIN, plus the "Questions?" phone if one is set), is called to a table, wins or loses a match, is eliminated, or cashes in, and when cash-in opens or last call happens. Every text starts with the sender name if one is set. The director can also text every team from the dashboard.

To send real texts, create a [Twilio](https://www.twilio.com) account and set:

```bash
export TWILIO_ACCOUNT_SID=ACxxxxxxxx
export TWILIO_AUTH_TOKEN=xxxxxxxx
export TWILIO_FROM_NUMBER=+15551234567
```

Without these, texts are only logged (see **Recent texts** on the director page), so you can test without paying for SMS. US carriers require A2P 10DLC registration for business texting through Twilio.

## Data

Everything is saved in `chip_tournament/data/` (`tournament.json`, the undo history and `texts.log`). Set `CHIPS_DATA_DIR` to store the data somewhere else.

To start the next tournament, type RESET under **New tournament** on the director page and choose:

- **Archive and start over** keeps the old tournament and its texts. It's listed under **Past tournaments**, where you can delete it later.
- **Delete permanently and start over** erases the old tournament, its undo history and its texts right away.

## Security

- **Phone numbers** appear only on director pages. A team editing its roster sees only the last 4 digits of the numbers on file.
- **Wrong guesses are limited.** After 5 wrong director passwords from one device, that device waits 15 minutes. Team PINs allow 10 wrong tries per device and 20 per team in 15 minutes; once a team is locked, only devices that guessed wrong for it are blocked, so strangers can't lock the real players out.
- **View-only login (optional).** Set `VIEWER_PASSWORD` to give someone (e.g. the league office) a director login that can look but not change anything. It hides phone numbers, team PINs and text messages.
- **On the internet** (`CHIPS_PRODUCTION=1`), the app refuses to start without a strong director password, and login cookies are only sent over HTTPS.

## Code layout

- `chips/engine.py`: all tournament rules (no web code), tested in `tests/test_engine.py`
- `chips/storage.py`: JSON save/load and undo
- `chips/alerts.py`: Twilio texts with a log-only fallback
- `app.py`: Flask routes, with `templates/` and `static/` for the pages
