"""Heritage Short: Wan 2.2 TI2V-5B worker for the two-GPU notebook (kaggle/wan/wan_i2v_2gpu.ipynb).

Same settings as the proven single-GPU notebook (wan_i2v.ipynb), split into processes so the two T4s of a Kaggle
"T4 x2" session each run their own full pipeline (no model is split across GPUs; see FAILURES.md #22):

  python wan_worker.py encode <jobs.json> <emb.pt>
      phase 1, alone: every prompt through the text encoder on GPU 0 (fp16; CPU bf16 if it overflows) -> emb.pt.
      The process then exits, so the 11 GB text encoder is gone before any video model is loaded.
  python wan_worker.py render <jobs.json> <emb.pt> <gpu> <shot,shot,...>
      phase 2, one process per GPU (the notebook sets CUDA_VISIBLE_DEVICES=<gpu>): loads the pipeline without the
      text encoder, float16 transformer with model CPU offload, float32 tiled VAE, renders its shots in the given
      order and writes out/<shot>.mp4 the moment each one is done.

Every attempt is one JSON line in /kaggle/working/timing.jsonl (pipeline/kaggle_job.py copies them into runs.jsonl).
"""
import gc
import glob
import hashlib
import json
import os
import sys
import time
import traceback

OUT = "/kaggle/working"


def ram_gb() -> float:
    """MemAvailable of the session, to watch CPU RAM with two pipelines loaded."""
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return round(int(line.split()[1]) / 1e6, 1)
    return -1.0


