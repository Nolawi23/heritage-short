"""Stage 1 (runs first): Gemini TTS, one call per line, cached per line.

out/narration/<shot>_<n>.wav  one line (preset voice + style instruction of its speaker)
out/narration/<shot>.wav      the shot's lines joined with PAUSE_S of silence
out/timeline.json             shot duration = max(duration_s, narration + 0.5 s), snapped to frames;
                              absolute start/end of every line (subtitles, assembly) and music cue spans
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import wave
from pathlib import Path

from . import common as c

PAUSE_S = 0.4   # silence between two lines of the same shot
LEAD_S = 0.25   # narration starts this long after the cut
TAIL_S = 0.5    # spec: shot >= narration + 0.5 s


def tts(model: str, text: str, style: str, voice: str, seed: int | None):
    """One Gemini TTS call (Interactions API) -> ((pcm16 bytes, sample_rate), usage).

    The text is spoken verbatim; the speaker's style_instruction goes in the speech_metadata annotation,
    so it can never be read aloud. Preset voice names only. store=False: the request is not kept server-side.
    """
    gen = {"speech_config": [{"voice": voice}]}
    if seed is not None:
        gen["seed"] = seed
    it = c.gemini_client().interactions.create(
        model=model,
        input=[{"type": "user_input", "content": [
            {"type": "text", "text": text, "annotations": [{"type": "speech_metadata", "style": style}]}]}],
        response_format={"type": "audio", "mime_type": "audio/l16", "sample_rate": 24000},
        generation_config=gen, store=False, api_version="v1beta", timeout=120)  # TTS is only served on v1beta
    audio = getattr(it, "output_audio", None)
    if audio is None or not audio.data:
        raise c.ProviderError(f"TTS returned no audio (status={getattr(it, 'status', '?')})", 500)
    data = audio.data if isinstance(audio.data, bytes) else base64.b64decode(audio.data)
    rate = getattr(audio, "sample_rate", None) or 24000
    if data[:4] == b"RIFF":  # a WAV came back after all: unwrap it
        with wave.open(io.BytesIO(data)) as w:
            rate, data = w.getframerate(), w.readframes(w.getnframes())
    u = getattr(it, "usage", None)
    usage = {"input_tokens": getattr(u, "total_input_tokens", 0) or 0, "output_tokens": getattr(u, "total_output_tokens", 0) or 0}
    return (data, rate), usage


def pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    """Gemini TTS returns raw signed 16-bit little-endian mono PCM; wrap it in a WAV header."""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def join_wavs(paths: list[Path], pause_s: float) -> bytes:
    frames, rate = [], None
    for i, p in enumerate(paths):
        with wave.open(str(p)) as w:
            if rate is None:
                rate = w.getframerate()
            if w.getframerate() != rate or w.getsampwidth() != 2 or w.getnchannels() != 1:
                raise c.PipelineStop(f"{p.name}: unexpected WAV format for joining")
            if i:
                frames.append(b"\x00\x00" * int(rate * pause_s))
            frames.append(w.readframes(w.getnframes()))
    return pcm_to_wav(b"".join(frames), rate or 24000)


def line_key(ctx: c.Ctx, shot: dict, line: dict) -> tuple[str, str, dict]:
    voice = ctx.sb["narration"]["voices"][line["speaker"]]
    model = c.model_cfg(ctx, "gemini_tts")["id"]
    params = {"voice": voice["voice"], "style": voice["style_instruction"]}
    return c.Cache.key("narration", model, params, line["text"], shot["seed"]), line["text"], params


def run(ctx: c.Ctx) -> dict:
    model = c.model_cfg(ctx, "gemini_tts")["id"]
    ndir = ctx.out / "narration"
    keep, shots_tl, t = set(), [], 0.0
    fps = ctx.sb["outputs"]["fps"]
    for shot in ctx.sb["shots"]:
        line_paths, lines_tl = [], []
        for n, line in enumerate(shot["lines"], 1):
            key, prompt, params = line_key(ctx, shot, line)
            obj = ctx.cache.lookup(key)
            if obj:
                c.log_hit("narration", "gemini", model, key, shot_id=shot["id"], seed=shot["seed"])
            else:
                voice, style = params["voice"], params["style"]
                seed = shot["seed"] if c.model_cfg(ctx, "gemini_tts").get("accepts_seed", True) else None
                pcm, rate = c.call_model(lambda: tts(model, prompt, style, voice, seed), stage="narration",
                                         provider="gemini", model=model, throttle_key="gemini_tts", models=ctx.models,
                                         params={**params, "prompt": prompt, "line": n}, price_kind="gemini_tts",
                                         shot_id=shot["id"], seed=shot["seed"])
                wav = pcm_to_wav(pcm, rate)
                obj = ctx.cache.put(key, wav, ".wav", "narration", duration_s=round(len(pcm) / 2 / rate, 3))
            name = f"{shot['id']}_{n}.wav"
            c.link(obj, ndir / name)
            keep.add(name)
            line_paths.append(obj)
            lines_tl.append({"speaker": line["speaker"], "text": line["text"],
                             "subtitle": line.get("subtitle_en", line["text"]),
                             "wav": f"narration/{name}", "sha256": ctx.cache.sha(key),
                             "dur": _dur(obj)})
        narr = 0.0
        if line_paths:  # joined shot narration (derived, cached by its inputs)
            jkey = c.Cache.key("narration_join", "local", {"pause_s": PAUSE_S}, None, None,
                               {str(i): ln["sha256"] for i, ln in enumerate(lines_tl)})
            jobj = ctx.cache.lookup(jkey) or ctx.cache.put(jkey, join_wavs(line_paths, PAUSE_S), ".wav", "narration_join")
            c.link(jobj, ndir / f"{shot['id']}.wav")
            keep.add(f"{shot['id']}.wav")
            narr = _dur(jobj)
        dur = max(shot["duration_s"], narr + TAIL_S) if line_paths else shot["duration_s"]
        dur = math.ceil(round(dur * fps, 6)) / fps  # whole frames, so the cut list never drifts
        cursor = t + LEAD_S
        for ln in lines_tl:
            ln["start_s"], ln["end_s"] = round(cursor, 3), round(cursor + ln["dur"], 3)
            cursor += ln.pop("dur") + PAUSE_S
        shots_tl.append({"id": shot["id"], "start_s": round(t, 3), "duration_s": round(dur, 4),
                         "narration_s": round(narr, 3), "motion": shot["motion"], "music_cue": shot["music_cue"],
                         "lines": lines_tl})
        t += dur
    c.prune_links(ndir, keep)
    end = ctx.sb["end_card"]["duration_s"]
    total = round(t + end, 4)
    spans = []
    for s in shots_tl:  # contiguous runs of the same cue; the last one also covers the end card
        if spans and spans[-1]["cue"] == s["music_cue"]:
            spans[-1]["end_s"] = round(s["start_s"] + s["duration_s"], 4)
        else:
            spans.append({"cue": s["music_cue"], "start_s": s["start_s"], "end_s": round(s["start_s"] + s["duration_s"], 4)})
    spans[-1]["end_s"] = total
    timeline = {"storyboard": os.path.relpath(ctx.path, c.ROOT), "fps": fps, "total_s": total,
                "shots": shots_tl, "end_card": {"start_s": round(t, 4), "duration_s": end}, "music_spans": spans}
    c.write_text(ctx.out / "timeline.json", json.dumps(timeline, indent=2, ensure_ascii=False) + "\n")
    words = sum(len(ln["text"].split()) for s in ctx.sb["shots"] for ln in s["lines"])
    print(f"narration: {sum(len(s['lines']) for s in shots_tl)} lines, {words} words -> timeline {total:.1f} s "
          f"({os.path.relpath(ctx.out / 'timeline.json', c.ROOT)})")
    return timeline


def _dur(p: Path) -> float:
    with wave.open(str(p)) as w:
        return round(w.getnframes() / w.getframerate(), 3)


def load_timeline(ctx: c.Ctx) -> dict:
    p = ctx.out / "timeline.json"
    if not p.exists():
        raise c.PipelineStop(f"{os.path.relpath(p, c.ROOT)} missing: run `make narration` first")
    tl = json.loads(p.read_text())
    for s in tl["shots"]:
        for ln in s["lines"]:
            ln["wav_abs"] = str(Path(os.path.realpath(ctx.out / ln["wav"])))
    return tl


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    c.storyboard_arg(ap)
    run(c.load_storyboard(ap.parse_args().storyboard))


if __name__ == "__main__":
    c.stage_main(main)
