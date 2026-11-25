"""Transcript verification used to accept a cut piece of a batch recording."""
import pytest

from pipeline.narration import MATCH_MIN, match_score


@pytest.mark.parametrize("spoken, heard", [
    ("ዕጣን smoke curls through the room. Nobody is in a hurry.", "It's on smoke curls through the room. Nobody is in a hurry."),
    ("The third cup is በረካ, the blessing. The room gets quieter.", "The third cup is Barakah, the blessing, the room gets quieter."),
    ("Nolawi! Delina! Come down here!", "Nolawe, Delina, come down here."),
])
def test_geez_words_and_spelling_slips_are_tolerated(spoken, heard):
    assert match_score(spoken, heard) >= MATCH_MIN


@pytest.mark.parametrize("spoken, heard", [
    ("Whatever the week was, we come down.", "Whatever the week was, we come down. Downstairs, the coffee has already begun."),
    ("Whatever the week was, we come down.", "the week was"),
    ("With the first cup, አቦል, we catch up on the week: school, tests, work.", "With the second, tona, it's university applications."),
])
def test_wrong_cuts_are_rejected(spoken, heard):
    assert match_score(spoken, heard) < MATCH_MIN
