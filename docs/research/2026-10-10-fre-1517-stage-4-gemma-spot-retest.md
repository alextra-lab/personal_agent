# FRE-1517 stage 4 — Gemma spot retest after FRE-1564, FRE-1360, FRE-1566 and FRE-1562

**Date:** 2026-10-10 · **Seat:** cc-explore · **Ticket:** FRE-1517 · **Pre-registration:**
`scripts/eval/fre1517/spot_retest_stage4.md` (commit `8a0cc639`, before the first turn) · **Earlier stage:**
`docs/research/2026-10-10-fre-1517-stage-3-tool-and-learning-scripts.md`

The owner asked for a Gemma retest after the fixes, as a spot check. Master approved the plan, and the owner
said "Go". The retest ran Gemma 4 26B-A4B only, on three items: the turns that failed in stage 3, plus the
D7 planner probe.

## Summary

| Item | Stage 3 | Stage 4 | Reading |
|---|---|---|---|
| A — s3 t4, telemetry task | Not delivered (888 s, 20 researcher `web_search` rounds) | Delivered (501 s). No researcher web search | **PASS** on routing. The latency half is confounded (reading note) |
| B — s2 t7 | Runaway: sub_agent at 8,192, then the primary for 600 s | Not delivered (817 s): a researcher filled its context with `fetch_url` pages | **NOT PASS.** The runaway did not recur. New cause: FRE-1569 |
| B — s2 t13 | Runaway: the primary for 600 s | Delivered (168 s). Gemma's largest output 4,650 tokens | **PASS** (owner ruling, 2026-10-10, G5) |
| C — D7 | 10/11, decline-correct 27/30 | 10/11, decline-correct 24/30 | **FAIL.** `susan_3` 0/3 again, and `tool_logs` 3/3 → 0/3 |

The runaways did not recur in 13 turns. That is weak evidence: at the stage-3 rate, 13 clean turns happen
25–50% of the time with no fix. Both D7 misses are specific to Gemma. Flash-Next declines both fixtures on
the same planner prompt (G7).

## Scope as run

| Item | Session or run | Window (UTC) | Command |
|---|---|---|---|
| A | `f7458125` | 13:42:58–13:55:11 | `prod_session_runner.py --arm stage4_gemma_spot --script s3_logs_learning --stop-after 4` |
| B | `755dc023` | 13:55:20–14:43:29 | `prod_session_runner.py --arm stage4_gemma_spot --script s2_tool --stop-after 13` |
| C | probe tag `gemma-spot` | 14:43–14:52:27 | `fre1537` replay and longhist, mode `thinking_off`, then score |

- **Gateway:** main `baa281af`, `/health` build fingerprint `a6b16e59…`. All 355 `src/**/*.py` files and
  `config/models.yaml` are byte-identical between the container and `baa281af`. Consolidation was paused
  (`AGENT_ENABLE_SECOND_BRAIN=false`).
- **Engine, from slm_server's ready report (13:42 UTC):** `unsloth/gemma-4-26B-A4B-it`, UD-Q8_K_XL, MTP
  `draft-mtp` depth 2 (head `mtp-gemma-4-26B-A4B-it-Q8_0.gguf`), llama.cpp `b11521-b42b7e6d3`, `--parallel 3`,
  `--ctx-size 131072` as one unified KV. The catalog entry still says Q8_0 and now has `max_tokens` 12288
  (FRE-1562). Stage 3 ran Q8_0 without MTP, so **the code and the quant both changed.**
- **Workspace:** master moved the stage-3 outputs out of `/app/agent_workspace` before the run.
- **Graph:** 13571 / 2722 / 9734 (nodes, `:Turn`, `:Entity`) at both preflights. Master read it unchanged
  after the window.

---

## Findings

### G1 — Delivery and time per turn

**Verdict: POSITIVE.**

**Query:** the runner rows in `scripts/eval/fre1517/out/stage4-gemma-s{2,3}/` and `stage3-gemma-s{2,3}/`
(git-ignored): `http_wall_s`, `outcome`, and `trace_reads.model_calls` (the largest `output_tokens` of a
Gemma role, so `span_extraction` is left out).

**Output:**

