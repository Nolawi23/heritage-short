"""Stage 4: original score with ACE-Step 1.5 on a Kaggle T4 (same pattern as shots.py).

For every cue: 3 instrumental candidates (seed, seed+1, seed+2). The caption carries mood and
instrumentation only; bpm, key and time signature go in as metadata. Each candidate is
duration_s long (30-60 s); assembly joins/trims the chosen one to the cue's span in the timeline.

Without music_cues[].selected the candidates land in out/music/<cue>_c<n>.wav (not committed) and the
stage stops. Choose with:
    python -m pipeline.music --select <cue>=<n> --reason "why"     (reason is appended to EXPERIMENTS.md)
With selected set, only that candidate is needed (and regenerated only if its key is missing).
"""
from __future__ import annotations

import argparse
import os
from datetime import date

from . import common as c
from . import kaggle_job

NOTEBOOK = "ace_step.ipynb"


def cue_params(ctx: c.Ctx, cue: dict) -> dict:
    m = c.model_cfg(ctx, "music")
    return {"bpm": cue["bpm"], "keyscale": cue["key"], "timesignature": m["timesignature"],
            "duration_s": cue["duration_s"], "instrumental": cue["instrumental"], "steps": m["steps"],
            "shift": m["shift"], "dtype": m["dtype"]}


def cand_key(ctx: c.Ctx, cue: dict, n: int) -> str:
    return c.Cache.key("music", c.model_cfg(ctx, "music")["id"], cue_params(ctx, cue), cue["caption"],
                       cue.get("seed", 7) + n - 1)


def selected_path(ctx: c.Ctx, cue: dict):
    if not cue.get("selected"):
        return None
    return ctx.cache.path(cand_key(ctx, cue, cue["selected"]))


def run(ctx: c.Ctx) -> None:
    m = c.model_cfg(ctx, "music")
    pending, waiting, keep = [], [], set()
    for cue in ctx.sb["music_cues"]:
        wanted = [cue["selected"]] if cue.get("selected") else list(range(1, m["candidates"] + 1))
        for n in wanted:
            key = cand_key(ctx, cue, n)
            obj = ctx.cache.lookup(key)
            if obj:
                c.log_hit("music", "kaggle", m["id"], key, shot_id=f"{cue['id']}_c{n}", seed=cue.get("seed", 7) + n - 1)
                if cue.get("selected"):
                    obj = ctx.cache.promote(key)  # the chosen take must live in the committed store
                _link(ctx, cue, n, obj, keep)
            else:
                pending.append((cue, n, key))
        if not cue.get("selected"):
            waiting.append(cue["id"])
    if pending:
        jobs = [{"id": f"{cue['id']}_c{n}", "key": key, "caption": cue["caption"], "seed": cue.get("seed", 7) + n - 1,
                 **cue_params(ctx, cue)} for cue, n, key in pending]
        res = kaggle_job.run("music", NOTEBOOK, jobs, {"model": m["id"]}, ctx.models)
        kaggle_job.log_session("music", m["id"], res)
        for ln in res["lines"]:
            if ln.get("id"):
                job = next(j for j in jobs if j["id"] == ln["id"])
                c.log_call("music", "kaggle", m["id"], shot_id=ln["id"], seed=ln.get("seed"),
                           params={"prompt": job["caption"], **{k: job[k] for k in ("bpm", "keyscale", "duration_s", "instrumental", "steps")}},
                           gpu_seconds=ln.get("gpu_seconds", 0), latency_ms=ln.get("gpu_seconds", 0) * 1000,
                           list_price_usd=c.list_price("kaggle_t4", gpu_seconds=ln.get("gpu_seconds", 0)),
                           status=ln.get("status", "error"), attempt=ln.get("attempt", 1), error=ln.get("error"))
        failed = []
        for cue, n, key in pending:
            r = res["results"].get(f"{cue['id']}_c{n}", {})
            f = res["dir"] / r.get("file", "missing")
            if r.get("status") == "ok" and f.exists():
                obj = ctx.cache.put(key, f, ".wav", "music", keep=bool(cue.get("selected")), duration_s=r.get("duration_s"))
                _link(ctx, cue, n, obj, keep)
            else:
                failed.append(f"{cue['id']}_c{n}: {r.get('error') or res['failure'] or res['status']}")
        if failed:
            raise c.PipelineStop("music generation failed (nothing cached for these):\n  " + "\n  ".join(failed))
    c.prune_links(ctx.out / "music", keep)
    if waiting:
        listing = "\n  ".join(os.path.relpath(ctx.out / "music" / n, c.ROOT) for n in sorted(keep))
        raise c.AwaitingChoice(f"music candidates ready for {', '.join(waiting)}:\n  {listing}\n"
                               f"Choose:  .venv/bin/python -m pipeline.music --select {waiting[0]}=<n> --reason \"...\"")
    print(f"music: {len(ctx.sb['music_cues'])} cue(s) ready")


def _link(ctx: c.Ctx, cue: dict, n: int, obj, keep: set) -> None:
    name = f"{cue['id']}.wav" if cue.get("selected") == n else f"{cue['id']}_c{n}.wav"
    c.link(obj, ctx.out / "music" / name)
    keep.add(name)


def select(ctx: c.Ctx, choice: str, reason: str) -> None:
    cid, _, n = choice.partition("=")
    cue = next((x for x in ctx.sb["music_cues"] if x["id"] == cid), None)
    if cue is None or not n.isdigit() or not 1 <= int(n) <= 3:
        raise c.PipelineStop(f"--select expects <cue>=<1..3>, got '{choice}'")
    if not reason:
        raise c.PipelineStop("--reason is required: it is recorded in EXPERIMENTS.md")
    key = cand_key(ctx, cue, int(n))
    obj = ctx.cache.promote(key)
    cue["selected"] = int(n)
    c.save_storyboard(ctx)
    c.link(obj, ctx.out / "music" / f"{cid}.wav")
    entry = (f"\n### {date.today()} - music cue `{cid}`: chose candidate {n}\n"
             f"- **Hypothesis:** one of 3 seeds ({cue.get('seed', 7)}..{cue.get('seed', 7) + 2}) fits the narration best\n"
             f"- **Change:** generated 3 candidates (caption: \"{cue['caption']}\", {cue['bpm']} bpm, {cue['key']})\n"
             f"- **Result:** candidate {n} chosen. Reason: {reason}\n"
             f"- **run_id:** {ctx.cache.entry(key).get('run_id')}\n")
    with open(c.ROOT / "EXPERIMENTS.md", "a") as f:
        f.write(entry)
    print(f"selected {cid} candidate {n}; reason recorded in EXPERIMENTS.md")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    c.storyboard_arg(ap)
    ap.add_argument("--select", metavar="CUE=N")
    ap.add_argument("--reason", default="")
    a = ap.parse_args()
    ctx = c.load_storyboard(a.storyboard)
    select(ctx, a.select, a.reason) if a.select else run(ctx)


if __name__ == "__main__":
    c.stage_main(main)
