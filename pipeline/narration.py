"""Stage 1 (runs first): Gemini TTS -> one cached WAV per line, then timeline.json.

Batch mode (default; the free tier allows 10 TTS requests/day): all uncached lines go out in ONE
multi-speaker request (at most 2 speakers per request, e.g. MOM + NARRATOR), each line its own turn,
separated by <long pause> tags. The model does not keep those pauses consistent (FAILURES.md #13), so
the recording is split with a local forced alignment instead of silence alone:
  1. faster-whisper (local, no API) transcribes the recording with word timestamps;
  2. the heard words are aligned to the expected lines; at every line boundary the cut goes into the
     longest silence (ffmpeg silencedetect) between the last word of one line and the first of the next;
  3. every piece is re-transcribed on its own (no prompt) and must match its line (fuzzy score >= MATCH_MIN,
     scores logged) and be plausible for its word count.
Pieces that pass are stored under their own per-line cache key, so a later edit of one line costs one
single-line request. Lines that fail are re-requested in small batches (<= 4 lines), and only then one
request per line, and only while today's budget covers all of them.

out/narration/<shot>_<n>.wav  one line (preset voice + style of its speaker)
out/narration/<shot>.wav      the shot's lines joined with PAUSE_S of silence
out/timeline.json             shot duration = max(duration_s, narration + 0.5 s), snapped to frames;
                              absolute start/end of every line (subtitles, assembly) and music cue spans
"""
from __future__ import annotations

import argparse
import base64
import difflib
import io
import tempfile
import time
import json
import math
import os
import re
import statistics
import wave
from pathlib import Path

from . import av
from . import common as c

PAUSE_S = 0.4          # silence between two lines of the same shot
LEAD_S = 0.25          # narration starts this long after the cut
TAIL_S = 0.5           # spec: shot >= narration + 0.5 s
BATCH_PAUSE_TAG = "<long pause> <long pause>"  # one tag gave 1.0-2.0 s, as long as sentence pauses
MIN_GAP_S = 0.6        # a pause between two batch lines is at least this long...
GAP_MARGIN_S = 0.25    # ...and this much longer than any pause inside a line
EDGE_PAD_S = 0.05      # silence kept around each cut piece
SILENCE_DB = -40
MATCH_MIN = 0.80       # fuzzy transcript match a cut piece needs (tolerates "Ethan" for "Etan")
RETRY_BATCH = 4        # lines that fail alignment are re-requested in batches of at most this many


# ------------------------------------------------------------------ TTS -----
def tts(model: str, turns: list[dict], seed: int | None, timeout: float = 300):
    """One Gemini TTS request (Interactions API) for 1+ turns -> ((pcm16 bytes, rate), usage).

    turns: [{"text", "style", "speaker", "voice"}]. Text is spoken verbatim; style goes in the speech_metadata
    annotation (never read aloud). Two distinct speakers -> multi-speaker config. Preset voices only.
    Every turn but the last ends in BATCH_PAUSE_TAG. store=False: the request is not kept server-side.
    """
    speakers = list(dict.fromkeys(t["speaker"] for t in turns))
    if len(speakers) > 2:
        raise c.PipelineStop("multi-speaker TTS takes at most 2 speakers per request")
    multi = len(speakers) == 2
    voice_of = {t["speaker"]: t["voice"] for t in turns}
    speech = ({"speakers": [{"speaker": s, "voice": voice_of[s]} for s in speakers]} if multi
              else [{"voice": voice_of[speakers[0]]}])
    content = []
    for i, t in enumerate(turns):
        meta = {"type": "speech_metadata", "style": t["style"]} | ({"speaker": t["speaker"]} if multi else {})
        text = t["text"] + (f" {BATCH_PAUSE_TAG}" if i < len(turns) - 1 else "")
        content.append({"type": "text", "text": text, "annotations": [meta]})
    gen = {"speech_config": speech} | ({"seed": seed} if seed is not None else {})
    it = c.gemini_client().interactions.create(
        model=model, input=[{"type": "user_input", "content": content}],
        response_format={"type": "audio", "mime_type": "audio/l16", "sample_rate": 24000},
        generation_config=gen, store=False, api_version="v1beta", timeout=timeout)  # TTS is only on v1beta
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


