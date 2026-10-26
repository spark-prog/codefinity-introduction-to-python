# Pool League Hub: plan and workflows

A separate app from Control Table (the tournament runner). Control Table runs a tournament night; the Pool League Hub runs the league around it: the calendar, the host locations, and players and teams finding each other.

This plan comes from the Pool League Hub prototype (a single-page demo with sample data and a "Viewing as" role switcher), which grew out of a conversation about host location schedules, Google Calendar, and division reps. The first build in this folder turns that prototype into a real app: data saved on the server, and each role signing in instead of switching roles.

Decisions since the prototype:
- **Google Calendar: two-way sync** with the league's Google Calendar (workflow 8).
- **Division reps** claim host requests *and* see the full master calendar (view only).
- **Host schedules in their own calendars:** each host gets a private subscribe link to its schedule.

## Who uses it

| Role | Signs in with | Main job |
|---|---|---|
| League office | Office password | Owns the master calendar, confirms events, connects players with teams, sets up locations and reps |
| Host location | 6-digit host PIN (from the office) | Sees what's planned at its location, blocks dates, asks for weekend events, keeps its public page current |
| Division rep | 6-digit rep PIN (from the office) | Claims host requests to run them; sees the full master calendar |
| Player | Phone or email + 4-digit PIN | Posts a "looking for a team" listing, reads team invites |
| Team captain | Phone or email + 4-digit PIN | Posts open spots, browses players, sends invites |
| Anyone | — | Browses host locations and what's coming up; asks to play out of a location |

## Event types

Each type is marked with a pool-ball color everywhere it appears.

| Type | Ball | Where it comes from |
|---|---|---|
| League-wide date (general calendar) | 1, yellow | Office; shows at every location (captains' meetings, sign-up deadlines, playoffs, session breaks) |
| League night | 2, blue | Repeats weekly per location (set in Setup), between the season dates |
| Singles tournament | 3, red | Host request → rep claim → office confirm, or added by the office |
| Doubles tournament | 4, purple | Same |
| Singles board | 5, orange | Same |
| Blackout | 8, black | Host marks days unavailable |
| From Google Calendar | 6, green | Added directly in the league's Google Calendar; the office can change its type |

## Workflows

### 1. Weekend event: host request → rep claim → office calendar
1. **Host** posts a request: type (singles board, doubles tournament, singles tournament), preferred date, optional backup date, notes for the rep (added money, tables, food, start time). A date the host has blacked out is refused.
2. The request goes on the **request board** as *open*.
3. A **division rep** claims it (*claimed*). Only that rep can release it back to *open*.
4. The **league office** presses **Add to calendar**: the event form is filled in from the request (title, location, date, rep as director, notes). Saving puts it on the calendar and marks the request *scheduled*. The office can also **decline** a request that isn't on the calendar yet.
5. Deleting the event later puts the request back to *claimed* (still with its rep).
6. If a host blacks out a requested date afterward, the board card warns about it.

### 2. Blackouts and conflicts
1. **Host** adds a blackout (one day or a range, from today on) with a reason the office sees.
2. If events are already scheduled on those days, the host is told how many, and the office sees each one in a **Blackout conflicts** banner on the calendar.
3. The office can't add a location event on a blackout day (league-wide dates aren't blocked).
4. Blackout days are hatched on the month calendar and on the host's agenda.

### 3. Office master calendar
- Month view of every location, filterable by location; click a day for its details.
- Stats: league nights and weekend events in the next 30 days, open host requests, blackout conflicts.
- Add an event (type, location or league-wide, title, date, start time, director/rep, notes for the host), delete an event.

- **Edit** an event (the same blackout check applies), and the request it came from keeps its new date.

### 4. Host location
- **My schedule**: an agenda grouped by week for the next 30 or 60 days: league nights, league-wide dates, scheduled events, its own blackouts and pending requests. Stats for each.
- **Blackout dates**: add and remove.
- **Request an event**: the form above, and the status of its requests.
- **My location page**: name, area, address, phone, email, tables, hours, specials, menu highlights, table rates. Lines like `Tue: $3 domestics` show as a label and a value. League nights are set by the office.
- **Put this schedule in your Google Calendar**: a private subscribe link to the host's own schedule (league nights, its events, league-wide dates, its blackouts). Read-only; Google refreshes it every few hours.

### 4b. Division rep
- **Request board**: claim open host requests, release ones they claimed.
- **Calendar**: the master calendar, every location, view only (the office makes the changes). The whole-league subscribe link is on it.

