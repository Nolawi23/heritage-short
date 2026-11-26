"""Shared helpers for every stage.

- paths and the per-variant output directory
- storyboard loading + validation (JSON Schema + cross-references)
- content-addressed cache (objects stored by cache key, stage outputs are symlinks)
- run log (runs.jsonl, one JSON line per model call, failures and cache hits included)
- retries with exponential backoff (honours Retry-After), per-provider throttling,
  clean stop when a free-tier quota is exhausted
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema" / "storyboard.schema.json"
load_dotenv(ROOT / ".env")  # no-op when .env is missing: keys are only needed on a cache miss

SECRET_ENV = ("GEMINI_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "KAGGLE_API_TOKEN", "KAGGLE_KEY",
              "KAGGLE_USERNAME")  # the username is not secret, but it is personal: keep it out of the repo
RUN_ID = os.environ.get("RUN_ID") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:4]


# ---------------------------------------------------------------- errors ----
class PipelineStop(Exception):
    """A clean, explained stop. The CLI prints the message and exits with exit_code."""
    exit_code = 1


class AwaitingChoice(PipelineStop):
    exit_code = 2  # a human decision is needed (reference sheet, music candidate)


class QuotaExhausted(PipelineStop):
    exit_code = 3


class MissingKey(PipelineStop):
    exit_code = 4


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None, quota: bool = False):
        super().__init__(message)
        self.status, self.retry_after, self.quota = status, retry_after, quota


# ----------------------------------------------------------------- paths ----
def outputs_root() -> Path:
    return Path(os.environ.get("HS_OUTPUTS", ROOT / "outputs"))


def runs_path() -> Path:
    return Path(os.environ.get("HS_RUNS", ROOT / "runs.jsonl"))


def default_storyboard() -> Path:
    return Path(os.environ.get("HS_DEFAULT_STORYBOARD", ROOT / "storyboard.json"))


def models_path() -> Path:
    return Path(os.environ.get("HS_MODELS", ROOT / "config" / "models.json"))


def force() -> bool:
    return os.environ.get("FORCE", "0") == "1"


def repo_path(p: str | Path) -> Path:
    """Storyboard paths (e.g. reference_image) are repo-relative unless absolute."""
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


def out_dir_for(storyboard_path: Path) -> Path:
    """Default storyboard -> outputs/, any other -> outputs/variants/<stem>/."""
    if storyboard_path.resolve() == default_storyboard().resolve():
        return outputs_root()
    return outputs_root() / "variants" / storyboard_path.stem


def require_env(*names: str) -> list[str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise MissingKey(f"This step needs {', '.join(missing)} (set it in .env, see .env.example). "
                         "Keys are only required when an item is not in the cache.")
    return [os.environ[n] for n in names]


# ---------------------------------------------------------------- hashing ---
def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def source_hash(*modules: str) -> str:
    """Hash of local (non-model) code, so changing assembly code re-assembles."""
    return sha256_bytes(b"".join((ROOT / "pipeline" / f"{m}.py").read_bytes() for m in modules))[:16]


# ------------------------------------------------------------- storyboard ---
def validate(sb: dict) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text())
    v = jsonschema.Draft202012Validator(schema)
    errors = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
              for e in sorted(v.iter_errors(sb), key=lambda e: list(map(str, e.absolute_path)))]
    if errors:
        return errors
    # cross-references the schema cannot express
    for kind, items in (("shot", sb["shots"]), ("character", sb["characters"]), ("music cue", sb["music_cues"])):
        ids = [i["id"] for i in items]
        errors += [f"duplicate {kind} id: {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    cues = {c["id"] for c in sb["music_cues"]}
    chars = {c["id"] for c in sb["characters"]}
    voices = sb["narration"]["voices"]
    for s in sb["shots"]:
        if s["music_cue"] not in cues:
            errors.append(f"shots/{s['id']}: music_cue '{s['music_cue']}' is not a music_cues id")
        errors += [f"shots/{s['id']}: unknown character '{c}'" for c in s.get("characters", []) if c not in chars]
        errors += [f"shots/{s['id']}: speaker '{ln['speaker']}' has no entry in narration.voices"
                   for ln in s["lines"] if ln["speaker"] not in voices]
        errors += [f"shots/{s['id']}: person '{n}' is not in cast" for n in s.get("people", []) if n not in sb.get("cast", {})]
        errors += [f"shots/{s['id']}: reference image {r} does not exist" for r in s.get("reference_images", [])
                   if not repo_path(r).exists()]
    for c in sb["characters"]:
        if c["reference_image"] and not repo_path(c["reference_image"]).exists():
            errors.append(f"characters/{c['id']}: reference_image {c['reference_image']} does not exist")
    for c in sb["music_cues"]:
        if "start_s" in c and "end_s" in c and c["end_s"] <= c["start_s"]:
            errors.append(f"music_cues/{c['id']}: end_s must be after start_s")
    return errors


@dataclass
class Ctx:
    sb: dict
    path: Path          # storyboard file
    out: Path           # per-variant output directory
    cache: "Cache"
    models: dict


def load_storyboard(path: str | Path | None = None) -> Ctx:
    path = Path(path or os.environ.get("STORYBOARD") or default_storyboard())
    if not path.is_absolute():
        path = (Path.cwd() / path)
    try:
        sb = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise PipelineStop(f"cannot read storyboard {path}: {e}")
    errors = validate(sb)
    if errors:
        raise PipelineStop(f"{path.name} is invalid:\n  " + "\n  ".join(errors))
    models = json.loads(models_path().read_text())
    return Ctx(sb=sb, path=path, out=out_dir_for(path), cache=Cache(), models=models)


def model_cfg(ctx: Ctx, name: str) -> dict:
    cfg = ctx.models.get(name)
    if not cfg:
        raise PipelineStop(f"config/models.json has no '{name}' model yet: run `make smoke` first.")
    return cfg


def save_storyboard(ctx: Ctx) -> None:
    ctx.path.write_text(json.dumps(ctx.sb, indent=2, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ cache ---
class Cache:
    """Content-addressed store.

    key      = sha256(stage, model, params, prompt, seed, upstream input hashes)
    object   = outputs/cache/<key><ext>          (committed)
               outputs/.scratch/<key><ext>       (unselected candidates, gitignored)
    manifest = outputs/cache_manifest.json       key -> {stage, file, sha256, bytes, run_id, status, ...}
    Stage outputs (outputs/narration/s01_1.wav, ...) are relative symlinks to objects, so
    variants share identical items and git never stores a large file twice.
    """

    def __init__(self, root: Path | None = None):
        self.root = root or outputs_root()
        self.dir = self.root / "cache"
        self.scratch = self.root / ".scratch"
        self.manifest_path = self.root / "cache_manifest.json"
        self.manifest = json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else {}
        self._dirty: dict[str, dict | None] = {}  # this process's changes, merged into the file on save()

    @staticmethod
    def key(stage: str, model: str, params: dict, prompt: str | None = None, seed: int | None = None,
            upstream: dict | None = None) -> str:
        return sha256_bytes(canonical({"stage": stage, "model": model, "params": params, "prompt": prompt,
                                       "seed": seed, "upstream": upstream or {}}).encode())

    def entry(self, key: str) -> dict | None:
        return self.manifest.get(key)

    def path(self, key: str) -> Path | None:
        e = self.manifest.get(key)
        if not e or e.get("status") != "ok":
            return None
        for base in (self.dir, self.scratch):
            if (base / e["file"]).exists():
                return base / e["file"]
        return None

    def lookup(self, key: str) -> Path | None:
        """Cached object for key, or None when it must be (re)generated. FORCE=1 ignores the cache."""
        return None if force() else self.path(key)

    def is_fallback(self, key: str) -> bool:
        e = self.manifest.get(key)
        return bool(e) and e.get("status") == "fallback" and not force()

    def sha(self, key: str) -> str:
        e = self.manifest.get(key)
        if not e or "sha256" not in e:
            raise PipelineStop(f"cache has no object for key {key[:12]}…: run the upstream stage first")
        return e["sha256"]

    def put(self, key: str, src: bytes | Path, ext: str, stage: str, keep: bool = True, **meta) -> Path:
        dest = (self.dir if keep else self.scratch) / f"{key}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        if isinstance(src, (bytes, bytearray)):
            tmp.write_bytes(src)
        else:
            shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
        self._set(key, {"stage": stage, "file": dest.name, "sha256": sha256_file(dest), "bytes": dest.stat().st_size,
                        "kept": keep, "run_id": RUN_ID, "created": now_iso(), "status": "ok", **meta})
        return dest

    def promote(self, key: str) -> Path:
        """Move a chosen candidate from .scratch/ to the committed store."""
        src = self.path(key)
        if src is None:
            raise PipelineStop(f"candidate {key[:12]}… is not in the cache")
        if src.parent != self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
            shutil.move(src, self.dir / src.name)
            self._set(key, self.manifest[key] | {"kept": True})
        return self.dir / src.name

    def mark(self, key: str, status: str, stage: str, **meta) -> None:
        self._set(key, {"stage": stage, "status": status, "run_id": RUN_ID, "created": now_iso(), **meta})

    def drop(self, key: str) -> None:
        self.manifest.pop(key, None)
        self._dirty[key] = None

    def _set(self, key: str, entry: dict) -> None:
        self.manifest[key] = entry
        self._dirty[key] = entry
        self.save()

    def save(self) -> None:
        """Merge this process's changes into the manifest on disk under a lock.

        Stages can run concurrently (e.g. a Kaggle job for 30 min while narration runs); writing the whole
        in-memory copy would silently drop entries other processes added in the meantime."""
        import fcntl
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.manifest_path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            disk = json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else {}
            for k, v in self._dirty.items():
                if v is None:
                    disk.pop(k, None)
                else:
                    disk[k] = v
            tmp = self.manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(disk, indent=1, sort_keys=True) + "\n")
            os.replace(tmp, self.manifest_path)
        self.manifest, self._dirty = disk, {}


def link(target: Path, link_path: Path) -> Path:
    """Point link_path at target with a relative symlink (idempotent)."""
    link_path.parent.mkdir(parents=True, exist_ok=True)
    rel = os.path.relpath(target, link_path.parent)
    if link_path.is_symlink():
        if os.readlink(link_path) == rel:
            return link_path
        link_path.unlink()
    elif link_path.exists():
        link_path.unlink()
    os.symlink(rel, link_path)
    return link_path


def prune_links(directory: Path, keep: set[str]) -> None:
    """Remove stale symlinks (e.g. a deleted shot) so committed outputs match the storyboard."""
    if directory.is_dir():
        for p in directory.iterdir():
            if p.is_symlink() and p.name not in keep:
                p.unlink()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def replace_block(path: Path, name: str, content: str) -> None:
    """Rewrite the generated section between <!-- name:start --> and <!-- name:end --> in a doc."""
    start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    text = path.read_text()
    if start not in text or end not in text:
        raise PipelineStop(f"{path.name} lacks the {start} ... {end} markers")
    head, rest = text.split(start, 1)
    path.write_text(f"{head}{start}\n{content.strip()}\n{end}{rest.split(end, 1)[1]}")


# -------------------------------------------------------------------- log ---
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def redact(text: str) -> str:
    for name in SECRET_ENV:
        val = os.environ.get(name)
        if val and len(val) >= 6:
            text = text.replace(val, "[REDACTED]")
    return re.sub(r"(AIza[0-9A-Za-z_\-]{20,}|AQ\.[0-9A-Za-z_\-]{20,}|cfut_[0-9A-Za-z_\-]{10,}|KGAT_[0-9A-Za-z_\-]{10,})",
                  "[REDACTED]", text)


def log_call(stage: str, provider: str, model: str, *, shot_id: str | None = None, params: dict | None = None,
             seed: int | None = None, latency_ms: float = 0, gpu_seconds: float = 0.0, est_cost_usd: float = 0.0,
             list_price_usd: float = 0.0, status: str = "ok", cache_hit: bool = False, attempt: int = 1,
             error: str | None = None) -> dict:
    rec = {"ts": now_iso(), "run_id": RUN_ID, "stage": stage, "shot_id": shot_id, "provider": provider,
           "model": model, "params": params or {}, "seed": seed, "latency_ms": round(latency_ms),
           "gpu_seconds": round(gpu_seconds, 1), "est_cost_usd": round(est_cost_usd, 6),
           "list_price_usd": round(list_price_usd, 6), "status": status, "cache_hit": cache_hit,
           "attempt": attempt, "error": error}
    path = runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(redact(json.dumps(rec, ensure_ascii=False, default=str)) + "\n")
    return rec


def quota_day_start() -> datetime:
    """Gemini free-tier daily quotas reset at midnight Pacific time."""
    from zoneinfo import ZoneInfo
    pt = datetime.now(ZoneInfo("America/Los_Angeles"))
    return pt.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def quota_reset_text() -> str:
    from datetime import timedelta
    nxt = (quota_day_start() + timedelta(days=1))
    return f"{nxt:%H:%M} UTC = {nxt.astimezone():%H:%M} local, midnight Pacific"


def requests_today(model: str) -> tuple[int, bool]:
    """(requests sent to `model` since the quota reset, whether a daily-quota 429 was seen).

    Counted from runs.jsonl: every non-cached attempt (failed ones count on the free tier), except
    404s (unknown model, nothing served) and quota refusals."""
    start, used, exhausted = quota_day_start(), 0, False
    path = runs_path()
    if not path.exists():
        return 0, False
    for line in path.read_text().splitlines():
        r = json.loads(line)
        if r["model"] != model or r.get("cache_hit") or datetime.fromisoformat(r["ts"]) < start:
            continue
        err = r.get("error") or ""
        if r["status"] == "quota_exhausted":
            exhausted |= bool(re.search(r"per day|PerDay|daily", err, re.I))
        elif not err.startswith(("Error code: 404", "404")):
            used += 1
    return used, exhausted


def log_hit(stage: str, provider: str, model: str, key: str, **kw) -> None:
    kw.setdefault("params", {})["cache_key"] = key[:16]
    log_call(stage, provider, model, status="cached", cache_hit=True, **kw)


# ----------------------------------------------------------------- prices ---
def _prices() -> dict:
    return json.loads((ROOT / "config" / "prices.json").read_text())


def list_price(kind: str, **u) -> float:
    """List-price equivalent of one call (see config/prices.json)."""
    p = _prices().get(kind, {})
    if kind.startswith("gemini"):
        return (u.get("input_tokens", 0) * p.get("input_per_mtok", 0) + u.get("output_tokens", 0) * p.get("output_per_mtok", 0)) / 1e6
    if kind.startswith("cloudflare"):
        return (u.get("output_tiles", 0) * p.get("usd_per_output_tile", 0) + u.get("input_tiles", 0) * p.get("usd_per_input_tile", 0)
                + u.get("steps", 0) * p.get("usd_per_step", 0))
    if kind == "kaggle_t4":
        return u.get("gpu_seconds", 0) / 3600 * p.get("usd_per_gpu_hour", 0)
    return 0.0


def billed(provider: str, price: float) -> float:
    return price if _prices()["billing"].get(provider) == "paid" else 0.0


# ---------------------------------------------------------- calls/retries ---
_last_call: dict[str, float] = {}


def throttle(provider_key: str, models: dict) -> None:
    gap = models.get("min_interval_s", {}).get(provider_key, 0)
    wait = _last_call.get(provider_key, -1e9) + gap - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call[provider_key] = time.monotonic()


def as_provider_error(e: Exception) -> ProviderError:
    if isinstance(e, ProviderError):
        return e
    text = redact(str(e))
    if type(e).__module__.startswith("google.genai"):
        # generate_content errors carry .code; Interactions API errors carry .status_code + .response
        status = getattr(e, "code", None) or getattr(e, "status_code", None)
        if status is None and ("Connection" in type(e).__name__ or "Timeout" in type(e).__name__):
            return ProviderError(text, None)
        if status is not None:
            m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", text)
            headers = getattr(getattr(e, "response", None), "headers", None) or {}
            ra = str(headers.get("retry-after", ""))
            retry_after = float(m.group(1)) if m else None
            if retry_after is None and ra.replace(".", "", 1).isdigit():
                retry_after = float(ra)
            quota = status == 429 and bool(re.search(r"PerDay|per day|limit: 0|daily", text, re.I))
            return ProviderError(text, int(status), retry_after, quota)
    import requests
    if isinstance(e, (requests.ConnectionError, requests.Timeout)) or any(
            w in type(e).__name__ for w in ("Timeout", "ConnectError", "RemoteProtocolError")):  # httpx too
        return ProviderError(text, None)
    return ProviderError(text, -1)  # unknown -> not retried


RETRYABLE = {None, 429, 500, 502, 503, 504}


def call_model(fn, *, stage: str, provider: str, model: str, throttle_key: str, models: dict, params: dict,
               price_kind: str, shot_id: str | None = None, seed: int | None = None, max_attempts: int = 4):
    """Run fn() -> (result, usage) with throttling and retries; log every attempt to runs.jsonl."""
    for attempt in range(1, max_attempts + 1):
        throttle(throttle_key, models)
        t0 = time.monotonic()
        try:
            result, usage = fn()
        except Exception as exc:  # noqa: BLE001 - every failure is logged, then re-raised or retried
            err = as_provider_error(exc)
            latency = (time.monotonic() - t0) * 1000
            last = attempt == max_attempts or err.status not in RETRYABLE
            exhausted = err.quota or (err.status == 429 and last)
            log_call(stage, provider, model, shot_id=shot_id, params=params, seed=seed, latency_ms=latency,
                     status="quota_exhausted" if exhausted else "error", attempt=attempt, error=str(err)[:600])
            if exhausted:
                raise QuotaExhausted(f"{provider} quota exhausted for {model} (stage '{stage}', shot {shot_id}). "
                                     "Cached work is kept; rerun the same command when the quota resets.") from None
            if last:
                raise PipelineStop(f"{provider} call failed after {attempt} attempt(s) "
                                   f"(stage '{stage}', shot {shot_id}): {str(err)[:300]}") from None
            # Free tiers count failed requests against the daily quota, so overload (503) waits longer
            # before retrying instead of burning requests on a demand spike.
            base = 15 * 2 ** (attempt - 1) if err.status == 503 else 2 ** attempt
            delay = err.retry_after if err.retry_after else min(120.0, base + random.random())
            delay = max(delay, models.get("min_retry_s", {}).get(throttle_key, 0))  # e.g. TTS: never within a minute
            print(f"  {provider}: {err.status or 'network'} error, retry {attempt + 1}/{max_attempts} in {delay:.0f}s",
                  file=sys.stderr)
            time.sleep(delay)
            continue
        latency = (time.monotonic() - t0) * 1000
        price = list_price(price_kind, **usage)
        log_call(stage, provider, model, shot_id=shot_id, params=params, seed=seed, latency_ms=latency,
                 list_price_usd=price, est_cost_usd=billed(provider, price), attempt=attempt)
        return result


def raise_for_http(r, provider: str) -> None:
    """Turn a requests.Response error into a ProviderError (Retry-After honoured, daily quota detected)."""
    if r.status_code < 400:
        return
    ra = r.headers.get("Retry-After")
    try:
        retry_after = float(ra) if ra else None
    except ValueError:
        retry_after = None
    body = redact(r.text[:800])
    quota = r.status_code == 429 and bool(re.search(r"daily|free allocation|4006|quota", body, re.I))
    raise ProviderError(f"{provider} HTTP {r.status_code}: {body}", r.status_code, retry_after, quota)


_gemini = None


def gemini_client():
    global _gemini
    if _gemini is None:
        from google import genai
        (key,) = require_env("GEMINI_API_KEY")
        # a request that hangs (seen under 503 overload) becomes a retryable timeout after 120 s
        _gemini = genai.Client(api_key=key, http_options={"timeout": 120_000})
    return _gemini


def gemini_usage(resp) -> dict:
    um = getattr(resp, "usage_metadata", None)
    return {"input_tokens": getattr(um, "prompt_token_count", 0) or 0,
            "output_tokens": getattr(um, "candidates_token_count", 0) or 0}


# -------------------------------------------------------------------- CLI ---
def stage_main(fn) -> None:
    """Run a stage entry point; turn PipelineStop into a clear message + exit code."""
    try:
        fn()
    except PipelineStop as e:
        tag = {2: "WAITING FOR YOU", 3: "QUOTA EXHAUSTED", 4: "MISSING KEY"}.get(e.exit_code, "STOPPED")
        print(f"\n[{tag}] {e}", file=sys.stderr)
        sys.exit(e.exit_code)


def storyboard_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("storyboard", nargs="?", default=os.environ.get("STORYBOARD", "storyboard.json"))


def gc(delete: bool) -> None:
    """List (or delete) committed cache objects that no symlink under outputs/ points to."""
    cache = Cache()
    used = {Path(os.path.realpath(p)).name for p in outputs_root().rglob("*") if p.is_symlink()}
    stale = [p for p in sorted(cache.dir.glob("*")) if p.name not in used]
    for p in stale:
        print(f"{'deleting' if delete else 'unreferenced'}: {p.name} ({p.stat().st_size / 1e6:.1f} MB)")
        if delete:
            p.unlink()
            cache.drop(p.stem)
    if delete:
        cache.save()
    print(f"{len(stale)} unreferenced object(s), {sum(p.stat().st_size for p in stale if p.exists()) / 1e6:.1f} MB"
          + ("" if delete else " (run `make gc CONFIRM=1` to delete)"))


def last_run(run_id: str | None = None) -> None:
    """Print one run's lines from runs.jsonl (default: the most recent run) as a short table."""
    p = runs_path()
    rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []
    if not rows:
        print("runs.jsonl has no runs yet")
        return
    rid = run_id or rows[-1]["run_id"]
    run = [r for r in rows if r.get("run_id") == rid]
    if not run:
        raise PipelineStop(f"no run '{rid}' in runs.jsonl")

    def call(r: dict) -> str:
        if r.get("cache_hit"):
            return "cache hit"
        return "local step" if r.get("provider") in ("ffmpeg", "local") else "new call"

    table = [("stage", "shot", "provider", "call", "status")]
    table += [(r.get("stage", ""), r.get("shot_id") or "-", r.get("provider", ""), call(r), r.get("status", "")) for r in run]
    widths = [max(len(str(row[i])) for row in table) for i in range(len(table[0]))]
    print(f"run {rid}: {len(run)} line(s), {run[0]['ts'][:19].replace('T', ' ')} - {run[-1]['ts'][11:19]} UTC")
    for i, row in enumerate(table):
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths)).rstrip())
        if i == 0:
            print("  ".join("-" * w for w in widths))
    kinds = [call(r) for r in run]
    not_ok = sum(r.get("status") not in ("ok", "cached") for r in run)
    print(f"{kinds.count('new call')} new call(s), {kinds.count('cache hit')} cache hit(s), "
          f"{kinds.count('local step')} local step(s); {not_ok} not ok")


