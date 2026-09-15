# FRE-1517 — primary model qualification over long sessions: 27B and Flash-Next on MTPLX, Sonnet, workers on and off

**Study:** FRE-1517, commissioned by the owner on 2026-09-14 and ended by the owner on 2026-09-15 at 19:25 UTC.
**Seat:** explore. **Result chosen by the owner:** Flash-Next MTPLX with no workers.

This document records what was measured. Every finding carries its verdict, the query as run, and
its actual output. A negative finding also carries its evidence arms. Recommendations appear only in
**Proposals**.

---

## Scope as run

The commission planned four arms (27B OVH, 27B MTPLX, Flash-Next MTPLX, Flash-Next llama.cpp) on the
isolated eval stack, with three 20-turn scripts and two replicates. The run changed on the owner's
directions:

1. **Eval stack (2026-09-14, 17:23–20:14 UTC):** Phase 1 on arm 1 hit two setup failures, then a
   capture-replay leak and an OOM kill. No study data came from the eval stack.
2. **Production (2026-09-14 20:27 → 2026-09-15 19:21 UTC):** the owner moved the study to the
   production gateway, with `channel=EVAL`, a synthetic study identity, consolidation paused, and a
   read-only graph guard. Scope was cut to script s1_trip, one replicate. Then followed a Sonnet
   ceiling arm, a no-worker A/B (FRE-1520), a planner briefing fix (FRE-1521), and a worker context
   reserve fix (FRE-1522).

---

## Findings

### F1 — Flash-Next MTPLX with no workers delivered every turn, and was the fastest local arm

**Verdict: POSITIVE.**

**Query:** runner rows `scripts/eval/fre1517/out/<run>/*.jsonl` (the `outcome.delivered` and
`http_wall_s` fields), written per turn by `prod_session_runner.py` from the `/chat` response. The
`model_call_error` counts come from production ES `agent-logs-*` by `trace_id`. s1_trip, turns 1–8.

**Output:**

| Run | Session | Delivered, t1–8 | Wall, t1–8 |
|---|---|---|---:|
| prod-arm2b (27B MTPLX, workers) | `3ed2c8c0` | 5/8 | 8,093 s |
| prod-arm3a (Flash-Next MTPLX, workers) | `ef9473fe` | 6/8 | 6,576 s |
| **prod-flash-po (Flash-Next MTPLX, no workers)** | `05495846` | **8/8** | **1,712 s** |
| prod-flash-brief (Flash-Next, workers + briefing) | `26deb270` | 5/8 | 6,325 s |
| prod-flash-reserve (Flash-Next, workers + briefing + reserve; stopped after t5) | `5592b9c8` | 3/5 | 3,960 s (t1–5) |
| prod-sonnet-a (Sonnet, workers) | `3a0954b9` | 8/8 | 528 s |
| prod-sonnet-po (Sonnet, no workers) | `a0bfd47f` | 8/8 | 394 s |

### F2 — On the local engines, the fan-out turns fail on worker limits, not on reasoning

**Verdict: POSITIVE.**

**Query:** sub-agent captures in production ES `agent-captains-captures-subagents-*` by `trace_id`
(fields `report_kind`, `stop_reason`, `finish_reason`, `tool_iterations`, `error`,
`tool_result_chars_absorbed`), copied into the rows.

**Output (fan-out turns 2, 5 and 8):**

* **27B MTPLX workers:** four workers ended `synthesized/cap/20`, and one ended `narration/completed/6`.
* **Flash-Next workers:**
  * t2: `ledger/error/9`, with the error text "ContextWindowExceededError".
  * t8: `narration/completed/19`.
  * The other workers completed after 4–17 rounds.
* **Flash-Next with the briefing planner:**
  * t2 and t5: three workers ended `ledger/error`. Their errors read "…maximum context length is 131072 tokens, but the prompt alone has 141015 / 137567 / 134107 tokens". The turn-5 failures absorbed 419,076 and 388,635 chars.
  * t8: two workers ended `narration` with `finish_reason length`.
* **Flash-Next with the context reserve:**
  * t2: `synthesized/context_reserve`, after `sub_agent_landing_reserved` `reason=context` `estimated_prompt_tokens 119536` `context_length 131072`.
  * t5: one worker ended `narration/completed`, `finish_reason length`, after 18 rounds and 294,434 chars.
* **Sonnet workers:** every worker ended `synthesized/completed`, in 0–3 rounds.

