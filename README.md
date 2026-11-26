# Heritage Short

A reproducible pipeline that turns [`storyboard.json`](storyboard.json) into a 60–90 s narrated short film:
English narration (two preset voices), an original instrumental score, burned-in English subtitles, an
AI-disclosure end card, and exports in **16:9** and **9:16**.

Everything that is generated is traceable: every model call is a line in [`runs.jsonl`](runs.jsonl), and every
generated file is stored in a content-addressed cache keyed by what it was made from.

## Quick start: the one command

```bash
make setup      # venv + pinned dependencies, git pre-commit hook, ffmpeg check
make film       # narration -> keyframes -> shots -> music -> assemble
```

`make film` starts from `storyboard.json` and **skips every item whose inputs have not changed**.
Results: `outputs/film_16x9.mp4`, `outputs/film_9x16.mp4`, `outputs/subtitles.srt`, `outputs/timeline.json`.

### Reproduce vs regenerate

| You want to… | Run | Needs keys? | Cost |
|---|---|---|---|
| **Reproduce** the committed film | `make film` | no: all items are cache hits from the committed `outputs/cache/` | none |
| Rebuild after editing one shot or line | `make film` | only for the providers of what changed | only the changed items, plus re-assembly |
| **Regenerate** everything from scratch | `make film FORCE=1` | yes (all four) | full quota (see below); GPU outputs are not bit-identical |
| Try a variant storyboard | `make film STORYBOARD=variants/alt.json` | only for items not shared with other storyboards | a variant that differs in one line costs exactly one TTS call |

Keys go in `.env` (copy [`.env.example`](.env.example)): `GEMINI_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`,
`CLOUDFLARE_API_TOKEN`, `KAGGLE_USERNAME`, `KAGGLE_API_TOKEN`. A stage asks for a key only when it has to call that provider.

### Requirements

- macOS or Linux, Python ≥ 3.10 (`make setup PYTHON=python3.11` to choose one), `git`, `make`.
  `make setup` creates the virtualenv in `~/.venvs/heritage-short`, outside the repo (synced folders such as iCloud
  evict its files, FAILURES.md #31); `make setup VENV=.venv` keeps it in the repo instead.
- **ffmpeg with libass and freetype**, needed only to re-assemble. On macOS run `brew install ffmpeg-full`; the pipeline
  finds it automatically, or you can set `FFMPEG=` and `FFPROBE=`. `make setup` warns if the `subtitles` or `drawtext`
  filters are missing.
- Splitting a new narration batch runs faster-whisper locally on the CPU (installed by `make setup`; downloads the
  `small.en` model, ~0.5 GB, on first use). It is not needed to rebuild from the cache.
- To regenerate: a Google AI Studio key, a Cloudflare account (Workers AI) and a Kaggle account with phone
  verification (GPU + internet notebooks).

### Why the media in `outputs/` is committed

A clean clone must rebuild the film **without any keys**: every generated file (narration, keyframes, clips, the
chosen score, the films) is committed once in the content-addressed `outputs/cache/`, and the stage outputs are small
symlinks to it. So `make film` on a fresh clone is all cache hits, while `FORCE=1` regenerates everything (and costs
the free-tier quota). Unselected candidates and temp files are not committed (`.gitignore`); every file is < 100 MB.

### Quota strategy

The free tier allows only **10 TTS requests a day**, so narration sends all changed lines in **one** multi-speaker
request and splits the recording locally with faster-whisper word timestamps, verifying each piece against its line;
a later edit of one line costs exactly one request. `make budget` shows what is left today. Image generation stays
within Workers AI's 10,000 free Neurons/day, and the Kaggle GPU work (6 h/week) is measured first (`make shots-test`).

## Stages

| Stage (`make …`) | What it does | Provider / model | Cost per film (free tiers) |
|---|---|---|---|
| `script` (not in `film`) | drafts `storyboard.json` from `story/notes.md`; never overwrites without `FORCE=1` | Gemini Flash (text) | 1–2 requests of 20/day |
| `narration` | all uncached lines in **one** multi-speaker request, split locally into lines by faster-whisper word timestamps and verified per line (cached per line), lines of a shot joined with 0.4 s of silence, `timeline.json` | Gemini 3.8 Flash TTS (+ faster-whisper `small.en` locally) | 1 of 10 requests/day (+1 per later edited line); stops cleanly on quota |
| `radio` (not in `film`) | narration over a quiet drone on black, to test the story by ear | ffmpeg | none |
| `keyframes` | reference-sheet candidates per character (you approve one), then one keyframe per shot conditioned on the approved sheets | FLUX.2 klein 4B on Workers AI | ~208 Neurons per keyframe of 10,000/day |
| `shots` | image-to-video for `"wan"` shots whose key changed, in one Kaggle T4 run (`make shots-test` measures one short clip first) | Wan 2.2 TI2V-5B | GPU minutes of a 6 h/week quota |
| `music` | 3 instrumental candidates per cue (you choose one), ~35 s sections joined to the timeline | MusicGen-medium fp32 on a Kaggle T4 (ACE-Step 1.5 overflowed in fp16, FAILURES.md #19) | ~2.5 GPU min per candidate |
| `assemble` | cut to the timeline, fit clips, Ken Burns stills, score looped with crossfades (cut at `clean_until_s`), `ambience` beds (CC0 murmur under shot 6), ducking, subtitles, end card, two-pass loudnorm to −14 LUFS (true peak under −1 dBTP), H.264/AAC at 24 fps, 16:9 + 9:16 | ffmpeg | none |
| `costs` | per-stage calls, latency, GPU minutes and list-price equivalent → [`COSTS.md`](COSTS.md) | — | — |

Human decisions are recorded in the storyboard so a fresh clone runs straight through:
`python -m pipeline.keyframes --approve <character>=<n>` and
`python -m pipeline.music --select <cue>=<n> --reason "..."` (the reason goes to [`EXPERIMENTS.md`](EXPERIMENTS.md)).

Other targets: `make last-run` (the latest run's calls from `runs.jsonl`: stage, shot, provider, cache hit /
new call / local step, status; `RUN=<run_id>` for another run), `make validate`, `make test` (no network), `make check-secrets` (also runs as the pre-commit hook),
`make smoke` (lists models, pins them in `config/models.json`, one call per provider), `make gc` (unreferenced cache objects).
Options: `VERTICAL=crop|blur|both` (9:16 treatment of Wan clips: centre crop and/or blurred-background fill; default both), `PREVIEW=1` (assemble with Ken Burns
and a placeholder score before Kaggle has run).

## Licence of the music

The score is generated with MusicGen-medium (Meta), whose weights are **CC-BY-NC 4.0 (non-commercial)**. The public
post will not be monetised, and its description credits "Music generated with MusicGen (Meta), CC-BY-NC 4.0"
(ETHICS.md).

## Docs

[ARCHITECTURE.md](ARCHITECTURE.md) (stages, contracts, cache, failure handling) ·
[DESIGN.md](DESIGN.md) (audience, intent, user tests, a reason per shot) ·
[EXPERIMENTS.md](EXPERIMENTS.md) · [FAILURES.md](FAILURES.md) · [COSTS.md](COSTS.md) · [ETHICS.md](ETHICS.md)

## Built with Claude Code

This repository was planned and written with [Claude Code](https://claude.com/claude-code) (Anthropic). A human
directed the work and approved the plan, and makes every story and creative decision (story, voices, reference
sheet, music choice).
