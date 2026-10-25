# Putting Control Table online

The league needs the app on the internet so teams can register from their phones days ahead and check the board on tournament night. Control Table is hosted on **Render**, which builds the included `Dockerfile` straight from GitHub and keeps the tournament data on a persistent disk. Any host that can run a Docker image with a persistent disk works the same way (Railway, Fly.io, a small VPS).

## What the host must provide

| Need | Why |
|---|---|
| A **persistent disk** mounted at `/data` | Every tournament, its undo history and text log are saved there. Without it, a restart or redeploy wipes them. |
| **Always on** (no sleeping) | The check-in clock gives grace periods and records forfeits even when nobody has a page open. |
| **One instance** | The app keeps the tournament in one process. Don't scale it to more than one copy. |
| HTTPS | Render provides it automatically. |

**Use a paid instance, not Free.** Render's Free web services can't have a disk, and they go to sleep after 15 minutes without visitors (waking takes about a minute). Either one breaks the app: tournaments would vanish on restart, and the check-in clock would stop. A paid instance (Starter or above) runs all the time.

Cost: the smallest paid instance is about $7/month, plus the disk at $0.25 per GB per month (1 GB is plenty). Check [Render's pricing page](https://render.com/pricing) for current numbers.

## Render, step by step

1. **Get the code onto the branch you'll deploy.** The app lives in the `chip_tournament/` folder of this repository, on `master` (or deploy from a feature branch).
2. **Create the service.** Sign in at [render.com](https://render.com) with GitHub, choose **New → Web Service**, and pick this repository.
3. **Point it at the app folder.**
   - **Language / Runtime:** Docker.
   - **Branch:** the one you'll deploy.
   - **Root Directory:** `chip_tournament`. Render then finds the `Dockerfile` inside it.
   - **Instance type:** a paid one (Starter is enough).
4. **Add the disk.** Under **Advanced** on the new-service form (or later on the service's **Disk** page), add a disk with mount path **`/data`** and size **1 GB**. A disk can be made bigger later but never smaller, so start small.
5. **Set the environment variables** (on the form, or later under **Environment**):

   | Variable | Value |
   |---|---|
   | `ADMIN_PASSWORD` | The director password, at least 8 characters. The app refuses to start without it. |
   | `VIEWER_PASSWORD` | Optional. A second, **view-only** director login (at least 8 characters, different from the director password), e.g. for the league office to look around. It sees the director page and guide but can't change anything, and phone numbers, PINs and text messages are hidden. |
   | `TZ` | Your venue's time zone, e.g. `America/New_York`, `America/Chicago`, `America/Denver`, `America/Los_Angeles`. Without it, every time shows in UTC. |
   | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | Optional, for real texts (see below). |

   Render sets `PORT` itself, and the app listens on it. `CHIPS_DATA_DIR=/data` and `CHIPS_PRODUCTION=1` are already in the Dockerfile.
6. **Health check.** Under **Settings → Health Check Path**, enter `/healthz`. Render then only switches to a new deploy once the app answers.
7. **Create the service** and wait for the first deploy to say **Live**. Your address is shown at the top of the service page (e.g. `your-league.onrender.com`). To use your own domain, add it under **Settings → Custom Domains**.
8. **Check it.**
   - Open the address and the home page loads.
   - Log in at `/admin/login` with your password.
   - In the *Tournament details & settings* box, the **clock check** line should show your venue's current time.
   - Register a practice team, then use **Manual Deploy → Restart service** (or redeploy) and confirm the team is still there. That proves the disk works. Remove the practice team.

## Text messages (Twilio)

1. Create a Twilio account and buy a phone number that can send SMS.
2. **US carriers require registration** before a business number can text people: A2P 10DLC for a regular number, or toll-free verification for a toll-free number. Approval can take days to a few weeks, so **start early**. Until it's approved, texts may be blocked.
3. Put the Account SID, Auth Token and your Twilio number (e.g. `+15551234567`) into the three variables above. Saving environment variables redeploys the app.
4. Register a practice team with your own cell. The text should arrive, and *Text alerts → Recent texts* on the director page should show `sent (201)`.

Until Twilio is set up, everything else works, and texts are only logged under *Recent texts*.

## Backups

Each tournament has its own folder, `/data/events/<id>/`, with `tournament.json`, `history.json` (undo) and `texts.log`; `/data/events.json` lists them. Rules, the background photo and the login key are shared at the top of `/data`. A disk from before Control Table hosted several tournaments is moved into `/data/events/` automatically on the first start.

Render takes a snapshot of the disk every day and keeps each one for seven days. To go back, open the service's **Disk** page and restore a snapshot. That replaces the **whole** disk, every tournament, and erases anything saved since, so for one bad action inside a tournament use **Undo** in the app instead. For a copy that lasts longer than seven days, download the files after each tournament from the service's **Shell** tab.

**Archive and start over** keeps old tournaments on the disk (with their texts). They're listed under **Past tournaments** on the director page, where you can delete them permanently; **Delete permanently and start over** skips the archive.

## Updating the app later

Pushing to the deployed branch makes Render rebuild and redeploy automatically. The tournament data on the disk isn't touched.

**Avoid deploying during a tournament.** With a disk attached, Render stops the old copy before starting the new one, so the app is down for a short time on every deploy (usually under a minute), and anyone pressing a button then sees an error. Merging a pull request into the deployed branch counts as a deploy, and so does changing an environment variable. To hold deploys on tournament night, turn **Auto-Deploy** off under **Settings** and turn it back on afterward.

## Running it on a laptop instead

This works for a one-off night on the venue's Wi-Fi, but teams can't register from home and phones must be on the same network:

```bash
cd chip_tournament
pip install -r requirements.txt
ADMIN_PASSWORD=your-password TZ=America/New_York python app.py
```

Then share `http://<laptop's IP address>:5000` (shown when the app starts). Don't let the laptop sleep.
