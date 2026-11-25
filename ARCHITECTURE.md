# Architecture

Plain Python scripts, one per stage, glued by `make`. No framework: `pipeline/common.py` holds the shared parts
(validation, cache, run log, retries) and `pipeline/av.py` the ffmpeg helpers.

```
storyboard.json ──validate──▶ narration ──▶ keyframes ──▶ shots ──▶ music ──▶ assemble ──▶ film_16x9.mp4
   (schema/)                  │ timeline.json   (FLUX)      (Kaggle)   (Kaggle)   (ffmpeg)    film_9x16.mp4
                              └──▶ radio_cut.mp4 (optional)                                    subtitles.srt
```

## Stage contracts

Every stage begins with `load_storyboard()`, which validates against `schema/storyboard.schema.json` plus
cross-references (unique ids, known speakers, cues and characters, existing reference files) and refuses to run otherwise.
`out/` is `outputs/` for `storyboard.json` and `outputs/variants/<stem>/` for any other storyboard.

| Stage | Reads | Writes (symlinks into the cache unless noted) |
|---|---|---|
| `narration` | lines, `narration.voices` | `out/narration/<shot>_<n>.wav` (one per line), `out/narration/<shot>.wav` (joined, 0.4 s pauses), `out/timeline.json` (file) |
| `radio_cut` | timeline, line WAVs | `out/radio_cut.mp4` |
| `keyframes` | prompts, `style_block`, characters, approved reference images | `out/reference/<char>_c<n>.*` (candidates, not committed), `outputs/reference/<char>.*` (approved), `out/keyframes/<shot>.*` |
| `shots` | keyframes, `motion_prompt`, seed, `config/models.json:wan` | `out/clips/<shot>.mp4` for `"wan"` shots, or a `fallback` mark in the manifest |
| `music` | `music_cues` | `out/music/<cue>_c<n>.wav` (candidates), `out/music/<cue>.wav` (selected) |
| `assemble` | everything above | `out/film_16x9.mp4`, `out/film_9x16.mp4` (`_blur` variant), `out/subtitles.srt` (file) |

