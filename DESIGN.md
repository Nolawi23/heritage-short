# Design

> Status: **placeholder storyboard** (3 dummy shots). This document is filled in once the story notes are written
> and `make script` has drafted the real storyboard. Story and creative decisions are made by the author, not the pipeline.

## Audience

_To fill: who the film is for (e.g. family members abroad, the next generation)._

## Intent

_To fill: what the audience should feel or understand by the end._

## User tests

Two listening or viewing tests with people from the audience, each followed by a change to the storyboard.
Use `make radio` for test 1 (story and voices only, by ear) and the full film for test 2.

| # | Date | Who (role) | What they saw | What they said | What changed (commit / run_id) |
|---|---|---|---|---|---|
| 1 | | | radio cut (`outputs/radio_cut.mp4`) | | |
| 2 | | | film (16:9 or 9:16) | | |

## A reason per shot

The `reason` field of every shot in `storyboard.json` is the source of truth; this table mirrors it.

| Shot | Size | Motion | Reason |
|---|---|---|---|
| s01 | wide | wan | PLACEHOLDER: opens on the house and introduces both voices (MOM, then NARRATOR) |
| s02 | close_up | kenburns | PLACEHOLDER: shows a character close up |
| s03 | medium | kenburns | PLACEHOLDER: closes on a still image |

## Fixed design rules (enforced in code)

- Subject centred and important content in the central third of every keyframe, so the 9:16 version is cut from the same frame.
- No text in generated images; the only on-screen text is subtitles and the end card.
- Subtitles at most 2 lines of about 40 characters, kept inside the 9:16 safe area.
- Narration sets the pace: a shot is never shorter than its narration + 0.5 s.
- Music sits under the voice (sidechain ducking); final mix at −14 LUFS for streaming platforms.
- "Made with AI" is always on the end card.
