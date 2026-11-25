"""End-to-end ffmpeg assembly on synthetic media (tiny resolutions, ~10 s). Skipped without ffmpeg-full.

Checks: both aspect ratios + blur variant render, constant 24 fps, exact timeline length, AAC audio,
-14 LUFS / -1 dBTP within tolerance, SRT written, radio cut renders, and a rerun is a cache hit.
"""
import json
import subprocess

import pytest

from pipeline import assemble, av, keyframes, music, narration, radio_cut, shots
from pipeline.common import load_storyboard

pytestmark = pytest.mark.skipif(bool(av.missing_filters()), reason="ffmpeg lacks libass/drawtext (brew install ffmpeg-full)")


@pytest.fixture
def media(env, tmp_path, monkeypatch):
    sb = json.loads(env.sb_path.read_text())
    sb["outputs"]["resolutions"] = {"16:9": [320, 180], "9:16": [180, 320]}
    sb["music_cues"][0]["duration_s"] = 30
    env.sb_path.write_text(json.dumps(sb))

    def still(cfg, prompt, seed, w, h, refs=()):
        out = tmp_path / f"kf_{seed}.png"
        av.ffmpeg("-f", "lavfi", "-i", f"testsrc2=s=640x360:d=1", "-frames:v", 1, out)
        return out.read_bytes(), {}

    def kaggle(stage, notebook, jobs, config, models, files=None):
        out = tmp_path / "k" / stage
        (out / "out").mkdir(parents=True, exist_ok=True)
        lines = []
        for j in jobs:
            if stage == "wan":  # a 3 s clip: shorter than its shot -> trim, slow down, Ken Burns tail
                av.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x176:r=24:d=3", "-pix_fmt", "yuv420p", out / "out" / f"{j['id']}.mp4")
                f = f"out/{j['id']}.mp4"
            else:  # 12 s of "music": shorter than the span -> joined with crossfades
                av.ffmpeg("-f", "lavfi", "-i", "sine=f=220:d=12:r=48000", "-ac", 2, out / "out" / f"{j['id']}.wav")
                f = f"out/{j['id']}.wav"
            lines.append({"job_id": "t", "id": j["id"], "status": "ok", "attempt": 1, "seed": j["seed"],
                          "gpu_seconds": 1, "file": f})
        return {"results": {x["id"]: x for x in lines}, "lines": lines, "dir": out, "status": "complete",
                "failure": "", "wall_s": 1, "kernel": "u/k", "job_id": "t"}

    monkeypatch.setattr(keyframes, "flux_generate", still)
    monkeypatch.setattr(shots.kaggle_job, "run", kaggle)
    monkeypatch.setenv("VERTICAL", "both")
    return env


def probe(path):
    out = subprocess.run([av.FFPROBE, "-v", "error", "-show_entries", "stream=codec_name,avg_frame_rate,width,height:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_full_assembly_on_synthetic_media(media):
    ctx = load_storyboard(media.sb_path)
    tl = narration.run(ctx)
    keyframes.run(ctx)
    shots.run(ctx)
    music.run(ctx)
    radio_cut.run(ctx)
    assemble.run(ctx)
    out = media.tmp / "outputs"
    for name, size in (("film_16x9.mp4", (320, 180)), ("film_9x16.mp4", (180, 320)), ("film_9x16_blur.mp4", (180, 320))):
        info = probe(out / name)
        v = next(s for s in info["streams"] if s["codec_name"] == "h264")
        assert (v["width"], v["height"]) == size and v["avg_frame_rate"] == "24/1"
        assert any(s["codec_name"] == "aac" for s in info["streams"])
        assert abs(float(info["format"]["duration"]) - tl["total_s"]) < 0.1, name
    loud = av.measure_loudness(out / "film_16x9.mp4")
    assert abs(float(loud["input_i"]) + 14) < 1.0 and float(loud["input_tp"]) <= -0.5
    assert (out / "subtitles.srt").read_text().startswith("1\n")
    assert (out / "radio_cut.mp4").exists()
    fits = [json.loads(x) for x in media.runs.read_text().splitlines() if '"fit"' in x]
    assert fits and fits[0]["params"]["speed"] == 1.3 and fits[0]["params"]["kenburns_tail_s"] > 0
    n = len(media.runs.read_text().splitlines())
    assemble.run(load_storyboard(media.sb_path))  # rerun: all films are cache hits
    assert all(json.loads(x)["cache_hit"] for x in media.runs.read_text().splitlines()[n:])
