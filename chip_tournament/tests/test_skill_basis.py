"""A singles event asks for 8-Ball and 9-Ball levels; the player's skill follows the event's setting
("Skill levels from the player list"), not always the higher of the two."""

from chips import skill_check
from chips.bracket import BracketTournament
from chips.engine import Player
from chips.players import level_from

MOVIN = [[1, 3], [4, 4], [5, 5], [6, 6], [7, 9]]


def test_level_from():
    assert level_from(4, 6, "8") == 4 and level_from(4, 6, "9") == 6
    assert level_from(4, 6, "high") == 6 and level_from(5, 9, "high7") == 7 and level_from(5, 9, "high") == 9
    assert level_from(0, 9, "8") == 7 and level_from(0, 1, "8") == 2 and level_from(0, 5, "8") == 5  # 9-Ball converts
    assert level_from(4, 0, "9") == 4 and level_from(0, 0, "8") == 0


def singles(basis):
    t = BracketTournament()
    t.settings.skill_groups, t.settings.race_method, t.settings.skill_game = MOVIN, "rack_chart", basis
    return t


def test_8_ball_event_uses_8_ball_level():
    t = singles("8")
    e = t.register_entry("Ann", [Player("Ann", "", 0, skill_8=4, skill_9=6)])
    assert e.skill == 4 and e.division == "SL 4"
    e = t.update_entry(e.id, "Ann", [Player("Ann", "", 0, skill_8=5, skill_9=6)])
    assert e.skill == 5 and e.division == "SL 5"
    # No 8-Ball level: the 9-Ball one converts.
    assert t.register_entry("Bo", [Player("Bo", "", 0, skill_8=0, skill_9=9)]).skill == 7


def test_other_settings():
    assert singles("9").register_entry("A", [Player("A", "", 0, skill_8=4, skill_9=6)]).skill == 6
    assert singles("high").register_entry("A", [Player("A", "", 0, skill_8=4, skill_9=6)]).skill == 6
    assert singles("high7").register_entry("A", [Player("A", "", 0, skill_8=5, skill_9=9)]).skill == 7


def test_entries_saved_with_the_old_rule_are_corrected_before_the_draw():
    t = singles("8")
    e = t.register_entry("Ann", [Player("Ann", "", 0, skill_8=4, skill_9=6)])
    data = t.to_dict()
    data["entries"][e.id]["players"][0]["skill"] = 6  # what the old code saved: the higher level
    data["entries"][e.id]["division"] = "SL 6"
    loaded = BracketTournament.from_dict(data)
    assert loaded.entries[e.id].skill == 4 and loaded.entries[e.id].division == "SL 4"
    # Changing the setting before the draw moves them too.
    data["settings"]["skill_game"] = "9"
    assert BracketTournament.from_dict(data).entries[e.id].skill == 6
    # After the draw nothing moves.
    data["settings"]["skill_game"], data["status"] = "8", "running"
    assert BracketTournament.from_dict(data).entries[e.id].players[0].skill == 6


def test_skill_check_fix_follows_the_setting():
    t = singles("8")
    e = t.register_entry("Ann", [Player("Ann", "", 0, skill_8=3, skill_9=3)])
    rows = [{"status": skill_check.DIFFERS, "member": "", "list": {"skill8": 4, "skill9": 6}}]
    fixed = skill_check.fixed_players(e, rows, True, "8")
    assert (fixed[0].skill_8, fixed[0].skill_9, fixed[0].skill) == (4, 6, 4)
