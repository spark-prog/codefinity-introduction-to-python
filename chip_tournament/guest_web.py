"""Guest TD pages: the division rep's own events page, and the league operators' controls.

See chips/guest_td.py for the rules. The flow:
  1. The LO adds a rep by member number and emails them the agreement (a link to /guest).
  2. The rep signs in with their player login and signs it online (tick + typed name).
     The league office is emailed that they signed.
  3. The LO approves their account (it stays approved until the LO removes them).
  4. The rep creates events from their own page; each one waits for the LO's approval.
"""

from __future__ import annotations

import os

from flask import flash, g, redirect, render_template, request, url_for

from chips import backup, guest_td
from chips.engine import FINISHED, TournamentError
from chips.guest_td import APPROVED, DECLINED, PENDING, GuestError

MAX_PENDING = 3  # requests waiting for approval at once, per guest TD


def register(app, h) -> None:
    hub, guests, players = h["hub"], h["guests"], h["players"]
    admin_required = h["admin_required"]

    def site_root() -> str:
        return os.environ.get("SITE_URL", "").strip().rstrip("/") or request.url_root.rstrip("/")

    def send_email(to: str, subject: str, text: str) -> bool:
        """Email from the league; False when email isn't set up or the send failed."""
        mailer = (app.config.get("mailer_factory") or backup.Mailer.from_env)()
        if mailer is None or not to:
            return False
        legal = app.jinja_env.globals["legal"]
        try:
            mailer.send_to(to, subject, text, sender=legal["brand"], reply_to=legal["email"])
            return True
        except backup.BackupError:
            return False

    def event_rows(member: str | None = None) -> list[dict]:
        """Guest TD events with their names and where they stand (newest decisions first for the LO)."""
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
                           "invited": rec.get("invited"), "approved": rec.get("approved"),
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
        mine = guests.events_of(member) if rec else {}
        # Their events as the LO's own list shows them: type, date, status, field size.
        rows = [{**r, "approval": mine[r["event"].id]["status"]} for r in hub.summaries() if r["event"].id in mine]
        return render_template("guest.html", t=None, rec=rec, agreement=guests.agreement(),
                               accepted=guests.has_accepted(member), approved=guests.is_approved(member),
                               fresh=h["guest_fresh"](), rows=rows, formats=h["FORMATS"],
                               fresh_hours=guest_td.FRESH_HOURS, max_pending=MAX_PENDING)

    @app.post("/guest/accept")
    def guest_accept():
        if not g.get("player"):
            return redirect(url_for("player_login", next=url_for("guest_home")))
        member = g.player["member"]
        try:
            if request.form.get("agree") != "1":
                raise GuestError("Tick the box to say you agree.")
            if request.form.get("version") != str(guests.agreement()["version"]):
                raise GuestError("The agreement changed while you were reading it. Read the new version below.")
            guests.accept(member, request.form.get("typed_name", ""))
        except GuestError as exc:
            flash(str(exc), "error")
            return redirect(url_for("guest_home"))
        rec, legal = guests.guest(member), app.jinja_env.globals["legal"]
        acc = rec["accepted"]
        send_email(legal["email"], f"Guest TD agreement signed: {rec['name']}",
                   f"{rec['name']} (member {member}) signed version {acc['version']} of the guest TD agreement "
                   f"on {acc['at'].replace('T', ' at ')}, typing their name as \"{acc['typed_name']}\".\n\n"
                   + ("Their account is already approved, so they can run events again now."
                      if rec.get("approved") else
                      f"Approve their account on the Director page:\n{site_root()}{url_for('director')}#guest-tds"))
        flash("Thanks, you've signed the agreement." + ("" if rec.get("approved") else
              " The league office will approve your account, and you'll be able to create events here."), "ok")
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
                raise GuestError("Sign the agreement first.")
            if not guests.is_approved(member):
                raise GuestError("The league office hasn't approved your account yet.")
            if not h["guest_fresh"]():
                raise GuestError("For safety, sign in again before creating an event.")
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
        legal, t = app.jinja_env.globals["legal"], ev.load()
        send_email(legal["email"], f"Guest TD event to approve: {t.settings.name}",
                   f"{guests.guest(member)['name']} created {t.settings.name}"
                   f"{' for ' + t.settings.date if t.settings.date else ''} and sent it for your approval.\n\n"
                   f"Look it over and approve it on the Director page:\n{site_root()}{url_for('director')}#guest-tds")
        flash("Your event is created and sent to the league office for approval. Until it's approved it's hidden, "
              "registration is closed and no texts go out. You can fill in the settings now.", "ok")
        return redirect(url_for("admin", event=ev.id) + "#settings")

    # ------------------------------------------------------------ the league operators' controls

    def back():
        return redirect(url_for("director") + "#guest-tds")

    def email_agreement(member: str) -> bool:
        """Email the rep a link to sign the current agreement. False if email isn't set up."""
        rec, agreement, legal = players.get(member), guests.agreement(), app.jinja_env.globals["legal"]
        link = f"{site_root()}{url_for('guest_home')}"
        sent = send_email(rec["email"] if rec else "", f"{legal['brand']}: guest tournament director agreement",
                          f"Hi {rec['first'] if rec else ''},\n\n"
                          f"{legal['brand']} has invited you to run tournaments as a guest tournament director.\n\n"
                          f"To get started, open this link, sign in with your member number, then read and sign the "
                          f"agreement (version {agreement['version']}):\n{link}\n\n"
                          f"Once you've signed, the league office approves your account, and you can create events "
                          f"for us to approve.\n\nThe agreement is below for your records.\n\n"
                          f"----------------------------------------\n{agreement['text']}\n"
                          f"----------------------------------------\n\n{legal['brand']}")
        if sent:
            guests.mark_invited(member)
        return sent

    @app.post("/admin/guests/add")
    @admin_required
    def admin_guest_add():
        member = "".join(c for c in request.form.get("member", "") if c.isdigit())
        rec = players.get(member)
        try:
            if rec is None:
                raise GuestError(f"Member {member or '(blank)'} isn't in the player list.")
            if not rec.get("email"):
                raise GuestError(f"{rec['first']} {rec['last']} has no email in the player list, so we can't send "
                                 "them the agreement. Add one to the sheet first.")
            name = f"{rec['first']} {rec['last']}"
            guests.add(member, name)
        except GuestError as exc:
            flash(str(exc), "error")
            return back()
        if not guests.agreement()["version"]:
            flash(f"Added {name}. Publish the agreement below, then press Email the agreement.", "ok")
        elif email_agreement(member):
            flash(f"Added {name} and emailed them the agreement to sign.", "ok")
        else:
            flash(f"Added {name}, but email isn't set up, so the agreement wasn't sent. "
                  f"Ask them to sign in at {site_root()}{url_for('guest_home')}", "error")
        return back()

    @app.post("/admin/guests/<member>/email")
    @admin_required
    def admin_guest_email(member):
        rec = guests.guest(member)
        if rec is None:
            flash("That member isn't a guest TD.", "error")
        elif not guests.agreement()["version"]:
            flash("Publish the agreement first.", "error")
        elif email_agreement(member):
            flash(f"Emailed {rec['name']} the agreement to sign.", "ok")
        else:
            flash(f"Email isn't set up, so nothing was sent. Ask {rec['name']} to sign in at "
                  f"{site_root()}{url_for('guest_home')}", "error")
        return back()

    @app.post("/admin/guests/<member>/approve")
    @admin_required
    def admin_guest_approve(member):
        try:
            name = guests.approve(member)
        except GuestError as exc:
            flash(str(exc), "error")
            return back()
        rec, legal = players.get(member), app.jinja_env.globals["legal"]
        send_email(rec["email"] if rec else "", f"{legal['brand']}: you're approved as a guest TD",
                   f"Hi {rec['first'] if rec else ''},\n\nYour guest tournament director account is approved. "
                   f"Create an event here and send it to us for approval:\n{site_root()}{url_for('guest_home')}\n\n"
                   f"{legal['brand']}")
        flash(f"Approved {name} as a guest TD. They can create events now.", "ok")
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
            flash(f"Published version {version} of the agreement. Every guest TD must sign it before they can "
                  "run an event: press Email the agreement beside each person to send it.", "ok")
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
            rec = guests.decide(event_id, status)
            if status == APPROVED:  # out of hiding and able to text; the guest TD opens registration
                with ev.store.change("Approved by the league office") as t:
                    t.settings.test_event = False
            name, guest = ev.load().settings.name, players.get(rec["member"])
            legal = app.jinja_env.globals["legal"]
            send_email(guest["email"] if guest else "", f"{name}: {'approved' if status == APPROVED else 'not approved'}",
                       f"Hi {guest['first'] if guest else ''},\n\n" +
                       (f"{name} is approved and on the public list. Open registration from its director page "
                        f"when you're ready:\n{site_root()}{url_for('guest_home')}" if status == APPROVED else
                        f"The league office didn't approve {name}. Reply to this email with any questions.")
                       + f"\n\n{legal['brand']}")
            flash(f"Approved {name}. It's on the public list now; the guest TD opens registration when ready."
                  if status == APPROVED else
                  f"Declined {name}. It stays hidden and the guest TD can't open it. Delete it when you're ready.",
                  "ok")
        except GuestError as exc:
            flash(str(exc), "error")
        return back()
