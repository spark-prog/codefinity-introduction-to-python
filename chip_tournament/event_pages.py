"""An event's tabbed pages: Details, Teams, Bracket (Live for chip events), and Rules (see app.py).

Each tab is its own page with its own web address, under the event's header and a sticky tab bar. The event's home
address opens on the tab that matters right now: the teams while people are signing up, the bracket once it's running.
"""

from __future__ import annotations

from flask import g, redirect, render_template, url_for

from chips import event_info


def register(app) -> None:
    def tab_page(tab: str | None = None):
        t = g.event.store.load()
        tab = tab or event_info.default_tab(t)
        if tab == "partners" and not event_info.partners_label(t):  # nothing to show (any more): go to the default tab
            return redirect(url_for("home"))
        return render_template("event_page.html", t=t, active_tab=tab, tabs=event_info.tabs(t),
                               panel=f"_panel_{tab}_{g.event.kind}.html")

    app.config["event_tab_page"] = tab_page

    @app.get("/t/<event>/details")
    def event_details():
        return tab_page("details")

    @app.get("/t/<event>/teams")
    def event_teams():
        return tab_page("teams")

    @app.get("/t/<event>/partners")
    def event_partners():
        return tab_page("partners")

    @app.get("/t/<event>/bracket")
    def event_bracket():
        return tab_page("bracket")
