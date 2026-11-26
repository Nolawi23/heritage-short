"""Pure assembly rules (no ffmpeg): clip fitting, music fades and subtitle wrapping."""
from pipeline.assemble import CHARS_PER_LINE, MUSIC_FADE_IN_S, fit_plan, music_tail, srt, subtitle_cues


def test_music_entrance_fades_in_and_out():
    soft = music_tail(30.0, 1.5)  # after Mom's call: no hard start
    assert soft.startswith("atrim=0:30.000,afade=t=in:d=1.5,")
    assert "afade=t=out:st=28.000:d=2" in soft
    assert f"afade=t=in:d={MUSIC_FADE_IN_S:g}," in music_tail(30.0)


def test_music_dip_merges_consecutive_shots_and_ramps_outside():
    from pipeline.assemble import music_dips
    sb = {"shots": [{"id": "a"}, {"id": "b", "music_gain_db": -4}, {"id": "c", "music_gain_db": -4}, {"id": "d"}]}
    tl = {"shots": [{"id": i, "start_s": 5.0 * n, "duration_s": 5.0} for n, i in enumerate("abcd")]}
    f = music_dips(sb, tl)
    assert f.count("clip(") == 2  # b + c form ONE window, 5-15 s
    assert "(1+(0.6310-1)*clip((t-4.000)/1,0,1)*clip((16.000-t)/1,0,1))" in f
    assert music_dips({"shots": [{"id": "a"}]}, {"shots": tl["shots"][:1]}) == ""


def test_vertical_crop_window_can_move_off_centre():
    from pipeline.av import reframe_filter
    assert "crop=720:1280," in reframe_filter(720, 1280, "fill")
    assert "crop=720:1280:x='(iw-ow)*0.7500'" in reframe_filter(720, 1280, "fill", 0.5)
    assert "gblur" in reframe_filter(720, 1280, "blur", 0.5)


def test_long_clip_drops_weak_ends_first():
    p = fit_plan(5.0, 3.5)
    assert p["speed"] == 1.0 and p["kenburns_tail_s"] == 0
    assert p["trim_head_s"] == 0.5 and abs(p["trim_tail_s"] - 1.0) < 1e-6


def test_short_clip_trims_little_slows_at_most_1_25_then_kenburns():
    p = fit_plan(5.0, 8.0)
    assert p["trim_head_s"] <= 0.25 and p["trim_tail_s"] <= 0.25
    assert p["speed"] == 1.25 and p["interpolate"]
    assert abs((5.0 - 0.25) * 1.25 + p["kenburns_tail_s"] - 8.0) < 1e-3


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


def test_ambience_plays_over_its_shots_and_fades_inside():
    from pipeline.assemble import ambience_filter, ambience_windows
    tl = {"shots": [{"id": "a", "start_s": 0.0, "duration_s": 5.0}, {"id": "b", "start_s": 5.0, "duration_s": 4.0},
                    {"id": "c", "start_s": 9.0, "duration_s": 3.0}]}
    bed = {"id": "murmur", "shots": ["b", "c"]}
    assert ambience_windows({"ambience": [bed]}, tl) == [(bed, 5.0, 12.0)]
    assert ambience_windows({}, tl) == []
    f = ambience_filter(bed, 7.0)
    assert f.startswith("atrim=0:7.000,") and "lowpass=f=2500" in f
    assert "afade=t=in:d=1," in f and "afade=t=out:st=6.000:d=1" in f


def test_music_generation_key_ignores_shot_durations_and_clean_until(env):
    """Shortening a shot or cutting a damaged section end must not re-generate the score (only assembly changes)."""
    import json
    from pipeline.common import load_storyboard
    from pipeline.music import cand_key
    ctx = load_storyboard(env.sb_path)
    before = cand_key(ctx, ctx.sb["music_cues"][0], 1)
    sb = json.loads(env.sb_path.read_text())
    sb["shots"][-1]["duration_s"] = 3.1
    sb["music_cues"][0]["clean_until_s"] = 29.5
    env.sb_path.write_text(json.dumps(sb))
    assert cand_key(load_storyboard(env.sb_path), sb["music_cues"][0], 1) == before
