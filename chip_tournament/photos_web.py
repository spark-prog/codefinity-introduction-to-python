"""An event's header photo and flyer (PNG or JPG), uploaded by the director.

The files live in the event's own folder (events/<id>/header.jpg, flyer.png, ...), so they're in every
backup, go away when the event is deleted, and come along when the event is copied. The header shows on
All Events, the event's page and Register page, a player's My account, and as the link preview picture;
the flyer gets a "View flyer" button. The flyer can stand in as the header too. Not on the TV board.

Photos are made smaller in the director's browser before they're sent (about 1600 px wide), so a big
phone photo doesn't slow the pages down; the server still checks every file is a real PNG or JPG.
"""

from __future__ import annotations

from pathlib import Path

from flask import abort, flash, g, redirect, request, send_file, url_for

from chips.engine import TournamentError

KINDS = ("header", "flyer")
TYPES = {b"\x89PNG\r\n\x1a\n": ("png", "image/png"), b"\xff\xd8\xff": ("jpg", "image/jpeg")}
MAX_BYTES = 8 * 1024 * 1024  # after the browser has made it smaller this is never close
ALT_MAX = 200


def stored(folder: Path, kind: str) -> Path | None:
    """The saved file for header or flyer, if there is one."""
    for ext in ("jpg", "png"):
        path = folder / f"{kind}.{ext}"
        if path.is_file():
            return path
    return None


def save_photo(folder: Path, kind: str, data: bytes) -> str:
    """Keep an uploaded photo (replacing the old one). Raises TournamentError unless it's a PNG or JPG."""
    if kind not in KINDS:
        raise TournamentError("That's not a photo spot.")
    if not data:
        raise TournamentError("Choose a photo first.")
    if len(data) > MAX_BYTES:
        raise TournamentError("That photo is too big. Use one under 8 MB (Canva: Download, PNG or JPG).")
    found = next((v for magic, v in TYPES.items() if data.startswith(magic)), None)
    if found is None:
        raise TournamentError("Choose a PNG or JPG photo (from Canva: Share, Download, PNG).")
    remove_photo(folder, kind)
    path = folder / f"{kind}.{found[0]}"
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return found[0]


def remove_photo(folder: Path, kind: str) -> bool:
    gone = False
    for ext in ("jpg", "png"):
        path = folder / f"{kind}.{ext}"
        if path.is_file():
            path.unlink()
            gone = True
    return gone


def header_file(folder: Path, settings) -> Path | None:
    """The picture used as the header: the flyer when the director chose that, else the header photo."""
    if getattr(settings, "flyer_as_header", False):
        flyer = stored(folder, "flyer")
        if flyer:
            return flyer
    return stored(folder, "header")


def register(app, admin_required) -> None:
    hub = app.config["hub"]

    def folder_of(event_id: str) -> Path | None:
        ev = hub.get(event_id)
        return ev.dir if ev is not None else None

    def photo_file(event_id: str, kind: str) -> Path | None:
        folder = folder_of(event_id)
        if folder is None or kind not in KINDS:
            return None
        if kind == "header":
            try:
                settings = hub.get(event_id).load().settings
            except Exception:  # an unreadable event shows no photo rather than breaking the page
                return None
            return header_file(folder, settings)
        return stored(folder, kind)

    def event_photo(event_id: str, kind: str = "header", external: bool = False) -> str:
        """The address of an event's header or flyer, or "" when it has none (for templates)."""
        path = photo_file(event_id, kind)
        if path is None:
            return ""
        # The file's change time in the address: a new photo shows at once, the same one stays cached.
        return url_for("event_photo_file", event=event_id, kind=kind, v=int(path.stat().st_mtime),
                       _external=external)

    def event_photo_alt(t, kind: str = "header") -> str:
        s = t.settings
        if kind == "header" and getattr(s, "flyer_as_header", False) and getattr(s, "flyer_alt", ""):
            return s.flyer_alt
        alt = getattr(s, f"{kind}_alt", "")
        return alt or (f"{s.name} flyer" if kind == "flyer" else s.name)

    def header_is_flyer(event_id: str) -> bool:
        """The header is the flyer: shown whole (a tall flyer would be cut off if it filled a wide banner)."""
        path = photo_file(event_id, "header")
        return bool(path) and path.name.startswith("flyer.")

    app.jinja_env.globals["event_photo"] = event_photo
    app.jinja_env.globals["header_is_flyer"] = header_is_flyer
    app.jinja_env.globals["event_photo_alt"] = event_photo_alt

    @app.get("/t/<event>/photo/<kind>")
    def event_photo_file(kind):
        path = photo_file(g.event.id, kind) or abort(404)
        resp = send_file(path, mimetype="image/png" if path.suffix == ".png" else "image/jpeg", conditional=True)
        # Versioned addresses never change, so phones can keep them; a bare address is checked again soon.
        resp.cache_control.public = True
        resp.cache_control.max_age = 31536000 if request.args.get("v") else 300
        return resp

    @app.post("/t/<event>/admin/photo/<kind>")
    @admin_required
    def admin_event_photo(kind):
        if kind not in KINDS:
            abort(404)
        ev, f = g.event, request.form
        action = f.get("action", "upload")
        label = "Header photo" if kind == "header" else "Flyer"
        try:
            if action == "remove":
                flash(f"{label} removed." if remove_photo(ev.dir, kind) else f"There was no {label.lower()}.", "ok")
            elif action == "upload":
                upload = request.files.get("photo")
                save_photo(ev.dir, kind, upload.read() if upload and upload.filename else b"")
                flash(f"{label} uploaded. It shows on All Events and the event's pages now." if kind == "header"
                      else "Flyer uploaded. Players see a View flyer button on the event and Register pages.", "ok")
            elif action != "save":
                abort(400)
            # The description (and, for the flyer, "use it as the header") ride along with any of the three.
            with ev.store.lock:  # bookkeeping, not an undo step: Undo can't bring back a photo file anyway
                t = ev.store.load()
                if "alt" in f:
                    setattr(t.settings, f"{kind}_alt", " ".join(f.get("alt", "").split())[:ALT_MAX])
                if kind == "flyer" and f.get("flyer_as_header_shown"):
                    t.settings.flyer_as_header = f.get("flyer_as_header") == "1"
                ev.store.save(t)
            if action == "save":
                flash("Saved.", "ok")
        except TournamentError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin", event=ev.id) + "#photos")
