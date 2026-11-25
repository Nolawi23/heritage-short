"""`make costs`: per-stage summary of runs.jsonl -> table in COSTS.md (between the costs markers).

calls      = real model/provider calls (cache hits and local ffmpeg steps are counted separately)
latency    = mean and total wall time of real calls
GPU min    = Kaggle GPU time (per clip/candidate + session overhead)
list price = what the same calls would cost at list price (config/prices.json); actual cost is est_cost_usd
"""
from __future__ import annotations

import json
from collections import defaultdict

from . import common as c


def summarise(records: list[dict]) -> list[dict]:
    rows = defaultdict(lambda: {"calls": 0, "hits": 0, "errors": 0, "fallbacks": 0, "lat": [], "gpu": 0.0,
                                "list": 0.0, "cost": 0.0, "local": 0})
    for r in records:
        s = rows[r["stage"]]
        if r.get("cache_hit"):
            s["hits"] += 1
            continue
        if r["provider"] == "ffmpeg":
            s["local"] += 1
            s["fallbacks"] += r["status"] == "fallback"
            continue
        if r["status"] in ("error", "quota_exhausted"):
            s["errors"] += 1
        else:
            s["calls"] += 1
        s["lat"].append(r.get("latency_ms", 0))
        s["gpu"] += r.get("gpu_seconds", 0) or 0
        s["list"] += r.get("list_price_usd", 0) or 0
        s["cost"] += r.get("est_cost_usd", 0) or 0
    out = []
    for stage, s in rows.items():
        lat = s["lat"]
        out.append({"stage": stage, "calls": s["calls"], "failed": s["errors"], "cache_hits": s["hits"],
                    "local_steps": s["local"], "fallbacks": s["fallbacks"],
                    "mean_latency_s": sum(lat) / len(lat) / 1000 if lat else 0, "total_latency_s": sum(lat) / 1000,
                    "gpu_min": s["gpu"] / 60, "list_usd": s["list"], "cost_usd": s["cost"]})
    order = ["smoke", "script", "narration", "radio", "reference", "keyframes", "shots", "music", "assemble"]
    return sorted(out, key=lambda r: order.index(r["stage"]) if r["stage"] in order else 99)


def table(rows: list[dict]) -> str:
    head = ("| Stage | Calls | Failed | Cache hits | Local steps | Fallbacks | Mean latency (s) | Total latency (s) "
            "| GPU min | List-price equiv. (USD) | Actual cost (USD) |\n|---|---|---|---|---|---|---|---|---|---|---|")
    body = [f"| {r['stage']} | {r['calls']} | {r['failed']} | {r['cache_hits']} | {r['local_steps']} | {r['fallbacks']} "
            f"| {r['mean_latency_s']:.1f} | {r['total_latency_s']:.1f} | {r['gpu_min']:.1f} | {r['list_usd']:.4f} "
            f"| {r['cost_usd']:.4f} |" for r in rows]
    tot = {k: sum(r[k] for r in rows) for k in ("calls", "failed", "cache_hits", "gpu_min", "list_usd", "cost_usd")}
    body.append(f"| **total** | {tot['calls']} | {tot['failed']} | {tot['cache_hits']} | | | | | {tot['gpu_min']:.1f} "
                f"| {tot['list_usd']:.4f} | {tot['cost_usd']:.4f} |")
    return "\n".join([head, *body])


def main() -> None:
    path = c.runs_path()
    records = [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []
    runs = sorted({r["run_id"] for r in records})
    md = table(summarise(records)) + f"\n\nFrom {len(records)} lines of `runs.jsonl` across {len(runs)} run(s)."
    c.replace_block(c.ROOT / "COSTS.md", "costs", md)
    print(md)


if __name__ == "__main__":
    c.stage_main(main)
