import copy
import json
import re
from pathlib import Path

import pytest

from pipeline.common import validate

ROOT = Path(__file__).resolve().parent.parent
SB = json.loads((ROOT / "tests" / "fixtures" / "storyboard.json").read_text())


def broken(fn):
    sb = copy.deepcopy(SB)
    fn(sb)
    return validate(sb)


def test_placeholder_storyboard_is_valid():
    assert validate(SB) == []


def test_shot_one_is_mom_then_narrator():
    assert [ln["speaker"] for ln in SB["shots"][0]["lines"]] == ["MOM", "NARRATOR"]


@pytest.mark.parametrize("label, mutate", [
    ("missing title", lambda sb: sb.pop("title")),
    ("bad motion", lambda sb: sb["shots"][0].update(motion="zoom")),
    ("end card too long", lambda sb: sb["end_card"].update(duration_s=5)),
    ("fps not 24", lambda sb: sb["outputs"].update(fps=30)),
    ("unknown field", lambda sb: sb["shots"][0].update(narration="old single-voice field")),
    ("tempo outside range", lambda sb: sb["music_cues"][0].update(bpm=5)),
    ("unknown speaker", lambda sb: sb["shots"][0]["lines"].append({"speaker": "DAD", "text": "Hi"})),
    ("unknown music cue", lambda sb: sb["shots"][1].update(music_cue="nope")),
    ("duplicate shot id", lambda sb: sb["shots"][1].update(id="s01")),
    ("unknown character", lambda sb: sb["shots"][1].update(characters=["ghost"])),
    ("missing reference file", lambda sb: sb["characters"][0].update(reference_image="outputs/nope.png")),
])
def test_invalid_storyboards_are_rejected(label, mutate):
    assert broken(mutate), label


def test_live_storyboard_is_valid():
    assert validate(json.loads((ROOT / "storyboard.json").read_text())) == []


IMAGE_MODEL_UNKNOWN_WORDS = ("jebena", "sini", "rekebot", "etan", "kolo", "abol", "tona", "baraka", "bereka", "finjal")


def test_visual_prompts_describe_objects_instead_of_naming_them():
    """Image/video models don't know these Amharic words: they belong in notes, reasons and narration only."""
    sb = json.loads((ROOT / "storyboard.json").read_text())
    texts = [sb["style_block"]] + [c["description"] for c in sb["characters"]]
    texts += [s[f] for s in sb["shots"] for f in ("image_prompt", "motion_prompt")]
    assert not [(w, t) for t in texts for w in IMAGE_MODEL_UNKNOWN_WORDS if re.search(rf"\b{w}\b", t, re.I)]
