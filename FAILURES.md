# Failures

What broke → what we tried → the fix. Newest first. Failed calls are also in `runs.jsonl` (`status` ≠ `ok`).

| # | What broke | Tried | Fix |
|---|---|---|---|
| 8 | `FORCE=1` made assembly report every Wan clip as "not rendered" (caught by `tests/test_cache.py::test_force_regenerates`) | — | assembly reads the store directly (`Cache.path`); `FORCE` only affects generation (`Cache.lookup`) |
| 7 | A Gemini request hung for about 9 minutes while the model was overloaded | waiting | client timeout of 120 s (`http_options`) and `timeout=120` on TTS; httpx timeouts are now retryable |
| 6 | Retries on 503 "high demand" used up `gemini-3.8-flash`'s **20 requests/day** free quota (the 429 says failed requests count) | 4 quick retries (2/4/8 s) | 503 now backs off 15/30/60 s; smoke uses at most 2 attempts; a daily 429 stops the stage cleanly (`quota_exhausted`). Pinned `gemini-3.6-flash` for the script stage |
| 5 | No Gemini TTS model appeared in `models.list`; `generate_content` with `speechConfig` was rejected ("Unknown name speechConfig") | probing `*-tts` ids with `models.get` (404) | the current TTS models (`gemini-3.8-flash-tts`) use the **Interactions API**: `client.interactions.create(..., api_version="v1beta")`, style in a `speech_metadata` annotation, `store=False`. Smoke tries the documented ids in order |
| 4 | The FLUX.2 klein schema from Workers AI only declares an opaque multipart body, so reference-image support could not be read from it | parsing the schema | empirical check in smoke: same prompt and seed, with and without `input_image_0`; the output changed, so conditioning is used |
| 3 | `kaggle==2.2.5` fails on import: `No module named 'six'` (imported but not declared) | — | `six==1.17.0` pinned in `requirements.txt` |
| 2 | Homebrew's `ffmpeg` has no libass/freetype (no `subtitles`/`drawtext` filters) | — | `brew install ffmpeg-full` (keg-only); `pipeline/av.py` prefers it, `FFMPEG=`/`FFPROBE=` override, `make setup` checks the filters |
| 1 | The first `brew install ffmpeg-full` stopped halfway through the download | — | reran it with output to a log file; it completed |
