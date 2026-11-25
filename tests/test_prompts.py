"""Keyframe prompt rules: cast descriptions only where people are listed; other prompts and keys never move."""
import copy
import json
from pathlib import Path

from pipeline import keyframes
from pipeline.common import validate

ROOT = Path(__file__).resolve().parent.parent
SB = json.loads((ROOT / "tests" / "fixtures" / "storyboard.json").read_text())
CAST = {"NOLAWI": "an Ethiopian young man about 21, brown skin, short black hair",
        "MOM": "an Ethiopian middle-aged mother, brown skin"}


def test_adding_a_cast_leaves_shots_without_people_unchanged():
    before = [keyframes.shot_prompt(SB, s) for s in SB["shots"]]
    sb = copy.deepcopy(SB) | {"cast": CAST}
    assert [keyframes.shot_prompt(sb, s) for s in sb["shots"]] == before


def test_people_names_are_replaced_by_their_descriptions():
    sb = copy.deepcopy(SB) | {"cast": CAST}
    shot = sb["shots"][0] | {"people": ["NOLAWI", "MOM"], "image_prompt": "NOLAWI asleep while MOM calls from downstairs"}
    p = keyframes.shot_prompt(sb, shot)
    assert "NOLAWI" not in p and "MOM" not in p
    assert "(an Ethiopian young man about 21, brown skin, short black hair) asleep" in p
    assert "(an Ethiopian middle-aged mother, brown skin) calls" in p


def test_a_listed_person_missing_from_the_prompt_is_appended():
    sb = copy.deepcopy(SB) | {"cast": CAST}
    shot = sb["shots"][0] | {"people": ["NOLAWI"], "image_prompt": "A bedroom in the morning"}
    assert keyframes.shot_prompt(sb, shot).startswith("A bedroom in the morning. An Ethiopian young man about 21")


def test_unknown_person_is_rejected():
    sb = copy.deepcopy(SB) | {"cast": CAST}
    sb["shots"][0]["people"] = ["GRANDMA"]
    assert any("GRANDMA" in e for e in validate(sb))