# --------------------------------------------------------------- budget -----
def budget_line(ctx: c.Ctx) -> tuple[int | None, str]:
    """(remaining requests today or None if unlimited, human-readable line)."""
    cfg = c.model_cfg(ctx, "gemini_tts")
    limit = cfg.get("rpd")
    used, exhausted = c.requests_today(cfg["id"])
    if not limit:
        return None, f"TTS budget ({cfg['id']}): {used} used today, no daily limit configured"
    left = 0 if exhausted else max(0, limit - used)
    return left, (f"TTS budget ({cfg['id']}): {used}/{limit} used today, {left} left "
                  f"(resets {c.quota_reset_text()})")


def _spend(ctx: c.Ctx, what: str) -> None:
    """Refuse a request locally when today's budget is used up (a clean stop, nothing is sent)."""
    left, line = budget_line(ctx)
    print(f"  {line} -> {what}")
    if left == 0:
        c.log_call("narration", "gemini", c.model_cfg(ctx, "gemini_tts")["id"], params={"step": what},
                   status="quota_exhausted", error="local daily budget used up; nothing was sent")
        raise c.QuotaExhausted(f"today's TTS budget is used up ({line}). Cached lines are kept; "
                               "rerun `make narration` after the reset.")


# ---------------------------------------------------------------- split -----
def silences(wav: Path, min_d: float = 0.2) -> list[tuple[float, float]]:
    err = av.ffmpeg("-i", wav, "-af", f"silencedetect=noise={SILENCE_DB}dB:d={min_d}", "-f", "null", "-")
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    total = av.wav_duration(wav)
    ends += [total] * (len(starts) - len(ends))  # a silence running to the end has no silence_end line
    return [(max(0.0, s), e) for s, e in zip(starts, ends)]


def words(text: str) -> int:
    return len(re.sub(r"<[^>]+>", " ", text).split())


def plausible(dur: float, n_words: int) -> bool:
    return 0.15 * n_words + 0.15 <= dur <= 0.8 * n_words + 1.2


