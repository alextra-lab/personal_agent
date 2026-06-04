# FRE-475 — Birth-time digest placement redesign (ADR-0085 §D1 case-a)

> **Status:** Plan — codex review then implement
> **Ticket:** FRE-475 (In Progress · Tier-1) · project *Turn Cost & Latency Optimization*
> **Supersedes:** the keep-window-deferred placement shipped in PR #161 (PR-B)

## Context

PR-B's `apply_intra_turn_digest` ran **after** `ctx.messages.extend(tool_results)` and digested tool
messages **older** than the keep window — every such digest is an ADR-0085 **case-(b) deferred
rewrite**: it mutates bytes already in the cached prefix → invalidates the prefix from that point →
forces a re-cache. The live A/B (trace `5f2d1277`) measured the result: **fresh input 1,036,347 vs
768,484 baseline = +34.9% WORSE**, `cache_read` churned 108k→162k, and the artifact did not build.
The negative result is recorded in `scripts/eval/fre475_compression_ab/README.md`.

ADR-0085 §D1 was explicit that only **case-(a) birth-time digestion** — digesting *before* the
verbatim bytes ever enter `ctx.messages` — yields the no-invalidation win. PR-A infra and
`expand_tool_result` are sound; the **placement** is the defect. This redesign changes placement
only.

## Redesign (placement only)

### R1 — Birth-time pass on the fresh batch, before `extend`
Rewrite `apply_intra_turn_digest` to operate on the **freshly built `tool_results` list**, mutating
each eligible entry's `content` to a digest **before** `ctx.messages.extend(tool_results)`. Because
the verbatim bytes never enter `ctx.messages`, there is **no cached-prefix invalidation** (case-a) —
a pure forward append. Signature:
`apply_intra_turn_digest(ctx, tool_results, sidecar, *, trace_ctx=None, store, bus=None)`.

Per-entry eligibility (scan `tool_results`, not `ctx.messages`):
- skip if `tool_call_id` is **pinned** (D4 — see R2);
- skip if content missing / already a digest (`_is_existing_digest`) / fails `should_digest`
  (threshold, error-payload-verbatim, exclude-tools — PR-A);
- otherwise: `persist_tool_result` (concurrent, bounded by `put_timeout_ms`), `build_digest_message`,
  gate `digest_saves_enough`, replace `entry["content"]` in place, emit `record_digest`.

Remove `_digest_candidate_indices` (the `ctx.messages` scanner) — its post-extend scan is the defect.

### R2 — Keep the D4 read→write pin (birth-time-only; reconciled with ADR-0085)
`_update_pins` runs first on the current batch (unchanged): a `read` with a `path` is pinned; a
prior-round successful `write` to that path releases it (same-batch read+write defers; failed write
never releases; `pin_ttl_turns` abandonment). The birth pass **skips pinned entries**, so a `read`
the model may edit against stays **verbatim** in the batch.

**Codex Q2/Q6 — scope reconciliation (explicit).** ADR-0085 §D1 also describes digesting a *released*
pin on a later round (the deferred case-(b)). That requires a `ctx.messages`-after rewrite, which the
owner's directive **explicitly excludes** ("operate on the fresh `tool_results` list, not on
`ctx.messages` after") — because that rewrite is exactly the churn that failed the A/B. So this
redesign is **birth-time-only**: released/abandoned pins are simply never digested (the read stays
verbatim). This is **strictly ≥ baseline**: verbatim reads are the pre-FRE-475 behaviour, so they
cannot re-create the +34.9% (which came from case-(b) prefix rewrites, not from verbatim content).
The measured bulk is bash discovery (20 bash vs 9 read in `a0a07227`), so birth-time digestion of
non-read oversized results captures the ≥30% win without any churn. **Deferred released-pin
digestion is filed as a follow-up FRE**, and ADR-0085 §D1 needs a one-line scope note (adr session,
not this build PR) recording that the shipped first flag is birth-time-only.

### R3 — Executor: move the call before `extend`
In `step_tool_execution` (`executor.py:3463`), move the guarded call to **before**
`ctx.messages.extend(tool_results)` and pass `tool_results`:
```python
if settings.tool_result_compression_enabled:
    _store = get_artifact_store()
    if _store is not None:
        await apply_intra_turn_digest(ctx, tool_results, digest_sidecar, trace_ctx=trace_ctx, store=_store, bus=None)
ctx.messages.extend(tool_results)
```
Flag-off (default) ⇒ skipped ⇒ zero behaviour change. **FRE-476 coordination:** the diff is a 3-line
move localized at the shared `extend` site; FRE-476 has no open impl PR (only ADR #159 merged), so a
future decompose PR rebases cleanly.

## Out of scope — filed as follow-up
**Latent `litellm.UnsupportedParamsError: Anthropic doesn't support tool calling without tools=`**
(README "open bug"). Attribution: the forced-synthesis path (`executor.py:2275`) sets `tools=None`
and calls the model; on the Anthropic cloud path a transcript containing `tool_use`/`tool_result`
blocks requires `tools=` even for a no-tool synthesis call. **Digestion-independent** (baseline
`a0a07227` succeeded because it never hit forced synthesis; the treatment run did). It is a real
blocker for the A/B `zero task_failed` gate but a different subsystem (error-recovery/Anthropic
adapter) — file Needs-Approval, flag as an A/B dependency, do **not** bundle here.

## Tests (TDD — rewrite the keep-deferred suite to birth-time)
`tests/personal_agent/orchestrator/test_intra_turn_digest.py`:
- **Birth-time invariant (the fix):** a large `bash` entry in `tool_results` is digested *in place*
  by `apply_intra_turn_digest` before any `ctx.messages` mutation; assert the entry content is a
  digest and the full bytes were never appended verbatim (simulate extend after the pass and assert
  `ctx.messages[-1]["content"]` is the digest).
- **D4 pin:** a `read` entry stays verbatim (pinned); a non-read oversized entry is digested;
  same-batch read+write defers; failed write does not release; TTL release.
- error-payload / below-threshold / excluded-tool entries stay verbatim; idempotent re-pass;
  put-timeout → verbatim.
- Keep PR-A byte-stability suite, the `expand_tool_result` tests, and the `_sanitize_tool_pairs`
  survival test (digest still a well-formed `tool_result`).

## Validation
- **Build session:** unit tests above + `make test` / `mypy` / `ruff` / `pre-commit`. Flag default-off.
- **Post-deploy (master):** `uv run python scripts/eval/fre475_compression_ab/run_ab.py --email <owner> --profile cloud`
  with `AGENT_TOOL_RESULT_COMPRESSION_ENABLED=true` + rebuild. PASS = fresh ≥30% below 768,484 **and**
  artifact built **and** zero `task_failed`. The zero-`task_failed` gate depends on the filed
  forced-synthesis follow-up.
