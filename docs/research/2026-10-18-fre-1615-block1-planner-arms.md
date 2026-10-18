# FRE-1615 Block 1 — does the planner pay for itself on local Flash-Next?

> **Ticket:** [FRE-1615](https://linear.app/frenchforest/issue/FRE-1615) · **Plan:** `docs/superpowers/plans/2026-10-18-fre-1615-harness-evaluation-spike.md` · **Harness:** `scripts/eval/fre1615/` · **Date:** 2026-10-18 · **Status:** one draw; the owner's blind scoring is pending.

## Question

The owner's turn "Is there a version with coriander?" took 290 s, because the planner sent a follow-up question to two helpers. An external evaluation suggested that the planner costs more than it gives on this local model. Block 1 measures that claim on 24 tasks with three arms:

| Arm | Configuration |
|---|---|
| **S** | forced SINGLE (`AGENT_EXPANSION_ENABLED=false`) |
| **PL** | the production planner (ADR-0154 `planner_asked`, the FRE-1498 decline rule first) |
| **P2** | PL plus the owner's pasted "prefer SINGLE" policy, verbatim, before the planner's Rules (`scripts/eval/fre1615/arms/P2.patch`) |

P1 (a deterministic pre-gate for conversational, simple and follow-up turns) did not run. Stage 4 classes all 24 suite tasks `simple`, so P1 sends every task to SINGLE, which is the S configuration.

## Method

- Eval stack only (FRE-375): two eval gateways, the image built from `main` (`fff45eb9`), the P2 image from a patched scratch worktree that differs from `main` in one file. Production values for every behaviour setting except the documented ones (`scripts/eval/fre1615/docker-compose.fre1615.yml`). `fre1590.envdiff` found only expected differences.
- One full turn per (task, arm), in a new session, after a `neo4j-eval` wipe. Memory tasks get three seeded facts through the gateway's own memory path (`memory_seed.py`). A follow-up task runs its first turn live on the same arm. Arm order alternates by task.
- Per turn: ES events (planner decision, tool calls and arguments, pauses, failures, cost), llama.cpp per-call counts from Tempo `slm-server` spans (`slm.prompt_n`, prefill, decode, output tokens), and the FRE-1614 prompt captures. On every row, model calls = Tempo spans = captures.
- Quality: two blind LLM judges (FRE-1598 method: Sonnet and gpt-5.4-mini, Haiku on PASS/FAIL splits) against an expect line per task (`expects.yaml`, written before the measured run). PASS 2, PARTIAL 1, FAIL 0, so 0–4 points per reply. The owner blind-scores the 24 S/PL pairs.
- Phase A (S, PL, 24 tasks): 13:36Z–15:47Z. Phase B (P2 on the 6 tasks PL expanded): 15:50Z–16:27Z. One harness error row (a transient Tempo read timeout after the turn) was rerun.

## Results — speed and path

| | S | PL | P2 (6 tasks) |
|---|---|---|---|
| Wall, 24 tasks | 2,303 s | 3,652 s (+59%) | — |
| Wall p50 / p95, 24 tasks | 81 s / 238 s | 75 s / 566 s | — |
| Model calls, 24 tasks | 100 | 183 | — |
| Tokens prefilled (`slm.prompt_n`), 24 tasks | 260,323 | 453,239 | — |
| Planner, 24 tasks | — | 21 asked: 15 declined, 6 expanded | — |
| **The 6 tasks PL expanded: wall** | **1,127 s** | **2,554 s** | **1,892 s** |
| The 6 tasks: model calls | 38 | 103 | 76 |
| The 6 tasks: tokens prefilled | 162,759 | 360,114 | 218,838 |
| The 6 tasks: prefill / decode | 386 s / 563 s | 912 s / 1,362 s | 535 s / 1,043 s |
| The 6 tasks: expanded | — | 6 | 3 (the three research tasks) |

On the 18 tasks that PL did not expand, the arms take about the same time (S 1,176 s, PL 1,098 s). The planner's cost sits in the turns it expands: there, PL takes 2.3 times as long as S, and spends 2.2 times the prefilled tokens. P2 removes about a third of that extra time. It declined half of PL's expansions and kept the research expansions.

The owner's follow-up case: in the preflight, PL expanded the chimichurri follow-up (339 s) and P2 declined it (55 s). In the measured draw, PL declined all three follow-ups. One draw cannot give the planner's expansion rate on follow-ups.

## Results — quality (judges)

| Tasks | S | PL | P2 |
|---|---|---|---|
| 21 tasks with valid labels (rs1–rs3 excluded, see below) | 79 / 84 | 74 / 84 | — |
| The same, without the 3 memory tasks (identical configuration in S and PL) | 67 / 72 | 67 / 72 | — |
| The 6 tasks PL expanded (3 of them research, labels invalid) | 9 / 24 | 15 / 24 | 15 / 24 |

- No quality gain from the planner is visible in this draw. The judges' totals for S and PL are equal on the tasks where the configurations differ (67 and 67).
- Inside that total: on the 3 judge-valid tasks that PL expanded, PL scores 12 and S scores 8. The whole gap is one task, where the S turn failed and a SINGLE turn of P2 passed. On the other 15 tasks, S leads by 4 points.
- **The memory tasks show the noise.** Stage 5 routes memory recall to SINGLE in both arms (`memory_recall_always_single`), so S and PL run the same configuration there. They still differ by 5 points on 3 tasks: on one PL turn, recall failed (see the eval limits). One draw cannot separate a difference of a few points from noise.
- **The research labels are invalid.** The judges label replies about Python 3.15.0 (released 2026-10) and MySQL 26.x as "invented future releases", because their training data ends before the run date. The expect lines say "as of the run date", but the judge prompt does not give that date. rs1–rs3 need the owner's scoring or a date-aware judge.
- Per-task labels stay out of this note and the ticket until the owner finishes the blind scoring, so that they cannot unblind it. The judges' report is in the git-ignored run directory (`block1/judging/report.json`).

## Findings beyond the question

1. **Stage 4 complexity carries no information on this suite:** 24 of 24 tasks are `simple`, including multi-source research. Any gate keyed on complexity (P1, the old matrix) acts on all tasks or none.
2. **The planner call is logged with role `primary`** (in ES `model_call_completed` and in the FRE-1614 capture file names). A per-role cost report counts the planner as the primary.
3. **The artifact builder costs about $0.17 a call** on these tasks, not the $0.06 of the plan.
4. **Eval-stack gaps found** (equal in every arm, not fixed here):
   - The Voyage reranker key is not passed to eval, and the fallback reranker on `:8600/v1/rerank` answers 503. Memory recall runs without a reranker on eval.
   - The eval Postgres schema is stale: `route_traces.planner_decision` and `first_token_ms` are missing, so route-trace writes fail (telemetry only).
   - Eval bash runs as `nobody` with an allow-list. The S arm's cf3 turn failed to count lines (no glob in `wc -l`), while a SINGLE turn of P2 on the same task passed.
   - A scratch worktree lacks the git-ignored `config/governance/budget.yaml`, and a gateway built from it does not start.

## Recommendation (AC-4), pending the owner's blind veto

| Change | Recommendation | Why |
|---|---|---|
| **P2**: the "prefer SINGLE" planner policy | **Adopt** (through an ADR amendment and a ticket, AC-5) | On the 6 tasks PL expanded: wall −26%, model calls −26%, prefilled tokens −39%, judges' points equal (15 and 15). It declined the owner's chimichurri follow-up in the preflight. It is a prompt change that the FRE-1537 D7 probe can qualify. |
| **S**: no planner (expansion off) | **Needs more data** | Fastest arm, and no quality loss is visible on the 21 judge-valid tasks. But one draw, and the judges cannot grade the research tasks, where the planner expands most. A second draw on the expanded and follow-up tasks, with the owner's scores or a date-aware judge, decides it. |
| **P1**: deterministic pre-gate | **Reject as designed** | With Stage 4 calling everything `simple`, P1 equals S. A pre-gate needs a signal that tells tasks apart. |

## Limits

One draw per arm. The eval gateway has 768 MB and 1 CPU (production 2 GB). Approval asks are denied on eval, an artifact save fails at the disabled R2 store, `run_python` has no Docker socket, and eval bash cannot run skill scripts that need a credential. The judges have a knowledge cutoff before the run date. One reply (rs1, PL) says "the workers could not confirm…", which names its arm.

## Files

- Rows: `telemetry/evaluation/fre1615-harness-spike/block1-a/rows.jsonl` (S, PL), `block1-b/rows.jsonl` (P2), preflights `preflight-a`, `preflight-b` (git-ignored).
- Judging: `telemetry/evaluation/fre1615-harness-spike/block1/judging/` (items, key, labels, report). Owner pairs: `block1/owner_blind.md`, key `block1/owner_key.json`.
