"""Stage 5: ffmpeg assembly -> out/film_16x9.mp4 + out/film_9x16.mp4 (VERTICAL=blur|both adds film_9x16_blur.mp4).

Per shot (lengths from timeline.json, whole frames):
  wan clip longer than the shot : drop up to 0.5 s weak frames from each end, then trim the rest from the end
  wan clip shorter              : trim 0.125 s from each end, slow down <= 1.25x (motion-interpolated above 1.05x),
                                  cover any remaining gap with a slow Ken Burns push on the last frame (never a freeze)
  kenburns shot / wan fallback  : pan-and-zoom of the ORIGINAL keyframe, cropped per aspect ratio, varied direction
Audio: narration stem (per-speaker effect) and music bed brought to -20 LUFS, music ducked under the narration
  (sidechaincompress threshold 0.02, ratio 6, attack 300 ms, release 1500 ms), mixed, then two-pass loudnorm
  to -14 LUFS with a -1.5 dBTP target (delivered peak under -1 dBTP). A score shorter than its span is joined with
  2 s crossfades, cut at music_cues[].clean_until_s when the section's end is damaged. ambience[] beds (committed
  recordings, e.g. a family murmur) play under their shots inside the music bed, so they are ducked with it. Subtitles: SRT from the line timings (<= 2 lines x ~40 chars), burned in from an ASS
  file styled per aspect (9:16 kept inside the safe area). End card ("Made with AI") drawn in code.
9:16 Wan clips: centre crop (film_9x16.mp4) or sharp fit on a blurred fill (film_9x16_blur.mp4); Ken Burns shots
  are always cropped from the original keyframe. Export: H.264 + AAC, constant 24 fps. Each film is cached by
  the hashes of everything it is made from.
PREVIEW=1 assembles before Kaggle has run: missing Wan clips -> Ken Burns, missing score -> placeholder pad.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import textwrap
import time
from pathlib import Path

from . import av
from . import common as c
from .keyframes import keyframe_path
from .music import selected_path
from .narration import load_timeline
from .shots import clip_status

SHORT_TRIM_S = 0.125
LONG_TRIM_S = 0.5
MAX_SLOW = 1.25  # author's rule (2026-11-26): no strong slow motion; the rest is a Ken Burns move on the last frame
INTERP_ABOVE = 1.05
TAIL_ZOOM = 0.05
NARR_LUFS, MUSIC_LUFS = -20.0, -20.0
FINAL = "I=-14:TP=-1.5:LRA=11"  # -1.5 dBTP target so the delivered peak stays under -1 dBTP (author, 2026-11-26)
DUCK = "sidechaincompress=threshold=0.02:ratio=6:attack=300:release=1500"
MUSIC_XFADE_S = 2.0
MUSIC_FADE_IN_S = 0.5  # default fade-in of a music span; the first one can be longer (music_fade_in_s)
MUSIC_DIP_RAMP_S = 1.0  # ramp into and out of a per-shot music dip (shots[].music_gain_db)
AMB_LEVEL_DB, AMB_LOWPASS_HZ, AMB_FADE_S = -4.0, 2500, 1.0  # ambience[] defaults: dB vs the music level, low-pass, fades
CHARS_PER_LINE = 40
SUB_STYLE = {"16:9": {"size": 34, "margin_v": 42, "margin_lr": 90},
             "9:16": {"size": 30, "margin_v": 330, "margin_lr": 50}}  # 9:16: clear of the bottom ~25% UI zone
FINAL_X264 = ("-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-profile:v", "high")


def preview() -> bool:
    return os.environ.get("PREVIEW", "0") == "1"


def films_wanted() -> list[tuple[str, str]]:
    v = os.environ.get("VERTICAL", "both")
    if v not in ("crop", "blur", "both"):
        raise c.PipelineStop("VERTICAL must be crop, blur or both")
    return [("16:9", "fill")] + [("9:16", m) for m in (("crop", "blur") if v == "both" else (v,))]


def film_name(aspect: str, mode: str) -> str:
    base = "preview" if preview() else "film"
    return f"{base}_{aspect.replace(':', 'x')}{'_blur' if mode == 'blur' else ''}.mp4"


# ------------------------------------------------------------------ fit -----
def fit_plan(clip_s: float, target_s: float) -> dict:
    if clip_s >= target_s:
        excess = clip_s - target_s
        head = min(LONG_TRIM_S, excess / 2)
        return {"clip_s": round(clip_s, 3), "target_s": round(target_s, 3), "trim_head_s": round(head, 3),
                "trim_tail_s": round(excess - head, 3), "speed": 1.0, "interpolate": False, "kenburns_tail_s": 0.0}
    head = tail = SHORT_TRIM_S
    rem = clip_s - head - tail
    speed = min(MAX_SLOW, target_s / rem)
    return {"clip_s": round(clip_s, 3), "target_s": round(target_s, 3), "trim_head_s": head, "trim_tail_s": tail,
            "speed": round(speed, 4), "interpolate": speed > INTERP_ABOVE,
            "kenburns_tail_s": round(max(0.0, target_s - rem * speed), 3)}


def fitted_clip(clip: Path, target_s: float, fps: int, tmp: Path, sid: str) -> tuple[Path, dict]:
    """Fit a Wan clip to exactly target_s in its own resolution (aspect-independent)."""
    plan = fit_plan(av.duration(clip), target_s)
    n_total = av.frames(target_s, fps)
    n_tail = av.frames(plan["kenburns_tail_s"], fps) if plan["kenburns_tail_s"] > 0.5 / fps else 0
    n_main = n_total - n_tail
    keep_s = plan["clip_s"] - plan["trim_head_s"] - plan["trim_tail_s"]
    vf = f"setpts={plan['speed']}*PTS," + (f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
                                           if plan["interpolate"] else f"fps={fps}")
    main = tmp / f"fit_{sid}_main.mp4"
    av.ffmpeg("-ss", plan["trim_head_s"], "-t", round(keep_s, 3), "-i", clip,
              "-vf", vf + ",tpad=stop_mode=clone:stop_duration=0.2", "-frames:v", n_main, *av.X264_INTERMEDIATE, "-an", main)
    if not n_tail:
        return main, plan
    last, tail, out = tmp / f"fit_{sid}_last.png", tmp / f"fit_{sid}_tail.mp4", tmp / f"fit_{sid}.mp4"
    av.ffmpeg("-sseof", "-0.2", "-i", main, "-update", 1, "-frames:v", 1, last)
    w, h = _video_size(main)
    av.kenburns(last, tail, n_tail / fps, w, h, fps, "in", zoom=TAIL_ZOOM)
    av.ffmpeg("-i", main, "-i", tail, "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
              *av.X264_INTERMEDIATE, out)
    return out, plan


def _video_size(p: Path) -> tuple[int, int]:
    import subprocess
    out = subprocess.run([av.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "json", str(p)], capture_output=True, text=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    return s["width"], s["height"]


# ------------------------------------------------------------ subtitles -----
def subtitle_cues(tl: dict) -> list[tuple[float, float, str]]:
    cues = []
    for s in tl["shots"]:
        for ln in s["lines"]:
            rows = textwrap.wrap(ln["subtitle"], CHARS_PER_LINE, break_long_words=False)
            chunks = [rows[i:i + 2] for i in range(0, len(rows), 2)]
            weights = [len(" ".join(ch)) for ch in chunks]
            t, span = ln["start_s"], ln["end_s"] - ln["start_s"]
            for ch, wgt in zip(chunks, weights):
                d = span * wgt / sum(weights)
                cues.append([t, t + d, "\n".join(ch)])
                t += d
    for a, b in zip(cues, cues[1:]):  # hold each cue a little, never over the next one
        a[1] = min(a[1] + 0.4, b[0] - 0.05)
    if cues:
        cues[-1][1] += 0.4
    return [(round(a, 3), round(b, 3), t) for a, b, t in cues]


def _ts(t: float, sep: str, cs: bool = False) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}" if cs else f"{int(h):02d}:{int(m):02d}:{int(s):02d}{sep}{int(round((s % 1) * 1000)):03d}"


def srt(cues) -> str:
    return "".join(f"{i}\n{_ts(a, ',')} --> {_ts(b, ',')}\n{t}\n\n" for i, (a, b, t) in enumerate(cues, 1))


def ass(cues, w: int, h: int, aspect: str) -> str:
    st = SUB_STYLE[aspect]
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,Helvetica,{st['size']},&H00FFFFFF,&H000000FF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,"
            f"{st['margin_lr']},{st['margin_lr']},{st['margin_v']},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    body = "".join(f"Dialogue: 0,{_ts(a, '.', True)},{_ts(b, '.', True)},Default,,0,0,0,,"
                   f"{t.replace('{', '(').replace('}', ')').replace(chr(10), chr(92) + 'N')}\n" for a, b, t in cues)
    return head + body


# -------------------------------------------------------------- end card ----
def end_card(text: str, w: int, h: int, fps: int, seconds: float, out: Path, tmp: Path) -> None:
    width = 22 if w < h else 40
    (tmp / "end1.txt").write_text("\n".join(r for part in text.split("\n") for r in textwrap.wrap(part, width)))
    (tmp / "end2.txt").write_text("Made with AI")
    s = min(w, h)
    f1, f2 = round(s * 0.075), round(s * 0.045)
    vf = (f"drawtext=textfile=end1.txt:font=Helvetica:fontsize={f1}:fontcolor=white:line_spacing={f1 // 4}:"
          f"x=(w-text_w)/2:y=h/2-text_h-{f2 // 2},"
          f"drawtext=textfile=end2.txt:font=Helvetica:fontsize={f2}:fontcolor=white@0.85:x=(w-text_w)/2:y=h/2+{f2},"
          f"fade=t=in:st=0:d=0.4")
    av.ffmpeg("-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={fps}:d={seconds}", "-vf", vf,
              "-frames:v", av.frames(seconds, fps), *av.X264_INTERMEDIATE, out.name, cwd=tmp)


# ----------------------------------------------------------------- audio ----
def music_tail(length: float, fade_in: float = MUSIC_FADE_IN_S) -> str:
    """Trim a music span to its length, fade it in, and fade it out over its last 2 s."""
    return (f"atrim=0:{length:.3f},afade=t=in:d={fade_in:g},afade=t=out:st={max(0.0, length - 2):.3f}:d=2,"
            "aformat=channel_layouts=stereo")


def fit_music(src: Path, length: float, out: Path, fade_in: float = MUSIC_FADE_IN_S,
              clean_until: float | None = None) -> None:
    """Fit a music section to a span: trim it, or join copies of it with crossfades. clean_until
    (music_cues[].clean_until_s) drops a damaged end of the section (MusicGen's end-of-generation whistle,
    FAILURES.md #33), so every join crossfades back to the start before the damage."""
    d = av.duration(src)
    if clean_until:
        d = min(d, clean_until)
    tail = music_tail(length, fade_in)
    if d >= length:
        av.ffmpeg("-i", src, "-af", tail, "-ar", 48000, out)
        return
    n = math.ceil((length - MUSIC_XFADE_S) / (d - MUSIC_XFADE_S))  # join sections with crossfades
    chain = [f"[{i}:a]atrim=0:{d:.3f},asetpts=PTS-STARTPTS[s{i}]" for i in range(n + 1)]
    prev = "s0"
    for i in range(1, n + 1):
        chain.append(f"[{prev}][s{i}]acrossfade=d={MUSIC_XFADE_S}[x{i}]")
        prev = f"x{i}"
    args = [a for _ in range(n + 1) for a in ("-i", src)]
    av.ffmpeg(*args, "-filter_complex", ";".join(chain) + f";[{prev}]{tail}[o]", "-map", "[o]", "-ar", 48000, out)


def _gain(path: Path, target: float) -> float:
    m = av.measure_loudness(path)
    try:
        i = float(m["input_i"])
    except ValueError:
        return 0.0
    return 0.0 if math.isinf(i) else round(target - i, 2)


def music_dips(sb: dict, tl: dict) -> str:
    """shots[].music_gain_db lowers the score under those shots (after gain matching, before ducking). Consecutive
    shots with the same value form one window; the gain ramps over MUSIC_DIP_RAMP_S just outside it, so the window
    itself is at the full dip. Returns an ffmpeg ',volume=...' filter, or '' when no shot sets one."""
    gains = {s["id"]: s.get("music_gain_db", 0) for s in sb["shots"]}
    windows: list[list[float]] = []
    for s in tl["shots"]:
        g, start, end = gains.get(s["id"], 0), s["start_s"], s["start_s"] + s["duration_s"]
        if g and windows and windows[-1][2] == g and abs(windows[-1][1] - start) < 1e-3:
            windows[-1][1] = end
        elif g:
            windows.append([start, end, g])
    r = MUSIC_DIP_RAMP_S
    terms = [f"(1+({10 ** (g / 20):.4f}-1)*clip((t-{a - r:.3f})/{r:g},0,1)*clip(({b + r:.3f}-t)/{r:g},0,1))"
             for a, b, g in windows]
    return f",volume='{'*'.join(terms)}':eval=frame" if terms else ""


def ambience_windows(sb: dict, tl: dict) -> list[tuple[dict, float, float]]:
    """ambience[]: each bed plays from the start of its first shot to the end of its last one."""
    by_id = {s["id"]: s for s in tl["shots"]}
    out = []
    for a in sb.get("ambience", []):
        spans = [by_id[i] for i in a["shots"]]
        out.append((a, min(s["start_s"] for s in spans), max(s["start_s"] + s["duration_s"] for s in spans)))
    return out


def ambience_filter(a: dict, length: float) -> str:
    """Trim (the input loops), low-pass so no word is intelligible, fade in and out inside the window."""
    f = a.get("fade_s", AMB_FADE_S)
    return (f"atrim=0:{length:.3f},asetpts=PTS-STARTPTS,lowpass=f={a.get('lowpass_hz', AMB_LOWPASS_HZ)},"
            f"afade=t=in:d={f:g},afade=t=out:st={max(0.0, length - f):.3f}:d={f:g},aformat=channel_layouts=stereo")


def ambience_stem(sb: dict, tl: dict, tmp: Path, out: Path) -> dict | None:
    """All ambience beds on one track, each gain-matched to MUSIC_LUFS + level_db. None if the storyboard has none."""
    windows = ambience_windows(sb, tl)
    if not windows:
        return None
    args, parts, gains = [], [], {}
    for i, (a, start, end) in enumerate(windows):
        piece = tmp / f"ambience_{i}.wav"
        av.ffmpeg("-stream_loop", -1, "-i", c.repo_path(a["file"]), "-af", ambience_filter(a, end - start),
                  "-ar", 48000, "-ac", 2, piece)
        gains[a["id"]] = _gain(piece, MUSIC_LUFS + a.get("level_db", AMB_LEVEL_DB))
        args += ["-i", piece]
        parts.append(f"[{i}:a]volume={gains[a['id']]}dB,adelay={round(start * 1000)}:all=1[a{i}]")
    mix = "".join(f"[a{i}]" for i in range(len(parts)))
    av.ffmpeg(*args, "-filter_complex", ";".join(parts) + f";{mix}amix=inputs={len(parts)}:normalize=0,"
              f"apad=whole_dur={tl['total_s']},atrim=0:{tl['total_s']}[o]", "-map", "[o]", "-ar", 48000, "-ac", 2, out)
    return gains


def build_audio(ctx: c.Ctx, tl: dict, music: dict, tmp: Path) -> tuple[Path, dict]:
    total = tl["total_s"]
    narr, bed, pre, out = tmp / "narration.wav", tmp / "music.wav", tmp / "premix.wav", tmp / "mix.wav"
    av.narration_stem(tl, ctx.sb["narration"]["voices"], narr)
    cues = {cue["id"]: cue for cue in ctx.sb["music_cues"]}
    parts, args = [], []
    after = ctx.sb.get("music_start_after_line")
    start0 = next((ln["end_s"] + 0.3 for s in tl["shots"] for ln in s["lines"]
                   if after and ln["wav"] == f"narration/{after}.wav"), None)
    if after and start0 is None:
        raise c.PipelineStop(f"music_start_after_line: no line {after} in the timeline")
    for i, sp in enumerate(tl["music_spans"]):
        seg = tmp / f"music_{i}.wav"
        if i == 0 and start0:
            sp = sp | {"start_s": start0}  # the hook (e.g. Mom's call) plays over silence
        length = sp["end_s"] - sp["start_s"]
        fade_in = ctx.sb.get("music_fade_in_s", MUSIC_FADE_IN_S) if i == 0 else MUSIC_FADE_IN_S
        if music.get(sp["cue"]):
            fit_music(music[sp["cue"]], length, seg, fade_in, cues[sp["cue"]].get("clean_until_s"))
        else:
            av.placeholder_pad(length, seg)
        args += ["-i", seg]
        parts.append(f"[{i}:a]adelay={round(sp['start_s'] * 1000)}:all=1[m{i}]")
    mix = "".join(f"[m{i}]" for i in range(len(parts)))
    av.ffmpeg(*args, "-filter_complex", ";".join(parts) + f";{mix}amix=inputs={len(parts)}:normalize=0,"
              f"apad=whole_dur={total},atrim=0:{total}[o]", "-map", "[o]", "-ar", 48000, "-ac", 2, bed)
    gn, gm = _gain(narr, NARR_LUFS), _gain(bed, MUSIC_LUFS)
    amb = tmp / "ambience.wav"
    amb_gains = ambience_stem(ctx.sb, tl, tmp, amb)
    bedmix = "[m0][2:a]amix=inputs=2:normalize=0:duration=first[m];" if amb_gains else "[m0]anull[m];"
    av.ffmpeg("-i", narr, "-i", bed, *(("-i", amb) if amb_gains else ()), "-filter_complex",
              f"[0:a]volume={gn}dB,aformat=channel_layouts=stereo,asplit=2[sc][n];"
              f"[1:a]volume={gm}dB{music_dips(ctx.sb, tl)},aformat=channel_layouts=stereo[m0];{bedmix}"
              f"[m][sc]{DUCK}[d];"  # ambience sits in the bed, so it is ducked under the narration with the music
              f"[d][n]amix=inputs=2:normalize=0:duration=first[o]", "-map", "[o]", "-ar", 48000, pre)
    m = av.measure_loudness(pre, targets=FINAL)  # pass 1, same targets as pass 2
    av.ffmpeg("-i", pre, "-af", f"loudnorm={FINAL}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
              f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:"
              "linear=true,aresample=48000", "-ar", 48000, out)  # pass 2
    final = av.measure_loudness(out)
    return out, {"narration_gain_db": gn, "music_gain_db": gm, **({"ambience_gain_db": amb_gains} if amb_gains else {}),
                 "pass1_I": m["input_i"],
                 "final_I": final["input_i"], "final_TP": final["input_tp"], "final_LRA": final["input_lra"]}


# ----------------------------------------------------------------- video ----
def sources(ctx: c.Ctx, tl: dict) -> dict:
    """Per shot: keyframe + clip (or None -> Ken Burns) and why."""
    shots = {s["id"]: s for s in ctx.sb["shots"]}
    out = {}
    for s in tl["shots"]:
        shot = shots[s["id"]]
        src = {"keyframe": keyframe_path(ctx, shot), "clip": None, "source": "kenburns"}
        if shot["motion"] == "wan":
            status, key = clip_status(ctx, shot)
            if status == "clip":
                src |= {"clip": ctx.cache.path(key), "source": "wan"}
            elif status == "fallback":
                src["source"] = "kenburns (wan fallback)"
            elif preview():
                src["source"] = "kenburns (preview, wan pending)"
            else:
                raise c.PipelineStop(f"Wan clip for {s['id']} not rendered yet: run `make shots` "
                                     "(or PREVIEW=1 make assemble to preview with Ken Burns)")
        out[s["id"]] = src
    return out


def music_sources(ctx: c.Ctx) -> dict:
    out = {}
    for cue in ctx.sb["music_cues"]:
        p = selected_path(ctx, cue)
        if p is None and not preview():
            raise c.PipelineStop(f"music cue '{cue['id']}' has no selected candidate: run `make music` "
                                 "(or PREVIEW=1 make assemble to preview with a placeholder pad)")
        out[cue["id"]] = p
    return out


def vertical_override(ctx: c.Ctx, shot_id: str, aspect: str, mode: str) -> tuple[str, float]:
    """Per-shot 9:16 treatment of a Wan clip: shots[].vertical_fill "blur" (even in the crop film) and
    shots[].vertical_x (crop window, -1 left .. 0 centre .. 1 right). 16:9 is never changed."""
    if aspect != "9:16":
        return mode, 0.0
    shot = next(s for s in ctx.sb["shots"] if s["id"] == shot_id)
    return ("blur" if shot.get("vertical_fill") == "blur" else mode), float(shot.get("vertical_x", 0.0))


def render(ctx: c.Ctx, tl: dict, srcs: dict, aspect: str, mode: str, audio: Path, cues, tmp: Path, plans: dict) -> Path:
    w, h = ctx.sb["outputs"]["resolutions"][aspect]
    fps = ctx.sb["outputs"]["fps"]
    tag = aspect.replace(":", "x") + mode
    segs = []
    for i, s in enumerate(tl["shots"]):
        src, n = srcs[s["id"]], av.frames(s["duration_s"], fps)
        seg = tmp / f"seg_{tag}_{i:02d}.mp4"
        if src["clip"]:
            if s["id"] not in plans:
                plans[s["id"]] = fitted_clip(src["clip"], s["duration_s"], fps, tmp, s["id"])
            fitted = plans[s["id"]][0]
            m, x = vertical_override(ctx, s["id"], aspect, mode)
            av.ffmpeg("-i", fitted, "-vf", av.reframe_filter(w, h, m, x) + ",tpad=stop_mode=clone:stop_duration=0.2",
                      "-frames:v", n, "-r", fps, *av.X264_INTERMEDIATE, "-an", seg)
        else:
            direction = av.KB_DIRECTIONS[i % len(av.KB_DIRECTIONS)]
            plans.setdefault(s["id"], (None, {"source": src["source"], "direction": direction}))
            # every aspect ratio is cropped straight from the original keyframe (blur-fill is only for Wan clips,
            # which exist in 16:9 only)
            av.kenburns(src["keyframe"], seg, s["duration_s"], w, h, fps, direction, crop_aspect=w / h)
        segs.append(seg)
    card = tmp / f"seg_{tag}_end.mp4"
    end_card(ctx.sb["end_card"]["text"], w, h, fps, ctx.sb["end_card"]["duration_s"], card, tmp)
    segs.append(card)
    (tmp / f"list_{tag}.txt").write_text("".join(f"file '{p.name}'\n" for p in segs))
    (tmp / f"subs_{tag}.ass").write_text(ass(cues, w, h, aspect))
    out = tmp / f"film_{tag}.mp4"
    av.ffmpeg("-f", "concat", "-safe", "0", "-i", f"list_{tag}.txt", "-i", audio.name, "-vf", f"ass=subs_{tag}.ass",
              "-map", "0:v", "-map", "1:a",
              *FINAL_X264, "-r", fps, "-fps_mode", "cfr", "-c:a", "aac", "-b:a", "192k", "-ar", 48000,
              "-movflags", "+faststart", "-t", tl["total_s"], out.name, cwd=tmp)
    return out


def film_key(ctx: c.Ctx, tl_sha: str, srcs: dict, music: dict, aspect: str, mode: str) -> str:
    upstream = {"timeline": tl_sha, "code": c.source_hash("assemble", "av")}
    for sid, s in srcs.items():
        upstream[f"kf:{sid}"] = c.sha256_file(s["keyframe"])
        upstream[f"clip:{sid}"] = c.sha256_file(s["clip"]) if s["clip"] else s["source"]
    for cid, p in music.items():
        upstream[f"music:{cid}"] = c.sha256_file(p) if p else "placeholder"
    for a in ctx.sb.get("ambience", []):
        upstream[f"ambience:{a['id']}"] = c.sha256_file(c.repo_path(a["file"]))
    params = {"aspect": aspect, "mode": mode, "end_card": ctx.sb["end_card"], "outputs": ctx.sb["outputs"],
              "voices": ctx.sb["narration"]["voices"], "preview": preview(),
              "music_start_after_line": ctx.sb.get("music_start_after_line"),
              "music_fade_in_s": ctx.sb.get("music_fade_in_s"),
              "vertical_shots": {s["id"]: [s.get("vertical_fill"), s.get("vertical_x")] for s in ctx.sb["shots"]
                                 if "vertical_fill" in s or "vertical_x" in s},
              "music_gains": {s["id"]: s["music_gain_db"] for s in ctx.sb["shots"] if s.get("music_gain_db")},
              "music_clean_until": {cue["id"]: cue["clean_until_s"] for cue in ctx.sb["music_cues"]
                                    if cue.get("clean_until_s")},
              "ambience": ctx.sb.get("ambience", [])}
    return c.Cache.key("assemble", "ffmpeg", params, None, None, upstream)


def plan_keys(ctx: c.Ctx) -> dict:
    """Cache keys of the films this storyboard would produce (used by tests and run())."""
    tl = load_timeline(ctx)
    tl_sha = c.sha256_file(ctx.out / "timeline.json")
    srcs, music = sources(ctx, tl), music_sources(ctx)
    return {film_name(a, m): film_key(ctx, tl_sha, srcs, music, a, m) for a, m in films_wanted()}


def run(ctx: c.Ctx) -> None:
    tl = load_timeline(ctx)
    tl_sha = c.sha256_file(ctx.out / "timeline.json")
    srcs, music = sources(ctx, tl), music_sources(ctx)
    cues = subtitle_cues(tl)
    c.write_text(ctx.out / "subtitles.srt", srt(cues))
    todo = []
    for aspect, mode in films_wanted():
        key = film_key(ctx, tl_sha, srcs, music, aspect, mode)
        obj = ctx.cache.lookup(key)
        if obj:
            c.log_hit("assemble", "ffmpeg", "local", key, params={"film": film_name(aspect, mode)})
            c.link(obj, ctx.out / film_name(aspect, mode))
        else:
            todo.append((aspect, mode, key))
    if not todo:
        print("assemble: all films cached")
        return
    av.require_filters()
    tmp = c.outputs_root() / ".tmp" / f"assemble_{c.RUN_ID}"
    tmp.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    audio, loud = build_audio(ctx, tl, music, tmp)
    plans = {}
    for aspect, mode, key in todo:
        t1 = time.monotonic()
        film = render(ctx, tl, srcs, aspect, mode, audio, cues, tmp, plans)
        obj = ctx.cache.put(key, film, ".mp4", "assemble", duration_s=round(av.duration(film), 3), loudness=loud)
        c.link(obj, ctx.out / film_name(aspect, mode))
        c.log_call("assemble", "ffmpeg", "local", params={"film": film_name(aspect, mode), "aspect": aspect,
                                                           "vertical_mode": mode, **loud},
                   latency_ms=(time.monotonic() - t1) * 1000)
        print(f"  {film_name(aspect, mode)}: {av.duration(film):.2f} s, {obj.stat().st_size / 1e6:.1f} MB")
    for sid, (_, plan) in plans.items():  # what was applied to each shot
        c.log_call("assemble", "ffmpeg", "fit", shot_id=sid, params=plan)
    print(f"assemble: done in {time.monotonic() - t0:.0f} s, loudness {loud['final_I']} LUFS / {loud['final_TP']} dBTP")
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    c.storyboard_arg(ap)
    run(c.load_storyboard(ap.parse_args().storyboard))


if __name__ == "__main__":
    c.stage_main(main)
