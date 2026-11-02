"""An event's header photo and flyer: upload, where they show, what's refused, Copy, and who can change them."""

import struct
import zlib

import pytest

from app import create_app
from tests.test_phase7 import nested_forms


def png(width=4, height=2) -> bytes:
    """A real (tiny) PNG."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.setenv("VIEWER_PASSWORD", "viewerpw1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


def login(app, password="secretpw1"):
    c = app.test_client()
    c.post("/admin/login", data={"password": password})
    return c


def upload(c, ev, kind, data, name="photo.png", **form):
    import io
    return c.post(f"/t/{ev.id}/admin/photo/{kind}", data={"action": "upload", "photo": (io.BytesIO(data), name), **form},
                  content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)


@pytest.mark.parametrize("kind", ["bracket", "chip"])
def test_a_header_photo_shows_everywhere_but_the_board(app, kind):
    ev = app.config["hub"].create("Movin-On-Up", kind, **({"format": "double"} if kind == "bracket" else {}))
    c = login(app)
    page = upload(c, ev, "header", png(), alt="Movin-On-Up at Park Ave")
    assert "Header photo uploaded" in page and (ev.dir / "header.png").is_file()
    public = app.test_client()
    img = public.get(f"/t/{ev.id}/photo/header")
    assert img.status_code == 200 and img.mimetype == "image/png" and img.data == png()
    img.close()
    for path in ("/", f"/t/{ev.id}/", f"/t/{ev.id}/register"):
        html = public.get(path).get_data(as_text=True)
        assert f"/t/{ev.id}/photo/header?v=" in html, path
    home = public.get(f"/t/{ev.id}/").get_data(as_text=True)
    assert 'class="event-banner"' in home and 'alt="Movin-On-Up at Park Ave"' in home
    assert f'<meta property="og:image" content="http://localhost/t/{ev.id}/photo/header?v=' in home  # link preview
    board = public.get(f"/t/{ev.id}/board").get_data(as_text=True)
    assert 'class="event-banner"' not in board and "event-card-img" not in board  # not shown on the TV board
    assert "photo/header" not in public.get("/rules").get_data(as_text=True)  # the default preview elsewhere


def test_only_real_pngs_and_jpgs(app):
    ev = app.config["hub"].create("Mini", "bracket", format="single")
    c = login(app)
    for data, name in ((b"GIF89a" + b"\x00" * 20, "x.gif"), (b'<svg onload="alert(1)"></svg>', "x.svg"),
                       (b"just text", "fake.png"), (b"", "empty.png")):
        page = upload(c, ev, "header", data, name)
        assert "Choose a PNG or JPG photo" in page or "Choose a photo first" in page, name
    assert not list(ev.dir.glob("header.*"))
    assert "uploaded" in upload(c, ev, "header", JPG, "photo.jpg") and (ev.dir / "header.jpg").is_file()
    upload(c, ev, "header", png())  # replacing: one header at a time
    assert [p.name for p in ev.dir.glob("header.*")] == ["header.png"]
    assert app.test_client().get(f"/t/{ev.id}/photo/other").status_code == 404
    assert c.post(f"/t/{ev.id}/admin/photo/other", data={"action": "upload"}).status_code == 404


def test_the_flyer_and_using_it_as_the_header(app):
    ev = app.config["hub"].create("Masquer", "chip")
    c = login(app)
    assert "Flyer uploaded" in upload(c, ev, "flyer", JPG, "flyer.jpg", alt="Flyer: Jan 2, 5 PM, $40 a team")
    public = app.test_client()
    for path in (f"/t/{ev.id}/", f"/t/{ev.id}/register"):
        html = public.get(path).get_data(as_text=True)
        assert "View flyer" in html and f"/t/{ev.id}/photo/flyer?v=" in html and 'class="event-banner"' not in html
    assert 'alt="Flyer: Jan 2, 5 PM, $40 a team"' in public.get(f"/t/{ev.id}/").get_data(as_text=True)
    c.post(f"/t/{ev.id}/admin/photo/flyer", data={"action": "save", "flyer_as_header_shown": "1", "flyer_as_header": "1"})
    assert ev.load().settings.flyer_as_header
    header = public.get(f"/t/{ev.id}/photo/header")
    assert header.status_code == 200 and header.data == JPG  # the flyer stands in as the header
    header.close()
    assert 'class="event-banner contain"' in public.get(f"/t/{ev.id}/").get_data(as_text=True)  # shown whole
    assert 'event-card-img contain' in public.get("/").get_data(as_text=True)
    assert f"/t/{ev.id}/photo/header?v=" in public.get("/").get_data(as_text=True)
    c.post(f"/t/{ev.id}/admin/photo/flyer", data={"action": "save", "flyer_as_header_shown": "1"})  # unticked
    assert public.get(f"/t/{ev.id}/photo/header").status_code == 404
    c.post(f"/t/{ev.id}/admin/photo/flyer", data={"action": "remove"})
    assert "View flyer" not in public.get(f"/t/{ev.id}/").get_data(as_text=True)
    assert ev.load().settings.flyer_alt == "Flyer: Jan 2, 5 PM, $40 a team"  # kept for the next flyer


def test_caching_and_who_can_change_photos(app):
    ev = app.config["hub"].create("Mini", "bracket", format="single")
    c = login(app)
    upload(c, ev, "header", png())
    versioned = app.test_client().get(f"/t/{ev.id}/photo/header?v=1")
    bare = app.test_client().get(f"/t/{ev.id}/photo/header")
    assert versioned.cache_control.max_age == 31536000 and bare.cache_control.max_age == 300
    versioned.close(), bare.close()
    for who in (app.test_client(), login(app, "viewerpw1")):
        who.post(f"/t/{ev.id}/admin/photo/header", data={"action": "remove"})
        assert (ev.dir / "header.png").is_file()
    viewer_page = login(app, "viewerpw1").get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert "photo-preview" in viewer_page and 'name="photo"' not in viewer_page  # sees it, can't change it
    page = c.get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert nested_forms(page) == [] and 'data-resize="1600"' in page
    c.post(f"/t/{ev.id}/admin/photo/header", data={"action": "remove"})
    assert not (ev.dir / "header.png").exists()


def test_copy_brings_the_photos_and_delete_removes_them(app):
    hub = app.config["hub"]
    ev = hub.create("Movin-On-Up", "bracket", format="modified")
    c = login(app)
    upload(c, ev, "header", png(), alt="Header")
    upload(c, ev, "flyer", JPG, "f.jpg")
    copy, _ = hub.copy(ev.id, "Movin-On-Up 2027", "2027-11-20")
    assert (copy.dir / "header.png").read_bytes() == png() and (copy.dir / "flyer.jpg").read_bytes() == JPG
    assert copy.load().settings.header_alt == "Header"
    c.post(f"/t/{copy.id}/admin/photo/header", data={"action": "remove"})
    assert (ev.dir / "header.png").is_file()  # the original keeps its own
    folder = copy.dir
    c.post(f"/admin/event/{copy.id}/delete", data={"confirm": "DELETE"})
    assert not folder.exists()