**Narration batch mode** (`narration.py`). The free tier allows only 10 TTS requests/day, so one request per line
does not fit a 12-line film. All uncached lines go out in **one multi-speaker request** (at most 2 speakers per
request, here MOM + NARRATOR; more speakers are split into consecutive groups). Each line is its own turn with its own
`speaker` and `style`, separated by `<long pause>` tags. The recording is cached in `.scratch/`, so it can be
re-analysed without a new request. The model does not keep the pauses consistent: line gaps of 1.0–2.3 s against
pauses inside lines of up to 1.0 s (FAILURES.md #13). So the split is a **local forced alignment**:

1. faster-whisper `small.en` (CPU, int8, no API, MIT) transcribes the recording with word timestamps.
2. The heard words are aligned to the expected lines (`difflib` on word tokens). At every line boundary the cut goes
   into the longest ffmpeg `silencedetect` silence between the last word of one line and the first word of the next.
3. Every piece is **re-transcribed on its own** (no prompt, so the check is not biased toward the script) and must
   reach a fuzzy match score of ≥ 0.80 with its line. The score is character-level, so "Ethan" for *Etan* still counts.
   The piece must also be plausible for its word count.

The scores, and what was heard, are logged per line (`provider: local`, `params.scores`). Pieces that pass are stored under
their **own per-line key**, the same key a single-line request would use, so a later edit of one line costs exactly
one single-line request (the no-names variant: 1 request). Lines that fail are logged (`status: batch_split_failed`)
and re-requested in batches of at most 4 lines, then one request per line, and only if today's budget covers all of
them; otherwise the stage stops before sending anything. The batch is one `runs.jsonl` line whose `params.lines` lists the line ids it
covered. `python -m pipeline.narration --voice-test NARRATOR=Charon,Achird` compares two voices in one request.

**Timeline** (`narration.py`): shot duration = max(`duration_s`, narration + 0.5 s), rounded up to whole frames at
24 fps. Narration starts 0.25 s after the cut. Every line gets absolute start and end times, which drive subtitles
and audio placement. Music spans come from consecutive shots that share a `music_cue`; the last span also covers the end card.

**Prompts** (`keyframes.py`): `image_prompt` + descriptions of the shot's `characters` + `style_block` + fixed rules
(subject centred, important content in the central third so 9:16 can be cut from the same frame, no text anywhere).

## Reference images: decision

`make smoke` checked FLUX.2 klein 4B on Workers AI. Its published schema only declares an opaque multipart body, so the
check is empirical: the same prompt and seed, once without and once with `input_image_0`. The request was accepted and
the output changed, so **keyframes are conditioned on the approved reference sheet** of each character in the shot
(up to 4), and the sheet's hash is part of each keyframe's cache key. Consistency also comes from the fixed
`style_block`, the character description in every prompt, and fixed seeds. If a re-pin finds a model without reference
support, `accepts_reference_images` becomes false and the pipeline falls back to prompt-only consistency automatically.

## Cache

```
key      = sha256(canonical JSON {stage, model, params, prompt, seed, upstream: {name: sha256 of input file}})
object   = outputs/cache/<key>.<ext>        committed (≤ 100 MB per file)
lock     = outputs/cache_manifest.lock      saves re-read and merge under a file lock (stages may run concurrently)
           outputs/.scratch/<key>.<ext>     unselected candidates, gitignored
manifest = outputs/cache_manifest.json      key → {stage, file, sha256, bytes, run_id, status, …}
```

Stage outputs are **relative symlinks** to objects, so variants share identical items and git stores each file once.
Dependencies are explicit in the keys:

| Item | Key inputs |
|---|---|
| narration line | TTS model, voice, style instruction, text, shot seed |
| joined shot narration | hashes of its line WAVs + pause |
| keyframe | FLUX model, size, full prompt, seed, hashes of the reference images used |
| Wan clip | Wan model + settings, `motion_prompt`, seed, **keyframe hash** (not the timeline: clips have a fixed length and are fitted at assembly) |
| music candidate | model, caption, bpm, key, time signature, duration, seed (+ n) |
| film | hashes of every input (timeline, keyframes, clips, music), end card, outputs, voices, VERTICAL mode, source hash of `assemble.py` + `av.py` |

Consequences, tested in `tests/test_cache.py`: a rerun is all cache hits; editing one line costs one TTS call
plus re-assembly; a variant storyboard that differs in one line costs exactly one TTS call; `FORCE=1` regenerates.
Rule: an item whose key exists is never regenerated unless `FORCE=1`. Keys are only required on a cache miss, so a
clean clone with committed outputs rebuilds the film without `.env`. `make gc` lists objects no symlink points to.

**Why media is committed.** Reproduce = rebuild from the committed cache on a clean clone with no keys (every
item is a cache hit; only ffmpeg is needed to re-assemble). Regenerate = `FORCE=1` (all providers, all quota).
Committing the objects once and linking to them keeps the repository small: the same line or keyframe is never stored twice.

**Quota strategy.** Narration is designed around the 10 TTS requests/day of the free tier: one batch request plus a
local whisper split (above) instead of one request per line; a daily budget guard refuses requests locally when the
budget is used up; retries back off 15-60 s on overload because failed requests count too.

## Kaggle jobs (`kaggle_job.py`)

Only called when at least one key is missing. One batch, one kernel run:

1. Pending keyframes are uploaded as a new version of the private dataset `<user>/heritage-short-wan-in`, named by
   sha256 so the notebook can detect a stale version; the runner waits for `ready`.
2. `kaggle/<stage>/` is rendered: the notebook's `# @JOBS` cell gets the job list (base64 JSON) and
   `kernel-metadata.json` gets `machine_shape: "NvidiaTeslaT4"`, `enable_gpu`, `enable_internet` (pip and Hugging Face)
   and the dataset. Then `kernels push`.
3. Poll `kernels status` every 60 s (3 h timeout), then download `/kaggle/working`: clips or WAVs plus `timing.jsonl`
   (GPU seconds per attempt, peak VRAM, errors). Every line goes into `runs.jsonl`, together with a session-overhead line.
4. T4 settings: no bfloat16 on this GPU, so the transformer runs in float16 with model CPU offload; the Wan VAE stays
   float32 with tiling; NaN or black output counts as a failed attempt, and a retry uses the next seed.

## Failure handling

- **Retries:** exponential backoff on 429 and 5xx, honouring `Retry-After` / `retryDelay`. 503 (overload) waits
  15/30/60 s, because free tiers count failed requests against the daily quota. Requests time out after 120 s.
  Every attempt is logged, including failures.
- **Throttling:** a minimum interval per provider (`config/models.json:min_interval_s`).
- **Quota:** a daily or free-tier 429 (or 429 on the last attempt) stops the stage cleanly with exit code 3 and a
  `status: "quota_exhausted"` line. Finished items stay cached, so rerunning resumes where it stopped.
- **Video fallback:** a Wan clip that fails `max_attempts` times is marked `fallback` in the manifest (not retried
  unless `FORCE=1`) and assembly uses a Ken Burns move instead (`status: "fallback"` in `runs.jsonl`).
- **Human decisions:** a missing reference approval or music choice stops with exit code 2 and the exact command to run.
- **Secrets:** values from `.env` and the Kaggle username are redacted from every log line; `check-secrets` runs as a
  pre-commit hook.

## Assembly (`assemble.py`)

- **Wan clip longer than its shot:** drop up to 0.5 s of weak frames from each end, then trim the rest from the end.
- **Wan clip shorter:** trim 0.125 s from each end, slow down by at most 1.3× (motion-compensated interpolation above
  1.05×), then cover any remaining gap with a slow Ken Burns push on the last frame, never a freeze. The plan applied
  to each shot is logged (`provider: ffmpeg, model: fit`).
- **Ken Burns shots and fallbacks:** each aspect ratio is cropped from the original keyframe, and the direction
  cycles (in, right, out, left, up, down).
- **9:16:** Wan clips exist only in 16:9, so `film_9x16.mp4` centre-crops them and `film_9x16_blur.mp4` puts a sharp
  fit on a blurred fill (`VERTICAL=both`, the default; `crop` or `blur` render one). Ken Burns shots are always cropped
  from the original keyframe at full quality.
- **Audio:** the narration stem (a per-speaker effect, e.g. MOM "downstairs" = band-pass + short echo) and the music
  bed are gain-matched to −20 LUFS. The music is ducked with `sidechaincompress=threshold=0.02:ratio=6:attack=300:release=1500`,
  the two are mixed, then two-pass `loudnorm` brings the mix to −14 LUFS / −1 dBTP (the measured result is logged).
  Music sections are joined with 2 s crossfades to fill their span.
- **Subtitles:** cues come from the line timings (at most 2 lines of about 40 characters). The SRT is exported, and
  an ASS file per aspect is burned in (9:16 sits above the bottom 25% UI zone).
- **End card:** drawn with `drawtext`, and always includes "Made with AI".
- **Export:** H.264 (High, CRF 18) + AAC 192k, constant 24 fps, `+faststart`.

`tests/test_assembly_ffmpeg.py` runs the whole assembly on synthetic media and checks the sizes, fps, duration, loudness and cache hit.
