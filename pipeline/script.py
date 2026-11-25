"""Draft a storyboard from story/notes.md with Gemini (NOT part of `make film`).

Never overwrites an existing storyboard without --force: the draft is written to <stem>.draft.json instead.
The draft must validate against the schema and pass the brief's checks (8-12 shots, 130-220 narration
words, a reason per shot, at least 4 different shot sizes, shot 1 = a MOM line then a NARRATOR line);
if it does not, Gemini gets one repair round with the list of problems.

Privacy: notes.md is sent to the Gemini API. On the free tier Google may use it to improve its
products, so keep private family details (full names, addresses, health, dates of birth) out of it.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import common as c

NOTES = c.ROOT / "story" / "notes.md"
SEED = 2026

PROMPT = """You are the screenwriter of a 60-90 second narrated heritage short film.
Draft a storyboard as ONE JSON object that validates against this JSON Schema:
{schema}

Hard rules:
- 8 to 12 shots. Total narration across all lines: 130 to 220 words, in English.
- Shot 1 has exactly two lines: first a MOM line, then a NARRATOR line. Speakers must be keys of narration.voices
  (at least MOM and NARRATOR); voices are preset Gemini TTS voice names (e.g. Sulafat, Charon, Kore, Aoede, Gacrux).
- Every shot has a one-sentence "reason" stating why the shot exists in the story.
- Vary shot_size (use at least 4 different values). Use "wan" motion for at most half of the shots
  (the ones that need real movement) and "kenburns" for the others.
- image_prompt: only what is visible; no text, signs or lettering in the image; subject centred.
- motion_prompt: only motion and camera movement.
- style_block: one consistent visual style for every image.
- music_cues: instrumental; caption = instruments and mood only (tempo and key go in bpm and key).
- end_card.duration_s between 2 and 3. outputs = {{"fps": 24, "resolutions": {{"16:9": [1280, 720], "9:16": [720, 1280]}}}}.
- characters[].reference_image = null. Seeds: distinct integers.
Return only the JSON.

Story notes:
{notes}
"""


def checks(sb: dict) -> list[str]:
    errs = c.validate(sb)
    if errs:
        return errs
    shots = sb["shots"]
    words = sum(len(ln["text"].split()) for s in shots for ln in s["lines"])
    sizes = {s["shot_size"] for s in shots}
    if not 8 <= len(shots) <= 12:
        errs.append(f"need 8-12 shots, got {len(shots)}")
    if not 130 <= words <= 220:
        errs.append(f"need 130-220 narration words, got {words}")
    if len(sizes) < 4:
        errs.append(f"need at least 4 different shot sizes, got {sorted(sizes)}")
    if [ln["speaker"] for ln in shots[0]["lines"]] != ["MOM", "NARRATOR"]:
        errs.append("shot 1 must have a MOM line followed by a NARRATOR line")
    return errs


def generate(model: str, prompt: str):
    from google.genai import types
    resp = c.gemini_client().models.generate_content(
        model=model, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", seed=SEED, temperature=0.8))
    return resp.text, c.gemini_usage(resp)


def run(target: Path, force_write: bool) -> None:
    ctx_models = json.loads(c.models_path().read_text())
    cfg = ctx_models.get("gemini_text") or {}
    if not cfg:
        raise c.PipelineStop("config/models.json has no gemini_text model yet: run `make smoke` first")
    notes = NOTES.read_text()
    if "PLACEHOLDER" in notes:
        raise c.PipelineStop("story/notes.md is still the placeholder: write the story notes first")
    schema = c.SCHEMA_PATH.read_text()
    prompt = PROMPT.format(schema=schema, notes=notes)
    cache = c.Cache()
    key = c.Cache.key("script", cfg["id"], {"temperature": 0.8}, prompt, SEED)
    obj = cache.lookup(key)
    if obj:
        c.log_hit("script", "gemini", cfg["id"], key, seed=SEED)
        sb = json.loads(obj.read_text())
    else:
        call = dict(stage="script", provider="gemini", model=cfg["id"], throttle_key="gemini_text", models=ctx_models,
                    price_kind="gemini_text", seed=SEED)
        text = c.call_model(lambda: generate(cfg["id"], prompt), params={"prompt": prompt[-2000:]}, **call)
        sb, errs = _parse(text)
        if errs:
            print("draft has problems, asking for one repair:\n  " + "\n  ".join(errs[:10]))
            fix = (prompt + "\n\nYour previous draft:\n" + text + "\n\nFix these problems and return the full JSON:\n- "
                   + "\n- ".join(errs[:20]))
            text = c.call_model(lambda: generate(cfg["id"], fix), params={"prompt": fix[-2000:], "repair": True}, **call)
            sb, errs = _parse(text)
            if errs:
                raise c.PipelineStop("draft still invalid after one repair:\n  " + "\n  ".join(errs[:20]))
        cache.put(key, (json.dumps(sb, indent=2, ensure_ascii=False) + "\n").encode(), ".json", "script")
    dest = target if (force_write or not target.exists()) else target.with_suffix(".draft.json")
    dest.write_text(json.dumps(sb, indent=2, ensure_ascii=False) + "\n")
    words = sum(len(ln["text"].split()) for s in sb["shots"] for ln in s["lines"])
    print(f"script: {len(sb['shots'])} shots, {words} words -> {os.path.relpath(dest, c.ROOT)}"
          + ("" if dest == target else f"  ({target.name} exists; use FORCE=1 / --force to overwrite)"))


def _parse(text: str) -> tuple[dict, list[str]]:
    try:
        sb = json.loads(text)
    except json.JSONDecodeError as e:
        return {}, [f"not valid JSON: {e}"]
    return sb, checks(sb)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    c.storyboard_arg(ap)
    ap.add_argument("--force", action="store_true", help="overwrite the storyboard")
    a = ap.parse_args()
    run(Path(a.storyboard).resolve(), a.force or c.force())


if __name__ == "__main__":
    c.stage_main(main)
