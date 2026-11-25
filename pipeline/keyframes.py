"""Stage 2: keyframes with FLUX on Cloudflare Workers AI.

1. Reference sheet: for every character without an approved reference_image, REF_CANDIDATES
   candidates are generated (out/reference/<char>_c<n>.png, not committed) and the stage stops.
   Approve one with:  python -m pipeline.keyframes --approve <char>=<n>
   (moves it into the committed cache, links outputs/reference/<char>.png, writes storyboard.json)
2. One keyframe per shot (out/keyframes/<shot>.png). Prompt = image_prompt + descriptions of the
   characters in the shot + style_block + fixed composition rules. If the model accepts reference
   images (config/models.json flux.accepts_reference_images) the approved sheets of the characters
   in the shot are sent too, and their hashes are part of the cache key.
"""
from __future__ import annotations

import argparse
import base64
import math
import os
from pathlib import Path

import requests

from . import common as c

COMPOSITION = ("Composition: the main subject is centred and everything important sits inside the central third "
               "of the frame, so a 9:16 vertical crop works. No text, letters, numbers, captions, signs, logos or "
               "watermarks anywhere in the image.")
REF_CANDIDATES = 4
REF_SEED = 500  # candidate n uses REF_SEED + n


def image_ext(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    raise c.ProviderError("FLUX returned bytes that are not an image", 500)


def tiles(w: int, h: int) -> int:
    return math.ceil(w / 512) * math.ceil(h / 512)


def flux_generate(cfg: dict, prompt: str, seed: int, width: int, height: int, refs: list[Path] = ()):
    """One Workers AI call -> (image bytes, usage). FLUX.2 takes multipart form data, FLUX.1 JSON."""
    acct, token = c.require_env("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN")
    url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{cfg['id']}"
    headers = {"Authorization": f"Bearer {token}"}
    if cfg.get("api") == "multipart":
        fields = {"prompt": (None, prompt), "seed": (None, str(seed)),
                  "width": (None, str(width)), "height": (None, str(height))}
        for i, p in enumerate(refs):
            mime = {".png": "image/png", ".webp": "image/webp"}.get(p.suffix, "image/jpeg")
            slot = cfg.get("reference_field", "input_image_{n}").format(n=i + cfg.get("reference_index_base", 0))
            fields[slot] = (p.name, p.read_bytes(), mime)
        r = requests.post(url, headers=headers, files=fields, timeout=180)
    else:
        body = {"prompt": prompt, "seed": seed, "steps": cfg.get("steps", 4)}
        if cfg.get("accepts_size", True):
            body |= {"width": width, "height": height}
        r = requests.post(url, headers=headers, json=body, timeout=180)
    c.raise_for_http(r, "cloudflare")
    if r.headers.get("content-type", "").startswith("image/"):
        data = r.content
    else:
        data = base64.b64decode(r.json()["result"]["image"])
    image_ext(data)
    in_tiles = sum(tiles(*_png_size(p)) for p in refs)
    return data, {"output_tiles": tiles(width, height), "input_tiles": in_tiles, "steps": cfg.get("steps", 0) if cfg.get("api") != "multipart" else 0}


def _png_size(p: Path) -> tuple[int, int]:
    b = p.read_bytes()[:32]
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")
    return 1024, 1024  # conservative estimate for non-PNG references


def size(cfg: dict) -> tuple[int, int]:
    return cfg.get("width", 1792), cfg.get("height", 1008)


def shot_prompt(sb: dict, shot: dict) -> str:
    chars = {ch["id"]: ch["description"] for ch in sb["characters"]}
    who = " ".join(f"{chars[i]}." for i in shot.get("characters", []))
    return " ".join(p for p in (shot["image_prompt"].rstrip(".") + ".", who, sb["style_block"], COMPOSITION) if p)


def ref_prompt(sb: dict, ch: dict) -> str:
    return (f"Character reference sheet: {ch['description']}. The same character shown three times side by side, "
            f"exactly as they appear in the film, with consistent clothing, proportions and colours, on a plain "
            f"neutral background, even soft light. {sb['style_block']} No text, letters, numbers, labels or watermarks.")


def ref_key(ctx: c.Ctx, ch: dict, n: int) -> str:
    cfg = c.model_cfg(ctx, "flux")
    w, h = size(cfg)
    return c.Cache.key("reference", cfg["id"], {"width": w, "height": h}, ref_prompt(ctx.sb, ch), REF_SEED + n)


def keyframe_key(ctx: c.Ctx, shot: dict) -> tuple[str, list[Path]]:
    cfg = c.model_cfg(ctx, "flux")
    w, h = size(cfg)
    refs, upstream = [], {}
    if cfg.get("accepts_reference_images"):
        chars = {ch["id"]: ch for ch in ctx.sb["characters"]}
        with_ref = [cid for cid in shot.get("characters", []) if chars[cid].get("reference") is not False]
        for cid in with_ref[: cfg.get("max_reference_images", 4)]:
            p = c.repo_path(chars[cid]["reference_image"])
            refs.append(p)
            upstream[f"ref:{cid}"] = c.sha256_file(p)
    params = {"width": w, "height": h, "n_refs": len(refs)}
    return c.Cache.key("keyframes", cfg["id"], params, shot_prompt(ctx.sb, shot), shot["seed"], upstream), refs


def _generate(ctx: c.Ctx, key: str, prompt: str, seed: int, refs: list[Path], shot_id: str, keep: bool, stage: str) -> Path:
    cfg = c.model_cfg(ctx, "flux")
    w, h = size(cfg)
    data = c.call_model(lambda: flux_generate(cfg, prompt, seed, w, h, refs), stage=stage, provider="cloudflare",
                        model=cfg["id"], throttle_key="cloudflare", models=ctx.models,
                        params={"prompt": prompt, "width": w, "height": h, "refs": [p.name for p in refs]},
                        price_kind=cfg.get("price_kind", "cloudflare_flux2_klein_4b"), shot_id=shot_id, seed=seed)
    return ctx.cache.put(key, data, image_ext(data), stage, keep=keep)


def reference_candidates(ctx: c.Ctx) -> list[str]:
    """Generate candidates for unapproved characters; return the list of characters still waiting."""
    cfg = c.model_cfg(ctx, "flux")
    waiting = []
    for ch in ctx.sb["characters"]:
        if ch["reference_image"] or ch.get("reference") is False:  # approved, or deliberately without a sheet
            continue
        waiting.append(ch["id"])
        for n in range(1, REF_CANDIDATES + 1):
            key = ref_key(ctx, ch, n)
            obj = ctx.cache.lookup(key)
            if obj:
                c.log_hit("reference", "cloudflare", cfg["id"], key, shot_id=ch["id"], seed=REF_SEED + n)
            else:
                obj = _generate(ctx, key, ref_prompt(ctx.sb, ch), REF_SEED + n, [], ch["id"], keep=False, stage="reference")
            c.link(obj, ctx.out / "reference" / f"{ch['id']}_c{n}{obj.suffix}")
    return waiting


def approve(ctx: c.Ctx, choice: str) -> None:
    cid, _, n = choice.partition("=")
    ch = next((x for x in ctx.sb["characters"] if x["id"] == cid), None)
    if ch is None or not n.isdigit() or not 1 <= int(n) <= REF_CANDIDATES:
        raise c.PipelineStop(f"--approve expects <character>=<1..{REF_CANDIDATES}>, got '{choice}'")
    key = ref_key(ctx, ch, int(n))
    obj = ctx.cache.promote(key)
    c.link(obj, ctx.out / "reference" / f"{cid}_c{n}{obj.suffix}")
    dest = c.link(obj, c.outputs_root() / "reference" / f"{cid}{obj.suffix}")
    ch["reference_image"] = os.path.relpath(dest, c.ROOT)
    c.save_storyboard(ctx)
    print(f"approved {cid} candidate {n} -> {ch['reference_image']} (written to {ctx.path.name})")


def run(ctx: c.Ctx) -> None:
    cfg = c.model_cfg(ctx, "flux")
    waiting = reference_candidates(ctx)
    if waiting:
        listing = "\n  ".join(os.path.relpath(p, c.ROOT) for p in sorted((ctx.out / "reference").glob("*_c*")))
        raise c.AwaitingChoice(f"reference sheet needs approval for: {', '.join(waiting)}\n  {listing}\n"
                               f"Pick one per character:  .venv/bin/python -m pipeline.keyframes "
                               f"--approve {waiting[0]}=<n>  (then rerun)")
    keep = set()
    for shot in ctx.sb["shots"]:
        key, refs = keyframe_key(ctx, shot)
        obj = ctx.cache.lookup(key)
        if obj:
            c.log_hit("keyframes", "cloudflare", cfg["id"], key, shot_id=shot["id"], seed=shot["seed"])
        else:
            obj = _generate(ctx, key, shot_prompt(ctx.sb, shot), shot["seed"], refs, shot["id"], True, "keyframes")
        name = f"{shot['id']}{obj.suffix}"
        c.link(obj, ctx.out / "keyframes" / name)
        keep.add(name)
    c.prune_links(ctx.out / "keyframes", keep)
    print(f"keyframes: {len(keep)} shots ({'conditioned on approved references' if cfg.get('accepts_reference_images') else 'style_block + description + seed'})")


def keyframe_path(ctx: c.Ctx, shot: dict) -> Path:
    key, _ = keyframe_key(ctx, shot)
    p = ctx.cache.path(key)
    if p is None:
        raise c.PipelineStop(f"keyframe for {shot['id']} missing: run `make keyframes` first")
    return p


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    c.storyboard_arg(ap)
    ap.add_argument("--approve", metavar="CHAR=N", help="approve reference candidate N for a character")
    a = ap.parse_args()
    ctx = c.load_storyboard(a.storyboard)
    approve(ctx, a.approve) if a.approve else run(ctx)


if __name__ == "__main__":
    c.stage_main(main)
