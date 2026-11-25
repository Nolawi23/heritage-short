"""Stage 3: Wan 2.2 TI2V-5B image-to-video on a Kaggle T4, for shots with motion "wan".

Cache key per clip = (Wan model + settings, motion_prompt, seed, sha256 of the keyframe). Clips have a
fixed length (models.json wan.frames) and are fitted to the timeline in assembly, so narration edits
never re-render video. Only shots whose key is missing go to Kaggle, in ONE kernel run.
A clip that still fails after max_attempts is marked "fallback" in the manifest (not retried unless
FORCE=1) and assembly uses a Ken Burns move on the keyframe instead. "kenburns" shots are rendered
entirely in assembly (from the original keyframe, per aspect ratio).

--test renders one short clip (49 frames, ~2 s) of the first wan shot to measure T4 time; it is
stored in .scratch and never linked into the film.
"""
from __future__ import annotations

import argparse

from . import common as c
from . import kaggle_job
from .keyframes import keyframe_path

NOTEBOOK = "wan_i2v.ipynb"
TEST_FRAMES = 49


def wan_settings(ctx: c.Ctx) -> dict:
    w = c.model_cfg(ctx, "wan")
    return {k: w[k] for k in ("width", "height", "frames", "steps", "guidance", "fps", "negative_prompt", "dtype")}


def clip_key(ctx: c.Ctx, shot: dict, settings: dict | None = None) -> str:
    kf = keyframe_path(ctx, shot)
    return c.Cache.key("shots", c.model_cfg(ctx, "wan")["id"], settings or wan_settings(ctx), shot["motion_prompt"],
                       shot["seed"], {"keyframe": c.sha256_file(kf)})


def clip_status(ctx: c.Ctx, shot: dict) -> tuple[str, str]:
    """('clip', key) | ('fallback', key) | ('pending', key) for a wan shot (what assembly can use; ignores FORCE)."""
    key = clip_key(ctx, shot)
    if ctx.cache.path(key):
        return "clip", key
    if (ctx.cache.entry(key) or {}).get("status") == "fallback":
        return "fallback", key
    return "pending", key


def run(ctx: c.Ctx, test: bool = False) -> None:
    model = c.model_cfg(ctx, "wan")["id"]
    settings = wan_settings(ctx)
    wan_shots = [s for s in ctx.sb["shots"] if s["motion"] == "wan"]
    if test:
        wan_shots = wan_shots[:1] or ctx.sb["shots"][:1]
        settings = settings | {"frames": TEST_FRAMES}
    pending, keep = [], set()
    for shot in wan_shots:
        key = clip_key(ctx, shot, settings)
        obj = ctx.cache.lookup(key)
        if obj and not test:
            c.log_hit("shots", "kaggle", model, key, shot_id=shot["id"], seed=shot["seed"])
            c.link(obj, ctx.out / "clips" / f"{shot['id']}.mp4")
            keep.add(f"{shot['id']}.mp4")
        elif ctx.cache.is_fallback(key) and not test:
            print(f"  {shot['id']}: Wan failed earlier -> Ken Burns fallback (FORCE=1 retries)")
        elif obj is None or test:
            pending.append((shot, key))
    if not pending:
        c.prune_links(ctx.out / "clips", keep)
        print(f"shots: nothing to render ({len(keep)} clips cached)")
        return
    files, jobs = {}, []
    for shot, key in pending:
        kf = keyframe_path(ctx, shot)
        sha = c.sha256_file(kf)
        files[f"{sha}{kf.suffix}"] = kf
        jobs.append({"id": shot["id"], "key": key, "image": f"{sha}{kf.suffix}", "image_sha256": sha,
                     "prompt": shot["motion_prompt"], "seed": shot["seed"], "frames": settings["frames"],
                     "max_attempts": 1 if test else shot["max_attempts"]})
    config = {"model": model, **settings}
    res = kaggle_job.run("wan", NOTEBOOK, jobs, config, ctx.models, files)
    kaggle_job.log_session("shots", model, res)
    for ln in res["lines"]:  # every attempt the notebook made, with its GPU time
        if ln.get("id"):
            c.log_call("shots", "kaggle", model, shot_id=ln["id"], seed=ln.get("seed"),
                       params={"prompt": next(j["prompt"] for j in jobs if j["id"] == ln["id"]), **settings,
                               "test": test, "peak_vram_gb": ln.get("peak_vram_gb")},
                       gpu_seconds=ln.get("gpu_seconds", 0), latency_ms=ln.get("gpu_seconds", 0) * 1000,
                       list_price_usd=c.list_price("kaggle_t4", gpu_seconds=ln.get("gpu_seconds", 0)),
                       status=ln.get("status", "error"), attempt=ln.get("attempt", 1), error=ln.get("error"))
    for shot, key in pending:
        r = res["results"].get(shot["id"], {})
        f = res["dir"] / r.get("file", "missing")
        if r.get("status") == "ok" and f.exists():
            obj = ctx.cache.put(key, f, ".mp4", "shots", keep=not test, frames=r.get("frames"),
                                gpu_seconds=r.get("gpu_seconds"), seed_used=r.get("seed"))
            if test:
                print(f"  TEST clip {shot['id']}: {r.get('frames')} frames in {r.get('gpu_seconds')} GPU s "
                      f"(peak {r.get('peak_vram_gb')} GB) -> {obj}")
                continue
            c.link(obj, ctx.out / "clips" / f"{shot['id']}.mp4")
            keep.add(f"{shot['id']}.mp4")
        elif not test:
            reason = r.get("error") or res["failure"] or f"kernel {res['status']}, no clip"
            ctx.cache.mark(key, "fallback", "shots", reason=reason[:300])
            c.log_call("shots", "ffmpeg", "kenburns", shot_id=shot["id"], seed=shot["seed"], status="fallback",
                       params={"reason": reason[:300]}, error=reason[:300])
            print(f"  {shot['id']}: no Wan clip ({reason[:80]}) -> Ken Burns fallback")
    if not test:
        c.prune_links(ctx.out / "clips", keep)
    print(f"shots: kernel {res['status']}, {len(keep)} clips ready, session {res['wall_s'] / 60:.1f} min")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    c.storyboard_arg(ap)
    ap.add_argument("--test", action="store_true", help="render one short clip to measure the T4")
    a = ap.parse_args()
    run(c.load_storyboard(a.storyboard), test=a.test)


if __name__ == "__main__":
    c.stage_main(main)
