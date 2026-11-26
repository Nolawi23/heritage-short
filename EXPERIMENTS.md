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

### 2026-11-25 - NARRATOR voice: Charon vs Achird
- **Hypothesis:** one of two preset voices fits the calm, warm, unhurried first-person narrator better; one two-speaker
  request can produce both takes and also proves the batch split on real TTS audio.
- **Change:** `python -m pipeline.narration --voice-test NARRATOR=Charon,Achird --line shot-01-bedroom_2`. One
  multi-speaker request where speaker 1 = Charon and speaker 2 = Achird both read "Every Sunday morning, it starts the
  same way: my mom's voice, from downstairs." with NARRATOR's style, separated by a `<long pause>`.
- **Result:** the split worked (one gap of 1.63 s; Charon 5.04 s, Achird 5.49 s for 13 words) ->
  `outputs/voice_test/NARRATOR_Charon.wav`, `outputs/voice_test/NARRATOR_Achird.wav`. A `<long pause>` is about 1.6 s.
  **Chosen: Achird.** Reasons: (1) Charon mispronounced "Sunday" at the start of the line; (2) Achird sounds
  smoother and more natural, like a young man telling his family a story, while Charon sounds more like a documentary
  narrator. Fair comparison: same line, same style instruction, one batch request.
- **run_id:** `20261125T195544Z-voicetest`

### 2026-11-25 - Splitting one batch narration recording into lines
- **Hypothesis:** a 12-line multi-speaker request can be cut into lines reliably, saving 11 of the 10/day TTS requests.
- **Change:** (a) silence only with one `<long pause>` between lines, (b) two tags, (c) local forced alignment
  with faster-whisper word timestamps + cut at the nearest silence + per-piece re-transcription check (≥ 0.80).
- **Result:** (a) and (b) were refused as ambiguous: line gaps 0.95–2.3 s overlapped sentence pauses of up to
  1.03 s. (c) passed all 12 lines from the already-paid recording (b) with scores 0.94–1.00 (lowest: "Etan" heard as
  "It's on", 0.937; "Nolawi" as "Nolawe", 0.964), for 0 extra requests. The timeline is 71.2 s; the no-names variant
  cost exactly 1 request (70.1 s).
- **run_id:** (a) `20261125T200511Z-narration`, (b) `20261125T200634Z-narration`, (c) the last `-narration` run in `runs.jsonl`

### 2026-11-25 - Pronunciation: Latin spelling vs Ge'ez script inside English text
- **Hypothesis:** the English TTS voice mispronounces Amharic words written in Latin letters (all of them were wrong:
  etan, kolo, abol, tona, baraka); writing those words in Ge'ez script inside the English sentence
  (`lines[].tts_text`; subtitles keep `text`) makes the model pronounce them as Amharic.