| Turn | Stage 3: wall · delivered · largest Gemma output | Stage 4: wall · delivered · largest Gemma output |
|---|---|---|
| s3 t1 | 76 s · yes · 786 | 58 s · yes · 297 |
| s3 t2 | 276 s · yes · 1,684 | 26 s · yes · 1,003 |
| s3 t3 | 22 s · yes · 539 | 28 s · yes · 889 |
| s3 t4 | 888 s · **no** (fan-out trailer) · 1,727 | 501 s · yes · 2,182 |
| s2 t1 | 421 s · yes · 3,808 | 451 s · yes · **8,280** |
| s2 t2 | 148 s · yes · 2,069 | 242 s · yes · 1,405 |
| s2 t3 | 57 s · yes · 713 | 32 s · yes · 1,067 |
| s2 t4 | 142 s · yes · 3,031 | 324 s · yes · 1,595 |
| s2 t5 | 113 s · yes · 2,064 | 10 s · yes · 114 |
| s2 t6 | 161 s · yes · 3,001 | 87 s · yes · 1,988 |
| s2 t7 | 1,033 s · **no** (`model_call_error`) · 8,192 (sub_agent at its cap) | 817 s · **no** (fan-out trailer) · 3,542 |
| s2 t8 | 146 s · yes · 4,445 | 146 s · yes · 2,842 |
| s2 t9 | 30 s · yes · 876 | 34 s · yes · 1,026 |
| s2 t10 | 117 s · yes · 4,229 | 69 s · yes · 2,311 |
| s2 t11 | 63 s · yes · 1,949 | 36 s · yes · 1,274 |
| s2 t12 | 197 s · yes · 1,563 | 130 s · yes · 993 |
| s2 t13 | 602 s · **no** (`model_call_error`) · none completed | 168 s · yes · 4,650 |

- Stage 4 delivered 16 of 17 turns. Every turn was valid under FRE-1539.
- **Every Gemma call ended below its limit.** The largest output was 8,280 tokens on s2 t1, with 13,972
  reasoning chars. That is over the pre-registered near-miss mark of 6,000 and under the 12,288 limit. It
  is the only near miss.
- s2 t4 is slower and s2 t5 faster than in stage 3 for one reason: Gemma wrote the helper file on t4 this
  time, and only ran it on t5.

### G2 — A, s3 t4: the telemetry task no longer goes to a web search

**Verdict: POSITIVE** for the tool calls that ran. **NEGATIVE** for "the researcher made a tool call", with
the arms below.

**Query:** ES `agent-logs-*`, trace prefix `883b6190`, `event_type` in `tool_call_started`,
`sub_agent_start` and `sub_agent_complete`, sorted by time. Plus the runner row.

**Output:**
```
13:46:34 sub_agent_start     researcher  task 3b087ac1
13:46:39 sub_agent_complete  task 3b087ac1
13:46:39 sub_agent_start     general     task d7004a10
13:46:43 … 13:47:08  tool_call_started  query_telemetry ×6, run_python ×1
13:47:14 sub_agent_complete  task d7004a10
13:47:29 … 13:50:02  tool_call_started  bash ×5   (primary)
```
- One `query_telemetry` call was refused: "an aggregation has exactly one type".
- The reply gives p50 and p90 from two `curl` queries on `agent-captains-captures` (`steps.metadata.latency_ms`
  and `duration_ms`, by `model_role`). It names one role, `primary`.

**Arms for "the researcher made no tool call":**
- Arm 1 (1a): `tool_call_started` exists in the same store and trace. It is quoted above (13:46:43,
  `query_telemetry`).
- Arm 2: the same query, on the same trace and index, returns 12 `tool_call_started` events outside the
  researcher's window.
- Arm 3: scope is `agent-logs-*`, trace `883b6190`, and the researcher's window 13:46:34–13:46:39 (task
  `3b087ac1`). The tool events carry no task id, so the attribution of the `query_telemetry` calls to the
  `general` worker rests on time windows. ES counts are provisional (FRE-1051).

**Reading:** PASS on routing. The latency figures are not counted for or against the model (reading note).

### G3 — B, s2 t7: a researcher filled its context with fetched pages

**Verdict: POSITIVE.**

**Query:** the runner row (`trace_reads.model_calls`, `tools_completed`) and ES `agent-logs-*`
`sub_agent_complete` on trace `b791f193`.

