"""Run one batch of GPU jobs as a Kaggle notebook (used by shots.py and music.py).

1. input files (keyframes) -> new version of the private dataset <user>/heritage-short-<stage>-in,
   named by sha256 so the notebook can verify it got the right version; wait until 'ready'
2. render kaggle/<stage>/: notebook template + JOBS cell (base64 JSON) + kernel-metadata.json
   (GPU T4 via machine_shape, internet on for pip/Hugging Face) -> kernels push
3. poll kernels status every poll_s until complete/error (timeout_s)
4. download /kaggle/working (clips or audio + timing.jsonl) into outputs/.kaggle/<stage>/<job_id>/
Only called when at least one cache key is missing, so a fully cached film never touches Kaggle.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import time
from pathlib import Path

from . import common as c

KAGGLE_DIR = c.ROOT / "kaggle"
JOBS_MARKER = "# @JOBS"


def api():
    """Authenticated KaggleApi. Imported lazily: `import kaggle` authenticates on import."""
    if not (os.environ.get("KAGGLE_API_TOKEN") or Path("~/.kaggle/access_token").expanduser().exists()
            or Path("~/.kaggle/kaggle.json").expanduser().exists()):
        raise c.MissingKey("Kaggle needs KAGGLE_API_TOKEN (.env) or ~/.kaggle/access_token")
    from kaggle.api.kaggle_api_extended import KaggleApi
    k = KaggleApi()
    k.authenticate()
    return k


def username(k) -> str:
    user = os.environ.get("KAGGLE_USERNAME") or k.config_values.get("username")
    if not user:
        raise c.MissingKey("set KAGGLE_USERNAME in .env")
    return user


def _quota_error(e: Exception) -> bool:
    return any(w in str(e).lower() for w in ("quota", "exceeded", "limit reached"))


def publish_dataset(k, user: str, stage: str, files: dict[str, Path], build: Path, note: str) -> str:
    slug = f"heritage-short-{stage}-in"
    ref = f"{user}/{slug}"
    d = build / "dataset"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    for name, src in files.items():
        shutil.copyfile(src, d / name)
    (d / "dataset-metadata.json").write_text(json.dumps(
        {"title": slug, "id": ref, "licenses": [{"name": "CC0-1.0"}]}, indent=2))
    try:
        exists = bool(k.dataset_status(ref))
    except Exception:  # noqa: BLE001 - 404 means first run
        exists = False
    if exists:
        resp = k.dataset_create_version(str(d), version_notes=note, quiet=True, convert_to_csv=False, dir_mode="skip")
    else:
        resp = k.dataset_create_new(str(d), public=False, quiet=True, convert_to_csv=False, dir_mode="skip")
    if getattr(resp, "error", None):
        raise c.PipelineStop(f"Kaggle dataset upload failed: {resp.error}")
    for _ in range(60):  # up to 10 min for processing
        if str(k.dataset_status(ref)).lower() == "ready":
            return ref
        time.sleep(10)
    raise c.PipelineStop(f"Kaggle dataset {ref} not ready after 10 minutes")


def render_kernel(stage: str, notebook: str, jobs: dict, user: str, dataset: str | None, cfg: dict, dest: Path,
                  gpu: bool = True) -> str:
    src = KAGGLE_DIR / stage
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    nb = json.loads((src / notebook).read_text())
    payload = base64.b64encode(json.dumps(jobs).encode()).decode()
    hits = 0
    for cell in nb["cells"]:
        if cell["cell_type"] == "code" and "".join(cell["source"]).startswith(JOBS_MARKER):
            cell["source"] = [f"{JOBS_MARKER} (rendered by pipeline/kaggle_job.py)\n", "import base64, json\n",
                              f"JOBS = json.loads(base64.b64decode('{payload}'))\n"]
            hits += 1
    if hits != 1:
        raise c.PipelineStop(f"kaggle/{stage}/{notebook} needs exactly one cell starting with '{JOBS_MARKER}'")
    (dest / notebook).write_text(json.dumps(nb, indent=1))
    meta = json.loads((src / "kernel-metadata.json").read_text())
    kernel_ref = meta["id"].replace("USERNAME", user)
    meta |= {"id": kernel_ref, "dataset_sources": [dataset] if dataset else [], "enable_gpu": gpu}
    if gpu:
        meta["machine_shape"] = cfg["machine_shape"]
    else:
        meta.pop("machine_shape", None)  # CPU session: no accelerator, no GPU quota
    (dest / "kernel-metadata.json").write_text(json.dumps(meta, indent=2))
    return kernel_ref


def poll(k, kernel_ref: str, timeout_s: int, poll_s: int) -> tuple[str, str]:
    t0 = time.monotonic()
    while True:
        st = k.kernels_status(kernel_ref)
        status = str(getattr(st, "status", st)).split(".")[-1].lower()
        failure = getattr(st, "failure_message", "") or ""
        print(f"  kaggle {kernel_ref.split('/')[-1]}: {status} ({(time.monotonic() - t0) / 60:.0f} min)", flush=True)
        if status in ("complete", "error", "cancel_acknowledged"):
            return status, failure
        if time.monotonic() - t0 > timeout_s:
            return "timeout", f"no result after {timeout_s} s"
        time.sleep(poll_s)


def run(stage: str, notebook: str, jobs: list[dict], config: dict, models: dict, files: dict[str, Path] | None = None,
        gpu: bool = True, machine_shape: str | None = None) -> dict:
    """Returns {"results": {job id: last timing line}, "lines": all timing lines, "dir": output dir,
    "status": kernel status, "failure": message, "wall_s": seconds}."""
    kcfg = models["kaggle"] | ({"machine_shape": machine_shape} if machine_shape else {})
    payload = {"config": config, "jobs": jobs}
    # unique per push (run id + time), so output left over from an earlier kernel version is detected as stale
    job_id = c.sha256_bytes((c.canonical(payload) + c.RUN_ID + str(time.time())).encode())[:12]
    payload["job_id"] = job_id
    build = c.outputs_root() / ".kaggle" / stage / job_id
    k = api()
    user = username(k)
    t0 = time.monotonic()
    dataset = publish_dataset(k, user, stage, files, build, f"job {job_id}") if files else None
    kernel_ref = render_kernel(stage, notebook, payload, user, dataset, kcfg, build / "kernel", gpu)
    print(f"kaggle: pushing {kernel_ref.split('/')[-1]} with {len(jobs)} job(s) (job {job_id}, "
          f"{kcfg['machine_shape'] if gpu else 'CPU only'})", flush=True)
    try:
        resp = k.kernels_push(str(build / "kernel"))
    except Exception as e:  # noqa: BLE001
        if _quota_error(e):
            raise c.QuotaExhausted(f"Kaggle refused the push (GPU quota?): {c.redact(str(e))[:300]}") from None
        raise c.PipelineStop(f"kaggle kernels push failed: {c.redact(str(e))[:300]}") from None
    err = getattr(resp, "error", None)
    if err:
        if _quota_error(Exception(err)):
            raise c.QuotaExhausted(f"Kaggle refused the push (GPU quota?): {err}")
        raise c.PipelineStop(f"kaggle kernels push failed: {err}")
    time.sleep(20)  # the new version takes a moment to appear
    status, failure = poll(k, kernel_ref, kcfg["timeout_s"], kcfg["poll_s"])
    out = build / "output"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    k.kernels_output(kernel_ref, str(out), force=True, quiet=True)
    timing = out / "timing.jsonl"
    lines = [json.loads(x) for x in timing.read_text().splitlines() if x.strip()] if timing.exists() else []
    stale = [ln for ln in lines if ln.get("job_id") != job_id]
    if stale:
        raise c.PipelineStop(f"Kaggle output belongs to another job ({stale[0].get('job_id')} != {job_id})")
    if status != "complete" and _quota_error(Exception(failure)):
        raise c.QuotaExhausted(f"Kaggle stopped the session: {failure}")
    results = {}
    for ln in lines:
        if ln.get("id"):
            results[ln["id"]] = ln
    return {"results": results, "lines": lines, "dir": out, "status": status, "failure": failure,
            "wall_s": time.monotonic() - t0, "kernel": kernel_ref, "job_id": job_id}


def log_session(stage: str, model: str, res: dict, price_kind: str = "kaggle_t4", gpu: bool = True) -> None:
    """One runs.jsonl line for session overhead (pip, downloads, model load): session time minus clip time."""
    done = next((ln for ln in res["lines"] if ln.get("event") == "done"), None)
    setup = next((ln for ln in res["lines"] if ln.get("event") == "setup"), {})
    session_s = (done or {}).get("seconds", 0)
    work = sum(ln.get("gpu_seconds", 0) or ln.get("cpu_seconds", 0) for ln in res["lines"] if ln.get("id"))
    overhead = max(0.0, session_s - work) if gpu else 0.0  # a CPU session uses no GPU quota
    price = c.list_price(price_kind, gpu_seconds=overhead)
    c.log_call(stage, "kaggle", model, params={"event": "session", "kernel": res["kernel"].split("/")[-1], "job_id": res["job_id"],
                                                "kernel_status": res["status"], "gpu": setup.get("gpu"),
                                                "session_s": session_s, "jobs_gpu_s": round(work, 1)},
               latency_ms=res["wall_s"] * 1000, gpu_seconds=overhead, list_price_usd=price,
               status="ok" if res["status"] == "complete" else "error", error=res["failure"] or None)
