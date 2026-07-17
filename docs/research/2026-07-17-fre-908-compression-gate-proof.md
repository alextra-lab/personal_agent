# FRE-908 — Proving the within-session compression gate can trigger

**Date:** 2026-07-17
**Backing:** ADR-0061 (within-session progressive context compression), ADR-0059 (context-quality incidents).
**Also implicated:** ADR-0081 (cache-aware frozen append-only layout, FRE-434).

Scope per ticket: measurement and report only. No thresholds changed, no flags flipped, no live
gateway turns. All evidence below comes from offline synthetic fixtures and the checked-in code/config —
see `tests/test_orchestrator/test_compression_gate_proof.py` for the executable proof backing every claim.

## Findings table

| AC | Question | Answer |
|----|----------|--------|
| AC-1 | Does the hard-compression gate fire on a genuinely oversized history? | **Yes, it fires — but it cannot shrink the exact scenario it was built for.** See Finding 3. |
| AC-2 | What is the estimated-vs-real token gap? | The estimator itself is exact (tiktoken cl100k_base, no heuristic gap). The gap is a **blind spot**: `reasoning_content` (thinking) is invisible to it entirely. Measured ratio on a ~3.4k-token reasoning fixture: real total is >1.5x the estimate. |
| AC-3 | What is the precedence between the three trim mechanisms? | `_maybe_frozen_reset` (ADR-0081) runs once per turn **before** the LLM-call loop and resets accumulated tokens at 48,000 — below both ADR-0061 thresholds (62,400 soft / 81,600 hard). Under the production default (`cache_frozen_layout_enabled=True`), it structurally pre-empts the soft trigger, which is dead by design (`executor.py:4838-4842`). The hard trigger is the only surviving ADR-0061 mechanism, and only because it's checked *inside* the same turn's tool loop, after the frozen-reset checkpoint has already passed. |
| AC-4 | Where does telemetry land, and is it durable? | `telemetry/within_session_compression/` resolves relative to CWD → `/app/telemetry/within_session_compression` in the container. `docker-compose.cloud.yml`'s gateway volume list mounts a durable volume for the sibling ADR-0059 `context_quality` stream but **not** for `within_session_compression`. Records are lost on every rebuild. **Fixed 2026-07-17 by FRE-910** — see addendum below. |
| AC-5 | Behaviour/threshold changes in this ticket? | None. Measurement only. |

## Finding 0 — a mechanism the ticket didn't know about pre-empts everything

`_maybe_frozen_reset` (`executor.py:1264`, called from the context-loading step at `executor.py:3077` —
**before** the LLM-call loop even starts) is ADR-0081's cache-aware compaction scheduler. It fires whenever
`accumulated_tokens >= cache_frozen_accum_max_ratio(0.50) × context_window_max_tokens(96000) = 48,000`, or
earlier at its cost-optimum `L*` (`cache_reset_scheduler.py:88`). `accumulated_tokens` is
`estimate_messages_tokens(ctx.messages)` (`executor.py:1189`) — the **same estimator** the ADR-0061 gates use.

```
48,000 (frozen-reset ceiling) < 62,400 (ADR-0061 soft, 0.65) < 81,600 (ADR-0061 hard, 0.85)
```

Under `cache_frozen_layout_enabled=True` (production default since 2026-06-02, `settings.py:1078`), the
frozen-reset scheduler resets accumulated tokens back down once per turn, before either ADR-0061 threshold
is reached under normal steady growth. This is a **documented, deliberate supersession** —
`executor.py:4838-4842`:

```python
# ADR-0081 §D3 Decision 3: under the frozen layout the reactive 0.65 soft
# trigger is removed — the cache-aware scheduler (step_init) subsumes it;
# firing reactive compaction here would rewrite history off-schedule and
# break the forward-extension.
if ctx.session_id and not settings.cache_frozen_layout_enabled:
    ... compression_manager.maybe_trigger_compression(...)
```

So the **soft trigger (0.65) is dead by design** under the production default, not dead by bug.
`context_window.py:54-83`'s docstring says the same for `apply_context_window`'s `compressed_summary`
parameter ("Dead-by-default when `cache_frozen_layout_enabled=True`").

The **hard trigger (0.85)** has no such guard (`executor.py:3316`, `needs_hard_compression` only checks its
own `within_session_compression_enabled` master switch) — it remains live, and is structurally the *only*
surviving ADR-0061 mechanism in production. It can still fire because `_maybe_frozen_reset` evaluates once
per turn at entry, while `needs_hard_compression` is checked before every LLM dispatch inside that turn's
tool-loop — so a single large tool response arriving mid-turn can jump the working set from
comfortably-under-48k straight past 81,600 before the next turn's frozen-reset would have caught it. This
matches ADR-0061 §D1's own stated rationale ("catches in-flight overflow caused by large tool responses")
exactly.

