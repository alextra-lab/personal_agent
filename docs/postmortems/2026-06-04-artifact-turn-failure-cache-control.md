# Post-Mortem: Artifact-build turn failure (cache_control 5>4)

- **Date of incident:** 2026-06-04, ~04:33–04:34 UTC (06:33–06:34 local, UTC+2)
- **Author:** Claude (build session) · reviewed by owner
- **Severity:** High — user-visible "Turn failed", full loss of a turn's work
- **Status:** Root-caused; fixes ticketed (see Action Items)
- **Trace ID:** `c216bd40-9d92-4864-bd04-10b6858304da`
- **Session ID:** `1eb8b48a-a656-48a1-befd-fd3a2c963101`
- **Anthropic request ID:** `req_011CbhbZvmGsv2Mq3dB87Gxk`
- **Environment:** `cloud-sim-seshat-gateway` (LiteLLM → api.anthropic.com), `model_role=primary`

> Timestamps in this document are UTC (as stored in Elasticsearch / container logs).
> The reporting screenshot showed local time (UTC+2).

---

## 1. Summary

A user asked Seshat (cloud PWA) to explain its prompt/cache/compression internals **and build an
interactive HTML guide**. The turn ran for ~94s and then failed with a generic "An error occurred
while processing your request." Context usage was only **14% (29K/200K)** and cost was **$0.28** — so
this was **not** a capacity problem.

The turn was killed by a malformed request to the Anthropic API: **5 `cache_control` breakpoints were
attached when the API hard-caps at 4** (`invalid_request_error: "A maximum of 4 blocks with
cache_control may be provided. Found 5."`). The bug is in the cloud prompt-caching marker layer, not
in the byte-layout / frozen append-only cache work.

Three independent weaknesses compounded to produce the failure; the cache-control bug was the fatal one.

---

## 2. Impact

- One user turn lost entirely (no salvaged answer; partial tool results discarded).
- Affects **any multi-round turn on the cloud (Anthropic) path** with `cache_frozen_layout_enabled` —
  not specific to this prompt. Latent since the ADR-0081 §D2 frozen-layout marker shipped (FRE-434).
- Local (llama.cpp :8502) path **unaffected** — it attaches no `cache_control` markers.

---

## 3. Timeline (UTC)

| Time | Event |
|------|-------|
| 04:33:04 | `intent_classified` → **task_type=conversational**, complexity=moderate, confidence=0.7, signals=`['no_special_patterns']` |
| 04:33:05 | `decomposition_assessed` → `conversational_always_single`; gateway_output, single strategy |
| 04:33:05 | `step_llm_call_gateway_model` model_role=primary |
| 04:33:21 | `tool_budget_warning_injected` remaining=2 |
| 04:33:28 | `tool_budget_warning_injected` remaining=1 |
| 04:33–04:34 | Exploration rounds burn budget: `find` (empty), `search_memory` retry (missing `query_text`), `find` exit 123, `entity_match` 0 results, `find` exit 141 (SIGPIPE), `bash` file list, parallel file reads |
| 04:34:38 | `tool_loop_gate` allow → `artifact_draft` invoked with a **10524-char plan** |
| 04:34:38 | `tool_call_failed` — `plan exceeds the 8000-character limit (10524 chars)` |
| 04:34:38 | `tool_budget_warning_injected` remaining=0 → forced no-tools synthesis pass (message_count=20) |
| 04:34:39 | `model_call_error` — **400 BadRequest: cache_control 5>4** (LiteLLM retried 1×) |
| 04:34:39 | `task_failed` → user sees "Turn failed" (request duration ~93.88s) |

---

## 4. Root cause (the fatal bug)

**Anthropic caps `cache_control` breakpoints at 4. The cloud path emitted 5.**

`llm_client/litellm_client.py:_apply_anthropic_cache_control` (`:70-132`) sets three intended
breakpoints: the system message (`:100`), the frozen-layout "history-end" marker (`:115-126`, ADR-0081
§D2 / FRE-434), and the last tool definition (`:129-132`).

The history-end marker is **not idempotent across the in-turn tool loop**:

- `api_messages = list(messages)` (`:262`) is a **shallow copy** — a new list, but the *same message
  dict objects* the executor holds and reuses across rounds.
- `_mark_message_cache_control` (`:41-67`) mutates those dicts in place (`last_block.setdefault("cache_control", …)`).
- Each tool round appends a fresh `<turn_context>` **user** message, so "last user message" advances and
  the history-end marker lands on a **different** message each round. The previous round's marker stays
  stuck on the old dict.

