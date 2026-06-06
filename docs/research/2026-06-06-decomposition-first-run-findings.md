# Decomposition First Live Run — Findings (artifact build, trace `87cbd720`)

> **Date:** 2026-06-06
> **Context:** First real exercise of `artifact_decomposition_enabled=true` (ADR-0086, FRE-480/481) on a complex artifact build.
> **Prompt:** *"Build an interactive, dynamic artifact to teach me about how an organism that could see all the wavelengths of the spectrum of light would see and experience earth, and the universe."* (a subject the owner has discussed several times before)
> **Trace:** `87cbd720-7b98-4136-9235-ec14f28b6c43` (cloud, owner identity)

## TL;DR (corrected 2026-06-06 after deeper forensics)
**Decomposition-with-subs does NOT work yet.** The artifact was brilliant and cheaper — but that came from **Sonnet + the artifact-draft pipeline, not from working decomposition.** The mechanism degraded: the **planner failed** schema-validation → generic fallback tasks; the 2 subs ran **tool-less** (no discovery, no KG retrieval); proactive memory found **10 KG candidates but injected only 2** (~330 tok, 500-tok budget cap); the cost meter **under-counted** ($0.57 vs $0.90), the UI was **blind** during expansion, and the security gate **didn't fire** (33 KB JS shipped). The run was a **successful probe** — one pass exposed a **6-issue cluster**. **Flag stays ON; we iterate to working, visibility-first.** (Earlier framing — "quality+cost win, gaps are plumbing not mechanism" — was wrong; the core mechanism is the broken part.)

## What went right
- **Output quality:** owner-rated "next-level visual quality, big wow factor." A 6-module interactive HTML artifact ("Omnispectral Vision"). Built and committed.
- **Cost efficiency:** **$0.90 / 9 model calls**, vs the prior 20+-round single-agent artifact builds (~$1.1+ and a long accreting context). The fan-out kept the parent context small — the structural cost win decomposition was meant to deliver.
- **Fan-out is bounded:** `expansion_budget_max=3`, `sub_agent_max_tool_iterations=5`, `worker_timeout_seconds=60`, `worker_global_timeout_seconds=180`. 2 discovery + 2 artifact-draft sub-agents dispatched — within caps. No runaway pool.

## Gaps found (priority order)

### 1. Memory grounding is shallow on build/teach requests (the substantive one)
- The request asked to *teach about a previously-discussed subject*, where the knowledge graph holds prior threads.
- **Proactive memory DID ground it** (`memory/service.py::suggest_proactive_raw`, ADR-0039): a Neo4j **vector** query (`db.index.vector.queryNodes('entity_embedding')`) over KG entities + the most-recent **cross-session** turn discussing each. Always-on, no task gate. This is the likely source of the references in the artifact — so the output *is* grounded in prior discussions, **shallowly** (top-k, budget-trimmed).
- **The deep/targeted recall did NOT fire.** `request_gateway/recall_controller.py:172` gates recall to `task_type == CONVERSATIONAL`. The artifact build classified as `TOOL_USE`, so the controller was skipped. Different mechanism (cue-triggered, in-session, noun-phrase fact scan / reclassify-to-MEMORY_RECALL).
- **Net:** build/teach requests get the *shallow proactive* slice but not the *deep targeted* recall. For "teach me everything we've explored on X," that's a thin grounding. **This is the concrete, located instance of the recall-quality gap** (cf. ADR-0087 / memory-recall program): the requests that most need deep prior-thread grounding are `TOOL_USE`, which the deep path excludes.
- **Honest correction:** an earlier read called this "Sonnet used internal data only / references unverified." That was too pessimistic — proactive memory's cross-session KG retrieval ran and very likely surfaced relevant prior entities/turns. The accurate statement is "grounded, but shallowly; deep targeted recall gated out."

### 2. Cost/token meter under-counts decomposed turns
- The live meter showed **$0.57 / tools 2/25** — the **primary-only** cost. True cost was **$0.9028** across **9 calls** (primary Sonnet $0.573 + sub_agent Sonnet $0.138 + sub_agent Haiku $0.190 + router Haiku $0.003).
- **Sub-agents produced 45,718 of 71,311 output tokens (64%)** and ~$0.33 of the $0.90 — none of it reflected in the meter.
- Backend ledger is **correct**: `api_cost_recorded` booked all 9 = $0.9028 (the `reservation expired` warning did not corrupt the tally). The gap is purely the **user-facing roll-up**: turn cost/tokens are emitted from the executor/primary path and don't aggregate sub-agents.

### 3. Live status is blind for decomposed turns
- Only `chat_stream.launched` emitted; **no `turn_status`/token-delta events** during the ~13-min discovery+build. The meter appeared only at the end.
- Root cause: `turn_status`/STATE_DELTA is emitted from `orchestrator/executor.py` (single-agent loop) → `transport/agui`; the **expansion controller + sub-agent path emit none of it**. So decomposed turns run nearly invisible to the PWA.

### 4. Planner failed → decomposition degraded
- `planner_failed: schema_validation_failed` at turn start. The decomposition planner's output failed schema validation.
- Consequence: the discovery sub-agents invoked **zero discovery tools** (whole-turn tool list was just `artifact_draft` + `artifact_write`). They generated from context + injected proactive memory, not from tool-based research. No `web_search` ran (so any web-style citations are not live-sourced).

## Recommended follow-ups (no code in this doc)
1. **Recall on build/teach (highest value):** let deep/targeted recall (or a strengthened proactive pull) cover knowledge-building `TOOL_USE` requests, not just `CONVERSATIONAL`. Ties to the memory-recall program.
2. **Cost/token roll-up:** aggregate sub-agent usage into the turn meter (`$0.57 → $0.90`).
3. **Live status through the sub-agent path:** emit `turn_status` from the expansion controller / sub-agent loop so decomposed turns aren't blind.
4. **Planner reliability:** fix `schema_validation_failed` so decomposition runs as designed (real tool-using discovery) rather than degrading to generate-from-knowledge.

## Verdict (corrected)
Decomposition's **core mechanism is the broken part** — the planner failed, so the subs never did real tool-using discovery; the wins were the pipeline's, not decomposition's. But this is exactly what a first run of a major harness change should produce: a probe that surfaces the breakage. **Keep the flag ON and iterate, visibility-first** (the harness flywheel — fast iteration needs to *see* what the model and gates do).

**Fix queue (Linear, all Approved, each confirm-via-telemetry first; each carries a visibility deliverable):**
- **Wave 0 — SEE:** FRE-501 (live cost+status meter) · FRE-505 (sub-agent input/output/digest auditability) · FRE-506 (sandbox gate-decision telemetry + bypass confirm).
- **Wave 1 — make it real:** FRE-502 (planner reliability + discovery-aware fallback).
- **Wave 2 — ground it:** FRE-503 (proactive depth for build/teach — raise the 500-tok budget) + FRE-435.
- **Parallel:** FRE-500 (flag-gate sandbox off; temporary bridge). **adr:** FRE-504 (7 architecture threads → ADRs).

Plan: `docs/superpowers/plans/write-this-all-up-dynamic-graham.md`.
