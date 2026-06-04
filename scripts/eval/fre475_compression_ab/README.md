# FRE-475 — Intra-Turn Tool-Result Compression A/B (ADR-0085)

Reproducible before/after harness for the intra-turn tool-result digest feature.
Measures **total fresh (full-price) input tokens** over one artifact-build turn —
the cost the digestion is meant to reduce — and compares against the recorded
`a0a07227` baseline on the same prompt.

## Reference traces

| Trace | Arm | Fresh input | Artifact | Notes |
|---|---|--:|---|---|
| `a0a07227-121b-4ccb-871b-45072a32ccb0` | flag-off (baseline) | **768,484** | ✅ built | the original forensics turn |
| `5f2d1277-0d26-420b-811f-719d5b15bd6e` | flag-on, keep-deferred (PR-B) | **1,036,347** | ❌ failed | **−34.9% WORSE**; case-(b) cache churn |

## The finding (why arm 1 failed)

PR-B's `apply_intra_turn_digest` runs **after** `ctx.messages.extend(tool_results)`
and digests tool messages **older** than the keep window. Every such digest is a
**case-(b) deferred rewrite** (ADR-0085 §D1): it mutates bytes already in the
cached prefix → invalidates the prefix from that point → forces a re-cache. The
14 reinvalidations cost more than the 37k content-tokens they removed, so fresh
input went *up*. `cache_read` ballooned 108k → 162k (the churn signature).

ADR-0085 §D1 was explicit that only **case-(a) birth-time digestion** — digesting
*before* the verbatim bytes ever enter `ctx.messages` — yields the no-invalidation
win. The infra (PR-A) and `expand_tool_result` (used 3×) are sound; the
**placement** is the defect.

## Redesign contract (for the build session)

- Digest non-pinned oversized results **at insertion, before** `ctx.messages.extend`
  (operate on the freshly built `tool_results` list, not on `ctx.messages` after) so
  the verbatim bytes never enter the cached prefix.
- Keep the byte-stability fixed point (PR-A) and the D4 read→write pin (a pinned read
  is the *one* legitimate case-(b), economically gated by `min_savings`).
- Re-run this harness; PASS = fresh input ≥30% below baseline **and** artifact built
  **and** no `task_failed`.

## Open bug surfaced by the run (attributed → FRE-484)

`litellm.UnsupportedParamsError: Anthropic doesn't support tool calling without
tools= param specified` killed the turn (`model_call_error → task_failed`).
**Attributed (FRE-484): latent, digestion-independent** — the forced-synthesis path
(`executor.py:2275`) sets `tools=None`, and Anthropic rejects that when the transcript
already holds `tool_use`/`tool_result` blocks. Baseline `a0a07227` never hit forced
synthesis; the treatment run did. It is a **hard blocker for the `zero task_failed`
gate** below — the birth-time redesign re-validation cannot go fully green until
FRE-484 lands.

## Redesign shipped (birth-time, case-a)

The placement fix digests non-pinned oversized results **on the fresh `tool_results`
list before `ctx.messages.extend`** (`apply_intra_turn_digest`), so verbatim bytes
never enter the cached prefix. Reads stay verbatim (pinned); deferred digestion of
released pins is **birth-time-only out of scope → FRE-485** (with an ADR-0085 §D1
scope note).

## Run

```bash
# baseline arm: deploy with the flag off (default), then drive a turn
uv run python scripts/eval/fre475_compression_ab/run_ab.py --email <owner-email> --profile cloud

# treatment arm: AGENT_TOOL_RESULT_COMPRESSION_ENABLED=true in .env + rebuild, then re-run
ENV=cloud make rebuild SERVICE=seshat-gateway
uv run python scripts/eval/fre475_compression_ab/run_ab.py --email <owner-email> --profile cloud

# extract-only against an existing trace (no /chat traffic)
uv run python scripts/eval/fre475_compression_ab/run_ab.py --extract-trace 5f2d1277-0d26-420b-811f-719d5b15bd6e
```

**Identity:** pass the deployment owner's own CF-Access email. Never the injected
Claude Code `userEmail`.
