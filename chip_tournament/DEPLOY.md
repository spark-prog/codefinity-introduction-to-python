# Putting Control Table online

The league needs the app on the internet so teams can register from their phones days ahead and check the board on tournament night. This guide uses **Railway**, which runs the included `Dockerfile` straight from GitHub and offers a persistent volume. Any host that can run a Docker image with a persistent disk works the same way (Render, Fly.io, a small VPS).

## What the host must provide

| Need | Why |
|---|---|
| A **persistent volume** mounted at `/data` | Every tournament, its undo history and text log are saved there. Without it, a restart or redeploy wipes them. |
| **Always on** (no sleeping) | The check-in clock gives grace periods and records forfeits even when nobody has a page open. |
| **One instance** | The app keeps the tournament in one process. Don't scale it to more than one copy. |
| HTTPS | Railway and similar hosts provide it automatically. |

## Railway, step by step

Cost: the Hobby plan is about $5/month, and that includes usage credit that should cover this small app and its volume. Check [Railway's pricing page](https://railway.com/pricing) for current numbers.

1. **Get the code onto the branch you'll deploy.** The app lives in the `chip_tournament/` folder of this repository. Merge it into `main` (or deploy from the feature branch).
2. **Create the service.** Sign up at railway.com with GitHub, choose *New Project → Deploy from GitHub repo*, and pick this repository.
3. **Point it at the app folder.** In the service's *Settings*, set **Root Directory** to `chip_tournament` and pick the branch to deploy. Railway finds the `Dockerfile` and builds it.
4. **Add a volume.** Add a volume to the service with mount path **`/data`**.
5. **Set the variables** in the service's *Variables* tab:

   | Variable | Value |
   |---|---|
   | `ADMIN_PASSWORD` | The director password, at least 8 characters. The app refuses to start without it. |
   | `VIEWER_PASSWORD` | Optional. A second, **view-only** director login (at least 8 characters, different from the director password), e.g. for the league office to look around. It sees the director page and guide but can't change anything, and phone numbers, PINs and text messages are hidden. |
   | `TZ` | Your venue's time zone, e.g. `America/Chicago`, `America/New_York`, `America/Denver`, `America/Los_Angeles`. Without it, every time shows in UTC. |
   | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | Optional, for real texts (see below). |
   | `SECRET_KEY` | Recommended. A long random string that signs logins. Without it the app makes one and keeps it in the data volume, which works as long as the volume does; set it so a lost or replaced volume doesn't log everyone out. Never share it. |
   | `SITE_URL` | Recommended. The public address used in links inside texts and emails, e.g. `https://controltable.app`. Without it, links use whatever address the request came in on. |
   | `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | The Gmail (Google Workspace) account that sends email: player login codes, partner confirmations, waiting-list offers and emailed backups. The password is a Google **app password**, not the account password. |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | The whole service-account key file (JSON), pasted in. Used to read the player list from the restricted Google Sheet and to save backups to Google Drive. Share the sheet and the backup folder with the service account's email address. |
   | `BACKUP_DRIVE_FOLDER_ID` | Optional. The Google Drive folder for nightly backups (the ID or the folder's link). It must be in a **Shared drive** the service account belongs to; if Drive fails or this isn't set, backups are emailed instead. |
   | `BACKUP_EMAIL` | Optional. Where emailed backups go. Defaults to `GMAIL_ADDRESS`. |
   | `TRUSTED_IP_HEADER` | Optional. When the site sits behind a CDN such as Cloudflare, the header that carries the real visitor's address (`CF-Connecting-IP` for Cloudflare), so login limits count each visitor separately. Check what your host sends at `/admin/whoami` (director only) before setting it, and only set it if every request really comes through that CDN. |
   | `FLASK_DEBUG` | Local testing only (`python app.py`). Never set it on the live server. |

   Railway sets `PORT` itself. `CHIPS_DATA_DIR=/data` is already in the Dockerfile.
6. **Give it a web address.** Under *Settings → Networking*, generate a domain (e.g. `your-league.up.railway.app`) or connect your own.
7. **Health check (optional).** Set the health check path to `/healthz`.
8. **Check it.**
   - Open the address and the home page loads.
   - Log in at `/admin/login` with your password.
   - In the *Tournament details & settings* box, the **clock check** line should show your venue's current time.
   - Register a practice team, then **redeploy** (or restart) the service and confirm the team is still there. That proves the volume works. Remove the practice team.

## Text messages (Twilio)

1. Create a Twilio account and buy a phone number that can send SMS.
2. **US carriers require registration** before a business number can text people: A2P 10DLC for a regular number, or toll-free verification for a toll-free number. Approval can take days to a few weeks, so **start early**. Until it's approved, texts may be blocked.
3. Put the Account SID, Auth Token and your Twilio number (e.g. `+15551234567`) into the three variables above, then redeploy.
4. Register a practice team with your own cell. The text should arrive, and *Text alerts → Recent texts* on the director page should show `sent (201)`.

Until Twilio is set up, everything else works, and texts are only logged under *Recent texts*.

## Backups

Each tournament has its own folder, `/data/events/<id>/`, with `tournament.json`, `history.json` (undo) and `texts.log`; `/data/events.json` lists them. Rules, the background photo and the login key are shared at the top of `/data`. A volume from before Control Table hosted several tournaments is moved into `/data/events/` automatically on the first start. Railway can snapshot volumes, and you can also copy the file down after each tournament. **Archive and start over** keeps old tournaments in `/data` as `tournament-<date>.json` (with their texts). They're listed under **Past tournaments** on the director page, where you can delete them permanently; **Delete permanently and start over** skips the archive.

## Updating the app later

Push changes to the deployed branch and Railway rebuilds automatically. The tournament data on the volume isn't touched. **Avoid deploying during a tournament:** a deploy restarts the app for a few seconds, and anyone pressing a button at that moment would see an error.

## Running it on a laptop instead

This works for a one-off night on the venue's Wi-Fi, but teams can't register from home and phones must be on the same network:

```bash
cd chip_tournament
pip install -r requirements.txt
ADMIN_PASSWORD=your-password TZ=America/Chicago python app.py
```

Then share `http://<laptop's IP address>:5000` (shown when the app starts). Don't let the laptop sleep.
