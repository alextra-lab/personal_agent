# ADR-0061: Within-Session Progressive Context Compression (head-middle-tail)

**Status:** Accepted — Implemented 2026-05-01 (FRE-251)
**Date:** 2026-05-01
**Deciders:** Project owner
**Depends on:** ADR-0038 (Context Compressor Model), ADR-0041 (Event Bus — Redis Streams), ADR-0043 (Three-Layer Architectural Separation), ADR-0047 (Context Management & Observability), ADR-0054 (Feedback Stream Bus Convention)
**Related:** ADR-0032 (Robust Tool Calling — old-error eviction runs ahead of compression), ADR-0059 (Context Quality Stream — per-incident signal that Phase 2 will consume), ADR-0064 (Memory Visibility — head boundary respects user message ownership)
**Linear Issue:** FRE-251

---

## Context

### What is in place today

Two narrow mechanisms compress conversation context. Neither defends a head + tail invariant inside a single in-flight session:

1. **Stage 7 — `request_gateway/budget.py:apply_budget()`** — fires once per request at gateway entry. Trims in three phases (history → memory slab → tool defs). Never preserves the original task instruction; cannot run during an in-flight orchestration loop; entity granularity is binary.
2. **`orchestrator/compression_manager.py:maybe_trigger_compression()`** — runs *between* turns asynchronously. Threshold: `estimated_tokens > 0.65 × context_window_max_tokens` (`context_compression_threshold_ratio`). Calls `compress_turns(messages[1:-keep_recent])` where `keep_recent = 4`.

This is a rough head-middle-tail split today (head = `messages[0]` only, tail = the last 4 messages, middle = everything else). The existing slot for the LLM summary is the `compressed_summary` parameter on `apply_context_window` — when present, it replaces the static `[Earlier messages truncated]` marker.

### Three concrete gaps

1. **No pre-pass for large tool outputs.** The LLM compressor (`compress_turns`) is asked to summarise raw tool bodies — Elasticsearch responses, Neo4j query results, web fetch payloads — verbatim. A 10 000-token ES response burns 10 000 input tokens of compressor cost producing the same ~300-token summary as a compact descriptor would.
2. **Single-shot per session.** `_pending_tasks[session_id]` blocks any new compression while one is in flight; `_summaries[session_id]` is cleared on the *next* turn that consumes it; nothing fires a second compression as the post-summary middle grows. Long sessions produce one summary then drift back into overflow.
3. **Head is too narrow.** Only `messages[0]` is preserved. The first *user* message — which carries the original task instruction — is treated as middle and may be evicted on long sessions. The agent then "forgets why we started."

### The pattern (Hermes-inspired)

NousResearch's Hermes Agent (MIT, 2026) uses a head-middle-tail invariant for within-session compression:

- **Head** preserved verbatim: system prompt + original task instructions.
- **Tail** preserved verbatim: most recent N tokens.
- **Middle** compressed: pre-pass replaces large tool outputs with 1-line descriptors; LLM summariser then condenses what's left.

The pre-pass is the key insight: a compressor LLM operating on an already-tokenised, already-noisy dump cannot do better than ~3× compression, but a deterministic pre-pass collapses tool noise to a few lines for free, leaving the LLM to summarise actual conversational content.

**Credit:** Head-middle-tail compression approach inspired by NousResearch Hermes Agent (MIT, 2026). No dependency on Hermes code — adopted as a design pattern, recorded as "reference_hermes_agent_research" in agent memory.

### Composability with ADR-0059

ADR-0059 publishes `CompactionQualityIncidentEvent` on `stream:context.compaction_quality_poor` with a per-session governance signal: when the recall controller detects that a recently dropped entity overlaps with a new user noun phrase, the next request's budget tightens. ADR-0061 sits adjacent: it is the *trim strategy* layer, where ADR-0059 is the *budget ceiling* layer. Phase 2 of this ADR will consume the same signal to widen the tail or relax the pre-pass for sessions whose recall has been hurting — but Phase 1 ships without that wiring.

### Why now

Wave 4 of the implementation sequence (`docs/superpowers/specs/2026-04-22-implementation-sequence-wave-plan-design.md`). The only blocking predecessor (FRE-249 / ADR-0059) shipped 2026-04-27.

---

## Decision Drivers

