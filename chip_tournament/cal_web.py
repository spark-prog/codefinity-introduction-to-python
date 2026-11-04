"""Add to calendar: an .ics file per event (Apple Calendar, Outlook, most phones) and a Google Calendar link."""

from __future__ import annotations

from flask import Response, abort, g, request, url_for

from chips import reminders


def register(app) -> None:
    def event_url(event_id: str) -> str:
        root = reminders.site_root(g.event.store.load()) if g.get("event") else ""
        return (root or request.url_root.rstrip("/")) + url_for("home", event=event_id)

    @app.get("/t/<event>/calendar.ics")
    def event_calendar():
        t = g.event.store.load()
        body = reminders.ics(t, g.event.id, event_url(g.event.id))
        if body is None:
            abort(404)
        name = "".join(ch for ch in t.settings.name if ch.isalnum() or ch in " -_").strip().replace(" ", "-") or "event"
        return Response(body, mimetype="text/calendar",
                        headers={"Content-Disposition": f'attachment; filename="{name}.ics"'})

    def calendar_links(ev_id: str, t=None) -> dict | None:
        """{'ics': ..., 'google': ...} for an event with a date and start time, else None."""
        if t is None:
            ev = app.config["hub"].get(ev_id)
            t = ev.load() if ev else None
        if t is None or not reminders.start_of(t.settings):
            return None
        page = request.url_root.rstrip("/") + url_for("home", event=ev_id)
        return {"ics": url_for("event_calendar", event=ev_id), "google": reminders.google_link(t, page)}

    app.jinja_env.globals["calendar_links"] = calendar_links
