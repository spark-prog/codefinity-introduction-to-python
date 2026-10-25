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
| `/rep` | A division rep: the request board |
| `/office`, `/office/requests`, `/office/signups`, `/office/setup` | The league office: master calendar, request board, player sign-ups, setup |

## Settings

| Variable | |
|---|---|
| `OFFICE_PASSWORD` | League office password (required, 8+ characters, when `LEAGUE_PRODUCTION=1`) |
| `VIEWER_PASSWORD` | Optional view-only office login |
| `LEAGUE_DATA_DIR` | Where `league.json` is kept (default `data/`) |
| `SECRET_KEY` | Signs login cookies (default: generated once into the data folder) |
| `LEAGUE_PRODUCTION=1` | Refuses the default password and sends cookies over HTTPS only |

In production: `gunicorn --workers 1 --threads 8 "app:create_app()"` (one worker: `league.json` is written by one process).

## Code layout

- `league/core.py`: the league data and rules (no web code)
- `views.py`: the pages; `app.py`: settings, office login, and wiring
- `templates/`, `static/style.css`: pages and styles (light and dark)
- `tests/test_league.py`: each role's workflow end to end