### F3 — The expansion planner received only the current message

**Verdict: POSITIVE** (a property of the deployed code, read in the running container).

**Query:** `docker exec cloud-sim-seshat-gateway sed -n 535,542p /app/src/personal_agent/orchestrator/expansion_controller.py`
at image `6d2de181`. Also line 306 of the same file.

**Output:**

```
planner_messages = [
    {"role": "system", "content": planner_system_prompt},
    {"role": "user", "content": (f"Strategy: {strategy}\nQuery: {query}\n\nProduce the JSON plan.")},
```

Line 306 reads: `constraints: dict[str, Any] | None = None,  # TODO: wire into planner prompt`.

### F4 — Before the fix, no worker instruction carried the session's shellfish or euro rule

**Verdict: NEGATIVE.**

**Query:** production ES `agent-logs-*`, `event_type=sub_agent_start`, field `task`, for the turn
2, 5 and 8 traces of prod-arm3a, prod-arm2b and prod-sonnet-a. Each task is keyword-scored for a
shellfish mention (tasks about dining) and a euro mention (tasks about prices), then read.

**Output:** 0 of 8 lunch or price tasks mention shellfish or euros, on all three models.
Rescored per run with the Phase B script: prod-arm3a gives shellfish 0/4 and euros 0/6, with Sóller 4/4.

**Arms:**

* **Arm 1a, a raw instance in the same store and field:** prod-flash-brief turn 5, trace `6cb5082b`,
  `sub_agent_start.task` contains "…confirming the kitchen does not routinely serve shellfish… State a
  shellfish-free verdict… Constraints: All prices in euros". So the identifier and the field hold such
  text when a planner writes it.
* **Arm 2, path liveness:** the same query shape on prod-flash-brief returns non-zero carry-through
  (19 of 20).
* **Arm 3, scope:** s1_trip turns 2, 5 and 8; runs prod-arm2b, prod-arm3a and prod-sonnet-a;
  production ES; 2026-09-14/15.

### F5 — A worker received the last 4 conversation messages, which excluded rules set early

**Verdict: POSITIVE.**

**Query:** production ES `agent-logs-*` `sub_agent_start` and `agent-captains-captures-subagents-*`
for Sonnet turn 8, trace `8472de7c`, field `context_messages`.

**Output:** `context_message_count 4`, `context_chars 3361`. The roles are assistant, user, assistant, user:

1. the turn 6 reply, "None of the confirmed-date events fall on **Tuesday 13 October 2026**…";
2. turn 7, "Is the mountain road through the Serra de Tramuntana safe…";
3. the turn 7 reply;
4. turn 8, "Plan a day for Wednesday 14 October…".

The rules were set in turn 1.

### F6 — Giving the planner the conversation raised rule carry-through, measured on the plan alone

**Verdict: POSITIVE.**

**Query:** the Phase A probe `scripts/eval/fre1517/planner_probe/planner_probe_c7.py`: 27 planner
calls direct to Flash-Next on `:8600`, with the deployed prompt, turns 2, 5 and 8 of session
`ef9473fe`, and 3 draws. Scored by `score.py`. The raw rows are in `probe_rows.json.txt`.

**Output:**

| Variant | Carry-through | Planner wall per plan |
|---|---|---|
| V0, the deployed prompt | 14/53 | 13–31 s |
| V1, + conversation | 36/39 | 19–62 s |
| V2, + conversation + briefing rules | 40/41 | 29–82 s |

V0 turn 8 draw 1 wrote "Wednesday 14 October **2025**" and "Puig de sa Morisca in Deià".

### F7 — With the briefing planner in production, rules reached the workers, but delivery did not improve

**Verdict: POSITIVE.**

**Query:** prod-flash-brief rows: `planner_completed` (`brief_mode`, `history_chars`,
`task_constraints_count`) and `sub_agent_start.task`, scored by `phase_b_report.py`.

**Output:**

* `brief_mode briefing` on turns 2, 5 and 8, with `history_chars` 2,124 / 23,341 / 31,707.
* Carry-through: 19/20, against 4/14 for prod-arm3a.
* Delivered: 5/8, against 6/8 for prod-arm3a.

### F8 — Final-answer compliance with the standing rules was already high without the fix

**Verdict: POSITIVE** (scored by reading the replies; not blind).