**Consequence:** FRE-367 (ADR-0061 Phase 2) would be reading a signal (the soft-trigger telemetry) that is
architecturally dead by a later, higher-precedence ADR. Phase 2 needs to target the hard-trigger path
specifically, or ADR-0081's own scheduler — not the soft path.

## Finding 1 — the estimator is exact for what it counts; the gap is a blind spot

`estimate_tokens` (`llm_client/token_counter.py:26`) is tiktoken cl100k_base-backed, not a naive heuristic.
`estimate_message_tokens` (`context_window.py:15`) sums `count_content_tokens(content)` (also
tiktoken-backed, `message_content.py:93`) plus `tool_calls` payload tokens.

Test-verified (`TestEstimatorReconciliation.test_estimator_matches_cl100k_encoding`): for plain text,
`count_content_tokens(text) == len(tiktoken.get_encoding("cl100k_base").encode(text))` exactly. No
heuristic-vs-encoding gap.

The real gap: assistant messages in the working history carry a `reasoning_content` field
(`executor.py:4100`, `:4133` — Qwen3.6 unsloth template convention), populated from the model's thinking
trace. Neither `estimate_message_tokens` nor `message_content.py` ever reads `reasoning_content` — only
`content` and `tool_calls`. Test-verified
(`TestEstimatorReconciliation.test_reasoning_content_is_invisible_to_the_estimator`): a fixture with a
~3.4k-token reasoning trace produces `real_total / estimated > 1.5`. Every thinking token is invisible to
both ADR-0061 gates and the frozen-reset scheduler's `accumulated_tokens`. Corroborates FRE-755's ~45%
undercount finding from a different angle (there: route_traces vs ES; here: the gate's own live view of the
working set).

## Finding 2 — telemetry durability gap, asymmetric with ADR-0059's sibling stream

`telemetry/within_session_compression.py:71-73`, `_default_output_dir()` returns
`Path("telemetry/within_session_compression")` — relative to CWD. Test-verified
(`TestTelemetryDurability.test_output_dir_is_cwd_relative`). In the gateway container (`WORKDIR /app`,
`Dockerfile.gateway:56`), that resolves to `/app/telemetry/within_session_compression`.

`docker-compose.cloud.yml`'s `seshat-gateway` volumes list mounts durable volumes for `captains_log`,
`feedback_history`, `graph_quality`, **`context_quality` (ADR-0059)**, `error_patterns`,
`freshness_review`, and `agent_workspace` — but **not** `within_session_compression`. Test-verified
(`TestTelemetryDurability.test_gateway_volumes_mount_context_quality_but_not_within_session_compression`,
parsed via `yaml.safe_load`, not text-slicing). ADR-0061 §D7 claims the same ADR-0054 §D4 durable-JSONL
guarantee its sibling ADR-0059 stream actually has in production; ADR-0061's own directory is not wired to
survive a rebuild.

**Proposed fix (not applied — AC-5 scope guard):** add to `docker-compose.cloud.yml`'s `seshat-gateway`
volumes:
```yaml
- seshat_within_session_compression_cloud:/app/telemetry/within_session_compression
```
plus the matching top-level `volumes:` entry. For master/owner to fold in or ticket separately.

## Finding 3 — the hard trigger cannot shrink the exact scenario it exists for (new — discovered during TDD)

This is the most consequential finding in the ticket, and it was not anticipated by the investigation that
produced Findings 0-2. It surfaced because the first test-writing pass asserted `compress_in_place` would
shrink an oversized history built from the ADR-0061 §D1 "large tool response spiking mid-turn" scenario —
and the assertion failed. That failure is real, not a fixture bug (test-verified:
`TestHardGateFiresAtProductionScale.test_compress_in_place_cannot_shrink_a_tail_resident_spike`).

`compress_in_place` (`within_session_compression.py:350`) only compresses the **middle** band — head and
tail are preserved verbatim unconditionally, by design (`_extract_tail` docstring, §D3). The tail is built by
walking backward from the end of the message list, accumulating messages until **both**
`used_tokens >= min_tokens` and `len(tail) >= min_turns` — with no upper bound on how large a single message
in that walk may be. `min_tokens` defaults to
`within_session_min_tail_ratio(0.25) × context_window_max_tokens(96000) = 24,000` tokens.

