# FRE-1517 stage 3 — Gemma against Flash-Next on the tool-building and the log-and-learning scripts

**Study:** FRE-1517 stage 3, 2026-10-09 18:23 UTC to 2026-10-10 05:52 UTC. **Seat:** explore. Owner
direction: "In want those tests". The owner chose production as it is, and all 20 turns of each script.
Then "We should be running gemma first", then "Then flash if needed", and finally "yes, run Flash-Next".
Earlier stages: `2026-10-09-fre-1517-gemma-stage-2.md` (S2) and `2026-10-09-fre-1517-gemma-stage-2b-2c.md`
(S2b).

This document records what was measured. Every finding carries its verdict, the query as run and its
actual output. A negative finding also carries its evidence arms. Recommendations appear only in
**Proposals**. The owner decides.

---

## Scope as run

| Session | Arm | Script | Window (UTC) | Engine state |
|---|---|---|---|---|
| `860d7435` | `stage2_flash_next` | s2_tool t1–8, then t9–20 | 10-09 18:23–19:31, 10-10 02:17–03:32 | Flash-Next. Paused for the Gemma window. t9 has a cold cache |
| `c70518ad` | `stage2_gemma_planner_off` | s2_tool t1–20 | 10-09 19:33–20:48 | Gemma alone |
| `114cd7dd` | `stage2_gemma_planner_off` | s3_logs_learning t1–20 | 10-09 20:48–21:42 | Gemma alone |
| `514a039b` | `stage2_flash_next` | s3_logs_learning t1–20 | 10-10 03:32–05:26 | Flash-Next |
| D7 rerun | probe `gemma-thinking_off-r2` | — | 10-09 21:42–21:52 | Gemma alone |

- **One gateway build for all of it:** `29f36a50`, build fingerprint `87dde87f`. Master held every deploy
  (P2). Consolidation was paused (`AGENT_ENABLE_SECOND_BRAIN=false`), and the minimum-search setting was
  unset.