1. **Defend the head + tail invariant.** Whatever compression strategy we pick, system prompt + first user message must be untouchable, and the most recent K tokens must be untouchable. These are the two failure modes today.
2. **Cheap first, smart second.** Pre-pass (deterministic) before LLM summariser (cost). Don't pay compression cost on tool bytes that have a one-line descriptor.
3. **Layer above Stage 7, do not replace it.** Stage 7 stays as the safety net for cases the within-session pass can't handle (single tool response > full budget, or compressor failure). Two passes, two failure modes.
4. **Persist the rewrite.** Pre-pass replacements get written back to the session message store. Otherwise the next request runs the same expensive pre-pass on the same data.
5. **Re-fire as the session grows.** A cursor on `len(messages)` lets compression fire again N turns after the previous summary, so the middle stays bounded even on 50-turn sessions.
6. **Reuse, don't rebuild.** The compressor model role, structured prompt, dual-write JSONL pattern, EventBase shape — all already in the repo.

---

## Decision

### D1: Trigger condition — two-tier on token count

Both tiers measure `estimate_messages_tokens(messages)` from `orchestrator/context_window.py`:

| Tier | Threshold | Path | Latency budget |
|------|-----------|------|----------------|
| Soft | `≥ context_compression_threshold_ratio × max` (default 0.65) | Async, fire-and-forget; compressor LLM runs between turns | None — runs after `REPLY_READY` |
| Hard | `≥ within_session_hard_threshold_ratio × max` (default 0.85) | **Synchronous mid-orchestration** — blocks `step_llm_call` for ≤ 30 s | One compressor call (≤ 25 s timeout per ADR-0038) |

The soft trigger is the existing path; nothing about it changes except the helpers it calls. The hard trigger is new — fired from inside the orchestrator loop when a fresh tool result pushes the working messages list across the line.

The soft path also gains a re-fire cursor: `_last_compressed_at_msgcount[session_id]`. A subsequent soft fire requires `len(messages) >= cursor + within_session_compression_refire_after_messages` (default 4). This kills the "single-shot per session" gap.

### D2: Head boundary — system messages + first user message

```python
def _extract_head(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """system messages (1+) plus the first role='user' message."""
    head: list[dict[str, Any]] = []
    seen_first_user = False
    for m in messages:
        if m.get("role") == "system":
            head.append(m)
            continue
        if not seen_first_user and m.get("role") == "user":
            head.append(m)
            seen_first_user = True
            continue
        if seen_first_user:
            break
    return head
```

System messages are preserved unconditionally (an orchestrator may emit several — the original system prompt plus injected `[Earlier messages truncated]` or `compressed_summary` markers). The first `user` message is the task instruction; no other user messages are preserved as head.

The compressed summary marker, when produced, is written *after* the head. KV-cache prefix stability (ADR-0038 §4.6) is preserved because the head bytes are unchanged across compressions.

### D3: Tail size — dynamic by token count, with turn floor

```python
def _extract_tail(
    messages: list[dict[str, Any]],
    *,
    min_tokens: int,
    min_turns: int,
) -> list[dict[str, Any]]:
    """Last K messages such that sum tokens ≥ min_tokens AND len ≥ min_turns."""
```

Walk from the end backwards, accumulating messages until both invariants hold. Default floor: `min_tail_tokens = 2000`, `min_turns = 4`. A turn that contains both an assistant `tool_calls` message and the corresponding `role="tool"` reply is kept as a unit — the tail walker pulls the assistant message in if its `tool_call_id` is referenced by a kept tool message, even if the token floor was already met. This avoids producing orphaned tool-pair fragments that `_sanitize_tool_pairs` (`context_window.py:225`) would later drop.

Turn count is a coarse measure but stops the tail from being a single 4 000-token tool dump in degenerate cases. Token count is the primary measure for normal traffic.

### D4: Pre-pass rules — deterministic, before the LLM

Run after `_evict_old_tool_errors` (ADR-0032 §3.2) and after head/tail extraction; operate only on the middle band:

```python
def _pre_pass_tool_outputs(
    middle: list[dict[str, Any]],
    *,
    threshold_tokens: int,
) -> tuple[list[dict[str, Any]], int]:
    """Replace large tool messages with 1-line JSON descriptors.

    Returns (rewritten_middle, replacement_count).
    """
```

