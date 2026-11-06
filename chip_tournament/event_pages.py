"""An event's tabbed pages: Details, Teams, Bracket (Live for chip events), and Rules (see app.py).

Each tab is its own page with its own web address, under the event's header and a sticky tab bar. The event's home
address opens on the tab that matters right now: the teams while people are signing up, the bracket once it's running.
"""

from __future__ import annotations

from flask import g, render_template

from chips import event_info


def register(app) -> None:
    def tab_page(tab: str | None = None):
        t = g.event.store.load()
        tab = tab or event_info.default_tab(t)
        return render_template("event_page.html", t=t, active_tab=tab, tabs=event_info.tabs(t),
                               panel=f"_panel_{tab}_{g.event.kind}.html")

    app.config["event_tab_page"] = tab_page

    @app.get("/t/<event>/details")
    def event_details():
        return tab_page("details")

    @app.get("/t/<event>/teams")
    def event_teams():
        return tab_page("teams")

    @app.get("/t/<event>/bracket")
    def event_bracket():
        return tab_page("bracket")
