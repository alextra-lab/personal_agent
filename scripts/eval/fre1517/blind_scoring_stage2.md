# FRE-1517 stage 2 — blind scoring protocol (pre-registered before the first stage-2 turn)

Owner decisions, 2026-10-09, in the explore session: a Flash-Next control session runs on the same
gateway build as Gemma, and **the same two Claude scorers** score blind, as on 2026-09-15. This file
fixes the protocol before any stage-2 turn runs. A change after the first stage-2 turn discards the
scores made under the earlier version.

## The pool — one pool, one scale

| Sheet source | Session | Turns | Why it is in the pool |
|---|---|---|---|
| Gemma 4 26B-A4B, stage 2 | new | 1–8 | the candidate |
| Flash-Next llama.cpp, stage 2 control | new | 1–8 | today's production model on the same gateway build |
| Sonnet, workers (2026-09-15) | `3a0954b9` | 1–8 | ceiling anchor, and the bridge to the documented scores |
| Sonnet, no workers (2026-09-15) | `a0bfd47f` | 1–8 | second anchor and bridge |

The two Sonnet sessions carry documented 2026-09-15 scores (22.0 and 17.5 on turns 1–5). Their new
turn 1–5 subtotals measure how far the new scorers sit from the old ones. The documented Flash-Next
figure (17.0, session `de561b78`) has turns 1–5 only, so it cannot join a turn 1–8 pool. The control
replaces it as the Flash-Next reference. The report states the old figure beside the new one.

Export: `blind_export.py --last-turn 8 --out-dir out/blind-stage2 --run ...` (four runs). The script
prints the key's SHA-256. Post the hash on FRE-1517 before the first score. Post the key after the
last score.

## The scorers

- Two separate Claude Opus subagents. Each reads only `out/blind-stage2/sheets.md`, `rubric.md` and
  this file. Neither sees a run label, a model id, a timing or this study's comments.
- Scorer A scores in code order and turn order. Scorer B scores in reverse code order and reverse
  turn order.
- Each scorer may check at most 10 claims on the web, and names each one. All other fact
  judgements rest on general knowledge, and the scorer says so.

## The items, per turn

1. **Rubric Part 2, as approved:** H for every id in the turn's `holds` list (turns 5, 6, 7, 8), R
   for every `back_refs` entry (turns 6 and 8), G on turn 2.
2. **Additions, as on 2026-09-15:** truncation (a reply that stops mid-sentence or mid-list), drive
   times stated from Sóller, fact problems (count, with each one named), and the presence of the
   grounding note.
3. **Q, a holistic score from 1 to 5.** Q is not in the approved rubric. The owner accepted it on
   2026-09-15 for the question "which has best quality". The anchors:

| Q | Anchor |
|---|---|
| 5 | Answers every part of the request. No error found. Every standing rule held. Usable as it is. |
| 4 | Answers the request with a minor gap or one minor error. Every standing rule held. |
| 3 | Answers part of the request, or has several errors, or breaks one standing rule. |
| 2 | Misses a major part, or has a major factual error, or states an invented detail as fact. |
| 1 | No usable answer, mostly invented, or empty. |

A truncated reply scores at most 2.

## Output, per scorer

One table per sheet: turn, Q, each H, R and G score, truncation, fact problems (named), and a
one-line reason for Q. Then the sheet totals: Q over turns 1–5 and over turns 1–8.

## The report

- Q per sheet and scorer, the mean of the two scorers, over turns 1–5 and turns 1–8.
- Rubric items, with the V1 and V2 veto readings stated for one script only (the veto rule was
  written for three scripts, so it is indicative here).
- The Sonnet bridge: new turn 1–5 subtotals against the documented 22.0 and 17.5.
- Limits: one replicate, one script, web drift between 2026-09-15 and 2026-10-09, Claude scorers.