- **Gemma ran the study-only thinking-off planner mode** (#1231).
- **A difference from the earlier stages:** the SLM generation health check was on (FRE-1474). It sends one
  short request to the bound primary (Flash-Next) every 5 minutes, and it skips while Gemma is loaded.
- The protocol is `scripts/eval/fre1517/blind_scoring_stage3.md`, committed at 18:21 UTC, before the first
  turn.

---

## Findings

### F1 — Delivery and time: Flash-Next delivered every turn, Gemma 37 of 40, in less than half the time

**Verdict: POSITIVE.**

**Query:** `prod_session_runner.py`, the pre-registered Part 1 outcome and the FRE-1539 validity, per
session. The rows are in `scripts/eval/fre1517/out/stage3-*/` (git-ignored).

**Output:**

| Session | Delivered | Valid / invalid / unverified | Wall total · p50 · p90 · max | Not delivered | Cost |
|---|---|---|---|---|---|
| Flash-Next s2 | 20/20 | 13 / 5 / 2 | 8,102 s · 379 · 789 · 849 | — | USD 0.92 |
| Gemma s2 | 18/20 | 19 / 0 / 1 | 3,926 s · 117 · 421 · 1,033 | t7, t13 (`model_call_error`) | USD 0.71 |
| Flash-Next s3 | 20/20 | 18 / 1 / 1 | 6,313 s · 162 · 682 · 1,691 | — | USD 0.95 |
| Gemma s3 | 19/20 | 20 / 0 / 0 | 2,730 s · 46 · 276 · 888 | t4 (fan-out trailer) | USD 0.41 |

- Flash-Next s2 t9 has a cold prompt cache after the swap (155 s).
- No invalid primary tool arguments, no truncated reply, and graph 13568 / 2722 / 9734 at every read
  (18:23, 19:33, 02:11, 02:16, 05:52 UTC).
- Tool calls per session:
  - Flash-Next s2: web_search 48, run_python 27, bash 26, read 19, search_memory 15, write 9.
  - Gemma s2: bash 12, web_search 10, read 6, run_python 3.
  - Flash-Next s3: web_search 76, bash 18, run_python 17.
  - Gemma s3: web_search 25, bash 18.

### F2 — `bash` is gated by an allowlist, not denied outright

**Verdict: POSITIVE.** This corrects what the explore seat told the owner before the run, which was that
`bash` is denied on harness turns.

**Query:** ES `agent-logs-*`, by trace: `bash_auto_approved`, `bash_allowlist_miss`, `approval_denied`,
`tool_call_completed` (`tool_name: bash`).

**Output:** On Flash-Next s2 t5 (trace `2fed491a`), `ls`, `grep` and `python3 /app/agent_workspace/nfl_picks.py
schedule --week 11` each logged `bash_auto_approved`, then `bash_completed`. One command logged
`bash_allowlist_miss`, then `approval_denied … connection_lost`. Every invalid turn in F1 comes from such a
miss.
- Flash-Next missed the allowlist on 6 turns: s2 t1, t4, t5, t8 and t16, and s3 t3.
- Gemma missed it on none.

### F3 — Gemma generated without stopping until the 600 s limit, twice

**Verdict: POSITIVE.**

**Query:** the runner rows of `c70518ad` t7 (`8c347797`) and t13 (`b42dbd6e`): `errors_by_role` and
`model_calls`. Also slm_server's server-side figures, relayed by master.

**Output:**
- **t7:** a sub_agent call generated exactly 8,192 tokens, its `max_tokens` cap (229 s at 35.7 tok/s).
  Then the primary (prompt about 37.8k) generated 17,571 tokens in 600 s without stopping. Error:
  `LLMTimeout` "Local generation exceeded its 600.0s wall-clock budget".
- **t13** ("Add rest days and travel distance to the model in the helper"): the first primary call
  (prompt about 28.3k) generated about 18.0k tokens in 600 s without stopping. No other model call
  completed on that turn.
- slm_server: no stuck slot, a clean cancel each time (slot released 39–41 ms later), and the next request
  was normal. Without the cancel, the server's `n_predict` of 49,152 tokens allows about 27 minutes.
- Flash-Next had no `model_call_error` in its 40 turns.

**Whether the runaway was reasoning or answer text, and whether it looped: UNVERIFIABLE.** Searched: ES
`agent-logs-*` on trace `8c347797` from 20:01:10 to 20:12:00 UTC (no string field over 200 chars), and the
capture files of both turns (no string over 3,000 chars, where 18k tokens is about 70k chars). The router
forwarded 4.7–4.9 MB per runaway, and no store kept it.

### F4 — Both planners send an internal-telemetry task to a web researcher. Only Flash-Next recovers

**Verdict: POSITIVE.**

**Query:** `sub_agent_starts` and captures on s3 t4 ("Analyze the model call latencies over the last 24
hours…") in both sessions.

**Output:**
- Gemma (`bb8992a8`): a `researcher` (thorough) got "Retrieve latency metrics (specifically p50 and p90)
  for model calls over the last 24 hours". It ran 20 rounds of `web_search` up to its cap. A `general`
  worker had nothing to compare. The fan-out trailer followed, so the turn was not delivered.
- Flash-Next (`093d929d`): a `researcher` (standard) got "Query Elasticsearch `agent-logs-*` for the last 24
  hours…". It stopped after 2 rounds. The primary then used `read_skill` and `bash` 4 times, and delivered.

A researcher's only tool is `web_search` (ADR-0150), so both plans route the task to a worker that cannot
do it.

**Why both planners did it** (added 2026-10-10, ES `agent-logs-*` on both traces):
- The fan-out was decided before the planner. On both traces `intent_classified` reads `task_type:
  analysis, signals: [analysis_pattern]`. `decomposition_assessed` reads `strategy: hybrid, reason:
  analysis_moderate_hybrid`. The planner then received "Strategy: HYBRID", and the decline path is
  FRE-1515, which is parked.
- **No worker type can read our telemetry.** `WORKER_TYPES` grants `researcher` only `web_search`, and
  `general` grants `run_python`, `search_memory` and `recall_personal_history`. Every worker capture
  shows `skill_index_block_chars: 0`, so workers get no skills.
- **The primaries did use the skills,** inlined by keyword match without a `read_skill` call.
  `skill_index_assembled` shows `query-elasticsearch` (28,749 chars) on s3 t1, and
  `infrastructure-health` (8,908) on t3, in both sessions. Both primaries then queried Elasticsearch with
  `curl` through `bash`, as the skill shows. `read_skill` was called once in all 80 turns (Flash-Next s3
  t4, `self-telemetry`).

So F4 is a routing and capability gap, not a model failure. Flash-Next's primary redid the work itself,
and Gemma's did not.

### F5 — Blind quality: Flash-Next leads both 20-turn scripts by 7.5 to 8 points, with both scorers

**Verdict: POSITIVE.**

**Query:** two blind pools (s2, s3), two new Claude Opus scorers each, under `blind_scoring_stage3.md`.
Key hashes `da59bd39…` (s2) and `47722a35…` (s3), posted on FRE-1517 before scoring and matched after.

**Output:**

| Script | Flash-Next Q (A / B) | Gemma Q (A / B) | Gap |
|---|---|---|---|
| s2 tool, t1–20 | 68 / 70 = **69.0** | 61 / 62 = **61.5** | Flash-Next +7 / +8 |
| s3 logs + learning, t1–20 | 78 / 84 = **81.0** | 72 / 74 = **73.0** | Flash-Next +6 / +10 |
| s1 trip, t1–8 (S2b pool) | 33 / 34 = 33.5 | 35 / 35 = 35.0 | Gemma +2 / +1 |

- On s2, Gemma's two runaway turns score 1 each with both scorers. Without them, Flash-Next still leads by
  about 4 points over the other 18 turns.
- The s1 gap (1–2 points) is inside the scorer spread measured in S2b (up to 6 points on one sheet). The
  s2 and s3 gaps are not.

### F6 — Gemma states a re-queried figure as its earlier report (Fabricated R, s3 t16)

**Verdict: POSITIVE.**

**Query:** the delivered replies of `114cd7dd` t1 and t16.

**Output:**
- t1: "In the last hour, there were **24** error-level events recorded in the logs (from `curl …
  agent-logs-*/_search` count query)".
- t16, asked "This morning you checked the logs for me. How many errors did you report, and from which
  service?": "In the last 24 hours, I identified a total of **104 errors** in the logs", with a breakdown
  (web_search 6, fetch_url 3, generic 86, tool_call_failed 9, model_call_error 4).

Both scorers: Fabricated. Flash-Next's t16 denied any check that day and pointed to a May check. Both
scorers marked it Wrong, and B noted it as borderline.

### F6a — The recall turns that decide the veto were unanswerable by the study's own design

**Verdict: POSITIVE.** Added 2026-10-10, after the owner asked whether the routing failure provoked
Gemma's hallucinations.

**Query:** the scripts' `back_refs` labels, and each session's tools and reply on those turns.

**Output:**
- s3 t16 → t1 and s2 t19 → t1 both carry `window: memory`. The content sits beyond the 20-message live
  history (`AGENT_CONVERSATION_MAX_HISTORY_MESSAGES=20`), so only memory recall can reach it. **The study
  paused consolidation** (`AGENT_ENABLE_SECOND_BRAIN=false`), so no session content reached memory. Neither
  model could see turn 1.
- How each model handled the gap:

| Turn | Flash-Next | Gemma |
|---|---|---|
| s3 t16 | `recall_personal_history` ×2, `search_memory` ×2, `bash` ×1. "I have no record of a log check this morning" | `bash` ×5, no memory search. Presents a fresh 24-hour count ("104 errors") as its earlier report |
| s2 t19 | memory tools ×2, then reads the rules back from its own file's docstring ("rule #1") | memory tools ×2. Presents the owner's real stored preferences as the session's two rules |

**Reading:** the routing failure of F4 did not cause the fabrications. The study's memory pause did, by
making the answer unreachable. The difference in **gap handling** is real: Gemma fills the gap with
something plausible, and Flash-Next admits it or finds another route. But the V1 veto (F7) rests on two
turns that no model could answer correctly.

**The quality gap without the `memory`-window turns** (s2: t18, t19. s3: t16, t18, t19), per scorer:

| Script | Scorer A: Flash-Next / Gemma (gap) | Scorer B: Flash-Next / Gemma (gap) |
|---|---|---|
| s2, 18 turns | 60 / 58 (+2) | 63 / 59 (+4) |
| s3, 17 turns | 68 / 63 (+5) | 75 / 65 (+10) |

Flash-Next still leads on both scripts with both scorers. The s2 lead shrinks to 2–4 points, and it still
includes Gemma's two runaway turns (Q 1 each, F3).

### F7 — The approved veto over three scripts: Gemma is vetoed under one scorer's reading

**Verdict: POSITIVE** (the counts are stated).

**Query:** rubric V1 and V2 over s1 (S2b pool sheets QLAA = Gemma, QUSJ = Flash-Next), s2 and s3, per
scorer.

**Output:**

| | Gemma, scorer A | Gemma, scorer B | Flash-Next, scorer A | Flash-Next, scorer B |
|---|---|---|---|---|
| Scripts with a Fabricated R | s3 only | **s2 (t19) + s3** | none | s2 (t15) |
| V1 (two or more scripts) | no | **yes** | no | no |
| Broken H / scorable, 3 scripts | 3/31 = 10% | 4/34 = 12% | 3/38 = 8% | 3/40 = 8% |
| V2 (over 20%) | no | no | no | no |

- **The deciding item for Gemma is s2 t19.** Asked "Remind me what two rules I gave you at the very
  beginning", it gave two stored preferences from memory ("artifacts only on request", "plain text by
  default"), not the session's rules. A scored it Wrong, B Fabricated. Both gave Q 1.
- **Read F7 with F6a.** Both of Gemma's V1 items are `memory`-window recalls made unanswerable by the
  memory pause. The veto still measures gap handling, but not the ability to recall.
- **Flash-Next crosses V2 only under scorer A's strict reading of I4.** If its throwaway patch scripts
  count as a second code file, it reaches 8/38 = 21%. Scorer A chose the lenient reading and flagged it.
  Scorer B did not raise it.

### F8 — The D7 rerun on Gemma repeats run 1: the same single failure, now systematic

**Verdict: POSITIVE.**

**Query:** `fre1537.replay` and `longhist` with tag `gemma-thinking_off-r2`, the same run directory and
inputs as S2-F1, then `score`.

**Output:** 10/11 again. Decline-correct 27/30 (bar 28), with `susan_3_thought_i_asked` declined 0 of 3
again, so 0 of 6 over both runs. Expand 48/48, the follow-ups 12/12 and 12/12, 0 parse failures and 0
reasoning chars. Declined p50 0.54 s. 60k cold 20.7 s, extended 3.8 s.

### F9 — The shared agent workspace let both models draw on the owner's earlier NFL work

**Verdict: POSITIVE.**

**Query:** ES `tool_call_started` arguments (`read`, `write`, `bash`, `run_python`, `search_memory`) by
session, and `ls -la /app/agent_workspace` in the gateway container.

**Output:**
- Flash-Next s2's first calls were `search_memory` queries for "NFL weekly prediction tool design
  predict_week.py…". It then wrote a new `nfl_picks.py`.
- Gemma s2 ran `find /app -name "predict_week.py"` and read
  `/app/agent_workspace/nfl-predictor/predict_week.py`, `config.py` and `week2_2026_games.json`. Those
  files date from 2026-09-16, from the owner's own sessions. It then wrote `nfl_helper.py`.
- Neither session touched the other's file.

### External reports — what others see with Gemma 4 26B-A4B (web, not measured here)

Collected on 2026-10-10 by a research subagent. The explore seat opened the first two sources itself.
Reddit was unreachable for the search tool, two articles returned 403, and some Hugging Face dates are
inferred.

- **Runaway repetition** is the most widely reported failure on 31B and 26B-A4B. It runs until the budget
  is gone, gets worse with long context, and happens with thinking on and off. google-deepmind/gemma #622,
  opened 2026-04-11 and still open in September 2026 (opened by explore).
- **A template fix** of 2026-07-09 removed reasoning re-injection in multi-turn tool use, and cured the
  12B model (1/576 → 0/576). A tester then wrote that 26B was "still failing with runaway and rumination
  especially on the thinking channel". huggingface.co/google/gemma-4-12B-it/discussions/38 (opened by
  explore).
- **Tool-call markup in content** on llama.cpp: llama.cpp #22786, llama-cpp-python #2227.
- **Weaker with thinking off:** huggingface.co/google/gemma-4-26B-A4B-it/discussions/10.
- **Premature stopping in agent loops:** github.com/HazAT/pi-interactive-subagents/issues/22.
- **Agentic benchmarks:** Tau2 68.2 against 81.2 for Qwen3.6-35B-A3B
  (openrouter.ai/compare/google/gemma-4-26b-a4b-it/qwen/qwen3.6-35b-a3b).
- **Speed:** 63.9 tok/s on an M3 Max with llama.cpp (hiesch.eu/blog/llamacpp-benchmarks-speculative-decoding).
- **Not found:** reports on over-decomposition as a planner, or on restating re-queried data as an earlier
  report.

**Open question for this study:** the date of the chat template embedded in our GGUF, and the Gemma fixes
in llama.cpp build `b11521` (PRs #21326, #21418, #21661 per the reports). Both are UNVERIFIABLE from the
Seshat side. The slm_server seat can read them.

---

## Proposals

At most ten. Each is for master's disposition and the owner's decision. None was executed by this seat.

1. **On this evidence, keep Flash-Next as primary.** Gemma ties on the trip script and trails on tool
   building and on logs plus learning: by 7.5–8 points on all turns (F5), and by 2–10 points without the
   unanswerable `memory` turns (F6a). It has two runaways (F3). The veto (F7) is weaker evidence than it
   looked (F6a). Its advantages are speed (2.1–2.3× less wall time, F1) and memory.
2. **Remove the study-only Gemma planner mode** (#1231), as its own catalog comment says when Gemma is not
   chosen. D7 is 10/11 twice, and the miss is systematic (F8).
3. **Bound every local primary generation.** Either an explicit `max_tokens` on primary calls, or a lower
   `n_predict` on the server. The 600 s client budget is the only bound today, and it costs 10 minutes and
   the turn (F3). This applies to any local model, not only Gemma.
4. **Keep the tail of a cancelled stream at the router** (the last few KB, and the reasoning and content
   byte counts). Then the next runaway shows whether it loops, and in which channel (F3).
5. **Close the telemetry routing gap (F4).** A task over our own data must not fan out to workers that
   cannot reach it. Two ways: the planner may decline (FRE-1515), or a worker type gets the telemetry
   tools and skills. Flash-Next recovered only because its primary redid the work.
6. **Give each study session its own agent workspace,** or clear it at session start. A shared
   `/app/agent_workspace` lets an arm build on files another session wrote (F9).
7. **The owner adjudicates the borderline items, and reads the veto with F6a.** Gemma s2 t19 (Wrong or
   Fabricated), and Flash-Next's patch scripts under I4 (F7). Both of Gemma's V1 items were unanswerable
   by design.
8. **In future production studies, do not test `memory`-window recall while consolidation is paused.**
   Either leave those back-refs out, or let the study identity consolidate into a disposable store (F6a).
9. **Before more Gemma work, have slm_server read the GGUF's embedded template date and the llama.cpp
   build's Gemma fixes.** If the template predates 2026-07-09, rerun Gemma s2 once with the fixed template.
   That shows how much of F3 is the template.

## Filed tickets

None.

---

## Caveats

- One replicate per model per script. Local sampling is at temperature 1.0.
- **The sessions span about 11 hours, so web content can differ.** Flash-Next s2 was split around the
  Gemma window, and its t9 ran on a cold cache.
- **The s3 log turns read live production logs,** so each model saw different numbers. Scorers judged
  sourcing and consistency, not agreement between the sheets.
- One s3 reply quotes `unsloth/qwen3.8-flash-next` as a model id from telemetry. That is reply content, and
  the sheet was left as delivered.
- The scorers are Claude models, with at most 10 web checks each. Q is not in the approved rubric.
- The SLM generation check (FRE-1474) added one short request every 5 minutes during the Flash-Next
  sessions only.

## Method appendix

- **Stores.** Production gateway `:9001` (build `29f36a50` / `87dde87f`). ES `:9200` `agent-logs-*` and
  sub-agent captures through the runner. Postgres read-only. Neo4j `--access-mode read`. llama.cpp `:8600`
  (`/v1/models` checked before each window). Capture files in `cloud-sim-seshat-gateway:/app/telemetry/captains_log/`.
- **Archive.** Master moved all 80 study captures. `captures_archive_fre1517/2026-10-09/` received Gemma s2
  and s3 (40) and Flash-Next s2 t1–8 (8). `/2026-10-10/` received Flash-Next s2 t9–20 and s3 (32). The re-read at about 05:55 UTC on
  2026-10-10 found 0 left. Two non-study captures stay in `captures/2026-10-09/`: `9398ec01` (16:27) and
  `1f4c69d0` (17:31), turns of another user, outside every study window.
- **Pause and resume.** The Flash-Next s2 runner was stopped right after the t8 row (19:31:37 UTC), and ES
  showed no t9 trace. It resumed through `resume_state` on the same session at 02:17 UTC.
- **Item file.** The stage-2 item file named s1 turn numbers. The stage-3 scorers got a version that states
  the stage-3 rule (H and R per the script's lists, G on s2 t2 and t12, and s3 t7, t10 and t15).
- **Sessions.** Flash-Next s2 `860d7435-bac9-4efd-a95d-39cf1ab4d8cf`. Gemma s2
  `c70518ad-5a53-4a52-b794-7ad9293143c8`. Gemma s3 `114cd7dd-e293-4f6b-99b5-be6895d98d98`. Flash-Next s3
  `514a039b-d058-49bd-a9b8-b9aa702d31a9`.