### 5. Player looking for a team
- **Play out of here** (from any location page, no account): name, phone or email, formats, nights, a note. It goes privately to the office's **Player sign-ups**, which shows that location's teams with open spots. The office marks it *connected* once it has put them in touch.
- **Post a listing**: first name, last initial, area, level (beginner / intermediate / advanced), formats (8-Ball, 9-Ball, Scotch Doubles), nights, preferred locations, about, and whether captains can see it. Gives a 4-digit PIN.
- **Inbox**: invites from teams, with the team's night, location, format, open spots, message and the captain's contact. Mark as read, or "Not interested".

### 6. Team captain looking for players
- **Post a team**: name, location, night, format, open spots, captain name and phone or email. Gives a 4-digit PIN. A team with 0 open spots is off the lists.
- **Players looking**: visible listings, best fit first (one point each for the team's night, location and format: "Great fit" at 3, "Good fit" at 2), filterable by night and format.
- **Send our team info**: a message (pre-filled) that shows the player the captain's contact. One invite per player per team.
- **Invites sent**: not opened yet / seen / player passed.

### 7. League office setup
- Hub name and season dates (league nights repeat between them; blank means no end).
- Host locations: add, edit, delete (with their events, blackouts, requests, sign-ups and teams); weekly league nights written one per line, e.g. `Tue 7:00 PM 8-Ball Open · Division 1`; host PIN shown, with **New PIN**.
- Division reps: add with division, remove (their claimed requests go back on the board), **New PIN**.
- Player listings and teams: remove, or give a new PIN to someone who forgot theirs.

### 8. Google Calendar two-way sync
- **Out to Google** (the hub owns these): events; each location's league nights as one weekly repeating event per night, between the season dates; host blackouts as all-day events. Each carries its hub id in Google's private extended properties.
- **In from Google**: new events become "From Google Calendar" events, at a host location when the Google event's location or title names exactly one host, otherwise league-wide. Changes to an event's title, date, time or notes come back. Deleted in Google → deleted in the hub, and a request it came from goes back to its rep. Events more than 30 days old aren't imported.
- **Conflicts**: if an event changed in both places since the last sync, the hub wins. League nights and blackouts changed or deleted in Google are put back, except a single week of a league night moved or cancelled there (a holiday), which stays as Google has it.
- **When**: every few minutes, and within seconds of any change in the hub. Setup shows the last sync, what's waiting, and any error, with **Sync now**.
- **How it signs in**: a Google service account that the league calendar is shared with ("Make changes to events"). Event times use the league's time zone (default America/New_York). The hub has only start times, so timed events last 5 hours in Google and in the subscribe links (a 1:00 PM tournament shows as 1:00–6:00 PM); an end time changed in Google isn't kept.

## Privacy rules
- Captains never see a player's phone or email. A player sees a captain's contact only on an invite to them. The player decides whether to reach out.
- Sign-up contacts are seen only by the league office.
- Host and rep PINs are shown only to the office (not to the view-only login).
- A new PIN signs out whoever was using the old one.
- Wrong PINs: 10 per device per 15 minutes; wrong office passwords: 5.

## Built in this first version
Everything above, in `pool_league_hub/`: Flask, with the data in `data/league.json`. Rules are in `league/core.py` (no web code), Google Calendar sync in `league/gcal.py`, subscribe links in `league/ics.py`, pages in `views.py` and `templates/`, tests in `tests/`.

The sync is tested against a fake Google Calendar that behaves like the real API. It hasn't run against the league's real calendar yet: that needs the service account set up (README → Google Calendar).

## Not built yet (from the prototype or natural next steps)
- **Player photos** on listings (the prototype allowed an optional photo, shrunk to 200×200).
- **Notifications**: texting or emailing a player when a team invites them, the office when a sign-up or request arrives, a host when its request is confirmed. Needs consent handling like Control Table's texts.
- **Team recruiting import**: the prototype's teams came from a Google Form; a CSV import for those responses.
- **Skipping a single league night** in the hub itself (it can be done in Google Calendar today) instead of only flagging the blackout conflict.
- **Per-host Google calendars**: today each host subscribes to a read-only link; the sync could also write each host's schedule into a separate Google calendar shared with that host.
- **Event end times**: events are 5 hours long in Google; an end time field in the hub.
- **Divisions as data** (reps and league nights name a division as text today).
- **Links to Control Table**: an event could link to its Control Table tournament page for registration, without the two apps sharing code.
- **Deployment** (Dockerfile and hosting guide, like Control Table's DEPLOY.md).

## Open questions
- Should the public see the full month calendar, or only "coming up" and each location's page (as now)?
- Should a host be able to suggest changes to its league nights, or is that office-only (as now)?
- Should a rep be able to add or edit events on the calendar (today it's view only), or only for their division?
- Should events added in Google default to league-wide, or should the office review them before hosts see them?
- Separate repository, or keep it in this one alongside Control Table?
