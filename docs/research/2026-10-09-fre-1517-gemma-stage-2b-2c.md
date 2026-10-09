# FRE-1517 stages 2b and 2c — Gemma with a thinking-off planner, and the FRE-1561 minimum-search A/B

**Study:** FRE-1517 stages 2b and 2c, 2026-10-09. **Seat:** explore. **Windows:** 16:42–16:59 UTC
(2b) and 17:09–17:27 UTC (2c), gated by master. The stage-2 document is
`docs/research/2026-10-09-fre-1517-gemma-stage-2.md` (PR #1230), and its findings are referred to as
S2-F*n*.

This document records what was measured. Every finding carries its verdict, the query as run and its
actual output. A negative finding also carries its evidence arms. Recommendations appear only in
**Proposals**.

---

## Scope as run

| Stage | Owner direction | Arm | Session | Gateway |
|---|---|---|---|---|
| 2b | "apples to apples - test the gemma with a thinking off setting" | `stage2_gemma_planner_off` | `67bcc570` | `77d15414` (stage-2 build + #1231, the study-only planner mode) |
| 2c | "yes. and ask master to prioritize it" (FRE-1561) | `stage2c_gemma_min5` | `049306a2` | `af11e2c5` (2b build + #1232), `AGENT_SUB_AGENT_RESEARCHER_MIN_SEARCH_ROUNDS=5` |

The explore session read "thinking off" as Gemma's planner thinking off, with the primary unchanged.
That matches Flash-Next's own split, and master accepted that reading. Both stages ran s1_trip turns
1–8 on the production gateway: `channel=EVAL`, the 2026-09-15 study identity, consolidation paused, and
llama.cpp `b11521` with Gemma Q8_0 alone. Before each run, the arm and the scoring addendum were
committed: 2b at 16:23 UTC (`79a4f131`), and 2c at 17:01–17:02 UTC (`90594f26`, `e9e7b0ad`).

---

## Findings

### F1 — With the planner mode, the Gemma planner runs thinking off

**Verdict: POSITIVE.**

**Query:** the runner's `trace_reads.model_calls`, `role=primary`, at or before `planner_completed`, on
each fan-out turn of `67bcc570`. The catalog value was read in the container
(`/app/config/models.yaml`) at 16:35 UTC.

**Output** (input tokens · output tokens · reasoning chars):

| Turn | Stage 2 (`0513cc04`, no planner mode) | Stage 2b (`67bcc570`, planner mode) |
|---|---|---|
| 2 | 1,097 · 5,127 · 18,354 | 1,088 · 268 · **0** |
| 5 | 3,253 · 3,055 · 10,836 | 3,197 · 272 · **0** |
| 8 | 4,109 · 3,882 · 13,871 | 4,191 · 374 · **0** |

Every plan parsed. The output tokens fell by about 92%, to Flash-Next's level (401–481, S2-F3).

### F2 — Stage 2b delivered 8 of 8 in 846 s

**Verdict: POSITIVE.**

**Query:** `prod_session_runner.py --arm stage2_gemma_planner_off`. The outcome is the pre-registered
Part 1 outcome. The rows are in `scripts/eval/fre1517/out/stage2b-gemma-planner-off/` (git-ignored).

**Output:**

| Turn | Wall | Workers (type · level · rounds) | Web searches |
|---|---|---|---|
| 1 | 38 s | — | — |
| 2 | 185 s | res · thorough · 3 / gen · standard · 1 | 3 (+ run_python 1) |
| 3 | 142 s | — | perplexity 1, web 2 (+1 SearXNG timeout) |
| 4 | 22 s | — | — |
| 5 | 156 s | res · standard · 1 / res · standard · 3 | 6 |
| 6 | 45 s | — | — |
| 7 | 45 s | — | 1 |
| 8 | 213 s | res · standard · 1, 4, 1 | 6 |
| **Total** | **846 s, 8/8** | | |

The other figures:
- `model_call_error` 0, invalid primary tool arguments 0, truncated replies 0.
- Approval: 7 valid, and 1 unverified on t2, a worker `run_python` with `success: True` and 0
  `sub_agent_tool_approval_denied` events.
- Cost USD 0.48. Graph 13564 / 2722 / 9734 before and after.

For comparison (S2-F5): stage 2 Gemma delivered 7/8 in 1,072 s, and the Flash-Next control 8/8 in
2,795 s.

### F3 — On one scale, both Gemma sessions score at or above Flash-Next, inside scorer noise

**Verdict: POSITIVE.**

**Query:** three blind pools, each with two new Claude Opus scorers (A in code and turn order, B in
reverse), under `blind_scoring_stage2.md` and its addenda. Each key hash was posted on FRE-1517 before
scoring, and each key was checked against it after scoring.

**Output** — Q over turns 1–8 per scorer (A / B):

| Sheet | Pool 2 (16:08) | Pool 2b (17:00) | Pool 2c (17:28) |
|---|---|---|---|
| Gemma, planner on (`0513cc04`) | 32 / 33 | 35 / 34 | — |
| Gemma, planner off (`67bcc570`) | — | 35 / 35 | 34 / 34 |
| Gemma, min-5 variant (`049306a2`) | — | — | 34 / 35 |
| Flash-Next control (`ad0d090b`) | 31 / 34 | 33 / 34 | 29 / 35 |
| Sonnet workers (`3a0954b9`, anchor) | 35 / 34 | 36 / 36 | 34 / 35 |
| Sonnet no workers (`a0bfd47f`, anchor) | 32 / 32 | 31 / 33 | 33 / 32 |

- Take the planner-off Gemma sheet in pools 2b and 2c, and the planner-on sheet in pool 2. In those 6
  scorer readings, Gemma is higher in 4, and Flash-Next is higher by 1 point in 2. In pool 2b the
  planner-on sheet ties Flash-Next with scorer B (34 and 34).
- **Scorer variance is as large as the gap.** The same Flash-Next sheet ranges from 29 to 35. Pool 2c's
  scorer A docked many "minor errors" on it (Q 3 on t1, t2, t3 and t5), where scorer B gave 4 or 5.
- Every back-reference is Correct, and no H item is Broken, in all three pools with both scorers.

### F4 — The FRE-1561 variant reaches every researcher worker

**Verdict: POSITIVE.**

**Query:** `sub_agent_captures.system_prompt_chars` on the fan-out turns of `049306a2` and `67bcc570`.
Also `render_prompt_block(RESEARCHER, settings.sub_agent_researcher_min_search_rounds)` run in the
container at 17:08 UTC.

**Output:** every researcher capture in 2c shows 2,362 chars. In 2b every researcher capture shows 2,116.
The difference is 246, the variant's length at n=5. The container render gives a delta of 246 with the
setting read as 5. `general` workers show 1,180 in both stages, because the rule does not touch them.

### F5 — Gemma does not follow the minimum-search rule

**Verdict: NEGATIVE** (0 of 5 covered workers reached 5 rounds), with arms.

**Query:** the same captures. `tool_iterations`, `stop_reason` and `finish_reason` for every
`researcher` at `standard` level in `049306a2`.

**Output:**

| Turn | Worker | Rounds (`tool_iterations`) | stop_reason · finish_reason |
|---|---|---|---|
| 5 | res · standard | 3 | completed · stop |
| 5 | res · standard | 1 | completed · stop |
| 8 | res · standard | 1 | completed · stop |
| 8 | res · standard | 4 | completed · stop |
| 8 | res · standard | 2 | completed · stop |

Web searches per fan-out turn, 2b → 2c: 3 → 5, 6 → 6, 6 → 7.

- **Arm 1 (raw instance).** In the same capture store, the same session holds a researcher with
  `tool_iterations` 5: the t2 `thorough` worker. Flash-Next's control holds researchers with 9, 5, 6,
  10 and 5 rounds (S2-F5). A value of 5 or more is reachable and is recorded.
- **Arm 2 (path liveness).** The same query on the same captures returns the variant's prompt size
  (2,362, F4) and non-zero rounds on every researcher. Only the level filter selects these five rows.
- **Arm 3 (scope).** One session, s1_trip, Gemma Q8_0, n=5, 1 replicate, on 2026-10-09 17:09–17:27 UTC.
  The claim is "Gemma did not follow the rule in this session". It is not "Gemma cannot be made to
  search longer".

**A scope gap in the variant.** With the planner thinking off, the t2 researcher was `thorough` in both
2b and 2c. The rule's wording ("When your thoroughness is standard") does not cover that level.

### F6 — The pre-registered reading rule is met, but the gain is one turn from an uncovered worker

**Verdict: POSITIVE** (the scores are stated).

**Query:** pool 2c, Q on t2, t5 and t8 for the variant (`049306a2`) and the control (`67bcc570`). The
reading rule was fixed at 17:01 UTC: the hypothesis is supported only if both scorers give the variant
the higher sum.

**Output:**

| Scorer | Variant t2 · t5 · t8 = sum | Control t2 · t5 · t8 = sum |
|---|---|---|
| A | 4 · 4 · 3 = **11** | 3 · 4 · 3 = 10 |
| B | 4 · 5 · 3 = **12** | 3 · 4 · 4 = 11 |

- **The rule is met.**
- **The gain is t2.** Both scorers moved it from 3 to 4. Their reasons: the control's "worth the drive"
  ranking "rests on an inverted distance order". The variant's answer "evaluates each item". The worker
  behind t2 was `thorough`, which F5 shows the rule does not cover. It ran 5 rounds against 3.
- **t5:** +1 with scorer B only. **t8:** 0 with A, −1 with B.
- **Conclusion at this scope:** sampling variation explains the result as well as the rule does. The
  rule's own mechanism did not engage (F5).

---

## Proposals

At most ten. Each is for master's disposition and the owner's decision. None was executed by this seat.

1. **If Gemma is chosen, give it the thinking-off planner mode, after a D7 rerun.** F1 and F2 show that it
   matches Flash-Next's planner behaviour and makes the session faster. D7 on this exact configuration
   is 10/11 (S2-F1), and one rerun shows whether the single miss repeats.
2. **Do not ship FRE-1561's setting as a fix.** Gemma ignored the instruction (F5), and the measured gain
   is not attributable to it (F6). Keep the setting at its default of 0.
3. **If deeper worker research on Gemma matters, decide on a minimum enforced by the worker loop**, not
   by the prompt. This is a design change (ADR-0150 territory). It needs the owner's decision, and an ADR
   if the owner wants it.
4. **Treat Gemma against Flash-Next on s1_trip as a tie that leans to Gemma** (F3). The gap is smaller
   than the scorer variance. Gemma's clear advantages are speed (846 s against 2,795 s) and memory
   (28.8 GiB against 67–72 GiB, slm_server stage 1).
5. **Before a switch, run s2_tool and s3_logs_learning on Gemma with the planner mode and on Flash-Next.**
   s1_trip measures research and trip planning only. That was S2 proposal 4. Its case is stronger now,
   because the quality difference on s1_trip is inside noise.
6. **Score future pools with more than two scorers, or with fixed anchors per item.** The same sheet moved
   by up to 6 points between scorers (F3). That is the size of every difference this study can detect.

## Filed tickets

None by this document. FRE-1561 was filed earlier in this study by this seat, at the owner's request,
and is listed in that ticket's own record. It is the subject of stage 2c.

---

## Caveats

- One replicate per arm, one script, temperature 1.0.
- The pools ran about 20–30 minutes apart. Stage 2c is about 25 minutes after 2b, so web content may
  differ.
- The scorers are Claude models with at most 10 web checks each. Q is not in the approved rubric.
- Between 16:07 and 16:35 UTC the graph grew by 6 nodes and 1 Turn (trace `9398ec01`, a non-study
  session, while memory writes were on). Between 16:59 and 17:08 it grew by 2 nodes, with no Turn or
  Entity change. During each study session it did not change.

## Method appendix

- **Stores.** Production gateway `:9001`. ES `:9200` (`agent-logs-*`, sub-agent captures through the
  runner). Postgres in read-only transactions. Neo4j through `cypher-shell --access-mode read`. llama.cpp
  through `:8600`, which listed only `unsloth/gemma-4-26B-A4B-it` before each session.
- **Builds checked before each run.** 2b: `git diff --stat b6e3c98b 77d15414 -- src config` showed
  `config/models.yaml` only (+15). 2c: `git diff --stat 77d15414 af11e2c5 -- src config` showed 3 `src`
  files, all from #1232. The container env was read each time.
- **Captures.** Master moved 8 per session to `captains_log/captures_archive_fre1517/2026-10-09/` (2b
  confirmed by master, moved=8. 2c requested at 17:27 UTC).
- **Rejected.** Scoring the old 2026-09-15 Flash-Next session `de561b78` in the pools: it has turns 1–5
  only, and the control replaces it (S2-F11).
- **Sessions.** 2b `67bcc570-37ca-4a92-9407-23f6c0f8d8e1`, 16:42:00 to about 16:57 UTC. 2c
  `049306a2-9584-44aa-9df6-f18cc6243ce1`, 17:09:06 to about 17:25 UTC.
