"""Cache behaviour: rerun = all hits; one edited line = one TTS call (+ re-assembly); variants share items."""
import json
import os
from pathlib import Path

from pipeline import assemble, keyframes, music, narration, shots
from pipeline.common import load_storyboard


def run_all(sb_path: Path) -> dict:
    """Every model stage, then the film cache keys assembly would use."""
    ctx = load_storyboard(sb_path)
    narration.run(ctx)
    keyframes.run(ctx)
    shots.run(ctx)
    music.run(ctx)
    return assemble.plan_keys(ctx)


def new_log_lines(env, since: int) -> list[dict]:
    return [json.loads(x) for x in env.runs.read_text().splitlines()[since:]]


def n_lines(env) -> int:
    return len(env.runs.read_text().splitlines()) if env.runs.exists() else 0


def total_calls(env) -> dict:
    return {k: len(v) for k, v in env.calls.items()}


def edit_line(sb_path: Path, dest: Path, shot: str, text: str) -> Path:
    sb = json.loads(sb_path.read_text())
    next(s for s in sb["shots"] if s["id"] == shot)["lines"][0]["text"] = text
    dest.write_text(json.dumps(sb, indent=2))
    return dest


def test_first_run_generates_everything(env):
    run_all(env.sb_path)
    # 4 lines in ONE batch TTS request, 3 keyframes, 1 wan batch + 1 music batch
    assert total_calls(env) == {"tts": 1, "flux": 3, "kaggle": 2}
    batch = [json.loads(x) for x in env.runs.read_text().splitlines() if '"mode": "batch"' in x and '"gemini"' in x]
    assert len(batch) == 1 and batch[0]["params"]["lines"] == ["s01_1", "s01_2", "s02_1", "s03_1"]
    out = env.tmp / "outputs"
    assert (out / "timeline.json").exists()
    assert os.path.islink(out / "narration" / "s01_1.wav") and os.path.islink(out / "clips" / "s01.mp4")
    assert (out / "cache").is_dir() and (out / "cache_manifest.json").exists()


def test_rerun_is_all_cache_hits(env):
    keys1 = run_all(env.sb_path)
    before, mark = total_calls(env), n_lines(env)
    keys2 = run_all(env.sb_path)
    assert total_calls(env) == before, "no provider may be called on a rerun"
    lines = new_log_lines(env, mark)
    assert lines and all(r["cache_hit"] and r["status"] == "cached" for r in lines)
    assert keys1 == keys2, "assembly must hit its cache too"


def test_changing_one_line_regenerates_only_that_line(env):
    keys1 = run_all(env.sb_path)
    before, mark = total_calls(env), n_lines(env)
    old_s01 = os.readlink(env.tmp / "outputs" / "narration" / "s01_1.wav")
    edit_line(env.sb_path, env.sb_path, "s02", "A completely different second line.")
    keys2 = run_all(env.sb_path)
    after = total_calls(env)
    assert after["tts"] - before["tts"] == 1
    assert after["flux"] == before["flux"] and after["kaggle"] == before["kaggle"]
    fresh = [r for r in new_log_lines(env, mark) if not r["cache_hit"]]
    assert [(r["stage"], r["shot_id"]) for r in fresh] == [("narration", "s02")]
    assert os.readlink(env.tmp / "outputs" / "narration" / "s01_1.wav") == old_s01
    assert all(keys1[f] != keys2[f] for f in keys1), "assembly (downstream) must be redone"


def test_variant_differing_in_one_line_costs_one_tts_call(env):
    keys_default = run_all(env.sb_path)
    before, mark = total_calls(env), n_lines(env)
    variant = edit_line(env.sb_path, env.tmp / "alt_ending.json", "s03", "The variant's final line.")
    keys_variant = run_all(variant)
    after = total_calls(env)
    assert after["tts"] - before["tts"] == 1, "exactly one TTS call"
    assert (after["flux"], after["kaggle"]) == (before["flux"], before["kaggle"]), "no other regeneration"
    fresh = [r for r in new_log_lines(env, mark) if not r["cache_hit"]]
    assert [(r["stage"], r["shot_id"]) for r in fresh] == [("narration", "s03")]
    vdir = env.tmp / "outputs" / "variants" / "alt_ending"
    assert (vdir / "timeline.json").exists() and os.path.islink(vdir / "keyframes" / "s01.png")
    assert os.path.realpath(vdir / "keyframes" / "s01.png") == os.path.realpath(env.tmp / "outputs" / "keyframes" / "s01.png")
    assert all(keys_default[f] != keys_variant[f] for f in keys_default), "only assembly differs"
    assert json.loads((env.tmp / "outputs" / "timeline.json").read_text())["storyboard"] != \
        json.loads((vdir / "timeline.json").read_text())["storyboard"]


def test_force_regenerates(env, monkeypatch):
    run_all(env.sb_path)
    before = total_calls(env)
    monkeypatch.setenv("FORCE", "1")
    run_all(env.sb_path)
    assert total_calls(env)["tts"] == 2 * before["tts"]


def test_batch_pieces_match_their_lines(env):
    run_all(env.sb_path)
    tl = json.loads((env.tmp / "outputs" / "timeline.json").read_text())
    for shot in tl["shots"]:
        for ln in shot["lines"]:
            expected = 0.2 + 0.3 * len(ln["text"].split())  # the fake speaks 0.3 s per word
            got = ln["end_s"] - ln["start_s"]
            assert abs(got - expected) < 0.15, (ln["text"], got, expected)


def test_failed_batch_split_falls_back_to_one_call_per_line(env, monkeypatch):
    from pipeline import narration as n
    real = n.tts

    def no_pauses(model, turns, seed):  # batch audio without clear gaps -> must not be split by guessing
        (pcm, rate), usage = real(model, turns, seed)
        return (pcm.replace(b"\x00\x00" * int(1.2 * rate), b"\x00\x00" * int(0.1 * rate)), rate), usage

    monkeypatch.setattr(n, "tts", no_pauses)
    ctx = load_storyboard(env.sb_path)
    n.run(ctx)
    # 4 lines: the retry batch (<= 4 lines) is the same request as the first one -> cache hit, no TTS call
    assert len(env.calls["tts"]) == 1 + 4, "failed batch, (cached) retry batch, then one call per line"
    log = [json.loads(x) for x in env.runs.read_text().splitlines()]
    assert [r["status"] for r in log if r["provider"] == "local"] == ["batch_split_failed"] * 2


def test_concurrent_processes_do_not_lose_manifest_entries(env):
    """A long-running stage (e.g. a 30 min Kaggle job) must not overwrite entries another stage added meanwhile."""
    from pipeline.common import Cache
    slow, fast = Cache(), Cache()          # both loaded the (empty) manifest at start
    fast.put("a" * 64, b"narration", ".wav", "narration")
    slow.put("b" * 64, b"clip", ".mp4", "shots")   # saved later from a stale in-memory copy
    fresh = Cache()
    assert fresh.path("a" * 64) and fresh.path("b" * 64)


def test_alignment_scores_are_logged_per_line(env):
    run_all(env.sb_path)
    align = [json.loads(x) for x in env.runs.read_text().splitlines() if '"mode": "align"' in x]
    assert len(align) == 1 and align[0]["status"] == "ok"
    assert set(align[0]["params"]["scores"]) == {"s01_1", "s01_2", "s02_1", "s03_1"}
    assert min(align[0]["params"]["scores"].values()) >= 0.8