def main() -> None:
    ap = argparse.ArgumentParser(description="common utilities")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    storyboard_arg(v)
    g = sub.add_parser("gc")
    g.add_argument("--delete", action="store_true")
    sub.add_parser("budget")
    lr = sub.add_parser("last-run")
    lr.add_argument("--run", help="run_id to show (default: the most recent run)")
    a = ap.parse_args()
    if a.cmd == "last-run":
        stage_main(lambda: last_run(a.run))
    elif a.cmd == "budget":
        models = json.loads(models_path().read_text())
        for slot in ("gemini_tts", "gemini_text"):
            m = models.get(slot) or {}
            used, exhausted = requests_today(m.get("id", ""))
            limit = m.get("rpd")
            left = 0 if exhausted else (limit - used if limit else None)
            print(f"{slot:12} {m.get('id', '?'):24} used {used}/{limit or '?'} today, "
                  f"{'unknown' if left is None else max(0, left)} left")
        print(f"quotas reset at {quota_reset_text()}")
    elif a.cmd == "validate":
        ctx = load_storyboard(a.storyboard)
        n_words = sum(len(ln["text"].split()) for s in ctx.sb["shots"] for ln in s["lines"])
        print(f"{ctx.path.name}: valid ({len(ctx.sb['shots'])} shots, {n_words} narration words) -> outputs in "
              f"{os.path.relpath(ctx.out, ROOT)}/")
    else:
        gc(a.delete)


if __name__ == "__main__":
    stage_main(main)
