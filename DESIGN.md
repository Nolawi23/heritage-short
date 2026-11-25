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
  the end card reads "Come Down Here" / "for my family" (+ "Made with AI").
- **2026-11-25 - Pronunciation.** All Amharic words were mispronounced from Latin spelling; the spoken text now uses
  Ge'ez script inside the English sentence (`tts_text`), subtitles keep the Latin spelling, "baraka" became "bereka"
  (EXPERIMENTS.md, Latin vs Ge'ez).
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

## User tests

| # | Date | Who | What they saw | What they said | What changed |
|---|---|---|---|---|---|
| 1 | 2026-11-25 | the family (family chat) | two radio cuts: with names (`outputs/radio_cut.mp4`) vs "Come down here!" (`outputs/variants/storyboard_nonames/radio_cut.mp4`) | _pending_ | _pending: keep or drop the names in Mom's call_ |
| 2 | | the family | the film (9:16 crop vs blur fill) | | |

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
