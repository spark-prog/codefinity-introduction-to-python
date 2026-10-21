from chips import importer
from chips.engine import Tournament

NUMBERED = """Timestamp,Email Address,Team Name,Player 1 Name,Player 1 Skill Level,Player 1 Cell Phone,Player 2 Name,Player 2 Skill Level,Player 2 Cell Phone
10/1/2026 9:00:00,a@x.com,Sharks,Ann Lee,5,(555) 111-0001,Bob Ray,4,
10/1/2026 9:05:00,b@x.com,Rails,Cy Doe,SL 6,555-111-0002,Di Fox,6,555-111-0003
10/1/2026 9:10:00,c@x.com,Bankers,Ed Kim,3,5551110004,,4,
10/1/2026 9:15:00,d@x.com,Scratch,Fay Wu,2,5551110005,Gus Po,2,
10/1/2026 9:20:00,e@x.com,sharks,Hal Ng,3,5551110006,Ivy Oh,3,
"""

YOUR_PARTNER = """Timestamp,Your name,Your skill level,Your cell phone number,Partner's name,Partner's skill level,Partner's cell (optional)
10/2/2026 8:00:00,Ann,4,5551110001,Bob,4,
"""

COMBINED = """Timestamp,Team,Captain name,Partner name,Combined SL,Phone
10/3/2026 8:00:00,Cue Crew,Ann,Bob,9,5551110001
"""


def mapped(text):
    headers, rows = importer.read_csv(text)
    mapping = importer.guess_mapping(headers)
    return headers, mapping, importer.build_rows(rows, mapping)


def test_guesses_numbered_player_columns():
    headers, mapping, rows = mapped(NUMBERED)
    named = {k: headers[i] for k, i in mapping.items()}
    assert named == {
        "team": "Team Name",
        "name1": "Player 1 Name", "skill1": "Player 1 Skill Level", "phone1": "Player 1 Cell Phone",
        "name2": "Player 2 Name", "skill2": "Player 2 Skill Level", "phone2": "Player 2 Cell Phone",
    }
    assert rows[1].players[0].skill == 6  # "SL 6" is read as 6


def test_guesses_your_and_partner_columns():
    headers, mapping, rows = mapped(YOUR_PARTNER)
    named = {k: headers[i] for k, i in mapping.items()}
    assert named["name1"] == "Your name" and named["name2"] == "Partner's name"
    assert named["skill1"] == "Your skill level" and named["skill2"] == "Partner's skill level"
    assert named["phone1"] == "Your cell phone number" and named["phone2"] == "Partner's cell (optional)"
    assert rows[0].team_name == "Ann & Bob"  # no team name column


def test_combined_skill_only_is_split():
    _, mapping, rows = mapped(COMBINED)
    assert "combined" in mapping
    assert [p.skill for p in rows[0].players] == [4, 5]
    assert "Split combined 9" in rows[0].note


def test_apply_follows_registration_rules_in_order():
    t = Tournament()
    t.set_field_cap(1)
    _, _, rows = mapped(NUMBERED)
    added, waitlisted = importer.apply(t, rows)
    assert (added, waitlisted) == (1, 1)
    assert rows[0].result.startswith("Added")  # earliest sign-up gets the spot
    assert rows[1].result == "Skipped: Combined skill level 12 is over the registration cap of 10. Register with a different partner."
    assert rows[2].result == "Skipped: Missing a partner's name."
    assert rows[3].result == "Waiting list #1"
    assert rows[4].result == "Skipped: A team named 'sharks' is already registered."  # duplicate, any capitals


def test_reimport_skips_teams_already_registered():
    t = Tournament()
    _, _, rows = mapped(NUMBERED)
    importer.apply(t, rows)
    _, _, again = mapped(NUMBERED)
    assert importer.apply(t, again) == (0, 0)
    assert all(r.result.startswith("Skipped") for r in again)


def test_preview_changes_nothing():
    t = Tournament()
    _, _, rows = mapped(NUMBERED)
    assert importer.preview(t, rows) == (2, 0)
    assert t.teams == {} and t.outbox == []


def test_texts_can_be_turned_off():
    t = Tournament()
    _, _, rows = mapped(YOUR_PARTNER)
    importer.apply(t, rows, send_texts=False)
    assert len(t.teams) == 1 and t.outbox == []
