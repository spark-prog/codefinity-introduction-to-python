# Control Table — notes for Claude

Control Table (formerly Chip Tournament Manager) is a Flask web app for running billiards (pool) tournaments for APA Pool Leagues – First Coast. It hosts several tournaments at once: bracket tournaments (single, double, modified single elimination, round robin) and the league chip tournament.

The owner is learning to code and builds this by vibe coding. Explain changes in plain words, keep each change small, and say what to click to check it worked.

## Where things are

Everything lives in `chip_tournament/`. The `variables_and_types/` folder is an unrelated Python course exercise; leave it alone.

| File | What it holds |
|---|---|
| `chips/engine.py` | All chip tournament rules. No web code. |
| `chips/bracket.py` | All bracket tournament rules. No web code. |
| `chips/hub.py` | The list of tournaments (one folder each) and moving old data in |
| `chips/storage.py` | JSON save/load and undo (`Store.change`) |
| `chips/alerts.py` | Twilio texts, with a log-only fallback when Twilio isn't set up |
| `chips/importer.py` | CSV / Google Form import |
| `chips/rules.py`, `chips/themes.py`, `chips/throttle.py` | Rules page, color themes, wrong-guess limits |
| `app.py` | Flask routes: the hub, chip tournament pages, the check-in clock |
| `bracket_web.py` | Bracket tournament pages |
| `templates/`, `static/style.css` | Jinja pages (files starting `_` are partials; `b_*` are bracket pages) and styles. Themes are `[data-theme="..."]` blocks at the end of `style.css`. |
| `tests/` | pytest tests, one file per module |
| `README.md` | The full feature and rules description. Read it before changing behavior. |
| `DEPLOY.md` | Hosting guide. The app is hosted on **Render**; this guide still describes Railway and is out of date. |

## Run and test

```bash
cd chip_tournament
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
ADMIN_PASSWORD=choose-one python app.py   # http://localhost:5000, director login at /admin
python -m pytest                          # run before every commit
```

## How the code works (follow these patterns)

- **Rules stay out of web code.** Tournament logic goes in `chips/engine.py` or `chips/bracket.py`; routes only call it. A broken rule raises `TournamentError` with a message a player or director can read, and the page shows it.
- **Every change goes through `act(label, fn)` in `app.py`**, which wraps `store.change(label)`: load, change, save, keep an undo step. If anything raises, nothing is saved. Don't write the JSON files directly. The label is what Undo shows, so make it readable.
- **Texts:** rules code adds messages to `t.outbox`; `act` sends them after saving. Only text players who opted in.
- **Data** is JSON in `chip_tournament/data/` (or `CHIPS_DATA_DIR`): `events.json` plus `events/<id>/tournament.json`, `history.json`, `texts.log`. Old saved tournaments must still load, so give new fields a default when reading.
- **One process only.** The background check-in clock and the tournament files live in a single worker. Never suggest more gunicorn workers or scaling to more copies.
- **Keep old links working:** `/team/<id>`, `/board` and `/register` redirect to the right tournament. Settings keep their `CHIPS_*` names.
- **Privacy:** phone numbers and PINs show only on director pages, never on public pages, the board, or the view-only login.

## Working rules

- Read the relevant part of `README.md` before changing how a rule works, and update `README.md` (and the in-app guide at `templates/guide.html` if directors would notice) in the same change.
- Add or update tests for any rule change. `tests/test_bracket.py` plays out every format for many field sizes; keep it passing.
- Pages must work on phones (players score from their phones at the table) and on the TV board.
- Write player-facing text plainly and briefly; no jargon.
- The live app is hosted on **Render**. Tournament data must sit on a persistent disk (the `Dockerfile` points `CHIPS_DATA_DIR` at `/data`), or a redeploy wipes it. Pushing to the deployed branch redeploys it and restarts it for a few seconds. Don't push during a tournament night, and work on a branch with a pull request for anything bigger than a small fix.
- Never commit `data/`, passwords, or Twilio keys.