**Output:**
- Researcher 1 (standard, "Identify the specific matchups for Week 11… using the previously established
  ESPN API method") called `fetch_url` 7 times. Its prompt grew 2,026 → 6,783 → 11,205 → 30,444 → 49,684 →
  68,925 → 88,166 → 107,406 → 126,852 tokens.
- `sub_agent_complete` for it: `success: False`, `error: "stopped: context_reserve (report: synthesized)"`.
  The other two workers: `success: True`.
- The turn ended with the fan-out trailer. The primary's reply (3,542 tokens, 10,735 reasoning chars) says
  that the schedule step failed, and gives partial picks.
- No call stopped at a limit, and no call timed out.

**Reading:** NOT PASS under the pre-registration (not delivered). The F3 runaway did not recur. The cause
is FRE-1569: FRE-1564 gave the researcher `fetch_url`, whose `max_chars` goes up to 50,000, with no limit per
worker. Master reproduced it on Flash-Next (trace `948af8ba`, researcher 2, `context_reserve`), so it is not
specific to Gemma. A 127k-token worker prompt also fills most of a 131k KV that 3 slots share.

### G4 — B, s2 t13: delivered, Gemma clean

**Verdict: POSITIVE.**

**Query:** the runner row for trace `bedc4c8c`.

**Output:** delivered in 168 s. Calls: primary 25,728 in / 1,198 out (4,171 reasoning chars), primary
28,117 in / 4,650 out (2,724 reasoning chars), and `span_extraction` 10,729 in / **4,096** out. Tools:
`web_search` ×1. The reply is the extended helper code with rest days and travel distance.

### G5 — The pre-registered rule for B reads t13 two ways

**Verdict: POSITIVE** (the counts are stated).

**Query:** `spot_retest_stage4.md`, section B. The row of G4.

**Output:** the rule says "no call on any role stops at its limit". It defines a limit stop as "a call
whose `output_tokens` equals a cap of 4,096, 8,192 or 12,288". On t13 the `span_extraction` call returned
exactly 4,096. That call runs on Claude Sonnet (cloud) after the reply, to extract claims for grounding. It
is not a Gemma call.

| Reading | t13 |
|---|---|
| Literal, as written | **FAIL** |
| By the rule's intent: Gemma runaways, F3 | **PASS** |

The explore seat did not choose. **The owner ruled PASS (2026-10-10).**

### G6 — `span_extraction` stops at 4,096 on long replies, in both stages

**Verdict: POSITIVE.**

**Query:** the runner rows: `output_tokens` of the `span_extraction` calls.

**Output:** sessions with `span_extraction` calls that returned exactly 4,096:

| Session | Turns | Turns at 4,096 |
|---|---|---|
| Stage 3, Gemma s2 | 20 | t4, t6, t8, t10, t17 |
| Stage 3, Gemma s3 | 20 | none |
| Stage 3, Flash-Next s2 | 20 | t7, t17 |
| Stage 3, Flash-Next s3 | 20 | t7, t9, t10, t14, t15 |
| Stage 4, Gemma s2 | 13 | t1, t8, t10, t13 |
| Stage 4, Gemma s3 | 4 | none |

That is 16 of 97 turns. The span on stage-4 s2 t1 in Tempo reads `gen_ai.operation.name: span_extraction`,
`gen_ai.request.model: anthropic/claude-sonnet-5`, `gen_ai.usage.output_tokens: 4096`.

**Reading:** on a long code reply, the claim extraction for grounding may be cut off. It hits in both
models' sessions, so it does not bear on the model choice. Not filed. See the proposals.

### G7 — D7 on Gemma: 10/11, and both misses are specific to Gemma

**Verdict: POSITIVE.**

**Query:** `fre1537` replay, longhist and score, run directory `telemetry/evaluation/fre1537-planner-probe/20261010-gemma-spot`
(git-ignored). It holds the stage-2 captures, re-rendered on main's code before the window. For Flash-Next,
build1's joint D7 run for FRE-1360 and FRE-1564 (`worktrees/build/…/20261010-fre1360-1564`, rows `planner`
and `planner-digest`), read from its `decide.jsonl`.

**Output, render:** only the planner system prompt changed. It now lists the FRE-1564 worker tools (4 → 9).
The hash went from `6947c986` to `9cf3c233`, and the length from 2,855 to 3,037 chars. All 27 planner user
messages are identical.

**Output, score (Gemma):**
```
FAIL  Decline-correct     >= 28/30   24/30
PASS  the other 10 thresholds (expand 48/48, follow-ups 12/12 and 12/12, 0 parse failures,
      0 reasoning chars, declined p50 25 tokens and 0.56 s, 60k extended 3.8 s, cold 21.0 s)
RESULT: FAIL (10/11 thresholds)
```

**Output, the two fixtures on the same prompt `9cf3c233`, declined of 3:**

| Fixture (expected: decline) | Gemma, stage 3 (prompt `6947c986`) | Gemma, stage 4 | Flash-Next, plain | Flash-Next, digest |
|---|---|---|---|---|
| `tool_logs` — "Check the logs for errors in the last hour." | 3/3 | **0/3** | 3/3 | 2/3 |
| `susan_3_thought_i_asked` | 0/3 | **0/3** | 3/3 | 3/3 |

- Gemma's `tool_logs` plan: one `general` task, "Query the system telemetry or logs to identify any
  error-level entries that occurred within the last 60 minutes."
- Flash-Next passed D7 11/11 on both arms on this prompt: plain decline 30/30, digest decline 29/30 (build1,
  reported on FRE-1360 and FRE-1564).

**Reading:**
- The new tool list turned one more decline into an expansion on Gemma, and not on Flash-Next.
- `susan_3` is now 0 of 9 over three Gemma runs.
- Gemma does not qualify as planner on the production prompt.

