"""Smoke test: list models per provider, pick + pin them, make one call each, check Kaggle auth.

Choices (pinned in config/models.json; re-pick with --repin):
  Gemini text : newest gemini-<version>-flash (stable preferred over preview, no lite/image/live/tts)
  Gemini TTS  : first working id of TTS_CANDIDATES (TTS models use the Interactions API and are not returned
                by models.list, so the documented ids are tried in order)
  FLUX        : @cf/black-forest-labs/flux-2-klein-4b (Apache-2.0) if listed, else flux-1-schnell (Apache-2.0).
                Never the 9B (non-commercial). The model's input schema is saved to config/flux_schema.json
                and tells us whether it accepts reference images.
Every call is logged to runs.jsonl (stage "smoke"); sample outputs go to outputs/smoke/ (not committed).
Model IDs, licences and free-tier limits are written into COSTS.md and ETHICS.md.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date

import requests

from . import common as c
from . import kaggle_job
from .keyframes import flux_generate, image_ext
from .narration import pcm_to_wav, tts

SMOKE = c.outputs_root() / "smoke"
TTS_CANDIDATES = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts", "gemini-3.1-flash-tts-preview",
                  "gemini-2.5-flash-preview-tts")  # https://ai.google.dev/gemini-api/docs/speech-generation (2026-11-25)
FLUX_PREFERENCE = ("@cf/black-forest-labs/flux-2-klein-4b", "@cf/black-forest-labs/flux-1-schnell")
LICENCES = {
    "gemini": "Proprietary API (Gemini API Additional Terms). Free tier: content may be used to improve Google products.",
    "@cf/black-forest-labs/flux-2-klein-4b": "Apache-2.0 (FLUX.2 [klein] 4B weights); served under Cloudflare Workers AI terms",
    "@cf/black-forest-labs/flux-1-schnell": "Apache-2.0 (FLUX.1 [schnell] weights); served under Cloudflare Workers AI terms",
}


def rank_gemini(names: list[str], kind: str) -> list[str]:
    ranked = []
    for n in names:
        m = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-flash(-lite)?(.*)", n)
        if not m or m.group(2):
            continue
        rest = m.group(3)
        if kind == "tts" and "tts" not in rest:
            continue
        if kind == "text" and not re.fullmatch(r"(-preview)?(-\d{2}-\d{4}|-\d{3})?", rest):
            continue
        ranked.append((float(m.group(1)), "preview" not in rest, -len(rest), n))
    return [n for *_, n in sorted(ranked, reverse=True)]


def find_prop(schema, name: str):
    if isinstance(schema, dict):
        if name in schema and isinstance(schema[name], dict):
            return schema[name]
        for v in schema.values():
            r = find_prop(v, name)
            if r is not None:
                return r
    elif isinstance(schema, list):
        for v in schema:
            r = find_prop(v, name)
            if r is not None:
                return r
    return None


def _try(fn, label: str, report: dict):
    try:
        return fn()
    except c.PipelineStop as e:
        report["problems"].append(f"{label}: {e}")
        print(f"  FAIL {label}: {str(e)[:200]}")
        return None


def gemini(cfg: dict, repin: bool, report: dict) -> None:
    client = c.gemini_client()
    t0 = time.monotonic()
    models = list(client.models.list())
    names = sorted(m.name.removeprefix("models/") for m in models
                   if "generateContent" in (getattr(m, "supported_actions", None) or ["generateContent"]))
    c.log_call("smoke", "gemini", "models.list", params={"count": len(names)}, latency_ms=(time.monotonic() - t0) * 1000)
    report["gemini_models"] = [n for n in names if n.startswith("gemini")]
    for kind, slot in (("text", "gemini_text"), ("tts", "gemini_tts")):
        ranked = rank_gemini(names, kind) if kind == "text" else list(TTS_CANDIDATES)
        pinned = (cfg.get(slot) or {}).get("id")
        order = [pinned] if pinned and not repin else ranked
        if pinned and ranked and ranked[0] != pinned:
            report["notes"].append(f"newer {kind} model listed: {ranked[0]} (pinned {pinned}; --repin to switch)")
        failed = []
        for model in order:
            print(f"  gemini {kind}: trying {model}")
            ok = _try(lambda: _gemini_call(kind, model, cfg), f"gemini {kind} {model}", report)
            if ok is not None:
                cfg[slot] = {"id": model, "licence": LICENCES["gemini"], "pinned": str(date.today()), **ok}
                break
            failed.append(report["problems"].pop())  # a fallback is a note unless nothing works
        else:
            report["problems"].append(f"no working Gemini {kind} model among {order[:5]}")
        report["notes"] += [f"skipped {f[:240]}" for f in failed]


def _gemini_call(kind: str, model: str, cfg: dict) -> dict:
    common = dict(stage="smoke", provider="gemini", model=model, models=cfg, max_attempts=2)
    if kind == "text":
        def call():
            from google.genai import types
            r = c.gemini_client().models.generate_content(model=model, contents="Reply with the single word OK.",
                                                          config=types.GenerateContentConfig(seed=1, temperature=0))
            return r.text, c.gemini_usage(r)
        text = c.call_model(call, throttle_key="gemini_text", params={"prompt": "Reply with the single word OK."},
                            price_kind="gemini_text", seed=1, **common)
        print(f"    -> {text.strip()[:40]!r}")
        return {}
    text, style = "This is a short test of the narration voice.", "calm and clear"
    accepts_seed = True
    params = {"prompt": text, "style": style, "voice": "Kore"}
    try:
        pcm, rate = c.call_model(lambda: tts(model, text, style, "Kore", 1), throttle_key="gemini_tts",
                                 params=params, price_kind="gemini_tts", seed=1, **common)
    except c.PipelineStop as e:
        if "seed" not in str(e).lower():
            raise
        accepts_seed = False
        pcm, rate = c.call_model(lambda: tts(model, text, style, "Kore", None), throttle_key="gemini_tts",
                                 params=params | {"seed": None}, price_kind="gemini_tts", **common)
    SMOKE.mkdir(parents=True, exist_ok=True)
    (SMOKE / "tts.wav").write_bytes(pcm_to_wav(pcm, rate))
    print(f"    -> {len(pcm) / 2 / rate:.2f} s of audio at {rate} Hz (outputs/smoke/tts.wav)")
    return {"accepts_seed": accepts_seed, "sample_rate": rate}


def cloudflare(cfg: dict, repin: bool, report: dict) -> None:
    acct, token = c.require_env("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN")
    base = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai"
    h = {"Authorization": f"Bearer {token}"}
    t0 = time.monotonic()
    r = requests.get(f"{base}/models/search", headers=h, params={"task": "Text-to-Image", "per_page": 100}, timeout=60)
    c.raise_for_http(r, "cloudflare")
    names = sorted(m["name"] for m in r.json()["result"])
    c.log_call("smoke", "cloudflare", "models.search", params={"task": "Text-to-Image", "count": len(names)},
               latency_ms=(time.monotonic() - t0) * 1000)
    report["cloudflare_models"] = names
    pinned = (cfg.get("flux") or {}).get("id")
    model = pinned if pinned and not repin else next((m for m in FLUX_PREFERENCE if m in names), None)
    if not model:
        report["problems"].append(f"neither {FLUX_PREFERENCE} is listed on Workers AI")
        return
    s = requests.get(f"{base}/models/schema", headers=h, params={"model": model}, timeout=60)
    c.raise_for_http(s, "cloudflare")
    schema = s.json().get("result", s.json())
    (c.ROOT / "config" / "flux_schema.json").write_text(json.dumps(schema, indent=2) + "\n")
    text = json.dumps(schema)
    multipart = "multipart" in text
    refs = sorted(set(re.findall(r"input_image_(\d+)", text)))
    opaque = multipart and not refs  # the schema does not describe the multipart body: verify by experiment
    wmax = (find_prop(schema, "width") or {}).get("maximum", 1792)
    hmax = (find_prop(schema, "height") or {}).get("maximum", 1008)
    w = min(1792, int(wmax) // 16 * 16)
    hgt = min(1008, int(hmax) // 16 * 16, round(w * 9 / 16 / 16) * 16)
    flux = {"id": model, "licence": LICENCES.get(model, "see model card"), "pinned": str(date.today()),
            "api": "multipart" if multipart else "json", "accepts_reference_images": bool(refs) or opaque,
            "max_reference_images": len(refs) or 4,  # Workers AI changelog: FLUX.2 klein takes up to 4 input images
            "reference_field": "input_image_{n}", "reference_index_base": int(refs[0]) if refs else 0,
            "width": w, "height": hgt, "schema_width_max": wmax, "schema_height_max": hmax,
            "price_kind": "cloudflare_flux2_klein_4b" if "flux-2" in model else "cloudflare_flux1_schnell"}
    if "schnell" in model:
        flux |= {"steps": 4, "accepts_size": bool(find_prop(schema, "width"))}
    print(f"  flux: {model} ({flux['api']}, reference images: {flux['accepts_reference_images']}, "
          f"{len(refs)} slot(s), max {wmax}x{hmax})")
    prompt = "A small clay teapot on a wooden table, soft window light, photographic."

    def one(refs_paths, label):
        data = c.call_model(lambda: flux_generate(flux, prompt, 1, 512, 512, refs_paths), stage="smoke",
                            provider="cloudflare", model=model, throttle_key="cloudflare", models=cfg,
                            params={"prompt": prompt, "width": 512, "height": 512, "refs": len(refs_paths)},
                            price_kind=flux["price_kind"], seed=1, max_attempts=2)
        SMOKE.mkdir(parents=True, exist_ok=True)
        out = SMOKE / f"{label}{image_ext(data)}"
        out.write_bytes(data)
        print(f"    -> {out.relative_to(c.ROOT)} ({len(data) / 1e3:.0f} kB)")
        return out

    first = _try(lambda: one([], "flux"), f"flux {model}", report)
    if first and flux["accepts_reference_images"]:
        # Same prompt + seed with the first image as input_image_0: if the request is accepted AND the output
        # changes, the model is really conditioning on the reference (an ignored field would give the same image).
        ok = _try(lambda: one([first], "flux_with_reference"), f"flux {model} with reference image", report)
        changed = ok is not None and c.sha256_file(ok) != c.sha256_file(first)
        flux["accepts_reference_images"] = changed
        flux["reference_check"] = (f"{date.today()}: same prompt+seed with input_image_0 -> "
                                   f"{'accepted, output changed (conditioning works)' if changed else 'not used'}")
        print(f"    reference check: {flux['reference_check']}")
        if ok is None:
            report["problems"].pop()  # a rejected reference is a finding, not a smoke failure
    if first:
        cfg["flux"] = flux


def kaggle(cfg: dict, report: dict) -> None:
    t0 = time.monotonic()
    k = kaggle_job.api()
    user = kaggle_job.username(k)
    quota = k.quota_view()
    kernels = k.kernels_list(mine=True, page_size=5)
    q = str(quota)[:400]
    c.log_call("smoke", "kaggle", "api.authenticate", params={"username_set": bool(user), "kernels_listed": len(kernels or []),
                                                              "quota": q}, latency_ms=(time.monotonic() - t0) * 1000)
    report["kaggle_quota"] = q
    print(f"  kaggle: authenticated, {len(kernels or [])} kernel(s) listed, quota: {q[:160]}")


def write_docs(cfg: dict, report: dict) -> None:
    rows = ["| Role | Model ID | Licence | Free tier |", "|---|---|---|---|"]
    tiers = {
        "gemini_text": "Free tier: 20 requests/day per model (observed 429 'generate_content_free_tier_requests, limit: 20', "
                       "2026-11-25); failed requests count too. Live limits: https://aistudio.google.com/rate-limit",
        "gemini_tts": "Free tier, per-model daily request limit (see https://aistudio.google.com/rate-limit); narration "
                      "stops cleanly with status quota_exhausted and resumes from cache",
        "flux": "10,000 Neurons/day free on Workers AI (~26 Neurons per 512x512 output tile)",
        "wan": f"Kaggle free GPU quota (weekly hours; this account: {report.get('kaggle_quota', 'not checked')[:120]})",
        "music": "Kaggle free GPU quota (shared with Wan)",
    }
    for slot, role in (("gemini_text", "Script (text)"), ("gemini_tts", "Narration (TTS)"), ("flux", "Keyframes"),
                       ("wan", "Image-to-video"), ("music", "Score")):
        m = cfg.get(slot) or {}
        rows.append(f"| {role} | `{m.get('id', 'not pinned')}` | {m.get('licence', '?')} | {tiers[slot]} |")
    flux = cfg.get("flux") or {}
    extra = [f"\nSmoke test {date.today()} (run `{c.RUN_ID}`).",
             f"FLUX reference images: **{'accepted, keyframes are conditioned on the approved sheet' if flux.get('accepts_reference_images') else 'not used'}** "
             f"(schema saved in `config/flux_schema.json`)."]
    extra += [f"- note: {n}" for n in report["notes"]] + [f"- problem: {p}" for p in report["problems"]]
    block = "\n".join(rows + extra)
    for doc in ("COSTS.md", "ETHICS.md"):
        c.replace_block(c.ROOT / doc, "models", block)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repin", action="store_true", help="re-pick models even if config/models.json pins them")
    a = ap.parse_args()
    cfg = json.loads(c.models_path().read_text())
    report = {"problems": [], "notes": []}
    for label, fn in (("gemini", lambda: gemini(cfg, a.repin, report)),
                      ("cloudflare", lambda: cloudflare(cfg, a.repin, report)),
                      ("kaggle", lambda: kaggle(cfg, report))):
        print(f"[{label}]")
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - keep testing the other providers
            report["problems"].append(f"{label}: {c.redact(str(e))[:300]}")
            print(f"  FAIL: {c.redact(str(e))[:300]}")
    c.models_path().write_text(json.dumps(cfg, indent=2) + "\n")
    write_docs(cfg, report)
    print(f"\nmodels pinned in config/models.json; COSTS.md and ETHICS.md updated (run {c.RUN_ID})")
    if report["problems"]:
        raise c.PipelineStop("smoke test problems:\n  " + "\n  ".join(report["problems"]))
    print("smoke test passed")


if __name__ == "__main__":
    c.stage_main(main)