def main() -> None:
    mode, jobs_path, emb_path = sys.argv[1], sys.argv[2], sys.argv[3]
    J = json.load(open(jobs_path))
    cfg = J["config"]
    worker = f"gpu{sys.argv[4]}" if mode == "render" else "encode"
    os.makedirs(f"{OUT}/out", exist_ok=True)
    log_file = open(f"{OUT}/timing.jsonl", "a")

    def log(**kw):
        line = json.dumps({"job_id": J["job_id"], "worker": worker, **kw})
        log_file.write(line + "\n")  # one write per line, appended: safe with two workers
        log_file.flush()
        print(line, flush=True)

    import numpy as np
    import torch
    from PIL import Image
    from diffusers import AutoencoderKLWan, WanImageToVideoPipeline
    from diffusers.utils import export_to_video

    t0 = time.time()
    vae = AutoencoderKLWan.from_pretrained(cfg["model"], subfolder="vae", torch_dtype=torch.float32)

    if mode == "encode":
        pipe = WanImageToVideoPipeline.from_pretrained(cfg["model"], vae=vae, torch_dtype=torch.float16)

        def encode(device, dtype):
            pipe.text_encoder.to(device, dtype)
            out = {}
            with torch.no_grad():
                for job in J["jobs"]:
                    pe, ne = pipe.encode_prompt(prompt=job["prompt"], negative_prompt=cfg["negative_prompt"],
                                                do_classifier_free_guidance=cfg["guidance"] > 1, max_sequence_length=512,
                                                device=torch.device(device), dtype=dtype)
                    out[job["id"]] = (pe.to("cpu", torch.float16), None if ne is None else ne.to("cpu", torch.float16))
            return out

        torch.cuda.reset_peak_memory_stats()
        emb, where = encode("cuda", torch.float16), "gpu-fp16"
        if not all(torch.isfinite(p).all() and (n is None or torch.isfinite(n).all()) for p, n in emb.values()):
            pipe.text_encoder.to("cpu")
            torch.cuda.empty_cache()
            emb, where = encode("cpu", torch.bfloat16), "cpu-bf16"  # fp16 overflow in the T5 encoder
        torch.save(emb, emb_path)
        log(id=None, event="text_encoded", seconds=round(time.time() - t0, 1), where=where, prompts=len(emb),
            peak_vram_gb=round(torch.cuda.max_memory_allocated() / 1e9, 2), ram_avail_gb=ram_gb())
        return

    gpu, shots = sys.argv[4], [s for s in sys.argv[5].split(",") if s]
    emb = torch.load(emb_path)
    try:  # the text encoder is never needed here: prompts arrive as embeddings
        pipe = WanImageToVideoPipeline.from_pretrained(cfg["model"], vae=vae, text_encoder=None, torch_dtype=torch.float16)
        how = "without text encoder"
    except Exception as e:  # noqa: BLE001 - fall back to the proven way: load everything, then drop it
        log(id=None, event="load_fallback", error=repr(e)[:300])
        pipe = WanImageToVideoPipeline.from_pretrained(cfg["model"], vae=vae, torch_dtype=torch.float16)
        pipe.text_encoder = None
        gc.collect()
        how = "full, text encoder dropped"
    pipe.enable_model_cpu_offload()
    if hasattr(pipe.vae, "enable_tiling"):
        pipe.vae.enable_tiling()
    log(id=None, event="loaded", gpu_index=int(gpu), gpu=torch.cuda.get_device_name(0), how=how,
        seconds=round(time.time() - t0, 1), ram_avail_gb=ram_gb())
    open(f"/tmp/wan_loaded_{gpu}", "w").close()  # tells the notebook it may start the next worker

    def find_image(name, sha):
        for p in glob.glob(f"/kaggle/input/**/{name}", recursive=True):
            if hashlib.sha256(open(p, "rb").read()).hexdigest() == sha:
                return p
        return None

    def fit(img, w, h):
        """Centre-crop to the target aspect, then resize (keyframes are 16:9, Wan wants w x h)."""
        a, (W, H) = w / h, img.size
        if W / H > a:
            nw = round(H * a)
            img = img.crop(((W - nw) // 2, 0, (W - nw) // 2 + nw, H))
        else:
            nh = round(W / a)
            img = img.crop((0, (H - nh) // 2, W, (H - nh) // 2 + nh))
        return img.resize((w, h), Image.LANCZOS)

    jobs = {j["id"]: j for j in J["jobs"]}
    for sid in shots:
        job = jobs[sid]
        path = find_image(job["image"], job["image_sha256"])
        if path is None:
            log(id=sid, status="error", attempt=0, gpu_seconds=0, error="keyframe not found (stale dataset version?)")
            log(event="job_done", shot=sid, ok=False)
            continue
        image = fit(Image.open(path).convert("RGB"), cfg["width"], cfg["height"])
        frames_n, ok = job["frames"], False
        for attempt in range(1, job["max_attempts"] + 1):
            seed = job["seed"] + attempt - 1  # a retry uses the next seed
            torch.cuda.reset_peak_memory_stats()
            t = time.time()
            try:
                pe, ne = emb[sid]
                frames = pipe(image=image, prompt_embeds=pe.to("cuda"),
                              negative_prompt_embeds=None if ne is None else ne.to("cuda"),
                              height=cfg["height"], width=cfg["width"], num_frames=frames_n,
                              num_inference_steps=cfg["steps"], guidance_scale=cfg["guidance"],
                              generator=torch.Generator("cpu").manual_seed(seed), output_type="np").frames[0]
                if not np.isfinite(frames).all() or float(frames.mean()) < 0.02:
                    raise RuntimeError("NaN or black frames (float16 overflow?)")
                export_to_video(list(frames), f"{OUT}/out/{sid}.mp4", fps=cfg["fps"])
                log(id=sid, status="ok", attempt=attempt, seed=seed, gpu_seconds=round(time.time() - t, 1),
                    frames=len(frames), file=f"out/{sid}.mp4", gpu_index=int(gpu),
                    peak_vram_gb=round(torch.cuda.max_memory_allocated() / 1e9, 2), ram_avail_gb=ram_gb())
                ok = True
                break
            except Exception as e:  # noqa: BLE001
                oom = isinstance(e, torch.cuda.OutOfMemoryError)
                log(id=sid, status="error", attempt=attempt, seed=seed, gpu_seconds=round(time.time() - t, 1),
                    frames=frames_n, gpu_index=int(gpu), error=repr(e)[:400],
                    peak_vram_gb=round(torch.cuda.max_memory_allocated() / 1e9, 2), ram_avail_gb=ram_gb())
                traceback.print_exc()
                if oom and cfg.get("oom_frames") and frames_n > cfg["oom_frames"]:
                    frames_n = cfg["oom_frames"]  # the proven length (shots-test) for the next attempt
                gc.collect()
                torch.cuda.empty_cache()
        log(event="job_done", shot=sid, ok=ok)


if __name__ == "__main__":
    main()
