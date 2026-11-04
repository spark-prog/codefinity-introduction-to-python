"""Table QR codes: a printed sticker on each table opens that table's match for scoring.

* /table/<table>?at=<venue> is what each sticker's QR code points to. It finds the match on that table in
  every running tournament (only those at that venue, when the sticker names one; any venue otherwise, so
  a misspelled venue still works). A logged-in player on one side goes straight to their scoring page;
  anyone else picks their side and enters their PIN there, as usual.
* /t/<event>/admin/table-qr prints a sheet of stickers for the event's tables. The stickers name the venue,
  not the event, so the same sheet works for every event held there.
"""

from __future__ import annotations

import os
import re

import segno
from flask import abort, g, redirect, render_template, request, url_for
from markupsafe import Markup

from chips.engine import CALLED


def venue_key(venue: str) -> str:
    """'Park Ave Billiards' -> 'park-ave-billiards' (what a sticker's link says, and what it's matched on)."""
    return re.sub(r"[^a-z0-9]+", "-", (venue or "").lower()).strip("-")


def qr_svg(url: str) -> Markup:
    """The QR code for a link, as an inline SVG (scales to any print size)."""
    return Markup(segno.make(url, error="m").svg_inline(scale=10, border=2, dark="#000", light="#fff", omitsize=True))


def register(app, director_view) -> None:
    hub = app.config["hub"]

    def sides(ev, t, m) -> list[dict]:
        """Both sides of a match: name, players' first names, and their scoring page."""
        if ev.kind == "bracket":
            ids, groups, endpoint, key = (m.a, m.b), t.entries, "b_entry", "entry_id"
        else:
            ids, groups, endpoint, key = (m.team_a, m.team_b), t.teams, "team_page", "team_id"
        out = []
        for i in ids:
            grp = groups.get(i)
            if grp is None:
                continue
            out.append({"id": i, "name": grp.name, "names": grp.first_names if grp.shows_names else "",
                        "members": {p.member for p in grp.players if p.member},
                        "url": url_for(endpoint, event=ev.id, **{key: i})})
        return out

    def race(ev, t, m) -> str:
        if ev.kind == "bracket":
            return t.race_text(m)
        return f"race to {t.settings.race_to}"

    def same_table(a, b) -> bool:
        return str(a or "").strip().lower() == str(b or "").strip().lower()

    @app.get("/table/<table>")
    def table_scan(table):
        at = venue_key(request.args.get("at", ""))
        running = [(ev, t) for ev, t in hub.loaded() if t.status == "running"]
        if at and any(venue_key(t.settings.venue) == at for _, t in running):
            running = [(ev, t) for ev, t in running if venue_key(t.settings.venue) == at]
        playing, next_up = [], []
        for ev, t in running:
            for m in t.matches:
                if not same_table(m.table, table):
                    continue
                row = {"event": ev, "t": t, "m": m, "sides": sides(ev, t, m), "race": race(ev, t, m)}
                if m.status == CALLED:
                    playing.append(row)
                elif m.status in ("ready", "waiting"):
                    next_up.append(row)
        me = g.get("player")
        if me and len(playing) == 1:  # a logged-in player at their own table: straight to their scoring page
            mine = [s for s in playing[0]["sides"] if me["member"] in s["members"]]
            if mine:
                return redirect(mine[0]["url"])
        return render_template("table_scan.html", table=table, playing=playing, next_up=next_up,
                               events=running, venue=at)

    @app.get("/t/<event>/admin/table-qr")
    @director_view
    def admin_table_qr():
        t = g.event.store.load()
        s = t.settings
        tables = [x.strip() for x in request.args.get("tables", "").split(",") if x.strip()] or list(s.tables)
        if not tables:
            abort(404)
        at = venue_key(s.venue)
        stickers = []
        for tbl in tables:
            path = url_for("table_scan", table=tbl, at=at or None)
            link = (os.environ.get("SITE_URL", "").strip().rstrip("/") or request.url_root.rstrip("/")) + path
            stickers.append({"table": tbl, "link": link, "qr": qr_svg(link),
                             "short": link.split("://", 1)[-1].split("?", 1)[0]})
        return render_template("table_qr_sheet.html", t=t, stickers=stickers, venue=s.venue,
                               tables_text=", ".join(tables))
