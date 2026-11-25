"""Pure assembly rules (no ffmpeg): clip fitting, music fades and subtitle wrapping."""
from pipeline.assemble import CHARS_PER_LINE, MUSIC_FADE_IN_S, fit_plan, music_tail, srt, subtitle_cues


def test_music_entrance_fades_in_and_out():
    soft = music_tail(30.0, 1.5)  # after Mom's call: no hard start
    assert soft.startswith("atrim=0:30.000,afade=t=in:d=1.5,")
    assert "afade=t=out:st=28.000:d=2" in soft
    assert f"afade=t=in:d={MUSIC_FADE_IN_S:g}," in music_tail(30.0)


def test_long_clip_drops_weak_ends_first():
    p = fit_plan(5.0, 3.5)
    assert p["speed"] == 1.0 and p["kenburns_tail_s"] == 0
    assert p["trim_head_s"] == 0.5 and abs(p["trim_tail_s"] - 1.0) < 1e-6


def test_short_clip_trims_little_slows_at_most_1_3_then_kenburns():
    p = fit_plan(5.0, 8.0)
    assert p["trim_head_s"] <= 0.25 and p["trim_tail_s"] <= 0.25
    assert p["speed"] == 1.3 and p["interpolate"]
    assert abs((5.0 - 0.25) * 1.3 + p["kenburns_tail_s"] - 8.0) < 1e-3


def test_slightly_short_clip_needs_no_kenburns():
    p = fit_plan(5.0, 5.5)
    assert 1.0 < p["speed"] < 1.3 and p["kenburns_tail_s"] == 0


def test_subtitles_two_lines_of_about_40_chars():
    text = ("This is a deliberately long narration line that has to be split into more than one subtitle "
            "cue because it would never fit on two lines of forty characters.")
    tl = {"shots": [{"lines": [{"subtitle": text, "start_s": 1.0, "end_s": 9.0}]}]}
    cues = subtitle_cues(tl)
    assert len(cues) > 1
    for a, b, t in cues:
        rows = t.split("\n")
        assert len(rows) <= 2 and all(len(r) <= CHARS_PER_LINE for r in rows) and b > a
    assert srt(cues).startswith("1\n00:00:01,000 --> ")
