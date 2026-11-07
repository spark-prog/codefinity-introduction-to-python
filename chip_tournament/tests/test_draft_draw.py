"""Review the draw before starting: a draft only the director sees, swaps within a skill group, then start."""

import random

import pytest

from app import create_app
from chips.engine import Player, TournamentError


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    return app


def make(app, count=6, groups=None, review=True, seeding="manual"):
    ev = app.config["hub"].create("Draft Night", "bracket", format="single")
    t = ev.load()
    t.settings.team_size, t.settings.call_mode, t.settings.payouts = 1, "continuous", []
    t.settings.review_draw, t.settings.seeding, t.settings.require_checkin = review, seeding, False
    if groups:
        t.settings.skill_groups = groups
    for n in range(count):
        t.register_entry(f"P{n}", [Player(f"P{n}", skill=2 + n % 2 if groups else 4)])
    ev.store.save(t)
    return ev


def admin(app):
    c = app.test_client()
    c.post("/admin/login", data={"password": "secretpw1"})
    return c


def test_a_draft_is_built_nowhere_until_start(app):
    ev = make(app)
    t = ev.load()
    t.make_draft()
    assert t.status == "setup" and not t.matches and t.draft_valid()
    assert [len(d["pairs"]) for d in t.draft_view()] == [4]  # 6 players: a bracket of 8
    names = {e: t.entries[e].name for e in t.draft_ids}
    t.start()
    assert t.status == "running" and t.draft_ids == []
    assert [e.id for e in sorted(t.field_entries, key=lambda e: e.seed)] == list(names)  # the draft is the draw


def test_swapping_changes_the_draw_and_stays_inside_a_group(app):
    ev = make(app, count=8, groups=[[1, 2], [3, 3]], seeding="manual")
    t = ev.load()
    t.make_draft()
    low = [e for e in t.draft_ids if t.entries[e].skill == 2]
    high = [e for e in t.draft_ids if t.entries[e].skill == 3]
    first = low[:2]
    before = list(t.draft_ids)
    t.swap_in_draft(*first)
    assert t.draft_ids != before and sorted(t.draft_ids) == sorted(before)
    with pytest.raises(TournamentError, match="different groups"):
        t.swap_in_draft(low[0], high[0])
    t.start()
    assert t.entries[first[0]].seed == before.index(first[1]) + 1  # started from the swapped order


def test_a_change_to_the_field_throws_the_draft_out(app):
    ev = make(app)
    t = ev.load()
    t.make_draft()
    t.register_entry("Late", [Player("Late", skill=4)])
    assert not t.draft_valid() and t.draft_view() == []
    t.start()  # still starts, with a fresh draw
    assert t.status == "running" and len(t.field_entries) == 7


def test_the_director_flow_in_the_pages(app):
    ev = make(app)
    c = admin(app)
    page = c.get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert "Draft the draw" in page and 'id="draft"' not in page
    r = c.post(f"/t/{ev.id}/admin/start", follow_redirects=True).get_data(as_text=True)
    assert 'id="draft"' in r and "Start the event" in r and "Only you can see this" in r
    assert ev.load().status == "setup" and ev.load().draft_valid()
    public = app.test_client().get(f"/t/{ev.id}/bracket").get_data(as_text=True)
    assert "The bracket is drawn when the tournament starts" in public and "draft-player" not in public
    ids = ev.load().draft_ids
    c.post(f"/t/{ev.id}/admin/draft/swap", data={"a": ids[0], "b": ids[1]})
    assert ev.load().draft_ids[:2] == [ids[1], ids[0]]
    c.post(f"/t/{ev.id}/admin/draft/cancel")
    assert not ev.load().draft_ids
    c.post(f"/t/{ev.id}/admin/start")  # drafts again
    c.post(f"/t/{ev.id}/admin/start")  # now starts
    assert ev.load().status == "running"


def test_without_the_setting_start_works_as_before(app):
    ev = make(app, review=False)
    page = admin(app).get(f"/t/{ev.id}/admin").get_data(as_text=True)
    assert "Draw the bracket &amp; start" in page
    admin(app).post(f"/t/{ev.id}/admin/start")
    assert ev.load().status == "running"
