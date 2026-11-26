"""`make last-run`: the most recent run's lines from runs.jsonl as a table (no network)."""
import json

import pytest

from pipeline import common as c


def _runs(tmp_path, monkeypatch, rows):
    p = tmp_path / "runs.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setenv("HS_RUNS", str(p))


def test_last_run_shows_only_the_latest_run(tmp_path, monkeypatch, capsys):
    ts = "2026-11-26T09:00:00+00:00"
    _runs(tmp_path, monkeypatch, [
        {"ts": ts, "run_id": "old", "stage": "smoke", "provider": "gemini", "status": "ok", "cache_hit": False},
        {"ts": ts, "run_id": "new", "stage": "narration", "shot_id": "shot-01", "provider": "gemini", "status": "cached",
         "cache_hit": True},
        {"ts": ts, "run_id": "new", "stage": "keyframes", "shot_id": "shot-02", "provider": "cloudflare", "status": "ok",
         "cache_hit": False},
        {"ts": ts, "run_id": "new", "stage": "assemble", "provider": "ffmpeg", "status": "ok", "cache_hit": False},
    ])
    c.last_run()
    out = capsys.readouterr().out
    assert "run new: 3 line(s)" in out and "smoke" not in out
    assert "cache hit" in out and "new call" in out and "local step" in out
    assert out.strip().endswith("1 new call(s), 1 cache hit(s), 1 local step(s); 0 not ok")


def test_last_run_unknown_id_stops(tmp_path, monkeypatch):
    _runs(tmp_path, monkeypatch, [{"ts": "2026-11-26T09:00:00+00:00", "run_id": "a", "stage": "x", "status": "ok"}])
    with pytest.raises(c.PipelineStop):
        c.last_run("nope")
