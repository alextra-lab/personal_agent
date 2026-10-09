# FRE-1517 stage 2b — blind scoring addendum (pre-registered before the stage-2b session)

Owner, 2026-10-09, in the explore session: "apples to apples - test the gemma with a thinking off
setting". Stage 2b runs Gemma again with its planner thinking off, as Flash-Next's planner runs. This
addendum fixes the scoring before that session runs. Everything not named here follows
`blind_scoring_stage2.md`: the scorers, their order, the web-check limit, the items, the Q anchors and
the output.

## The pool — five sheets, new codes, new scorers

| Sheet source | Session | Turns |
|---|---|---|
| Gemma, planner thinking off (stage 2b) | new | 1–8 |
| Gemma, planner thinking on (stage 2) | `0513cc04` | 1–8 |
| Flash-Next control (stage 2) | `ad0d090b` | 1–8 |
| Sonnet, workers (2026-09-15) | `3a0954b9` | 1–8 |
| Sonnet, no workers (2026-09-15) | `a0bfd47f` | 1–8 |

All five sheets are re-exported under new random codes and scored by two new scorers, so that every
sheet sits on one scale. The stage-2 scores stay on record. They are not mixed into the stage-2b table.

Export: `blind_export.py --last-turn 8 --out-dir out/blind-stage2b --run ...` (five runs). Post the key
hash on FRE-1517 before the first score.