Replacement rules:

| Condition | Action |
|-----------|--------|
| `m["role"] == "tool"` AND `estimate_message_tokens(m) ≥ threshold_tokens` | Replace `content` with `json.dumps({"_replaced": true, "tool_call_id": m["tool_call_id"], "size_chars": len(orig), "shape": top_level_keys_or_truncated_repr})` |
| `m["role"] == "tool"` AND content is a Python error string (`'"error"'` or `'"status": "error"'` substring) | Skip — keep the error verbatim so the compressor sees what failed |
| `m["role"] == "assistant"` (with `tool_calls`) | Skip — model-authored, almost always small |
| `m["role"] in {"system", "user"}` | Skip — neither user inputs nor injected system markers ever get replaced |

The `tool_call_id` field is preserved on the replaced message so `_sanitize_tool_pairs` keeps the assistant↔tool pair intact. The descriptor is JSON so a downstream model can re-parse it if needed.

Default threshold: `within_session_pre_pass_threshold_tokens = 800` (≈ 3 200 chars). Below this size, the LLM compressor handles the message directly — replacement overhead isn't worth it.

### D5: Summariser model — reuse `compressor` role

No new model role. The compressor LLM defined in ADR-0038 (`compressor` role, default `gpt-5.4-nano`, 25 s timeout) is called via the existing `compress_turns` flow. The post-pre-pass middle is the input; the structured 4-section output (Decisions / Entities / Facts / Open Items) is the output. If the role is missing from `models.yaml`, we fall back to the static `[Earlier messages truncated]` marker per ADR-0038 §Fallback.

The summariser is shared between the soft and hard trigger paths. The hard path adds a hard 25 s timeout (one compressor call) — if it fires synchronously and times out, we fall back to assembling head + (pre-pass-only middle) + tail without an LLM summary. Pre-pass alone usually wins back enough headroom.

### D6: Interaction with Stage 7

Layered. Within-session compression is the **first line of defence**, running *during* the orchestrator loop on the working messages list. Stage 7 (`apply_budget`) remains the safety net at request entry for the next user turn.

```
Request N+1 enters gateway
  → Stage 7 trims if needed (operates on session messages already pre-passed)
  → Stage 6 assembled context goes to orchestrator
  → step_llm_call runs; tool results return
  → after each tool result: needs_hard_compression(messages, max_tokens)?
       → yes → await compress_in_place(messages, ..., trigger="hard")
       → no  → continue
  → after REPLY_READY: maybe_trigger_compression(...) (soft, async)
```

The pre-pass replacement is written **back to the session message store** through the same path the soft summary uses today (`SessionManager.update_messages`). When the next request enters Stage 7, the messages are already pre-passed; Stage 7 sees a smaller working set and is less likely to need to drop the memory slab.

This gives us a layered defence: pre-pass on tool noise (cheap, persistent), LLM summary on conversational noise (expensive, persistent), Stage 7 trim (last resort, throws state away).

### D7: Telemetry

```python
class WithinSessionCompressionEvent(EventBase):
    event_type: Literal["context.within_session_compressed"] = "context.within_session_compressed"
    trigger: Literal["soft", "hard"]
    head_tokens: int
    middle_tokens_in: int
    middle_tokens_out: int
    tail_tokens: int
    pre_pass_replacements: int
    summariser_called: bool
    summariser_duration_ms: int
    tokens_saved: int            # middle_tokens_in - middle_tokens_out
    # trace_id / session_id / source_component / schema_version inherited (ADR-0054)
```

Stream name: `stream:context.within_session_compressed` (per ADR-0054 `<domain>.<signal>`).
Source component: `orchestrator.within_session_compression`.
Durable JSONL: `telemetry/within_session_compression/WSC-<YYYY-MM-DD>.jsonl`, one line per compression event with the same fields plus `compressed_at` ISO timestamp.

Dual-write order per ADR-0054 §D4: durable file first, bus publish second; bus failures logged-and-swallowed; durable failures propagate (no silent loss of observability at the source).

No new consumer group. No Captain's Log handler. This is observability data, not a feedback signal — the Phase 2 hook (when added) will read the bus stream directly to drive parameter changes; nothing else needs to subscribe yet.

