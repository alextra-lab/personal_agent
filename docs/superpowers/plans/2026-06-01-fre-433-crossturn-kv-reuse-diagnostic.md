# FRE-433 — Cross-turn KV reuse ≈0 post-ADR-0081-D4: root-cause diagnostic

**Status**: findings (diagnostic spike — no product fix in this ticket)
**Date**: 2026-06-01
**Ticket**: [FRE-433](https://linear.app/frenchforest/issue/FRE-433)
**Relates**: [FRE-431](https://linear.app/frenchforest/issue/FRE-431) (ADR-0081 D4, PR #125) · [FRE-422](https://linear.app/frenchforest/issue/FRE-422) (D1) · [FRE-405](https://linear.app/frenchforest/issue/FRE-405)/406/407 (instruments)
**ADR**: `docs/architecture_decisions/ADR-0081-cache-aware-context-layout-and-compaction.md`

## TL;DR

- **D4 met its prefix-stability AC but the cross-turn-reuse AC failure is NOT a Seshat
  prompt/layout defect.** The orchestrator system prefix is byte-stable cross-turn
  (`prompt_static_prefix_hash = 5cae73c3636cec4d`, **1 distinct across all 41 SLM calls**
  today), yet the local SLM (qwen3.6-35-A3B via `slm.frenchforet.com`) reprefills from
  zero on every new turn's first call.
- **Leading root cause: the `:8502` primary loads a vision projector (`mmproj`), and
  llama.cpp disables cross-request KV reuse whenever `mmproj` is loaded — even for text-only
  turns** (`has_mtmd` treats every slot as image-bearing; documented limitation, issues
  #21133/#19466/#17200). This is a backend/ops issue, not the gateway. The gateway already does
  everything right: full message array on the stateless `/v1/chat/completions` path with
  `cache_prompt: true` (`adapters.py:655`), which is why **within-turn continuations still
  reuse ~10–12k tokens** — so reuse isn't 100% dead, hence the **decisive test: run the primary
  without `mmproj` and re-check cross-turn `cache_reuse`.**
- **Hypothesis 1 (history/summary reconstruction → ADR-0081 D2/D3) is REFUTED.** A literal
  `cache_reuse = 0` cannot come from prefix divergence at the volatile tail — that would
  still reuse the byte-identical leading system-prefix span. The cloud A/B confirms it: the
  **same gateway/prompt routed to Anthropic reuses the identical prefix cross-turn**
  (content-addressed cache), while the local backend does not.
- **Hypothesis 2 (eviction by another SLM request) is REFUTED for the observed traffic.**
  In the clean single-session window, the *only* role hitting the SLM is `primary`; nothing
  interleaves between turns, and the session ran alone (no concurrency). The miss happens
  even with no evictor.
- **Resolution lives in SLM-server config/ops (separate repo / Mac host), not Seshat code and
  not an ADR-0081 D2/D3 design.** Likely fix: serve `orchestrator.primary` from a **text-only**
  instance (drop `mmproj` from `:8502`) and route the rare actual vision requests to a separate
  multimodal endpoint. Secondary levers if reuse is still imperfect after that: `--cache-reuse`
  (unset ⇒ 0 = OFF) and `spec_type: draft-mtp` (MTP speculative decoding) — fold into the same
  no-mmproj test.

## Evidence

All data from `agent-logs-2026.06.01` (this ES), `model_call_completed` events whose
`endpoint = https://slm.frenchforet.com/v1/chat/completions`.

### 1. Slot trace — session `ffae77ac` (post-D4), grouped by turn (`trace_id`)

`cache_rd` is `cache_read_tokens`; `None` ⇒ 0 (no field emitted ⇒ full prefill). Every call
carries the identical `prompt_static_prefix_hash = 5cae73c3636cec4d`.

```
time          trace     in_tok cache_rd  lat_ms   note
14:00:16.861  622e5ac8    9867      0     12374    turn-first  → FULL PREFILL
14:00:18.669  622e5ac8   10156   9842      1531    continuation → reuse
14:00:39.367  3b1eb0f8    9752      0     11919    turn-first  → FULL PREFILL
14:00:43.705  3b1eb0f8   11243   9728      4008    continuation → reuse
14:00:57.869  3b1eb0f8   13455  11239     13957    continuation → reuse
14:01:37.892  3d9e1e2c    9929      0     10863    turn-first  → FULL PREFILL
...
14:02:29.091  444229fe   10733      0     17952    turn-first  → FULL PREFILL (single call)
14:03:09.682  8282c2cf   12605      0     25928    turn-first  → FULL PREFILL (single call)
14:05:05.919  b13b4b62   10331      0     34619    turn-first  → FULL PREFILL
14:05:50.766  e4c87aad   11762      0     29716    turn-first  → FULL PREFILL
14:05:55.637  e4c87aad   11948  11732      4218    continuation → reuse (5s gap)
14:06:33.576  e4c87aad   16868  11732     37651    continuation → reuse (38s gap)
14:07:54.462  2f5aca32   10315      0     21370    turn-first  → FULL PREFILL
```

**Reads:**
- Every **turn-first** call → `cache_read_tokens = 0`, 10–35s prefill, despite a stable
  system prefix.
- Every **within-turn continuation** → reuses ~10–12k, 1.5–4s. A **38-second** within-turn
  gap (`e4c87aad` 14:05:55 → 14:06:33) still reused, while a **21-second** cross-turn gap
  (`622e5ac8` 14:00:18 → `3b1eb0f8` 14:00:39) did not. **⇒ not an idle/TTL effect.**

### 2. No evictor and no concurrency (refutes Hyp 2 for this traffic)

Aggregation of *all* `model_call_completed` to the SLM endpoint today:
`role = primary` only, `model = unsloth/qwen3.6-35-A3B` only (41 calls). No `sub_agent`,
no router, no entity-extraction, no reflection touched the SLM.

Auxiliary per-turn LLM calls **do** exist but run on **cloud** backends and cannot touch the
SLM slot: `endpoint=anthropic` (intent/router-class, ~888-token prompts) and `endpoint=openai`
(`gpt-5.4-mini` entity/compressor). Confirmed by `config/models.cloud.yaml`:
`entity_extraction_role: gpt-5.4-mini`, `captains_log_role / insights_role: claude_sonnet`,
`compressor: gpt-5.4-mini` — all cloud. Only `primary` and `sub_agent` point at
`slm.frenchforet.com`.

Session timing: `ffae77ac` ran alone 14:00–14:08; the only other SLM session today
(`5af07bc0`) ended at 06:03 — **8 hours earlier**. So the cross-turn misses are **not**
inter-request slot eviction in this window.

### 3. Cloud A/B control (refutes Hyp 1)

The user's cloud-profile run (Anthropic primary) reuses the **identical** prefix cross-turn
(`cache_read ≈ 13,916`, constant across 6/7 turns) on the same gateway/prompt. Anthropic's
cache is content-addressed (5-min TTL, survives intervening requests), so a stable prompt
reuses regardless of slot mechanics. **The prompt/history is therefore cross-turn-reusable;
the variable is the local backend.** (Today's `endpoint=anthropic` calls are a *different*,
sub-1024-token path and show `cache_read=0` simply because they fall under Sonnet's 1024-token
minimum cacheable prefix — not the 13,916 control.)

### 4. Gateway request is clean (rules out a Seshat cache-buster)

`src/personal_agent/llm_client/adapters.py` `_build_chat_completions_payload`:
- `payload = {model, messages, tools, sampling…, extra_body, cache_prompt: True}` — no
  per-request nonce, no `id_slot`, no `previous_response_id` (the chat/completions path is
  stateless; `previous_response_id` is explicitly ignored, `adapters.py:538/561`).
- `cache_prompt: True` is set unconditionally (`adapters.py:655`). Within-turn reuse proves
  cross-request caching is live on the server.

## SLM server launch config (provided 2026-06-01)

The `:8502` primary (`reasoning`) instance, the relevant fields:

```yaml
reasoning:
  id: "unsloth/qwen3.6-35-A3B"
  backend: "llamacpp"
  port: 8502
  model_type: "multimodal"          # ← vision model
  context_length: 131072
  max_concurrency: 1                 # ≈ single slot
  cache_prompt: true                 # matches gateway payload
  chat_template_kwargs: {enable_thinking: true, preserve_thinking: true}
  spec_type: "draft-mtp"             # MTP speculative decoding
  kv_unified: true
  cache_type_k: "q8_0"; cache_type_v: "q8_0"; flash_attn: true
  model_path:  ".../Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
  mmproj_path: ".../mmproj-F32.gguf" # ← vision projector LOADED
```

(`--cache-reuse` / `n_cache_reuse` and `--slot-prompt-similarity` are **unset** ⇒ defaults
`0`/`0.10`. The sub_agent runs as a **separate, text-only** instance — no `mmproj`.)

## Root cause (leading hypothesis): `mmproj` disables cross-request KV reuse

The `:8502` primary loads a **vision projector** (`mmproj-F32.gguf`). In llama.cpp, loading
`--mmproj` sets the multimodal (`has_mtmd`) path for the **whole server**, and the codebase
treats every slot as potentially image-bearing — which **disables slot save/restore,
context-shift, and cross-request prompt-cache reuse, even for 100% text-only conversations.**
This is a documented, open limitation, not a Seshat defect:

- ggml-org/llama.cpp **#21133** — slot save/restore blocked for text-only convos when `mmproj` loaded
- ggml-org/llama.cpp **#19466** — KV-cache save fails for vision models
- ggml-org/llama.cpp **#17200** — Qwen3-VL fails on the *second* request (KV-cache bug)
- ggml-org/llama.cpp **#13606** (discussion) · `tools/server/README.md`

This single fact explains every contrast we measured:

| Endpoint | `mmproj`? | Cache | Cross-turn reuse |
|---|---|---|---|
| **`:8502` primary** (`reasoning`) | **yes** | llama.cpp slot | **0** ← mmproj disables reuse |
| sub_agent (separate instance) | no | llama.cpp slot | reuses (text-only) |
| Cloud Sonnet | n/a | managed content-cache | reuses (~13,916/turn) |

It is therefore **not** ADR-0081 D2/D3 history churn, **not** single-slot eviction, **not** a
gateway/prompt problem — it's the vision projector on the orchestrator's endpoint.

**Honest caveat (why this is leading, not proven):** within-turn continuation calls on `:8502`
*did* reuse ~10k, so reuse is not 100% dead in this build — consistent with the mmproj path
blocking the cross-request slot-restore / partial-prefix re-match while a strict back-to-back
forward-append on a still-warm slot survives, but not yet proven. **Decisive one-step test:
run the primary model without `mmproj` and re-check cross-turn `cache_reuse` on a repeat-prefix
session. If it leaves 0 → confirmed.** Secondary suspects to fold into the same test if mmproj
alone doesn't fully restore reuse: `--cache-reuse 0` (no post-divergence recovery) and
`spec_type: draft-mtp` (speculative-decode prompt path).

## Hypothesis verdict

| # | Hypothesis | Verdict | Basis |
|---|---|---|---|
| 1 | History/summary reconstruction breaks prefix (→ D2/D3) | **Refuted** | Stable hash + cloud A/B reuses identical prefix; a literal 0 ≠ tail divergence |
| 2 | Single-slot eviction by another SLM request | **Refuted (this traffic)** | Only `primary` hits SLM; `max_concurrency:1`; session alone; miss occurs with no evictor |
| 3 | `mmproj` (vision projector) disables cross-request KV reuse | **Leading (best-sourced, testable)** | `:8502` loads `mmproj`; documented llama.cpp limitation (#21133/#19466/#17200); explains primary=0 vs sub_agent/cloud=reuse contrast |
| 4 | `--cache-reuse 0` + `spec_type: draft-mtp` interaction | **Secondary** | Confirmed unset/enabled in config; fold into the no-mmproj test |

## Recommendation (routed)

**(b) Bounded config/ops fix on the SLM-server host — NOT this gateway, NOT an ADR-0081
D2/D3 design.** Hand to the SLM-server config owner (Mac), not the adr session.

1. **Decisive test (do first):** relaunch the `orchestrator.primary` model **without `mmproj`**
   and re-measure cross-turn `cache_reuse` on a ≥5-turn repeat-prefix session. Off-0 ⇒ confirmed.
2. **Likely fix:** serve `orchestrator.primary` from a **text-only** instance (drop `mmproj`
   from `:8502`); route the rare actual vision requests to a **separate multimodal endpoint**.
   Restores slot persistence + cross-request reuse and finally delivers D4's latency win.
3. **If reuse still imperfect after (2):** set `--cache-reuse N` (post-divergence KV-shifting)
   and/or test with `spec_type` disabled to isolate the speculative-decode path.

A follow-up implementation ticket should be scoped to the **SLM server**, AC = local
`cache_read_tokens > 0` on a new turn's first call for a repeat-prefix session.

## Build-owned cleanup shipped with this note

`adapters.py:655` comment claimed "llama-server defaults to false" for `cache_prompt`; current
llama.cpp defaults it **true** (version drift). Comment corrected — behavior unchanged
(harness sets it true regardless).
