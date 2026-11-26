# Design

**Come Down Here**: Every Sunday morning my mom calls us downstairs for coffee, the few hours a week our whole family is in the same room.

## Audience

My family (mom, dad, my sister, my brother), watching on their phones (vertical) in our family chat. Vertical (9:16) is the primary format; 16:9 is the archive copy.

## Intent

At the end they feel seen and grateful: this ordinary Sunday ritual is what keeps us close, whatever the week brought.

## Decisions log (human decisions and why)

- **2026-11-25 - Story and audience.** Chose our Sunday coffee ritual over the other ideas (a proverb, my journey
  abroad) because it is a real weekly family moment with strong sensory details: the smell of coffee and etan, the
  noise, the three rounds. Audience: my immediate family, watching on their phones in our family chat.
- **2026-11-25 - Consent and privacy approach.** Illustrated characters with no real faces (seen from behind, as
  silhouettes or as hands); preset AI voices, no voice cloning; Mom's line uses her own words, with her consent.
  See ETHICS.md.
- **2026-11-25 - Authenticity corrections from memory.** The first draft had grass under the whole living room
  (assumed from the traditional ceremony). Corrected: fresh grass only around the coffee-making area; the family sits
  on sofas in a U shape around a carpet and a big table (one at the back, two at the sides facing each other), in house slippers. The generic house was replaced with our real one
  (two storeys, a white gate, a small garden). The ceremony is shown the traditional way: the housekeeper on a low
  wooden stool by the charcoal brazier, roasting in a long-handled iron pan, pouring from a height. The first reference
  sheets defaulted to Western, light-skinned people and small children, so they were rejected and the descriptions
  made explicit (FAILURES.md #20).
- **2026-11-25 - "one hour" corrected to "few hours".** The author corrected the length of the Sunday coffee from their
  own knowledge: it lasts two to three hours, not one. Shot 11 now says "It's the few hours every week we're all in
  the same room. ..." and the logline says "the few hours a week".
- **2026-11-25 - Ending pacing.** Shots 10 and 11 were lengthened (6 s and 8 s) to give the ending room to breathe;
  the end card reads "Come Down Here" / "for my family" (+ "Made with AI"). Later (user test 2, 2026-11-26) shot 10
  went back down to 3.1 s: its line lasts 2.1 s and the rest played as dead time. Shot 11 keeps 8 s (8.5 s with its line).
- **2026-11-25 - Pronunciation.** The Amharic words sounded wrong from Latin spelling, so Ge'ez script inside the
  English sentence was tried (EXPERIMENTS.md: Latin vs Ge'ez, then a single Ge'ez word). Ge'ez changed the narrator's
  voice, so it stayed an experiment (takes in `outputs/experiments/`): **the final narration is spoken from Latin
  spelling everywhere**. The subtitles say "bereka" (corrected from "baraka"); shot 9's voice keeps its cached Latin
  take of "baraka" (`tts_text`).
- **2026-11-25 - Narrator voice.** Achird over Charon (EXPERIMENTS.md).
- **2026-11-25 - Music: candidate 2.** "c2 is calmer; c1 might clash with the narration, c2 sits under the voice."
  Pinned as `music_cues[0].selected = 2` (MusicGen, tizita caption, seed 43), so assembly always uses it. Mix check:
  ducking pulls the music down 7-13 dB under speech and keeps the voice 8-20 dB on top for lines 2-12, but MOM's opening
  call (short, filtered for the "downstairs" effect, at 0.25 s before the compressor engages) measured 2 dB *below*
  the music. **Fixed (author's decision, 2026-11-26, option A):** the score now starts 0.3 s after Mom's call
  (`music_start_after_line`) with a 1.5 s fade-in rather than a hard start (`music_fade_in_s`; the author asked for
  1-2 s), so the film's hook plays over silence, clearly on top.
- **2026-11-25 - "Etan" stays in Latin spelling.** After the single-word Ge'ez test the author kept the original Latin
  take (no TTS request; EXPERIMENTS.md).
- **2026-11-26 - Keyframe picks (round 2).** Shot 1 = c2 (closer to the centre, for the 9:16 crop), 2 = c1, 4 = c1,
  7 = c1, 8 = c2, 9 = c1, 10 = c2, 11 = c1; shot 6 stays as approved in round 1 (`shots[].keyframe_seed`).
  - Shot 7: the brother looks younger than 15; accepted by the author (FAILURES.md #28).
  - Shot 10: fewer cups than five is accurate, because the author and his brother don't really drink coffee.
  - Redo only shots 3 and 5 (2 candidates each). Shot 3 drops the housekeeper reference, which forced her frontal
    pose: her clothes are described in the text instead (the beige-grey dress and light head scarf of the reference),
    three-quarter view from the side, medium close-up, hands rinsing the beans in a metal bowl. Shot 5 describes the
    coffee pot's real shape (a round gourd, a narrow vertical neck as tall as the body, a short spout, a handle from the
    neck to the body, a tuft of straw in the opening); if neither candidate works, the author takes round-2 c2.
  Round 3: shot 3 = c1, shot 5 = c2 (the gourd-shaped pot). Shot 5's incense burner and smoke sit right of centre,
  so in 9:16 it uses the blur fill (EXPERIMENTS.md, crop vs blur).
- **2026-11-26 - Wan clip fitting.** A clip is slowed down by at most 1.25x (no strong slow motion); if it is still
  shorter than its shot, it plays and then continues with a slow Ken Burns move on its last frame, never a freeze.
  81-frame clips (3.4 s) were planned; they did not fit the T4 (FAILURES.md #32), so the final clips are all
  49 frames (2.04 s): shot 3 at 1280x704, shots 4, 5 and 9 at 960x544 (`shots[].wan`). Slowed 1.25x they cover
  about 2.55 s, and each Wan shot ends with a Ken Burns tail on its last frame: 2.8 s (shot 3), 3.3 s (shots 5 and 9),
  3.8 s (shot 4).
- **2026-11-26 - Mix (rough-cut check).** Mom's call plays over silence (the score enters 0.3 s after it with a
  1.5 s fade-in). Most lines sat 9-15 dB above the ducked music, but lines 7-9 (abol, tona, bereka) only 2-5 dB: a
  softer read over a louder passage of the score. Author's decisions: the score dips 4 dB under shots 7-9 only
  (`shots[].music_gain_db: -4`, 1 s ramps outside the dip; elsewhere unchanged), and the final loudness pass targets
  -1.5 dBTP (loudness stays -14 LUFS), because the rough cut measured -0.98 dBTP against a -1 dBTP ceiling.
- **2026-11-26 - Wan settings after the out-of-memory session (FAILURES.md #32).** Shot 3 keeps its 49-frame
  1280x704 clip. Shots 9, 5 and 4 are rendered at 49 frames and 960x544 (`shots[].wan`), on one T4 with the proven
  single-GPU notebook, in that order: 42 % fewer video tokens than the 49-frame 1280x704 setting that failed 3 times in
  4, at the cost of a softer image (1.33x upscale in 16:9). Rejected: 81 frames at 960x544 (94 % of the failing
  token count), 81 frames at 832x480 (softest), 33 frames at 1280x704 (only 1.4 s of motion).

- **2026-11-26 - After user test 2 (the author on a phone).** Vertical = centre crop (shot 5 blur fill only, as the
  crop would lose the incense smoke). The score's whistling end is cut (`music_cues[0].clean_until_s: 29.5`: the
  same c2 track loops back with a 2 s crossfade, no regeneration). Shot 6 gets a background murmur: AudioGen was
  rejected (audiocraft on Kaggle, estimated 25-40 min against the author's ~20 min limit), so a CC0 recording,
  "Small Crowd Walla" (Freesound #653920, chosen by the author), plays under shot 6 only, 4 dB under the score,
  low-passed at 2.5 kHz, 1 s fades, ducked under the narration. Shot 10: 6.0 -> 3.1 s (about 1 s before the next
  line). The family kept the names in Mom's call (user test 1).

## User tests

| # | Date | Who | What they saw | What they said | What changed |
|---|---|---|---|---|---|
| 1 | 2026-11-25 (answer 2026-11-26) | the family (family chat) | two radio cuts: with names (`outputs/radio_cut.mp4`) vs "Come down here!" (`outputs/variants/storyboard_nonames/radio_cut.mp4`) | they prefer the version **with names** | nothing to change: Mom's call stays "Nolawi! Delina! Come down here!" in `storyboard.json`; `storyboard_nonames.json` is kept only as the record of this test |
| 2 | 2026-11-26 | the author, watching the final films on a phone | `film_16x9.mp4`, `film_9x16.mp4` (crop), `film_9x16_blur.mp4` | (1) prefers the 9:16 **crop** (no blur); (2) a screech at about 0:35 and 1:06; (3) the "a lot of noise" line needs the noise: a low murmur of family chatter; (4) about 3 s of dead time at about 0:57 after "The coffee was never really the point." | (1) `film_9x16.mp4` is the vertical film; shot 5 keeps its blur fill because a crop cuts out the smoke (EXPERIMENTS.md); (2) the screech was the score's damaged end, cut with `clean_until_s` (FAILURES.md #33); (3) a CC0 recording of a small group's chatter under shot 6 (`ambience`, ETHICS.md); (4) shot 10 shortened from 6.0 to 3.1 s, so the gap before shot 11's line is about 1 s |
| 3 (final check) | 2026-11-26 | the author, re-watching the final films on a phone | the re-cut films of `de83110` (68.9 s, -14.38 LUFS, -1.38 dBTP) | everything is good: the Wan motion reads well in shots 3, 4, 5 and 9; the murmur has no words you can make out; the loop joins in the score are smooth; the screech is gone | nothing: these are the final films. ETHICS.md records the murmur as confirmed unintelligible |

## A reason per shot

Mirrors `shots[].reason` in `storyboard.json` (the source of truth).

| # | Shot | Size | Motion | Reason |
|---|---|---|---|---|
| 1 | `shot-01-bedroom` | medium | kenburns | Establishes the weekly morning call from downstairs that initiates the ritual. |
| 2 | `shot-02-staircase` | medium_close | kenburns | Shows the movement of stepping away from the busy week and heading toward family. |
| 3 | `shot-03-washing-beans` | close_up | wan | Introduces the careful preparation of the coffee ritual already underway. |
| 4 | `shot-04-roasting-beans` | close_up | wan | Focuses on the sensory aroma of roasting beans wafting up through the house. |
| 5 | `shot-05-jebena-etan` | insert | wan | Captures the unhurried atmosphere enhanced by fragrant frankincense smoke. |
| 6 | `shot-06-popcorn-table` | medium_close | kenburns | Highlights the homely snacks, popcorn and sometimes kolo, set out on the big living-room table. |
| 7 | `shot-07-abol-first-pour` | medium_wide | kenburns | Shows the initial round of coffee, abol, as conversation about the week begins. |
| 8 | `shot-08-tona-discussion` | medium | kenburns | Illustrates lively family animated discussions during the second cup, tona. |
| 9 | `shot-09-baraka-pour` | close_up | wan | Focuses on the calmer third round, bereka, bringing peaceful blessing. |
| 10 | `shot-10-empty-cups` | medium_close | kenburns | Signals that the real value of the ritual is togetherness rather than the drink itself. |
| 11 | `shot-11-house-exterior` | wide | kenburns | Concludes on the warm family home exterior, grounding the recurring weekly bond. |

## Fixed design rules (enforced in code)

- Subject centred and important content in the central third of every keyframe, so the 9:16 version is cut from the same frame.
- No text in generated images; the only on-screen text is subtitles and the end card.
- Visual prompts describe Ethiopian objects visually, never by their Amharic names (image models don't know them; a test enforces it).
- Subtitles at most 2 lines of about 40 characters, kept inside the 9:16 safe area.
- Narration sets the pace: a shot is never shorter than its narration + 0.5 s.
- Music sits under the voice (sidechain ducking); final mix at −14 LUFS for streaming platforms.
- "Made with AI" is always on the end card.
