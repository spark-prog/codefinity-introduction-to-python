"""Guest TD pages: the division rep's own home page, and the league director's controls.

See chips/guest_td.py for the rules. A guest TD signs in with their player login; the league
director manages the list, the agreement and event approvals from the Director page.
"""

from __future__ import annotations

from flask import flash, g, redirect, render_template, request, url_for

from chips import guest_td
from chips.engine import FINISHED, TournamentError
from chips.guest_td import APPROVED, DECLINED, PENDING, GuestError

MAX_PENDING = 3  # requests waiting for approval at once, per guest TD


def register(app, h) -> None:
    hub, guests, players = h["hub"], h["guests"], h["players"]
    admin_required = h["admin_required"]

    def event_rows(member: str | None = None) -> list[dict]:
        """Guest TD events with their names and where they stand."""
        rows = []
        recs = guests.events_of(member) if member else guests.load()["events"]
        for event_id, rec in recs.items():
            ev = hub.get(event_id)
            if ev is None:
                continue
            t = ev.load()
            guest = guests.guest(rec["member"])
            rows.append({"id": event_id, "name": t.settings.name, "date": t.settings.date, "status": rec["status"],
                         "finished": t.status == FINISHED, "member": rec["member"],
                         "guest_name": guest["name"] if guest else f"member {rec['member']} (removed)",
                         "requested_at": rec["requested_at"]})
        return sorted(rows, key=lambda r: (r["status"] != PENDING, r["date"] or "9999", r["name"]))

    def guest_overview() -> dict:
        data = guests.load()
        version = data["agreement"]["version"]
        people = []
        for member, rec in sorted(data["guests"].items(), key=lambda kv: kv[1]["name"].lower()):
            acc = rec.get("accepted")
            people.append({"member": member, "name": rec["name"], "added_at": rec["added_at"],
                           "accepted": acc, "current": bool(acc and version and acc["version"] == version)})
        return {"people": people, "agreement": data["agreement"], "events": event_rows()}

    app.jinja_env.globals["guest_overview"] = guest_overview

    def guest_event(event_id: str) -> dict | None:
        rec = guests.event(event_id)
        if rec is None:
            return None
        guest = guests.guest(rec["member"])
        return {**rec, "guest_name": guest["name"] if guest else f"member {rec['member']} (removed)"}

    app.jinja_env.globals["guest_event"] = guest_event

    # ------------------------------------------------------------ the guest TD's own page

    @app.get("/guest")
    def guest_home():
        if not g.get("player"):
            return redirect(url_for("player_login", next=url_for("guest_home")))
        member = g.player["member"]
        rec = guests.guest(member)
        fresh = h["guest_fresh"]()
        return render_template("guest.html", t=None, rec=rec, agreement=guests.agreement(),
                               accepted=guests.has_accepted(member), fresh=fresh,
                               events=event_rows(member) if rec else [], formats=h["FORMATS"],
                               fresh_hours=guest_td.FRESH_HOURS)

    @app.post("/guest/accept")
    def guest_accept():
        if not g.get("player"):
            return redirect(url_for("player_login", next=url_for("guest_home")))
        try:
            if request.form.get("agree") != "1":
                raise GuestError("Tick the box to say you agree.")
            if request.form.get("version") != str(guests.agreement()["version"]):
                raise GuestError("The agreement changed while you were reading it. Read the new version below.")
            guests.accept(g.player["member"], request.form.get("typed_name", ""))
            flash("Thanks. You've accepted the agreement.", "ok")
        except GuestError as exc:
            flash(str(exc), "error")
        return redirect(url_for("guest_home"))

    @app.post("/guest/new")
    def guest_new_event():
        if not g.get("player"):
            return redirect(url_for("player_login", next=url_for("guest_home")))
        member = g.player["member"]
        f = request.form
        try:
            if not guests.guest(member):
                raise GuestError("You're not on the guest TD list. Ask the league office.")
            if not guests.has_accepted(member):
                raise GuestError("Accept the agreement first.")
            if not h["guest_fresh"]():
                raise GuestError("For safety, sign in again before asking for an event.")
            waiting = [r for r in guests.events_of(member).values() if r["status"] == PENDING]
            if len(waiting) >= MAX_PENDING:
                raise GuestError(f"You have {len(waiting)} events waiting for approval. "
                                 "Ask the league office to look at those first.")
            kind = f.get("kind", "")
            settings = {"date": f.get("date", "").strip(), "start_time": f.get("start_time", "").strip(),
                        "venue": f.get("venue", "").strip(), "test_event": True, "registration_closed": True}
            if kind in dict(h["FORMATS"]):
                settings["format"], kind = kind, h["BRACKET"]
            elif kind != h["CHIP"]:
                raise GuestError("Pick a tournament type.")
            ev = hub.create(f.get("name", ""), kind, **settings)
            guests.request_event(member, ev.id)
        except (GuestError, TournamentError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("guest_home"))
        flash("Your event is set up and waiting for the league office to approve it. Until then it's hidden, "
              "registration is closed and no texts go out. You can fill in the settings now.", "ok")
        return redirect(url_for("admin", event=ev.id) + "#settings")

    # ------------------------------------------------------------ the league director's controls

    def back():
        return redirect(url_for("director") + "#guest-tds")

    @app.post("/admin/guests/add")
    @admin_required
    def admin_guest_add():
        member = "".join(c for c in request.form.get("member", "") if c.isdigit())
        rec = players.get(member)
        try:
            if rec is None:
                raise GuestError(f"Member {member or '(blank)'} isn't in the player list.")
            if not rec.get("email"):
                raise GuestError(f"{rec['first']} {rec['last']} has no email in the player list, so they can't sign in. "
                                 "Add one to the sheet first.")
            guests.add(member, f"{rec['first']} {rec['last']}")
            flash(f"Added {rec['first']} {rec['last']} as a guest TD. They sign in at /guest.", "ok")
        except GuestError as exc:
            flash(str(exc), "error")
        return back()

    @app.post("/admin/guests/<member>/remove")
    @admin_required
    def admin_guest_remove(member):
        try:
            name = guests.remove(member)
            flash(f"Removed {name}. They can't open any event as a TD now; their events stay on your list.", "ok")
        except GuestError as exc:
            flash(str(exc), "error")
        return back()

    @app.post("/admin/guests/agreement")
    @admin_required
    def admin_guest_agreement():
        try:
            version = guests.publish_agreement(request.form.get("text", ""))
            flash(f"Published version {version} of the agreement. Every guest TD must accept it "
                  "before they can run an event.", "ok")
        except GuestError as exc:
            flash(str(exc), "error")
        return back()

    @app.post("/admin/guests/event/<event_id>/<decision>")
    @admin_required
    def admin_guest_decide(event_id, decision):
        ev = hub.get(event_id)
        try:
            if ev is None:
                raise GuestError("That event no longer exists.")
            status = {"approve": APPROVED, "decline": DECLINED}.get(decision, "")
            guests.decide(event_id, status)
            if status == APPROVED:  # out of hiding and able to text; the guest TD opens registration
                with ev.store.change("Approved by the league office") as t:
                    t.settings.test_event = False
            name = ev.load().settings.name
            flash(f"Approved {name}. It's on the public list now; the guest TD opens registration when ready."
                  if status == APPROVED else
                  f"Declined {name}. It stays hidden and the guest TD can't open it. Delete it when you're ready.",
                  "ok")
        except GuestError as exc:
            flash(str(exc), "error")
        return back()
