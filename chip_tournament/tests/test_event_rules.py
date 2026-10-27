"""Each event's Rules tab shows only the rule set it plays by; the league page shows them all."""

from app import create_app


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "secretpw1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "VIEWER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(str(tmp_path))
    app.testing = True
    admin = app.test_client()
    admin.post("/admin/login", data={"password": "secretpw1"})
    return app, admin


def save_rules(admin, rule_id, title, body):
    admin.post(f"/admin/rules/{rule_id}", data={"title": title, "body": body})


def chip_settings(t, **extra):
    s = t.settings
    return {"name": s.name, "tables": ", ".join(s.tables), "race_to": str(s.race_to),
            "entry_fee": f"{s.entry_fee_cents / 100:.2f}", "added_money": "0", "registration_cap": str(s.registration_cap),
            "chip_brackets": "6 = 5\n8 = 4\n10 = 3\n11 = 2", **extra}


def test_event_rules_tab_shows_only_its_rule_set(tmp_path, monkeypatch):
    app, admin = setup(tmp_path, monkeypatch)
    save_rules(admin, "fast-saratoga-rules", "Fast Saratoga Rules", "# Break\nSaratoga break rule.")
    save_rules(admin, "8-ball-rules", "8-Ball Rules", "# Break\nEight ball break rule.")
    ev = app.config["hub"].create("Halloween Doubles", "chip")
    public = app.test_client()

    page = public.get(f"/t/{ev.id}/rules").data
    assert "haven't been posted yet".encode() in page and b"Eight ball" not in page and b"Saratoga break" not in page

    r = admin.post(f"/t/{ev.id}/admin/settings", data=chip_settings(ev.load(), rule_set="fast-saratoga-rules"))
    assert r.status_code == 302
    assert ev.load().settings.rule_set == "fast-saratoga-rules"
    page = public.get(f"/t/{ev.id}/rules").data
    assert b"Saratoga break rule" in page and b"Eight ball" not in page
    assert b"plays by Fast Saratoga Rules" in page and b'href="/rules"' in page

    league = public.get("/rules").data
    assert b"Saratoga break rule" in league and b"Eight ball break rule" in league


def test_unknown_rule_set_is_not_saved(tmp_path, monkeypatch):
    app, admin = setup(tmp_path, monkeypatch)
    ev = app.config["hub"].create("Chips", "chip")
    admin.post(f"/t/{ev.id}/admin/settings", data=chip_settings(ev.load(), rule_set="made-up"))
    assert ev.load().settings.rule_set == ""


def test_settings_menu_lists_rule_sets_and_bracket_saves_choice(tmp_path, monkeypatch):
    app, admin = setup(tmp_path, monkeypatch)
    ev = app.config["hub"].create("Bracket", "bracket")
    page = admin.get(f"/t/{ev.id}/admin").data
    assert b'name="rule_set"' in page and b"Fast Saratoga Rules" in page
    s = ev.load().settings
    admin.post(f"/t/{ev.id}/admin/settings", data={"name": s.name, "tables": ", ".join(s.tables), "rule_set": "9-ball-rules"})
    assert ev.load().settings.rule_set == "9-ball-rules"
    assert b"plays by 9-Ball Rules" in app.test_client().get(f"/t/{ev.id}/rules").data