After several rounds: system (1) + last-tool (1) + ≥3 accumulated stale history-end markers = **≥5 > 4**
→ Anthropic 400. The forced-synthesis fallback (triggered because tool budget was exhausted) is the call
that actually tripped it.

### Relationship to the prompt-cache work (important)

This bug is in the **`cache_control` marker layer** (Anthropic-only metadata), **not** in the
**byte-identical header / frozen append-only layout** (ADR-0081 §D2). `cache_control` is a sidecar JSON
field on content blocks; it changes no prompt text and no message ordering. The frozen-layout / KV-prefix
work is independent and is preserved. The fix (clear-then-reapply, clamp ≤4) makes breakpoint placement
*more* faithful to the §D2 intent (exactly one history-end marker at the frozen/volatile seam) and is
neutral-to-positive for cloud reuse. Local cross-turn reuse is untouched (no `cache_control` there).

---

## 5. Contributing factors

1. **Intent misclassification → starved tool budget.** A "build me an interactive guide + explore the
   codebase" request was classified `conversational` (`request_gateway/intent.py`), which caps tool
   iterations at **6** (`config/settings.py:175-184`) vs 25 for `analysis`/`planning`/`tool_use`. This is
   a **recurring failure family**: FRE-256 (tools stripped), FRE-210 (recall missed), now tool-runway. The
   `conversational` bucket is a low-capability default the classifier over-assigns to. **Not** covered by
   FRE-432 (that is model-role/thinking routing, reframed around the pedagogical North Star).

2. **Budget burned on low-value exploration.** Several of the 6 rounds were wasted: a `search_memory`
   validation retry (missing `query_text`), and a `find … | head` that returned **exit 141 (SIGPIPE)**
   surfaced as `success: false` (normal pipe close, not a real failure). Budget pressure ("before the
   budget runs out") likely pushed the model to dump a maximal 10.5K-char plan in one shot.

3. **`artifact_draft` hard-fails terminally.** The 8000-char plan cap (`artifact_tools.py:642,1042-1046`)
   raises with no recovery on the last available round, handing control to the forced-synthesis fallback
   that hit the cache-control 400. Plan is meant to be a *spec* the sub-agent expands, but the model wrote a
   near-complete spec (10524 chars).

---

## 6. What went well

- **Telemetry was sufficient to fully root-cause from logs alone** — intent decision, per-round tool
  budget, the exact tool args, the tool failure, and the underlying LiteLLM/Anthropic error (with request
  id) were all captured. The Four-Level observability paid off here.
- Partial tool results were salvaged and shown to the user (bash/bash success, artifact failure) rather
  than a blank failure.
- Cost gate held; no budget overshoot.

---

## 7. Action items

All tickets in Linear project **Turn Reliability Hardening (2026-06-04 incident)**.

| # | Type | Action | Ticket |
|---|------|--------|--------|
| 1 | Bug (Urgent) | Clamp `cache_control` to ≤4: make `_apply_anthropic_cache_control` clear-then-reapply, idempotent across the in-turn loop; assert ≤4 before send; regression test simulating repeated `<turn_context>` injection | **FRE-468** |
| 2 | Bug (High) | Classifier: route artifact/"build me a"/"make an interactive" intent to `tool_use`/`planning` (extend `_TOOL_INTENT_PATTERNS`, precedent FRE-256); add the recurring-family context | **FRE-469** |
| 3 | Bug (Low) | `bash` tool: treat exit 141 (SIGPIPE from `head`/`grep -q`) as success, not failure | **FRE-470** |
| 4 | Bug (Low) | `artifact_draft`: truncate-with-warning instead of terminal hard-fail; raise plan cap toward `_DRAFT_MAX_TOKENS` (refs FRE-391) | **FRE-471** |
| 5 | Research | "`conversational` capability trap": min tool-runway floor for any turn that starts calling tools; should validation-retries decrement the hard cap? Measure thinking/budget interaction (ties to FRE-432/447) | **FRE-472** |

---

## 8. Lessons

- **A "max" error is not always a "raise the limit" problem.** Three plausible "increase it?" knobs
  (max turns, plan size, context) were all red herrings; the failure was a malformed request and two
  classification/ergonomic bugs. Read the actual upstream error before tuning limits.
- **Shallow copies + in-place mutation across a loop accumulate state.** `list(messages)` does not protect
  the message dicts; any in-place tagging persists and compounds.
- **The `conversational` class is a silent capability downgrade.** Track it as a family, not as isolated
  bugs — every misroute into it strips something (tools, recall, runway).
