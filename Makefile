# Heritage Short: storyboard.json -> narrated short film.  The one command:  make setup && make film
#
#   STORYBOARD=path   storyboard to use (default storyboard.json; others write to outputs/variants/<stem>/)
#   FORCE=1           ignore the cache and regenerate everything (costs quota!)
#   VERTICAL=crop|blur|both   9:16 treatment of Wan clips (default both: film_9x16.mp4 + film_9x16_blur.mp4)
#   PREVIEW=1         assemble before Kaggle ran (Ken Burns for missing clips, placeholder score)
#   FFMPEG=/FFPROBE=  override the ffmpeg binaries (default: Homebrew ffmpeg-full if installed)

SHELL      := /bin/bash
PYTHON     ?= python3
VENV       := .venv
PY         := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,$(PYTHON))
STORYBOARD ?= storyboard.json
FORCE      ?= 0
VERTICAL   ?= both
PREVIEW    ?= 0
RUN_ID     := $(or $(RUN_ID),$(shell date -u +%Y%m%dT%H%M%SZ)-make)
export STORYBOARD FORCE VERTICAL PREVIEW RUN_ID FFMPEG FFPROBE

STAGE := $(PY) -m pipeline

.PHONY: help setup smoke validate test check-secrets script narration radio keyframes shots shots-test music \
        assemble budget costs film gc last-run

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | expand -t 16

setup: ## venv + pinned deps, git hook, ffmpeg filter check
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python >= 3.10 needed. Try: brew install python@3.11 && make setup PYTHON=python3.11")'
	@test -x $(VENV)/bin/python || $(PYTHON) -m venv $(VENV)
	@$(VENV)/bin/python -m pip install -q --upgrade pip
	@$(VENV)/bin/python -m pip install -q -r requirements.txt
	@git config core.hooksPath .githooks && chmod +x .githooks/pre-commit
	@$(VENV)/bin/python -m pipeline.av --check
	@echo "setup ok ($$($(VENV)/bin/python --version))"

smoke: ## list models, pin them, one call per provider, Kaggle auth
	$(STAGE).smoke_test

validate: ## validate the storyboard against schema/storyboard.schema.json
	$(STAGE).common validate $(STORYBOARD)

test: ## unit tests (no network, providers mocked)
	$(PY) -m pytest -q

check-secrets: ## fail if a tracked file contains a key (also the pre-commit hook)
	@$(PY) -m pipeline.check_secrets

script: ## draft the storyboard from story/notes.md (never overwrites without FORCE=1)
	$(STAGE).script $(STORYBOARD)

narration: ## Gemini TTS per line -> outputs/narration/, timeline.json
	$(STAGE).narration $(STORYBOARD)

radio: narration ## narration over a placeholder pad on black -> radio_cut.mp4
	$(STAGE).radio_cut $(STORYBOARD)

keyframes: ## FLUX reference sheet (approval) + one keyframe per shot
	$(STAGE).keyframes $(STORYBOARD)

shots: ## Wan 2.2 clips on a Kaggle T4 for changed "wan" shots
	$(STAGE).shots $(STORYBOARD)

shots-test: ## render ONE short Wan clip on Kaggle to measure the T4 (IMAGE=still.jpg before keyframes exist)
	$(STAGE).shots $(STORYBOARD) --test $(if $(IMAGE),--image $(IMAGE))

music: ## ACE-Step 1.5 candidates on a Kaggle T4 (choose one with --select)
	$(STAGE).music $(STORYBOARD)

assemble: ## ffmpeg: cut, subtitles, end card, ducking, loudnorm, 16:9 + 9:16
	$(STAGE).assemble $(STORYBOARD)

budget: ## today's Gemini requests used / left per model (from runs.jsonl; resets midnight Pacific)
	@$(STAGE).common budget

last-run: ## the most recent run's lines from runs.jsonl as a table (RUN=<run_id> for another run)
	@$(STAGE).common last-run $(if $(RUN),--run $(RUN))

costs: ## per-stage calls / latency / GPU minutes / list price -> COSTS.md
	$(STAGE).report_costs

film: ## THE command: narration -> keyframes -> shots -> music -> assemble (cached work is skipped)
	$(STAGE).common validate $(STORYBOARD)
	$(STAGE).narration $(STORYBOARD)
	$(STAGE).keyframes $(STORYBOARD)
	$(STAGE).shots $(STORYBOARD)
	$(STAGE).music $(STORYBOARD)
	$(STAGE).assemble $(STORYBOARD)

gc: ## list cache objects no output links to (CONFIRM=1 deletes them)
	$(STAGE).common gc $(if $(filter 1,$(CONFIRM)),--delete)