### D8: Configuration flags

Four new settings under the existing `context_*` block:

| Field | Default | Purpose |
|-------|---------|---------|
| `within_session_compression_enabled` | `True` | Master kill switch. When `False`, hard trigger is a no-op; soft trigger reverts to pre-FRE-251 behaviour. |
| `within_session_hard_threshold_ratio` | `0.85` | Hard-trigger ratio against `context_window_max_tokens`. |
| `within_session_min_tail_tokens` | `2000` | Tail floor in tokens. |
| `within_session_pre_pass_threshold_tokens` | `800` | Per-message size threshold for pre-pass replacement. |
| `within_session_compression_refire_after_messages` | `4` | Soft re-fire cursor — minimum new messages between consecutive compressions for the same session. |

All read via `personal_agent.config.settings`; no `os.getenv()` calls.

### D9: Out of scope

- **Phase 2 — adaptive tail / pre-pass tuning from ADR-0059 signal.** A follow-up Linear issue (will spin off after this PR merges) will read `IncidentTracker.count_in_window(session_id, 24)` from `telemetry/context_quality.py` and use it to widen `min_tail_tokens` (e.g. ×1.5) or raise `pre_pass_threshold_tokens` (preserve more middle detail) for sessions with recent recall pain. Flag-gated off until 14 days of Phase 1 telemetry validate the signal-to-noise.
- **Token-aware in-budget rebalancing.** Already deferred from ADR-0059 D9; remains deferred.
- **Cross-session calibration of compression thresholds.** Out of scope; per-session is the right granularity.
- **Pre-pass for large `assistant` content blocks.** Rare in this agent (Qwen3.6-35B-A3B emits short assistant messages with tool calls); not worth the implementation complexity. Reconsider if telemetry shows >5% of pre-pass would-be candidates are assistant messages.
- **Backfill of historical sessions.** Not meaningful — within-session compression operates only on a live session's working messages.

---

## Alternatives Considered

### Compression strategy

| Option | Verdict |
|--------|---------|
| A. Tail-only — keep last K turns, drop everything else | Rejected — loses task instruction; recall controller (ADR-0059) confirms users reference earlier turns regularly |
| B. LLM summariser with no pre-pass (status quo) | Rejected — the very tool dumps that overflow context are the most expensive thing to summarise verbatim |
| **C. Head-middle-tail with deterministic pre-pass** | **Selected** — defends both head and tail; pre-pass attacks the cheapest-to-collapse waste first |
| D. Embed-and-rank — score each middle message, drop low-scoring ones | Rejected — needs an embedding model in the loop; adds latency; harder to reason about |

### Hard trigger placement

| Option | Verdict |
|--------|---------|
| A. After every tool result | Rejected — over-fires on small tool results |
| **B. Token-threshold check at top of `step_llm_call`** | **Selected** — fires once per LLM turn at most; minimal overhead when no compression needed |
| C. Cooperative — orchestrator opts in via state machine flag | Rejected — every state would need to remember to set the flag; bug-prone |

### Tail size measure

| Option | Verdict |
|--------|---------|
| A. Fixed `keep_recent = 4` (status quo) | Rejected — a single 8 000-token tool result inside the tail would push the entire compressed summary plus head out of budget |
| **B. Dynamic by tokens with `min_turns` floor** | **Selected** — handles both small-message and large-message tails |
| C. Dynamic by tokens, no floor | Rejected — degenerate sessions (one giant tool dump) would have a 1-message tail, losing recent conversational state |

### Pre-pass aggressiveness

| Option | Threshold | Verdict |
|--------|-----------|---------|
| A. 200 tokens | Rejected — replaces small structured tool replies that add little noise |
| **B. 800 tokens** | **Selected** — captures large bodies (ES responses, web fetches) while leaving normal tool replies alone |
| C. 2 000 tokens | Rejected — leaves too much fat for the LLM compressor to handle |

### Telemetry shape

| Option | Verdict |
|--------|---------|
| A. Log-only (structlog event, no bus) | Rejected — Phase 2 needs to consume the bus stream; ADR-0054 dual-write convention applies |
| **B. Dual-write JSONL + bus event** | **Selected** — matches ADR-0054 D4; matches every other Phase 3 stream pattern (FRE-249, FRE-250) |
| C. Bus-only (no JSONL) | Rejected — Redis is not durable; outages would erase the signal |