def split_batch(wav: Path, texts: list[str]) -> tuple[list[bytes] | None, str]:
    """Cut a batch recording into len(texts) pieces of PCM, or (None, reason). Never guesses."""
    n, total = len(texts), av.wav_duration(wav)
    sil = silences(wav)
    lead = next((e for s, e in sil if s <= 0.01), 0.0)
    trail = next((s for s, e in sil if e >= total - 0.01), total)
    inner = sorted((s for s in sil if s[0] > 0.01 and s[1] < total - 0.01), key=lambda s: s[1] - s[0], reverse=True)
    if len(inner) < n - 1:
        return None, f"found {len(inner)} pauses, need {n - 1}"
    cuts, rest = sorted(inner[: n - 1]), inner[n - 1:]
    if cuts:
        shortest = min(e - s for s, e in cuts)
        longest_other = max((e - s for s, e in rest), default=0.0)
        if shortest < MIN_GAP_S or shortest < longest_other + GAP_MARGIN_S:
            return None, (f"ambiguous pauses: shortest line gap {shortest:.2f} s vs longest other pause "
                          f"{longest_other:.2f} s (need >= {MIN_GAP_S} s and {GAP_MARGIN_S} s clearer)")
    bounds = [lead] + [x for s, e in cuts for x in (s, e)] + [trail]
    spans = [(bounds[2 * i], bounds[2 * i + 1]) for i in range(n)]
    durs = [b - a for a, b in spans]
    bad = [f"line {i + 1}: {d:.2f} s for {words(t)} words" for i, (d, t) in enumerate(zip(durs, texts)) if not plausible(d, words(t))]
    rates = [d / words(t) for d, t in zip(durs, texts) if words(t) >= 4]
    if len(rates) >= 3:
        med = statistics.median(rates)
        bad += [f"line {i + 1}: {d / words(t):.2f} s/word vs median {med:.2f}" for i, (d, t) in enumerate(zip(durs, texts))
                if words(t) >= 4 and not 0.5 * med <= d / words(t) <= 2.0 * med]
    if bad:
        return None, "implausible pieces: " + "; ".join(bad)
    with wave.open(str(wav)) as w:
        rate, pcm = w.getframerate(), w.readframes(w.getnframes())
    pieces = []
    for a, b in spans:
        i0, i1 = max(0, int((a - EDGE_PAD_S) * rate)), min(len(pcm) // 2, int((b + EDGE_PAD_S) * rate))
        pieces.append(pcm[2 * i0: 2 * i1])
    return pieces, f"{n} pieces, gaps >= {min((e - s for s, e in cuts), default=0):.2f} s"


# -------------------------------------------------------------- aligner -----
_whisper = None


def whisper(ctx: c.Ctx):
    global _whisper
    if _whisper is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise c.PipelineStop("splitting a batch recording needs faster-whisper: pip install -r requirements.txt")
        cfg = ctx.models.get("aligner", {})
        _whisper = WhisperModel(cfg.get("size", "small.en"), device="cpu", compute_type=cfg.get("compute_type", "int8"))
    return _whisper


def transcribe(ctx: c.Ctx, wav: Path) -> list[tuple[str, float, float]]:
    """Word timestamps for a WAV. Audio is decoded with our ffmpeg (16 kHz mono float) and passed as an array:
    faster-whisper's own decoder calls PyAV with an argument PyAV 19 removed (FAILURES.md #15)."""
    import numpy as np
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        av.ffmpeg("-i", wav, "-ar", 16000, "-ac", 1, "-c:a", "pcm_s16le", f.name)
        with wave.open(f.name) as w:
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    segs, _ = whisper(ctx).transcribe(audio, word_timestamps=True, beam_size=5, language="en",
                                      condition_on_previous_text=False)
    return [(w.word.strip(), w.start, w.end) for seg in segs for w in seg.words]


GEEZ = re.compile(r"[\u1200-\u137F\u1380-\u139F\u2D80-\u2DDF]")  # Ethiopic script blocks
SLOT = "\x00"  # placeholder for a Ge'ez word: what whisper (English) hears there is not held against the line


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", re.sub(r"<[^>]+>", " ", text).lower().replace("\u2019", "'"))


def slot_tokens(text: str) -> list[str]:
    """Tokens of the spoken text with every Ge'ez word replaced by SLOT."""
    out = []
    for w in re.sub(r"<[^>]+>", " ", text).split():
        out += [SLOT] if GEEZ.search(w) else tokens(w)
    return out


def match_score(expected: str, heard: str) -> float:
    """How well a transcript matches the spoken line, 0..1 (F-measure over words, order-aware).

    Heard words that are near-misses of an expected word (character ratio >= 0.75, e.g. "Nolawe") count as that
    word; up to 2 heard words in the place of each Ge'ez word are ignored (an English model cannot spell them)."""
    exp, got = slot_tokens(expected), tokens(heard)
    vocab = [t for t in exp if t != SLOT]
    got = [t if t in vocab else max(vocab, key=lambda v: difflib.SequenceMatcher(None, t, v).ratio(), default=t)
           if vocab and max(difflib.SequenceMatcher(None, t, v).ratio() for v in vocab) >= 0.75 else t for t in got]
    sm = difflib.SequenceMatcher(None, exp, got, autojunk=False)
    matched = bad = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            matched += i2 - i1
            continue
        slots = exp[i1:i2].count(SLOT)
        bad += (i2 - i1 - slots) + max(0, (j2 - j1) - 2 * slots)
    return round(matched / (matched + bad), 3) if matched + bad else 1.0


def align_split(ctx: c.Ctx, wav: Path, texts: list[str]) -> tuple[list[bytes | None], list[dict]]:
    """Cut a batch recording into one piece per line using word timestamps; verify every piece.

    Returns (pieces, report): pieces[i] is PCM or None when line i failed; report[i] has score/heard/reason."""
    n, total = len(texts), av.wav_duration(wav)
    heard = transcribe(ctx, wav)
    h_tok = [(tokens(w) or [""])[0] for w, _, _ in heard]
    e_tok, e_line = [], []
    for i, t in enumerate(texts):
        for tok in tokens(t):
            e_tok.append(tok)
            e_line.append(i)
    matched = {}  # expected token index -> heard word index
    for a, b, size in difflib.SequenceMatcher(None, e_tok, h_tok, autojunk=False).get_matching_blocks():
        for k in range(size):
            matched[a + k] = b + k
    first = [min((matched[j] for j in matched if e_line[j] == i), default=None) for i in range(n)]
    last = [max((matched[j] for j in matched if e_line[j] == i), default=None) for i in range(n)]
    sil = silences(wav)
    lead = next((e for s0, e in sil if s0 <= 0.01), 0.0)
    trail = next((s0 for s0, e in sil if e >= total - 0.01), total)
    bounds, report = [lead], [{} for _ in range(n)]
    for i in range(n - 1):
        if last[i] is None or first[i + 1] is None:
            bounds += [None, None]
            continue
        lo, hi = heard[last[i]][2], heard[first[i + 1]][1]  # end of line i, start of line i+1 (whisper times)
        window = [(s0, e) for s0, e in sil if e > lo - 0.3 and s0 < hi + 0.3]
        if window:  # the longest silence near the boundary
            s0, e = max(window, key=lambda x: min(x[1], hi + 0.3) - max(x[0], lo - 0.3))
            bounds += [s0, e]
        else:
            mid = (lo + hi) / 2
            bounds += [mid, mid]
    bounds.append(trail)
    with wave.open(str(wav)) as w:
        rate, pcm = w.getframerate(), w.readframes(w.getnframes())
    pieces = []
    for i, t in enumerate(texts):
        a, b = bounds[2 * i], bounds[2 * i + 1]
        if a is None or b is None or b <= a:
            report[i] = {"score": 0.0, "reason": "line not found in the transcript"}
            pieces.append(None)
            continue
        i0, i1 = max(0, int((a - EDGE_PAD_S) * rate)), min(len(pcm) // 2, int((b + EDGE_PAD_S) * rate))
        piece = pcm[2 * i0: 2 * i1]
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:  # verify independently: re-transcribe the cut piece
            f.write(pcm_to_wav(piece, rate))
            f.flush()
            said = " ".join(w for w, _, _ in transcribe(ctx, Path(f.name)))
        score, dur = match_score(t, said), (i1 - i0) / rate
        ok = score >= MATCH_MIN and plausible(dur, words(t))
        report[i] = {"score": score, "heard": said, "dur_s": round(dur, 2),
                     **({} if ok else {"reason": f"score {score} < {MATCH_MIN}" if score < MATCH_MIN else f"{dur:.2f} s implausible"})}
        pieces.append(piece if ok else None)
    return pieces, report


# ---------------------------------------------------------------- items -----
def items(ctx: c.Ctx) -> list[dict]:
    """One item per line. "text" here is what is SPOKEN (tts_text if set, else text); subtitles use the line's text."""
    model = c.model_cfg(ctx, "gemini_tts")["id"]
    out = []
    for shot in ctx.sb["shots"]:
        for n, line in enumerate(shot["lines"], 1):
            v = ctx.sb["narration"]["voices"][line["speaker"]]
            style = v["style_instruction"] + (f"; {line['style_note']}" if line.get("style_note") else "")
            params = {"voice": v["voice"], "style": style}
            spoken = line.get("tts_text", line["text"])
            out.append({"id": f"{shot['id']}_{n}", "shot": shot["id"], "seed": shot["seed"], "speaker": line["speaker"],
                        "text": spoken, "voice": v["voice"], "style": style, "params": params,
                        "key": c.Cache.key("narration", model, params, spoken, shot["seed"])})
    return out


def speaker_groups(todo: list[dict]) -> list[list[dict]]:
    """Consecutive runs with at most 2 distinct speakers (the multi-speaker limit)."""
    groups = []
    for it in todo:
        if groups and len({x["speaker"] for x in groups[-1]} | {it["speaker"]}) <= 2:
            groups[-1].append(it)
        else:
            groups.append([it])
    return groups


def _call(ctx: c.Ctx, turns: list[dict], seed: int, shot_id: str, params: dict):
    cfg = c.model_cfg(ctx, "gemini_tts")
    s = seed if cfg.get("accepts_seed", True) else None
    return c.call_model(lambda: tts(cfg["id"], turns, s), stage="narration", provider="gemini", model=cfg["id"],
                        throttle_key="gemini_tts", models=ctx.models, params=params, price_kind="gemini_tts",
                        shot_id=shot_id, seed=seed, max_attempts=cfg.get("max_attempts", 2))


def batch(ctx: c.Ctx, group: list[dict]) -> set[str]:
    """One request for the group; returns the line keys it produced (empty if the split failed)."""
    cfg = c.model_cfg(ctx, "gemini_tts")
    ids = [it["id"] for it in group]
    turns = [{k: it[k] for k in ("text", "style", "speaker", "voice")} for it in group]
    bkey = c.Cache.key("narration_batch", cfg["id"], {"pause": BATCH_PAUSE_TAG, "turns": [
        [t["speaker"], t["voice"], t["style"]] for t in turns]}, "\n".join(t["text"] for t in turns), group[0]["seed"])
    obj = ctx.cache.lookup(bkey)
    if obj:
        c.log_hit("narration", "gemini", cfg["id"], bkey, shot_id="batch", params={"mode": "batch", "lines": ids})
    else:
        _spend(ctx, f"1 batch request for {len(group)} lines")
        pcm, rate = _call(ctx, turns, group[0]["seed"], "batch",
                          {"mode": "batch", "lines": ids, "voices": {t["speaker"]: t["voice"] for t in turns},
                           "prompt": "\n".join(f"{t['speaker']}: {t['text']}" for t in turns)})
        obj = ctx.cache.put(bkey, pcm_to_wav(pcm, rate), ".wav", "narration_batch", keep=False, lines=ids)
    t0 = time.monotonic()
    pieces, report = align_split(ctx, obj, [t["text"] for t in turns])
    aligner = f"faster-whisper/{ctx.models.get('aligner', {}).get('size', 'small.en')}"
    scores = {it["id"]: r.get("score") for it, r in zip(group, report)}
    failed = [it["id"] for it, p in zip(group, pieces) if p is None]
    c.log_call("narration", "local", aligner, shot_id="batch", latency_ms=(time.monotonic() - t0) * 1000,
               status="batch_split_failed" if failed else "ok", error=(f"{len(failed)} line(s) failed: " + "; ".join(
                   f"{i}: {r.get('reason')}" for i, r in zip(ids, report) if r.get("reason"))) if failed else None,
               params={"mode": "align", "lines": ids, "batch_key": bkey[:16], "match_min": MATCH_MIN, "scores": scores,
                       "heard": {it["id"]: r.get("heard") for it, r in zip(group, report)}})
    with wave.open(str(obj)) as w:
        rate = w.getframerate()
    done = set()
    for it, pcm, r in zip(group, pieces, report):
        if pcm is not None:
            ctx.cache.put(it["key"], pcm_to_wav(pcm, rate), ".wav", "narration", duration_s=round(len(pcm) / 2 / rate, 3),
                          mode="batch", batch_key=bkey[:16], match_score=r["score"])
            done.add(it["key"])
    print(f"  aligned {len(done)}/{len(group)} lines (scores {min(scores.values()):.2f}-{max(scores.values()):.2f})"
          + (f"; failed: {', '.join(failed)}" if failed else ""))
    return done


def synthesize(ctx: c.Ctx, use_batch: bool = True) -> None:
    model = c.model_cfg(ctx, "gemini_tts")["id"]
    todo = []
    for it in items(ctx):
        if ctx.cache.lookup(it["key"]):
            c.log_hit("narration", "gemini", model, it["key"], shot_id=it["shot"], seed=it["seed"])
        else:
            todo.append(it)
    if not todo:
        return
    print(f"narration: {len(todo)} line(s) to synthesise")
    done = set()
    if use_batch and len(todo) > 1:
        for group in speaker_groups(todo):
            if len(group) > 1:
                done |= batch(ctx, group)
        failed = [it for it in todo if it["key"] not in done]
        if len(failed) > 1:  # option 3: re-request only the failed lines, <= 4 per request
            for group in speaker_groups(failed):
                for k in range(0, len(group), RETRY_BATCH):
                    chunk = group[k:k + RETRY_BATCH]
                    if len(chunk) > 1:
                        done |= batch(ctx, chunk)
    rest = [it for it in todo if it["key"] not in done]
    left, line = budget_line(ctx)
    if len(rest) > 1 and left is not None and left < len(rest):  # a half-finished narration is useless
        raise c.QuotaExhausted(f"{len(rest)} lines still need one request each but only {left} are left today "
                               f"({line}). Nothing was sent; cached lines are kept.")
    for it in rest:
        _spend(ctx, f"1 request for line {it['id']}")
        pcm, rate = _call(ctx, [{k: it[k] for k in ("text", "style", "speaker", "voice")}], it["seed"], it["shot"],
                          {**it["params"], "prompt": it["text"], "line": it["id"]})
        ctx.cache.put(it["key"], pcm_to_wav(pcm, rate), ".wav", "narration", duration_s=round(len(pcm) / 2 / rate, 3),
                      mode="single")


# ------------------------------------------------------------- timeline -----
def run(ctx: c.Ctx, use_batch: bool = True) -> dict:
    synthesize(ctx, use_batch)
    all_items = items(ctx)
    ndir = ctx.out / "narration"
    keep, shots_tl, t = set(), [], 0.0
    fps = ctx.sb["outputs"]["fps"]
    for shot in ctx.sb["shots"]:
        line_paths, lines_tl = [], []
        for it in (x for x in all_items if x["shot"] == shot["id"]):
            obj = ctx.cache.path(it["key"])
            name = f"{it['id']}.wav"
            c.link(obj, ndir / name)
            keep.add(name)
            line_paths.append(obj)
            line = shot["lines"][int(it["id"].rsplit("_", 1)[1]) - 1]
            lines_tl.append({"speaker": line["speaker"], "text": line["text"], "spoken": it["text"],
                             "subtitle": line.get("subtitle_en", line["text"]),
                             "wav": f"narration/{name}", "sha256": ctx.cache.sha(it["key"]), "dur": _dur(obj)})
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
    n_words = sum(len(ln["text"].split()) for s in ctx.sb["shots"] for ln in s["lines"])
    print(f"narration: {sum(len(s['lines']) for s in shots_tl)} lines, {n_words} words -> timeline {total:.1f} s "
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


def voice_test(ctx: c.Ctx, speaker: str, voices: list[str], line_id: str) -> None:
    """ONE two-speaker request in which each candidate voice reads the same line -> out/voice_test/."""
    if len(voices) != 2:
        raise c.PipelineStop("--voice-test compares exactly 2 preset voices, e.g. NARRATOR=Charon,Achird")
    it = next((x for x in items(ctx) if x["id"] == line_id), None)
    if it is None or it["speaker"] != speaker:
        raise c.PipelineStop(f"{line_id} is not a {speaker} line (ids look like <shot>_<n>)")
    cfg = c.model_cfg(ctx, "gemini_tts")
    turns = [{"text": it["text"], "style": it["style"], "speaker": f"VOICE_{v.upper()}", "voice": v} for v in voices]
    key = c.Cache.key("voice_test", cfg["id"], {"voices": voices, "style": it["style"], "pause": BATCH_PAUSE_TAG},
                      it["text"], it["seed"])
    obj = ctx.cache.lookup(key)
    if obj:
        c.log_hit("narration", "gemini", cfg["id"], key, shot_id="voice_test")
    else:
        _spend(ctx, f"1 two-voice request ({' vs '.join(voices)})")
        pcm, rate = _call(ctx, turns, it["seed"], "voice_test",
                          {"mode": "voice_test", "voices": voices, "line": line_id, "prompt": it["text"], "style": it["style"]})
        obj = ctx.cache.put(key, pcm_to_wav(pcm, rate), ".wav", "voice_test", line=line_id, voices=voices)
    c.link(obj, ctx.out / "voice_test" / "both.wav")
    pieces, why = split_batch(obj, [it["text"]] * 2)
    if pieces is None:
        c.log_call("narration", "ffmpeg", "silencedetect", shot_id="voice_test", status="batch_split_failed", error=why)
        print(f"voice test: could not split ({why}); listen to {os.path.relpath(ctx.out / 'voice_test' / 'both.wav', c.ROOT)}")
        return
    with wave.open(str(obj)) as w:
        rate = w.getframerate()
    for v, pcm in zip(voices, pieces):
        pkey = c.Cache.key("voice_test_piece", "local", {"voice": v}, None, None, {"recording": ctx.cache.sha(key)})
        p = ctx.cache.lookup(pkey) or ctx.cache.put(pkey, pcm_to_wav(pcm, rate), ".wav", "voice_test")
        c.link(p, ctx.out / "voice_test" / f"{speaker}_{v}.wav")
        print(f"  {speaker} = {v}: {len(pcm) / 2 / rate:.2f} s -> "
              f"{os.path.relpath(ctx.out / 'voice_test' / f'{speaker}_{v}.wav', c.ROOT)}")
    print(f"voice test ({why}); run_id {ctx.cache.entry(key).get('run_id')}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    c.storyboard_arg(ap)
    ap.add_argument("--no-batch", action="store_true", help="one request per line")
    ap.add_argument("--budget", action="store_true", help="print today's TTS budget and exit")
    ap.add_argument("--voice-test", metavar="SPEAKER=V1,V2", help="one request: two preset voices read the same line")
    ap.add_argument("--line", help="with --voice-test: line id <shot>_<n> (default: the speaker's first line)")
    a = ap.parse_args()
    ctx = c.load_storyboard(a.storyboard)
    print(budget_line(ctx)[1])
    if a.budget:
        return
    if a.voice_test:
        spk, _, vs = a.voice_test.partition("=")
        line = a.line or next(x["id"] for x in items(ctx) if x["speaker"] == spk)
        voice_test(ctx, spk, [v.strip() for v in vs.split(",") if v.strip()], line)
        return
    run(ctx, use_batch=not a.no_batch)


if __name__ == "__main__":
    c.stage_main(main)
