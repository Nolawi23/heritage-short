"""Radio cut: the narration over a quiet placeholder drone on a black screen -> out/radio_cut.mp4.

For testing the story by ear before any image or score exists (needs only `make narration`).
Same narration stem as the film (line timings, per-speaker effect), not part of `make film`.
"""
from __future__ import annotations

import argparse
import shutil

from . import av
from . import common as c
from .narration import load_timeline


def run(ctx: c.Ctx) -> None:
    tl = load_timeline(ctx)
    key = c.Cache.key("radio", "ffmpeg", {"voices": ctx.sb["narration"]["voices"]}, None, None,
                      {"timeline": c.sha256_file(ctx.out / "timeline.json"), "code": c.source_hash("radio_cut", "av")})
    obj = ctx.cache.lookup(key)
    if obj:
        c.log_hit("radio", "ffmpeg", "local", key)
    else:
        av.require_filters()
        tmp = c.outputs_root() / ".tmp" / f"radio_{c.RUN_ID}"
        tmp.mkdir(parents=True, exist_ok=True)
        total = tl["total_s"]
        narr, pad, out = tmp / "narration.wav", tmp / "pad.wav", tmp / "radio_cut.mp4"
        av.narration_stem(tl, ctx.sb["narration"]["voices"], narr)
        av.placeholder_pad(total, pad)
        av.ffmpeg("-f", "lavfi", "-i", f"color=c=black:s=640x360:r=24:d={total}", "-i", narr, "-i", pad,
                  "-filter_complex", "[1:a]aformat=channel_layouts=stereo[n];[2:a]volume=-14dB[p];"
                  "[n][p]amix=inputs=2:normalize=0:duration=first,loudnorm=I=-16:TP=-1.5,aresample=48000[a]",
                  "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
                  "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-t", total, "-movflags", "+faststart", out)
        obj = ctx.cache.put(key, out, ".mp4", "radio", duration_s=total)
        c.log_call("radio", "ffmpeg", "local", params={"total_s": total})
        shutil.rmtree(tmp, ignore_errors=True)
    c.link(obj, ctx.out / "radio_cut.mp4")
    print(f"radio cut: {tl['total_s']:.1f} s -> {ctx.out.relative_to(c.ROOT) if ctx.out.is_relative_to(c.ROOT) else ctx.out}/radio_cut.mp4")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    c.storyboard_arg(ap)
    run(c.load_storyboard(ap.parse_args().storyboard))


if __name__ == "__main__":
    c.stage_main(main)