---

## Consequences

### Positive

- **Two failure modes today (head loss, single-shot) are closed.** First-user-message preservation; re-fire cursor.
- **Compressor LLM cost drops materially.** Pre-pass on the noisiest tool outputs (ES, web fetch, Neo4j) saves the LLM compressor input tokens 1:1 with the saved bytes.
- **Stage 7's job gets easier.** When pre-passed messages go back into the session store, the next request's Stage 7 sees a smaller working set; the memory-slab drop (ADR-0059's Bug A site) becomes rarer.
- **KV-cache prefix stability is preserved.** Head bytes don't change across compressions; the existing `compute_prefix_hash` invariant test catches regressions.
- **Phase 2 wiring is incremental.** ADR-0059's per-session governance signal is sitting idle on `stream:context.compaction_quality_poor`; ADR-0061 Phase 2 will consume it without producer changes — confirming the ADR-0054 composability promise.

### Negative

- **One synchronous LLM call (worst case) inside the orchestrator loop.** Hard trigger blocks `step_llm_call` for ≤ 25 s. Gated behind a high (0.85) threshold so the path is rare in practice.
- **One new bus stream + JSONL directory** to operate. Same flavour as Streams 7 / 8 / 9; no novel ops burden.
- **Pre-pass writes back to the session message store** — that store is now mutated by the orchestrator, not just the gateway. Logged on each rewrite for traceability.

### Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Pre-pass replaces a tool output the model later needs verbatim | Low | Replacement preserves `tool_call_id`; if the model re-issues the same tool call, the new response is fresh; descriptor includes `size_chars` and `shape` for debug |
| Hard trigger times out, head + middle + tail still over budget | Low | Stage 7 (next request) catches what's left; this PR adds an inline log warning so timeouts are visible |
| Re-fire cursor mis-tracks in long sessions, fires constantly | Low | Cursor advances by `_last_compressed_at_msgcount`; unit tests cover the math; log includes `messages_since_last` for verification |
| Pre-pass `shape` extraction throws on malformed JSON | Low | Wrap `json.loads` in try/except; fall back to `repr(content)[:120]` |

---

## Implementation Priority

### Phase 1 — Within-session compression with pre-pass

| Order | Work | Tier |
|-------|------|------|
| 1 | ADR-0061 (this file), accepted | Tier-1: Opus |
| 2 | `WithinSessionCompressionEvent` + `STREAM_CONTEXT_WITHIN_SESSION_COMPRESSED` + `parse_stream_event` arm in `events/models.py` | Tier-3: Haiku |
| 3 | `telemetry/within_session_compression.py` — `WithinSessionCompressionRecord`, `record_compression(record, bus)` dual-write | Tier-2: Sonnet |
| 4 | `orchestrator/context_compressor.py` — extract `_pre_pass_tool_outputs`; `summarize_middle()` wraps `compress_turns` and returns `(summary, stats)` | Tier-2: Sonnet |
| 5 | `orchestrator/within_session_compression.py` (new) — `_extract_head`, `_extract_tail`, `_assemble_compressed`, `compress_in_place`, `needs_hard_compression` | Tier-2: Sonnet |
| 6 | `orchestrator/compression_manager.py` — refactor `maybe_trigger_compression` to use new helpers; add `_last_compressed_at_msgcount` cursor | Tier-2: Sonnet |
| 7 | `orchestrator/executor.py` — call `needs_hard_compression` at top of `step_llm_call`; on true, `await compress_in_place(...)` synchronously | Tier-2: Sonnet |
| 8 | `config/settings.py` — five new fields (D8) | Tier-3: Haiku |
| 9 | Unit tests — head/tail extraction, pre-pass, compress_in_place, refire cursor, hard trigger gate | Tier-2: Sonnet |
| 10 | `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md` — flip the FRE-251 line to implemented | Tier-3: Haiku |

### Phase 2 — Adaptive tuning from ADR-0059 signal (out of scope for this PR)

Spun off as a follow-up Linear issue after this PR merges. Default flag-gated off; flip after 14 days of Phase 1 telemetry confirms ADR-0059 incident counts correlate with sessions where wider tail / relaxed pre-pass would have helped.

---

## Module Placement

Following ADR-0043 (Three-Layer Separation):

| Component | Module | Layer |
|-----------|--------|-------|
| Stream/event constant + event class | `src/personal_agent/events/models.py` | Infrastructure |
| `WithinSessionCompressionRecord` + `record_compression` | `src/personal_agent/telemetry/within_session_compression.py` | Observation |
| `_pre_pass_tool_outputs`, `summarize_middle` | `src/personal_agent/orchestrator/context_compressor.py` | Execution |
| `compress_in_place`, `needs_hard_compression`, head/tail helpers | `src/personal_agent/orchestrator/within_session_compression.py` | Execution |
| Refactored `maybe_trigger_compression` + refire cursor | `src/personal_agent/orchestrator/compression_manager.py` | Execution |
| Hard-trigger call site | `src/personal_agent/orchestrator/executor.py` | Execution |
| Config flags | `src/personal_agent/config/settings.py` | Infrastructure |

---

## Open Questions

1. **Pre-pass `shape` field detail.** Top-level keys for dict-shaped JSON, list length for list-shaped, first 120 chars otherwise. Will refine if telemetry shows the descriptor is too lossy for debug.
2. **Cursor reset on session ownership change.** If the same `session_id` ever transitions ownership (FRE-268), the cursor should reset. Today `cleanup_session` clears it; that's enough for current ownership semantics.
3. **Synchronous timeout default.** 25 s matches ADR-0038. If hard-trigger sessions consistently time out, the right answer is probably to keep the timeout and let pre-pass-only fall through, not to extend it.

---

## Acceptance Criteria

- [x] ADR-0061 written and accepted
- [ ] `_extract_head` preserves system + first user message; verified by unit test
- [ ] `_extract_tail` honours both `min_tokens` and `min_turns`; verified by unit test
- [ ] `_pre_pass_tool_outputs` replaces tool messages ≥ threshold; preserves `tool_call_id`; skips errors and small messages
- [ ] `compress_in_place` round-trip — head bytes unchanged, tail bytes unchanged, middle smaller
- [ ] `needs_hard_compression` returns `True` only when threshold crossed
- [ ] Soft re-fire cursor advances correctly; second compression fires only after `refire_after_messages` new turns
- [ ] `WithinSessionCompressionEvent` round-trips through `parse_stream_event`
- [ ] `record_compression` writes JSONL line before bus publish; bus failure does not raise
- [ ] `step_llm_call` calls `needs_hard_compression` and awaits `compress_in_place` on `True`
- [ ] `apply_budget` regression — Stage 7 still fires when within-session pass insufficient
- [ ] `compute_prefix_hash` invariant preserved across compressions in unit test
- [ ] `FEEDBACK_STREAM_ARCHITECTURE.md` line 275 flipped to "Implemented"

---

## References

- FRE-251: Draft ADR-0061 — Within-Session Progressive Context Compression (this ADR)
- ADR-0038: Context Compressor Model — compressor role + structured prompt + 25 s timeout
- ADR-0041: Event Bus via Redis Streams — transport
- ADR-0043: Three-Layer Architectural Separation — module placement
- ADR-0047: Context Management & Observability — D3 introduced the dropped-entity cache
- ADR-0054: Feedback Stream Bus Convention — `EventBase`, dual-write D4, naming
- ADR-0059: Context Quality Stream — Phase 2 governance signal that this ADR's Phase 2 will consume
- ADR-0032: Robust Tool Calling — `_evict_old_tool_errors` runs before this ADR's pre-pass
- `src/personal_agent/orchestrator/compression_manager.py` — pre-FRE-251 single-shot path
- `src/personal_agent/orchestrator/context_compressor.py` — `compress_turns` reused by `summarize_middle`
- `src/personal_agent/orchestrator/context_window.py` — `estimate_messages_tokens`, `apply_context_window`, `compute_prefix_hash`
- `src/personal_agent/orchestrator/executor.py` — `step_llm_call` (hard-trigger call site), `REPLY_READY` (soft-trigger call site)
- `src/personal_agent/request_gateway/budget.py` — Stage 7 safety net (untouched)
- `src/personal_agent/telemetry/context_quality.py` — pattern copied for the new telemetry module
- `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md` — FRE-251 row updated
