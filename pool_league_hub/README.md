# Pool League Hub

The league's calendar, host locations, and players and teams finding each other. A separate app from Control Table (`../chip_tournament`), which runs tournament nights.

**What it does and why:** see [PLAN.md](PLAN.md), which lays out the roles, workflows, privacy rules, and what's next.

## Run it

```bash
cd pool_league_hub
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
OFFICE_PASSWORD=choose-one python app.py
```

Open http://localhost:5000. The league office signs in under **League office** (password from `OFFICE_PASSWORD`; it's `office` if you don't set one, which is only allowed locally). Start in **Setup**: add the host locations with their league nights, and the division reps. Give each its PIN from the Setup page.

Run the tests with `python -m pytest`.

## Pages

| Page | Who |
|---|---|
| `/` | Everyone: host locations, what's coming up, links to find a team or players |
| `/loc/<id>` | Everyone: a location's hours, specials, rates, menu, league nights, contact, upcoming events, teams with open spots, and **Play out of here** |
| `/players/new`, `/me` | Players: post a listing; then the listing and inbox |
| `/teams/new`, `/captain` | Team captains: post a team; then players looking and invites sent |
| `/signin` | Hosts, reps, players and captains |
| `/host` | A host location: schedule, blackouts, event requests, its public page |
| `/rep`, `/rep/calendar` | A division rep: the request board, and the master calendar (view only) |
| `/office`, `/office/requests`, `/office/signups`, `/office/setup` | The league office: master calendar, request board, player sign-ups, setup (including Google Calendar sync) |
| `/feed/<link>.ics` | A calendar to subscribe to: each host's schedule (link on its page), or the whole league (link on the calendar and in Setup) |

## Settings

| Variable | |
|---|---|
| `OFFICE_PASSWORD` | League office password (required, 8+ characters, when `LEAGUE_PRODUCTION=1`) |
| `VIEWER_PASSWORD` | Optional view-only office login |
| `LEAGUE_DATA_DIR` | Where `league.json` is kept (default `data/`) |
| `SECRET_KEY` | Signs login cookies (default: generated once into the data folder) |
| `LEAGUE_PRODUCTION=1` | Refuses the default password and sends cookies over HTTPS only |
| `LEAGUE_TIMEZONE` | Time zone for event times (default `America/New_York`) |
| `GOOGLE_CALENDAR_ID` | The league's Google Calendar to sync with (see below) |
| `GOOGLE_SERVICE_ACCOUNT_FILE` or `GOOGLE_SERVICE_ACCOUNT_JSON` | The service account key the hub signs in to Google with (a file path, or the JSON itself) |
| `GOOGLE_SYNC_MINUTES` | How often to sync when nothing has changed (default 5) |

In production: `gunicorn --workers 1 --threads 8 "app:create_app()"` (one worker: `league.json` is written by one process).

## Google Calendar

The calendar syncs both ways with the league's Google Calendar:

- **Out to Google:** events, each location's league nights (one weekly repeating event per night, between the season dates) and host blackouts (all-day). Changes go out within seconds of being made in the hub, and on every sync.
- **In from Google:** events added in Google come into the hub as "From Google Calendar" (at a host location when the Google event's location or title names one, otherwise league-wide); the office can change their type. Changes made in Google to an event's title, date, time or notes come back, and deleting an event in Google deletes it in the hub (a request it came from goes back to its rep).
- **Who wins:** if an event changed in both places since the last sync, the hub's version wins. League nights and blackouts belong to the hub (the office and the hosts): changes to them in Google are put back. The exception is moving or cancelling a single week of a league night in Google (a holiday), which stays as Google has it.
- Events in Google more than 30 days old aren't imported.

Setup, one time:

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project, enable the **Google Calendar API**, and create a **service account**. Under the service account's **Keys**, add a JSON key and download it.
2. In Google Calendar, open the league calendar's **Settings and sharing**. Under **Share with specific people**, add the service account's email (it ends in `iam.gserviceaccount.com`) with **Make changes to events**. Under **Integrate calendar**, copy the **Calendar ID**.
3. Set `GOOGLE_CALENDAR_ID` to that ID and `GOOGLE_SERVICE_ACCOUNT_FILE` to the key file's path (or put the key's contents in `GOOGLE_SERVICE_ACCOUNT_JSON`), then restart. **Setup → Google Calendar** shows the last sync, anything waiting to send, and any error, with a **Sync now** button.

Keep the key secret: anyone with it can change the league calendar.

**Subscribe links (no setup needed).** Each host's page has a private link to its own schedule (its league nights, events, league-wide dates and blackouts), and the calendar page has one for the whole league. In Google Calendar: **Other calendars → + → From URL**. These are read-only, and Google refreshes them every few hours.

## Code layout

- `league/core.py`: the league data and rules (no web code)
- `league/gcal.py`: Google Calendar sync; `league/ics.py`: the subscribe-link calendars
- `views.py`: the pages; `app.py`: settings, office login, and wiring
- `templates/`, `static/style.css`: pages and styles (light and dark)
- `tests/test_league.py`: each role's workflow end to end; `tests/test_gcal.py`: sync against a fake Google Calendar, the feeds, the reps' calendar
