# FRE-1517 stage 3 — s2_tool and s3_logs_learning on both models (pre-registered before the first stage-3 turn)

Owner, 2026-10-09, in the explore session: "In want those tests". The owner chose production as is, and all
20 turns of each script. Everything not named here follows `blind_scoring_stage2.md`.

## The sessions

| Script | Flash-Next arm | Gemma arm |
|---|---|---|
| s2_tool (20 turns) | `stage2_flash_next` | `stage2_gemma_planner_off` |
| s3_logs_learning (20 turns) | `stage2_flash_next` | `stage2_gemma_planner_off` |

- The Gemma arm runs the study-only thinking-off planner mode (PR #1231).
- `AGENT_SUB_AGENT_RESEARCHER_MIN_SEARCH_ROUNDS` is unset (0), and consolidation is paused.
- All four sessions run on one gateway build, with no rebuild between them.
- Order: Flash-Next s2, then Flash-Next s3, then a Gemma window with Gemma s2 and Gemma s3.

**`bash` is denied on these turns** (FRE-1535, no approval transport on a harness turn). Both models meet
the same limit. Turns that the FRE-1539 check marks `invalid` are scored like every other turn, and they
are reported apart.

## Blind pools — one pool per script, two scorers each

- s2 pool: the two s2 sessions, turns 1–20, new codes.
- s3 pool: the two s3 sessions, turns 1–20, new codes.
- There are no anchors: no reference session exists for these scripts.
- Items: rubric Part 2 for every turn's `holds` and `back_refs`, and G on the research turns the rubric
  names (s2 turns 2 and 12, s3 turns 7, 10 and 15). Plus the stage-2 additions and Q.
- For s2, a reply that shows code is judged on what it shows. Scorers do not run code.

## The report

1. Q per sheet and scorer, over turns 1–20.
2. Broken H, as a share of the scorable H items (Held plus Broken), per sheet.
3. Fabricated R per sheet, with each instance named.
4. **The veto rule, now complete across three scripts per arm** (rubric V1 and V2). s1 comes from the
   pool-2b sheets: Flash-Next `ad0d090b` and Gemma `67bcc570`.
   - V1: at least one Fabricated R in two or more of the three scripts.
   - V2: more than 20% of scorable H items Broken over the three scripts.
5. The telemetry per session, as in stage 2. Part 1 delivered counts the valid turns only (FRE-1539), and
   the invalid turns are listed with their denied tool.

No winner rule is fixed in advance beyond the approved veto. The owner decides on the evidence (rubric
Phase 4).
