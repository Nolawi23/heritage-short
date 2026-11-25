"""Test harness: an isolated outputs/ + runs.jsonl in tmp, and fake providers that count their calls."""
import json
import math
import types
import wave
from array import array
from pathlib import Path

import pytest

from pipeline import kaggle_job, keyframes, narration

ROOT = Path(__file__).resolve().parent.parent
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                    "0000000d49444154789c63600000000200015e2a0a5b0000000049454e44ae426082")


@pytest.fixture
def env(tmp_path, monkeypatch):
    sb = json.loads((ROOT / "tests" / "fixtures" / "storyboard.json").read_text())
    ref = tmp_path / "ref.png"
    ref.write_bytes(PNG)
    for ch in sb["characters"]:
        ch["reference_image"] = str(ref)  # pretend the reference sheet is approved
    for cue in sb["music_cues"]:
        cue["selected"] = 1               # pretend candidate 1 was chosen
    sb_path = tmp_path / "storyboard.json"
    sb_path.write_text(json.dumps(sb, indent=2))
    models = json.loads((ROOT / "config" / "models.json").read_text())
    models |= {"gemini_text": {"id": "test-text"}, "gemini_tts": {"id": "test-tts"},
               "flux": {"id": "test-flux", "api": "multipart", "accepts_reference_images": True,
                        "max_reference_images": 4, "width": 1792, "height": 1008},
               "min_interval_s": {}}
    models_path = tmp_path / "models.json"
    models_path.write_text(json.dumps(models))
    for k, v in {"HS_OUTPUTS": tmp_path / "outputs", "HS_RUNS": tmp_path / "runs.jsonl",
                 "HS_DEFAULT_STORYBOARD": sb_path, "HS_MODELS": models_path, "FORCE": "0", "PREVIEW": "0",
                 "VERTICAL": "crop"}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.delenv("STORYBOARD", raising=False)
    calls = {"tts": [], "flux": [], "kaggle": []}

    recordings = {}  # pcm bytes -> [(text, start_s, end_s)]: what the fake "said" and when

    def fake_tts(model, turns, seed):
        """Speech-like audio: a tone per turn (0.3 s/word), 1.2 s of silence between turns, one request each."""
        calls["tts"].append([t["text"] for t in turns])
        rate, pcm, layout = 24000, bytearray(), []
        for i, t in enumerate(turns):
            if i:
                pcm += b"\x00\x00" * int(1.2 * rate)
            n = int(rate * (0.2 + 0.3 * len(t["text"].split())))
            layout.append((t["text"], len(pcm) / 2 / rate, (len(pcm) / 2 + n) / rate))
            f = 180 + 37 * (sum(map(ord, t["text"])) % 20)  # a distinct tone per line, so pieces are unambiguous
            pcm += array("h", (int(8000 * math.sin(2 * math.pi * f * k / rate)) for k in range(n))).tobytes()
        recordings[bytes(pcm)] = layout
        return (bytes(pcm), rate), {"input_tokens": 10, "output_tokens": 50}

    def fake_transcribe(ctx, wav):
        """Stand-in for faster-whisper: word timestamps from the known layout, also for a cut piece of a recording."""
        with wave.open(str(wav)) as w:
            rate, pcm = w.getframerate(), w.readframes(w.getnframes())
        for full, layout in recordings.items():
            at = full.find(pcm)
            if at < 0 or at % 2:
                continue
            off, dur = at / 2 / rate, len(pcm) / 2 / rate
            out = []
            for text, a, b in layout:
                ws = text.split()
                for k, word in enumerate(ws):
                    s0, e0 = a + (b - a) * k / len(ws), a + (b - a) * (k + 1) / len(ws)
                    if off <= (s0 + e0) / 2 <= off + dur:
                        out.append((word, s0 - off, e0 - off))
            return out
        return []  # audio the fake never produced: nothing intelligible

    def fake_flux(cfg, prompt, seed, w, h, refs=()):
        calls["flux"].append(prompt)
        return PNG + str(seed).encode(), {"output_tiles": 8}

    def fake_kaggle(stage, notebook, jobs, config, models, files=None, gpu=True):
        calls["kaggle"].append([j["id"] for j in jobs])
        out = tmp_path / "kaggle_out" / stage
        (out / "out").mkdir(parents=True, exist_ok=True)
        ext = ".mp4" if stage == "wan" else ".wav"
        lines = [{"job_id": "t", "id": None, "event": "setup", "seconds": 5}]
        for j in jobs:
            (out / "out" / f"{j['id']}{ext}").write_bytes(f"fake {j['id']} {j['seed']}".encode())
            lines.append({"job_id": "t", "id": j["id"], "status": "ok", "attempt": 1, "seed": j["seed"],
                          "gpu_seconds": 12.5, "file": f"out/{j['id']}{ext}", "frames": 121, "duration_s": 30})
        lines.append({"job_id": "t", "id": None, "event": "done", "seconds": 60})
        return {"results": {ln["id"]: ln for ln in lines if ln.get("id")}, "lines": lines, "dir": out,
                "status": "complete", "failure": "", "wall_s": 61, "kernel": f"u/heritage-short-{stage}", "job_id": "t"}

    monkeypatch.setattr(narration, "tts", fake_tts)
    monkeypatch.setattr(narration, "transcribe", fake_transcribe)
    monkeypatch.setattr(keyframes, "flux_generate", fake_flux)
    monkeypatch.setattr(kaggle_job, "run", fake_kaggle)
    return types.SimpleNamespace(tmp=tmp_path, sb_path=sb_path, runs=tmp_path / "runs.jsonl", calls=calls)