**Query:** the stored assistant replies (`reply` in the rows) for turns 2, 5 and 8 of prod-arm3a,
prod-flash-po and prod-flash-brief. Each is read for a shellfish status on lunch suggestions, euro
prices, and Sóller as drive origin.

**Output:** all applicable checks hold in all three runs. Examples:

* prod-arm3a turn 8: "**Shellfish: not certified**".
* prod-flash-po turn 8: "**Shellfish status: mixed menu.**".
* prod-flash-brief turn 8: "**Shellfish verdict: not safe**".

In prod-flash-reserve turn 5, the recommended lunch "QuitaPenas… €20–30" carries no shellfish status of its own.

### F9 — The FRE-1522 context reserve removed worker context overflow in the one run measured

**Verdict: NEGATIVE** (zero overflow errors).

**Query:** prod-flash-reserve rows, turns 1–5, `trace_reads.errors_by_role` and each capture's
`error`, for the string "ContextWindowExceeded". Production ES `agent-logs-*` `model_call_error`
and `agent-captains-captures-subagents-*`.

**Output:** `errors_by_role {}` on all 5 turns. No capture error contains ContextWindowExceeded.

**Arms:**

* **Arm 1a, a raw instance in the same store and field:** prod-flash-brief turn 2, trace `7e3e5517`,
  capture error: "Local model call failed: litellm.ContextWindowExceededError: … the prompt alone has
  141015 tokens…".
* **Arm 2, path liveness:** the identical read on prod-flash-brief returns 3 such errors (turns 2 and 5).
* **Arm 3, scope:** prod-flash-reserve only, s1_trip turns 1–5 (the run was stopped after turn 5),
  2026-09-15 18:11–19:21 UTC.

### F10 — The tool-budget warning is inserted as a user-role message in the middle of a turn

**Verdict: POSITIVE.**

**Query:** production ES `agent-logs-*`, trace `301f67f78b3b78235d6008a6451d39ab` (prod-flash-po turn 1),
event order. Deployed code `executor.py:6176-6183` (`docker exec cloud-sim-seshat-gateway grep`).

**Output:**

* 06:20:22.980: `tool_budget_warning_injected` `remaining 2`.
* The next primary call ran from 06:20:23.010 to 06:23:27.363 with `input_tokens` 53,628; the call before had 19,636.
* The code runs `ctx.messages.append({"role": "user", "content": budget_message})`.

That this causes a full re-prefill is slm_server's reading of the MTPLX log. This study did not verify it.

### F11 — Exact-prefix reuse works on MTPLX, and cold prefill slows as the prompt grows

**Verdict: POSITIVE.**

**Query:** `ttft_probe.py --arm mtplx_27b`, 6 streamed requests direct to `:8600`, cold then warm, at
8k, 32k and 64k.

**Output (MTPLX 27B):**

| Prompt tokens | Cold first token | Warm first token | Warm cached_tokens |
|---:|---:|---:|---:|
| 7,105 | 33.1 s | 0.074 s | 7,105 |
| 28,317 | 155.0 s | 0.084 s | 28,317 |
| 56,594 | 364.7 s | 0.107 s | 56,594 |

A prompt that extends the previous one was not measured.

### F12 — The eval stack could not isolate sessions after a restart

**Verdict: POSITIVE.**

**Query:**

1. `isolation_check.py` at 2026-09-14 19:21:56 UTC, reading `originating_session_id` on every node in `neo4j-eval`.
2. Eval ES `agent-logs-*` by `session_id`, from 18:40Z.
3. `docker inspect cloud-sim-neo4j-eval`.

**Output:**

* 97 nodes: 52 from the current session, plus 42 from `d0acad2b`, 2 from `b99f8e92` and 1 from `506b63c3`, all three created before the 18:47 wipe.
* `d0acad2b` logged `entity_created` ×60 from 18:48:29 to 19:24:31.
* neo4j-eval: `exit=137 oom=true finished=19:25:18Z`.

The mechanism, read by master in the code: consolidation replays capture files whose `:Turn` node is missing.

### F13 — Production graph counts did not change during the production runs

**Verdict: POSITIVE** (the counts are stated).

**Query:** `cypher-shell --access-mode read` in `cloud-sim-neo4j`:
`CALL { MATCH (n) RETURN count(n) } CALL { MATCH (t:Turn) … } CALL { MATCH (e:Entity) … }`, before and
after each run.

**Output:**