---

## Reading note — telemetry turns (owner, relayed by master, 2026-10-10)

The telemetry skills and tool are wrong for any model:
- The `query-elasticsearch` skill recommends fields retired in August.
- The `query-tempo` skill has no latency recipe.
- `query_telemetry` reads Elasticsearch only.

So a latency answer on a telemetry turn measures our instrument, not the model. See FRE-1567 (and the
explore comment on it) and FRE-1568.

**Rule for this stage:** keep the routing reading (no researcher web-searches for internal metrics).
Record what the model read and where. Do not count the latency figures for or against the model.

**Stage-3 findings that carry this confound:**

| Finding | What is confounded | What stands |
|---|---|---|
| F4 (s3 t4) | The latency half: both models' figures | The routing half: both planners sent the task to a web researcher |
| F5, s3 sheet | Each model's Q on s3 t4 | The other 19 s3 turns |
| F1, Gemma s3 t4 "not delivered" | — | Stands: it failed on routing (20 researcher `web_search` rounds) |

s3 t1, t2 and t16 count errors from `level` and `error` in `agent-logs`. Those fields are live, so these
turns do not carry the retired-field confound. They do share the long, stale `query-elasticsearch` skill
(25,692 chars). That is a weaker caveat, not a confound.

---

## Proposals

At most ten. Each is for master's disposition and the owner's decision. None was executed by this seat.

1. **s2 t13 is a PASS** (G5, owner ruling 2026-10-10). Future pre-registrations name the roles that a limit
   rule covers.
2. **Do not qualify Gemma as planner** on the production prompt `9cf3c233` (G7). Decline-correct is 24/30,
   and Flash-Next is 30/30 on the same prompt. This supports stage-3 proposal 2: remove the study-only
   Gemma planner mode.
3. **The stage-3 decision stands: keep Flash-Next as primary.** The runaways did not recur in 13 turns, but
   that is weak evidence (25–50% chance with no fix), and the code and the quant changed together. Any
   further Gemma primary work waits for FRE-1569, which also broke a Gemma turn here (G3).
4. **Triage the `span_extraction` 4,096 cap** (G6). It hits on 16 of 97 turns over stages 3 and 4, in both
   models' sessions. Find out whether the grounding extraction loses claims on long replies.
5. **Fix the telemetry path before any further telemetry turn is scored** (reading note). FRE-1568 is filed
   and approved. FRE-1567 is rewritten. **The owner ruled on 2026-10-10 that workers may read Tempo.** The
   build needs a read-only, allowlisted Tempo route for workers, under the same rules as `query_telemetry`.

## Filed tickets

- **FRE-1568** — Latency questions have no working path: the Elasticsearch skill points to retired fields,
  the Tempo skill has no model-call recipe, and `route_traces.latency_total_ms` is empty since 2026-08-08.
  Filed in Backlog by this study. Master approved it.

Related, not filed by this study: FRE-1567 (the explore seat commented on it), and FRE-1569 (master).

## Caveats

- One replicate. Local sampling is at temperature 1.0.
- **Two changes at once:** the code fixes, and Q8_0 → UD-Q8_K_XL with MTP. A clean result cannot say which
  change helped.
- Item A ran turns 1–4 only, and item B turns 1–13 only. No blind scoring in this stage.
- s3 t4 read live production telemetry, which differs between the stages.
- **A correction made during the window:** the explore seat first told master that nobody ran D7 on
  Flash-Next on the new prompt. That was wrong. build1 ran it (11/11 on both arms), and G7 uses it.

## Method appendix

- **Stores.** Production gateway `:9001`. ES `:9200` `agent-logs-*`. Tempo through the gateway container
  (`http://tempo:3200`). Postgres and Neo4j read-only. llama.cpp `:8600` (`/v1/models`: only
  `unsloth/gemma-4-26B-A4B-it`, UD-Q8_K_XL, at 13:42:04 and in each preflight).
- **Arm manifest.** `stage4_gemma_spot` in `arms.yaml` was committed with `quant` and `launch_flags` as TBD
  (the runner refuses TBD). Both were filled from slm_server's ready report before the first turn (commits
  `6a7f7ecd`, `70ef9ebb`).
- **D7 render.** `fre1537.gateway up/render/down` in the probe's own containers (`docker run` only), on the
  stage-2 captures copied to the new run directory. No digest file, as in stages 2 and 3. The timing arm
  uses the stage-2 captured primary bodies.
- **Captures.** 17, one per turn, all in `captures/2026-10-10/`. Master archived them to
  `captures_archive_fre1517/2026-10-10-stage4/`, restored consolidation and closed the window.
- **Sessions.** A `f7458125`, B `755dc023`.