- **Change:** shots 5-9 re-synthesised with ዕጣን, ቆሎ, አቦል, ቶና, በረካ, in ONE batch request together with the new shot 11
  line (6 lines, 1 TTS request), split with whisper alignment (Ge'ez words ignored when matching; all 6 scored 1.00).
  Both takes kept: `outputs/experiments/latin_vs_geez/latin/<line>.wav` vs `.../geez/<line>.wav`.
- **Result (author's listening verdict):** Ge'ez script pronounced *etan* better, but the batch with several Ge'ez
  words **changed the narrator's voice** (character, accent). Decision: keep the Latin versions of kolo, abol, tona
  and bereka (shot 9 subtitles say "bereka" but speak the cached Latin take); test a single Ge'ez word next.
- **run_id:** `20261125T211356Z-narration-geez` (Ge'ez); the Latin takes are from the batch in `20261125T200634Z-narration`

### 2026-11-25 - A single Ge'ez word: does the voice stay the same?
- **Hypothesis:** the voice change came from several Ge'ez words in one request; one Ge'ez word per line, plus an
  explicit style note, keeps Achird's voice and accent while fixing the pronunciation of *etan*.
- **Change:** shot 5 only: `tts_text` "ዕጣን smoke curls through the room. Nobody is in a hurry.", same voice (Achird),
  per-line `style_note` "keep the same voice and natural English accent throughout" (other NARRATOR lines untouched).
  One single-line TTS request. Compare `outputs/experiments/etan_single_geez.wav` with
  `outputs/experiments/latin_vs_geez/latin/shot-05-jebena-etan_1.wav`.
- **Result:** the single Ge'ez word plus the accent note was the closest yet, but the author **kept the Latin
  spelling** for "etan" too: shot 5 reverted to its original Latin take (a cache hit, 0 TTS requests; the timeline
  went back to 71.8 s, and the subtitles were re-checked: 14 cues, all inside their line spans, none overlapping).
  Conclusion for this voice: Latin spelling everywhere; the Ge'ez takes stay in `outputs/experiments/` for reference.
- **run_id:** `20261125T214913Z-etan-single`

### 2026-11-25 - Reference sheet: housekeeper
- **Hypothesis:** one approved sheet keeps the housekeeper's hands consistent across shots 3, 7 and 9.
- **Change:** 4 candidates from the explicit description (an Ethiopian woman's brown hands on a low wooden stool by a
  charcoal brazier); keyframes are conditioned on the approved one.
- **Result:** **candidate 3 approved** (`outputs/reference/housekeeper.jpg`). Reason (author): "c3 had the details of our ceremony, the fresh grass, the charcoal (kesel) and the low wooden stool, and I liked that her face is only half visible."
  Later: the left full-figure panel is used (`housekeeper_single.jpg`, FAILURES.md #26); shot 7 stopped using it (#27)
  and so did shot 3, whose pose it forced (#29), so only shot 9 is conditioned on it.
- **run_id:** the last `-reference` run in `runs.jsonl`

### 2026-11-25 - Wan 2.2 TI2V-5B on a Kaggle T4: does it fit, and what does it cost?
- **Hypothesis:** float16 + model CPU offload + VAE tiling runs Wan 2.2 TI2V-5B at 1280x704 on one T4 without NaNs,
  at a cost that fits a 6 h/week GPU quota.
- **Change:** `make shots-test IMAGE=outputs/smoke/flux.jpg` (one 49-frame, 2.04 s clip, 30 steps, guidance 5.0, seed 42).
  Run 1: plain `enable_model_cpu_offload()`. Run 2: prompts encoded first with only the text encoder on the GPU,
  encoder freed, then the transformer with offload (FAILURES.md #11).
- **Result:** run 1 ran out of GPU memory after 14.7 s. Run 2 succeeded: the text encoder took 8 s at 11.5 GB peak
  (fp16 gave no NaN), and generation took **1,330.8 GPU s for 2.04 s of video = 652 GPU s per second of video**, peak
  15.22 GB (the T4 limit is about 15.6 GB). Valid clip: 1280x704, 49 frames, 24 fps, mean luma 132 (no black/NaN
  frames). Session overhead: 327 s of setup (pip, ~24 GB download, load), 28 min session in total.
  Projection: a 121-frame (5 s) clip at these settings is about 1 h of GPU time, so the weekly quota fits about 4-5 clips.
  Cheaper settings (fewer frames, 20 steps, 832x480) to be chosen with the author.
- **run_id:** `20261125T194102Z-make` (run 2; run 1 is the earlier `-make` run with the OutOfMemoryError line)

### 2026-11-25 - Amharic words in image prompts
- **Hypothesis:** image and video models don't know jebena, sini, rekebot, etan or kolo and may garble or ignore them.
- **Change:** every image/motion prompt describes the objects visually ("a round-bodied black clay coffee pot with a
  long narrow neck and spout", "small handleless white porcelain coffee cups", "a low wooden coffee stand", "white
  frankincense smoke from a small clay incense burner", "roasted barley grains"); the Amharic words stay in the notes,
  reasons and narration. `tests/test_schema.py::test_visual_prompts_describe_objects_instead_of_naming_them` enforces it.
- **Result:** over three keyframe rounds (evidence: the round-2 review notes and the author's picks), the visual
  descriptions worked for the simple objects but not, at first, for the coffee pot:
  - small handleless white cups: recognisable wherever they appear (shots 7-10), but the count is unreliable
    (shot 10 shows 3 cups instead of 5; the author accepted it, DESIGN.md 2026-11-26);
  - the low wooden tray: recognisable under the cups in shots 9 and 10;
  - the clay incense burner with white smoke: recognisable in shot 5 in every round (in the approved keyframe it sits
    right of centre, which is why shot 5 uses the blur fill in 9:16);
  - the coffee pot: "a round-bodied black clay coffee pot with a long narrow neck" gave a kettle and a round teapot
    in round 2; only a full shape description (a round gourd, a narrow vertical neck as tall as the body, a short spout
    from the body, a handle from the neck to the body, a tuft of straw in the opening) gave a pot the author approved
    (shot 5 c2, round 3). In shot 9, pouring, the shorter description was enough.
  Conclusion: a visual description replaces an Amharic name for everyday objects; a culturally specific object needs
  its exact shape spelled out. There was no control with the Amharic names (a test keeps them out of every prompt).
- **run_id:** `20261125T214952Z-keyframes` (round 1), `20261125T224605Z-kf-candidates` (round 2),
  `20261126T000420Z-6fb6` (round 3, shots 3 and 5)

### 2026-11-25 - Music caption: masenqo vs tizita-inspired
- **Hypothesis:** naming the masenqo (one-string Ethiopian fiddle) may not work, because ACE-Step may not know how to
  render one; describing the feeling instead (tizita, the Ethiopian nostalgic mode, which matches the film) with
  instruments the model surely knows (acoustic guitar, plucked lyre, light hand percussion) gives a more fitting score.
- **Change:** candidates 1 and 2 use the tizita-inspired caption (seeds 42, 43); candidate 3 uses the original
  masenqo caption (`music_cues[].ab_caption`, seed 44). Same bpm (75), key (D minor) and duration.
- **Result:** MusicGen candidates (ACE-Step could not run, FAILURES.md #19): **candidate 2 (tizita caption, seed 43)
  chosen**: "c2 is calmer; c1 might clash with the narration, c2 sits under the voice." The masenqo caption
  (candidate 3) was not chosen.
- **run_id:**

### 2026-11-25 - Which shots get Wan video
- **Hypothesis:** video models handle gentle physical motion (water, smoke, a pouring stream) well but morph moving
  bodies and limbs.
- **Change:** `wan` for shots 3 (washing beans), 4 (roasting smoke), 5 (incense smoke), 9 (pouring); people and limbs
  (shot 2, the feet on the stairs; shot 8, the siblings gesturing) stay Ken Burns. Four clips at 81 frames, 20 steps,
  1280x704 fit the ~5.4 h of weekly GPU left (about 25 min each by extrapolation).
- **Result:** all four shots have Wan clips, but shorter and (for 9, 5, 4) smaller than planned: 81 frames at
  1280x704 did not fit the T4 (FAILURES.md #32). Shot 3: 49 frames at 1280x704 (1,089 GPU s); shots 9, 5, 4: 49
  frames at 960x544 (about 580 GPU s each, peak 14.23 GB). Each 2.0 s clip plays at up to 1.25x and ends on a slow Ken
  Burns move (DESIGN.md). Whether the motion reads well (water, smoke, the pouring stream) is the author's verdict.
- **run_id:** `20261126T002226Z-wan-2gpu` (shot 3), `20261126T062836Z-wan-960` (shots 9, 5, 4)

### 2026-11-26 - Both T4s in parallel: one full Wan pipeline per GPU
- **Hypothesis:** the Kaggle "T4 x2" session has two GPUs and its quota is charged per session hour, not per GPU
  (measured: `quota_view` 3,538 s used vs about 1.07 h of logged GPU sessions, and the one session that used both GPUs,
  the music split, was not counted double). Two independent pipelines, each rendering 2 of the 4 Wan shots, should
  halve the session time at the same settings, as long as CPU RAM holds two 10 GB float16 transformers.
- **Change:** `python -m pipeline.shots --parallel --order 9,5,4,3` (`kaggle/wan/wan_i2v_2gpu.ipynb` +
  `wan_worker.py`): the prompts are encoded first in their own process (text encoder freed when it exits), then one
  worker per GPU starts, the second only after the first has loaded; GPU 0 renders 9 then 4, GPU 1 renders 5 then 3.
  81 frames, 20 steps, 1280x704, float16 + model CPU offload, float32 tiled VAE (the proven single-GPU settings and
  cache keys). Safeguards: a crashed worker's shots run one at a time on one GPU in the same order; after a CUDA
  out-of-memory error a worker retries that shot at the proven 49 frames (81 frames was never measured on the T4;
  the 49-frame test peaked at 15.22 of about 15.6 GB). Plan B, if the parallel session fails in its first minutes:
  the proven single-GPU notebook, `python -m pipeline.shots --order 9,5,4,3`.
- **Result:** the plumbing worked (prompts encoded once in 296 s at 11.5 GB; both workers loaded without the text
  encoder in 68-74 s; the crashed worker's shot was rerun on one GPU), but the settings did not fit: 81 frames ran out
  of GPU memory on every shot in the first step, the 49-frame retries succeeded only for shot 3 (1,089 GPU s, peak
  15.22 GB), and the GPU 0 worker was killed for CPU RAM (about 10 GB free with two pipelines). Session 43 min of
  GPU quota for 1 clip (FAILURES.md #32). Quota: per session hour, confirmed; two pipelines at once need more than
  the session's ~29 GB of CPU RAM with model CPU offload.
- **run_id:** `20261126T002226Z-wan-2gpu`

### 2026-11-26 - 9:16 treatment of the Wan shots: centre crop vs blurred fill
- **Hypothesis:** the 1280x704 Wan clips lose too much when centre-cropped to 9:16 (about 1.8x upscale of a
  396x704 slice); a sharp fit on a blurred fill keeps the whole frame but leaves the subject small.
- **Change:** `make assemble` (VERTICAL=both) exports `film_9x16.mp4` (crop) and `film_9x16_blur.mp4` (blur fill),
  identical except for Wan shots 3, 4, 5 and 9; Ken Burns shots are cropped from the 1792x1008 keyframe in both.
  **Exception, shot 5 (author's note, 2026-11-26):** its incense burner and smoke sit right of centre and fall outside
  the 9:16 centre crop, so shot 5 uses the blur fill in both films (`shots[].vertical_fill: "blur"`); a per-shot crop
  offset (`shots[].vertical_x`, -1 left .. 1 right) is the alternative if the author prefers a full-bleed crop there.
  So the two films differ only in shots 3, 4 and 9.
- **Result (author, watching both on a phone, 2026-11-26):** the **crop** version is preferred (no blur).
  `film_9x16.mp4` is the vertical film for the family chat; `film_9x16_blur.mp4` is still exported (VERTICAL=both)
  but only as an alternative.
- **Shot 5:** keeps the blur fill, by the author's rule "blur only if the crop would cut out the incense smoke": the
  author's own note above says the burner and smoke fall outside the centre crop. A shifted crop (`vertical_x`)
  remains the option if the author wants shot 5 full-bleed too.
- **run_id:** compared on the films of `2eab5ba` (now `f5b453a`); re-exported in the run that applied user test 2
  (DESIGN.md).

### 2026-11-26 - music cue `ethiopian-acoustic`: chose candidate 2
- **Hypothesis:** one of 3 seeds (42..44) fits the narration best
- **Change:** generated 3 candidates (caption: "gentle acoustic guitar and soft plucked lyre, light hand percussion, Ethiopian tizita-inspired nostalgic pentatonic feel, warm, reflective, homely, no strong lead melody, instrumental"; candidate 3 used the A/B caption "Warm acoustic guitar paired with soft traditional masenqo and gentle percussive rhythm, warm, reflective, homely.", 75 bpm, D minor)
- **Result:** candidate 2 chosen. Reason: c2 is calmer; c1 might clash with the narration, c2 sits under the voice.
- **run_id:** 20261125T213619Z-music-musicgen