For a single trailing tool response to single-handedly cross the hard threshold (81,600) from a modest
pre-turn baseline, it must itself be roughly 80,000+ tokens — far larger than the 24,000-token tail floor.
So **any tool response large enough to trip the hard gate is, by construction, also large enough to satisfy
the tail's own floor on its own** — it is swept wholesale into the protected tail band and never reaches
`_pre_pass_tool_outputs` or the summariser, both of which only operate on the middle band. The result,
measured directly: `record.middle_tokens_out == record.middle_tokens_in`, `record.tokens_saved == 0`,
and the compressed message list is byte-for-byte the same size as the input.

This is a general property of the current design, not specific to this fixture's proportions: **whenever
the working-set growth that crosses the hard threshold is concentrated in the most recent turn(s) — exactly
ADR-0061 §D1's own stated rationale — the hard-compression pass achieves zero reduction.** It only reduces
history that has grown through many *moderate*-sized turns accumulating past the threshold over time, where
the offending bulk sits further back than the tail's 24,000-token/4-turn floor reaches. That growth pattern
is largely pre-empted earlier by the frozen-reset scheduler (Finding 0), which resets at 48,000 tokens
before the hard threshold (81,600) is ever reached through gradual accumulation.

Net effect: the hard trigger fires in the scenario it was designed for, but the mechanism it triggers is
not the one that helps in that scenario. It only has room to act on the accumulation pattern the frozen-reset
scheduler already handles first.

## Order-of-operations trace (AC-3, narrative)

1. **Turn entry**, before the LLM-call loop starts: `_maybe_frozen_reset` (`executor.py:1264`, called from
   `executor.py:3077`) evaluates `accumulated_tokens >= 48,000` (or the cost-optimum `L*`). Fires and resets
   if true. Guarded by `cache_frozen_layout_enabled` (default `True`).
2. **Inside the tool-call loop, before each LLM dispatch**: `needs_hard_compression` (`executor.py:3316`)
   checks `estimate_messages_tokens(messages) >= 81,600`. No `cache_frozen_layout_enabled` guard — always
   live. This is the only path that can still act after step 1 has passed for this turn, per Finding 0.
3. **Soft trigger** (`compression_manager.maybe_trigger_compression`, gated at `executor.py:4842` by
   `not settings.cache_frozen_layout_enabled`) — unreachable under the production default. Dead by design.
4. Test-verified quantitatively: `frozen_ceiling(48,000) < soft_threshold(62,400) < hard_threshold(81,600)`
   (`TestPrecedenceOrdering.test_frozen_reset_ceiling_is_tighter_than_both_adr_0061_thresholds`), and the
   soft-trigger call-site guard's precondition (`cache_frozen_layout_enabled is True` in production) is
   pinned (`TestPrecedenceOrdering.test_soft_trigger_call_site_guard_is_closed_under_production_default`).

## Consequence for FRE-367 (ADR-0061 Phase 2)

FRE-367 stays parked behind this ticket, and the picture is worse than "maybe unbuildable": Phase 2 would be
adaptive tuning bolted onto a soft-trigger signal that is dead by design (Finding 0) and a hard-trigger
signal that, even when it fires, cannot reduce the exact working-set growth pattern it fires for
(Finding 3). Any Phase 2 work needs to target either the ADR-0081 frozen-reset scheduler directly, or extend
the tail-extraction logic to exempt individually-oversized trailing messages from the verbatim-preservation
floor — a design change, not a calibration tweak, and out of scope here.

## Addendum (2026-07-17) — AC-4 fixed by FRE-910

This ticket's own AC-4 finding proposed (but explicitly did not apply — measurement-only scope) a
one-directory volume mount for `within_session_compression`. FRE-910 superseded that narrow fix with a
single parent mount at `/app/telemetry` on the `seshat-gateway` service, covering `within_session_compression`
and every other CWD-relative telemetry writer (nine total) rather than adding mounts one directory at a
time. `within_session_compression` is now durable in `docker-compose.cloud.yml`.

The test this doc originally cited,
`TestTelemetryDurability.test_gateway_volumes_mount_context_quality_but_not_within_session_compression`,
was renamed and its assertion flipped to match: it is now
`test_gateway_volumes_mount_within_session_compression_durably` and asserts the stream **is** covered. The
general-purpose guard (any current or future `Path("telemetry/...")` writer resolves under a mounted path)
lives in `tests/personal_agent/telemetry/test_mount_coverage.py::TestTelemetryMountCoverage`.
