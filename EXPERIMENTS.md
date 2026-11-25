# Experiments

One entry per experiment: **hypothesis → change → result → run_id** (the `run_id` finds every call in `runs.jsonl`).
`python -m pipeline.music --select <cue>=<n> --reason "..."` appends the music choice here automatically.

### Template
- **Hypothesis:**
- **Change:**
- **Result:**
- **run_id:**

---

### 2026-11-25 - Does FLUX.2 klein 4B on Workers AI really use a reference image?
- **Hypothesis:** the model's published schema only shows an opaque multipart body, but the changelog says it accepts
  up to 4 input images; if conditioning works, keyframes can be tied to the approved character sheet.
- **Change:** `make smoke` generated the same prompt with the same seed twice: once with no reference and once with the
  first image sent as `input_image_0`.
- **Result:** both requests were accepted and the second image differed (different sha256), so the reference is used.
  `config/models.json` has `flux.accepts_reference_images: true`, and keyframes are conditioned on approved sheets.
- **run_id:** `20261125T190207Z-smoke` (also `20261125T185943Z-smoke`)

### 2026-11-25 - Which Gemini Flash text model to pin
- **Hypothesis:** the newest listed Flash (`gemini-3.8-flash`) is the right choice for the script stage.
- **Change:** smoke tried 3.8, then 3.7, then 3.6 Flash.
- **Result:** 3.8 returned 503 "high demand"; the retries then exhausted its 20 requests/day free quota (429).
  3.7 was also out of daily quota. 3.6 answered and is pinned. Retry policy changed (see FAILURES.md).
  Re-pin with `python -m pipeline.smoke_test --repin` once 3.8 is available.
- **run_id:** `20261125T185943Z-smoke`

### Planned - Wan 2.2 TI2V-5B on a Kaggle T4
- **Hypothesis:** float16 + model CPU offload + VAE tiling runs Wan 2.2 TI2V-5B at 1280x704 on one T4 (16 GB) without
  NaNs, at a cost that fits a 6 h/week GPU quota.
- **Change:** `make shots-test` (one 49-frame, ~2 s clip of the first wan shot).
- **Result:** _pending (needs approval to run)_. Decide frames, steps and resolution from the measured GPU seconds and peak VRAM.
- **run_id:**
