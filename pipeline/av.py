"""ffmpeg/ffprobe helpers shared by radio_cut.py and assemble.py (no model calls here).

FFMPEG / FFPROBE can be overridden; by default Homebrew's ffmpeg-full is preferred because the
regular formula lacks libass (subtitles filter) and freetype (drawtext filter).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

from .common import PipelineStop

REQUIRED_FILTERS = ("subtitles", "ass", "drawtext", "sidechaincompress", "loudnorm", "zoompan", "minterpolate",
                    "gblur", "acrossfade", "adelay", "amix")
INSTALL_HINT = "brew install ffmpeg-full   (or set FFMPEG=/path/to/ffmpeg FFPROBE=/path/to/ffprobe)"


def _find(tool: str) -> str:
    env = os.environ.get(tool.upper())
    if env:
        return env
    for prefix in ("/opt/homebrew/opt/ffmpeg-full/bin", "/usr/local/opt/ffmpeg-full/bin"):
        if Path(prefix, tool).exists():
            return str(Path(prefix, tool))
    return shutil.which(tool) or tool


FFMPEG, FFPROBE = _find("ffmpeg"), _find("ffprobe")


def missing_filters() -> list[str]:
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return list(REQUIRED_FILTERS)
    have = {line.split()[1] for line in out.splitlines() if len(line.split()) > 2 and line.startswith(" ")}
    return [f for f in REQUIRED_FILTERS if f not in have]


def require_filters() -> None:
    missing = missing_filters()
    if missing:
        raise PipelineStop(f"{FFMPEG} lacks filters {', '.join(missing)}. Install: {INSTALL_HINT}")


def ffmpeg(*args: str, cwd: Path | None = None) -> str:
    """Run ffmpeg quietly; return stderr (loudnorm prints its JSON there)."""
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", *map(str, args)]
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise PipelineStop("ffmpeg failed:\n  " + " ".join(cmd[:12]) + " ...\n" + p.stderr[-1500:])
    return p.stderr


def duration(path: Path) -> float:
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


def wav_duration(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def frames(seconds: float, fps: int) -> int:
    return max(1, round(seconds * fps))


# ------------------------------------------------------------- loudness -----
def measure_loudness(path: Path, filt: str = "") -> dict:
    """First loudnorm pass: integrated loudness, true peak, LRA, threshold."""
    err = ffmpeg("-i", path, "-af", f"{filt + ',' if filt else ''}loudnorm=I=-14:TP=-1:LRA=11:print_format=json",
                 "-f", "null", "-")
    return json.loads(err[err.rindex("{"):err.rindex("}") + 1])


# ---------------------------------------------------------- Ken Burns -------
KB_DIRECTIONS = ("in", "right", "out", "left", "up", "down")


def kenburns_filter(w: int, h: int, n: int, fps: int, direction: str, zoom: float = 0.12, crop_aspect: float | None = None) -> str:
    """Slow pan-and-zoom of a still. crop_aspect crops the still to the target aspect first (9:16 from the
    original keyframe); the still is upscaled 3x before zoompan to avoid integer-pixel jitter."""
    t = f"(on/{max(n - 1, 1)})"
    cx, cy = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    z, x, y = {
        "in": (f"1+{zoom}*{t}", cx, cy),
        "out": (f"{1 + zoom}-{zoom}*{t}", cx, cy),
        "right": (f"{1 + zoom * 0.8}", f"(iw-iw/zoom)*{t}", cy),
        "left": (f"{1 + zoom * 0.8}", f"(iw-iw/zoom)*(1-{t})", cy),
        "up": (f"{1 + zoom * 0.8}", cx, f"(ih-ih/zoom)*(1-{t})"),
        "down": (f"{1 + zoom * 0.8}", cx, f"(ih-ih/zoom)*{t}"),
    }[direction]
    a = crop_aspect or w / h
    return (f"crop='min(iw,ih*{a:.6f})':'min(ih,iw/{a:.6f})',scale={w * 3}:{h * 3}:flags=lanczos,"
            f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={w}x{h}:fps={fps},setsar=1,format=yuv420p")


X264_INTERMEDIATE = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p")


def kenburns(still: Path, out: Path, seconds: float, w: int, h: int, fps: int, direction: str,
             zoom: float = 0.12, crop_aspect: float | None = None) -> None:
    n = frames(seconds, fps)
    ffmpeg("-i", still, "-vf", kenburns_filter(w, h, n, fps, direction, zoom, crop_aspect),
           "-frames:v", n, "-r", fps, *X264_INTERMEDIATE, "-an", out)


def reframe_filter(w: int, h: int, mode: str) -> str:
    """Fit a (16:9) source into w x h. fill = scale + centre crop; blur = sharp fit on a blurred fill."""
    if mode == "blur":
        return (f"split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                f"gblur=sigma=30,eq=brightness=-0.06[bg];[b]scale={w}:-2:flags=lanczos[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p")
    return f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},setsar=1,format=yuv420p"


def placeholder_pad(seconds: float, out: Path) -> None:
    """Quiet synthetic drone (D-A-D) used by the radio cut and by PREVIEW assemblies without a score."""
    s = round(seconds, 3)
    src = [a for f in (146.83, 220.0, 293.66) for a in ("-f", "lavfi", "-i", f"sine=f={f}:d={s}:r=48000")]
    ffmpeg(*src, "-filter_complex",
           f"[0][1][2]amix=inputs=3,tremolo=f=0.15:d=0.5,lowpass=f=900,volume=0.3,"
           f"afade=t=in:d=2,afade=t=out:st={max(0.0, s - 2)}:d=2,aformat=channel_layouts=stereo[o]",
           "-map", "[o]", "-ar", 48000, out)


# ---------------------------------------------------------- narration -------
EFFECTS = {
    "none": "",
    # subtle "calling from downstairs": band-limited, slightly quieter, a short room reflection
    "downstairs": "highpass=f=180,lowpass=f=1800,aecho=0.8:0.55:35|70:0.22|0.12,volume=0.85,",
}


def narration_stem(timeline: dict, voices: dict, out: Path, sample_rate: int = 48000) -> None:
    """All narration lines placed at their timeline positions, per-speaker effect applied, mono."""
    lines = [ln for s in timeline["shots"] for ln in s["lines"]]
    total = timeline["total_s"]
    if not lines:
        ffmpeg("-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono", "-t", total, out)
        return
    args, parts = [], []
    for i, ln in enumerate(lines):
        args += ["-i", ln["wav_abs"]]
        fx = EFFECTS[voices[ln["speaker"]].get("effect", "none")]
        ms = round(ln["start_s"] * 1000)
        parts.append(f"[{i}:a]aresample={sample_rate},aformat=channel_layouts=mono,{fx}adelay={ms}:all=1[l{i}]")
    mix = "".join(f"[l{i}]" for i in range(len(lines)))
    graph = ";".join(parts) + f";{mix}amix=inputs={len(lines)}:normalize=0:duration=longest,apad=whole_dur={total},atrim=0:{total}[out]"
    ffmpeg(*args, "-filter_complex", graph, "-map", "[out]", "-ar", sample_rate, "-ac", 1, out)


if __name__ == "__main__":
    if "--check" in sys.argv:
        missing = missing_filters()
        if missing:
            print(f"WARNING: {FFMPEG} lacks filters: {', '.join(missing)}\n  Install: {INSTALL_HINT}\n"
                  "  (Not needed to rebuild the film from the committed cache; needed to re-assemble.)")
        else:
            print(f"ffmpeg ok: {FFMPEG} has all required filters")