* 13223 / 2687 / 9483 after every run, from prod-arm2b to prod-flash-reserve.
* The single +1 (13222 → 13223) after prod-arm2 is the study identity's `:Person` node, with keys `name, source, email, created_at, user_id`.

---

## Proposals

At most ten. Each is for master's disposition; none was executed by this seat.

1. **Keep workers off by default for Flash-Next MTPLX.** The owner has already decided this; the
   evidence is F1 and F2.
2. **Count a reserve-stopped worker that lands a synthesized report as delivered, not incomplete.**
   Otherwise FRE-1522's reserve still produces the failure trailer (F2, prod-flash-reserve turn 2).
3. **Stop a landing from ending in narration cut at length below the reserve,** for example by a
   trim triggered on absorbed chars, not only on the context estimate (F2, prod-flash-reserve turn 5).
4. **Give the thoroughness levels different round budgets.** All three render as 20 rounds today.
   Open-ended tasks ran 16–20 rounds locally (F2).
5. **Add a planner rule against naming places the conversation does not contain.** Every planner
   variant still seeded unverifiable places (F6, F7).
6. **Deliver the tool-budget warning without a user-role message mid-turn,** or measure its prefill
   cost first (F10).
7. **Before any further eval-stack study, archive or clear capture files at session start.** The
   graph wipe alone does not isolate sessions (F12).
8. **Log the text of a worker's context messages, not only previews,** so that a rule's presence
   in worker input can be measured directly (F5).
9. **Re-run the comparison with the owner's blind rubric and a second replicate** before treating
   the Sonnet and Flash-Next gaps as settled (the caveats below).

## Filed tickets

* **FRE-1521** — The expansion planner sees only the current message, so worker instructions drop the
  session's standing rules and earlier context (Backlog, filed by this seat on 2026-09-15).

FRE-1518, FRE-1520 and FRE-1522 were filed by master, not by this seat.

---

## Caveats

* **One replicate per arm**, at temperature 1.0 on the local engines.
* **Host state differed between runs:** lid open or closed, start temperatures from 40 C to 64.6 C,
  swap from 134 MB to 1,302 MB, MTPLX restarted before some runs only. The arm order is confounded with time.
* **Answer compliance was scored by this seat, not blind.** The owner's rubric was not applied.
  Scripts s2 and s3 never ran.
* **Wall time includes the runner's pre-turn waits.** Cost figures are row sums of `api_costs`, and
  the runner's session totals are higher.
* **Routing was the pre-ADR-0152 matrix,** which decided which turns fanned out.

## Method appendix

* **Stores and windows.** Production gateway `:9001`; production ES `:9200` (`agent-logs-*`,
  `agent-captains-captures-subagents-*`); production Postgres in read-only transactions (`sessions`,
  `route_traces`, `api_costs`); production Neo4j via `cypher-shell --access-mode read`. Eval stack
  `:9003` / ES `:9202` / neo4j-eval for the eval phase only. Windows: 2026-09-14 17:37 UTC to
  2026-09-15 19:21 UTC.
* **Identity resolutions.**
  * Production ES term filters use plain fields; `.keyword` aggregations returned empty on production.
  * The planner's model call logs `role=primary`.
  * Local telemetry records the served model id; cloud telemetry records the litellm id (`anthropic/claude-sonnet-5`, `ovhcloud/Qwen3.8-27B`).
* **Rejected instruments, with reasons.**
  * The first `isolation_check.py` compared only against sessions of the named run, and missed foreign nodes (F12).
  * A paragraph-level answer-compliance regex flagged implicit origins and concert listings; compliance was read manually instead.
  * An ES `.keyword` aggregation on production returned zero for fields that exist.
* **Runners and scripts** (all committed under `scripts/eval/fre1517/`):
  * `session_runner.py` (eval stack) and `prod_session_runner.py` (production);
  * `isolation_check.py`, `ttft_probe.py`;
  * `planner_probe/` (probe, scoring, deployed prompt export, raw rows, Phase B report);
  * the scripts `scripts/*.yaml`, `rubric.md`, `arms.yaml`.
* **Sessions.**
  * Production: `b9efcfaf` (setup), `3ed2c8c0`, `ef9473fe`, `3a0954b9`, `a0bfd47f`, `05495846`, `26deb270`, `5592b9c8`.
  * Eval: `d0acad2b`, `b82ef6ff`, `a47463a8`, `6c7826cd`.
