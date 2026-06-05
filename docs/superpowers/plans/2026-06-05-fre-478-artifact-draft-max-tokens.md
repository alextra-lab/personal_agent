# FRE-478 — Artifact-draft sub-agent `max_tokens` cap (config + cap-hit observability)

> **Ticket:** FRE-478 (Approved, Tier-3:Haiku, Bug) · Project: *Turn Cost & Latency Optimization (artifact builds)*
> **Refs:** `docs/research/2026-06-04-artifact-turn-cost-latency-forensics.md` · FRE-469 (origin) · FRE-476 (structural sectioning, separate)

## Problem (measured — trace a0a07227, 2026-06-04)

The artifact-draft sub-agent hit its hardcoded `max_tokens` ceiling of **16,384** in round 21
(`output_tokens=16384`, 96s) and spilled into a continuation call in round 22
(`output_tokens=14,835`, 214s — worst single call of the turn). The full artifact was
~**31,219** output tokens, generated in two chunks *not by design* but because it ran out of
output budget — inflating latency and risking seam artifacts.

`_DRAFT_MAX_TOKENS = 16384` is a module constant in `tools/artifact_tools.py:647`, passed
explicitly at the two `respond()` call sites (lines 1149, 1160), overriding the model-default
`max_tokens`.

## Fix (minimal, independent win — the structural sectioning is FRE-476)

1. **Parameterize** the cap via `settings.artifact_draft_max_tokens` (default **32768**), resolved
   at call time (mirrors the existing `_draft_timeout_s()` pattern — no import-time freeze, env-overridable).
2. **Add cap-hit observability**: after the sub-agent call, compare actual `completion_tokens`
   against the cap; emit a `artifact_draft_output_cap_hit` warning when the cap binds, and include
   `output_tokens` in the existing `artifact_draft_sub_agent_complete` log so per-round output is
   reportable (AC #2).

### Why default 32768 (justified against observed sizes)

- Observed full artifact ≈ 16,384 + 14,835 = **31,219** output tokens → a 32,768 cap fits a typical
  artifact in **one** generation call (the prior 16,384 cap did not).
- 32,768 is a natural, non-arbitrary ceiling: cloud `claude-sonnet-4-6` model def declares
  `max_tokens: 32768` (`config/models.cloud.yaml`), and local `sub_agent` declares
  `context_length: 32768` (`config/models.yaml`). The cap won't exceed either backend's headroom.
- **No cost-reservation impact**: `llm_client/cost_estimator.py` sizes the reservation against
  `min(max_tokens, default_output_tokens)`, so raising `max_tokens` does not enlarge the budget hold.

## Atomic steps

### Step 1 — Failing tests first (TDD)
File: `tests/personal_agent/tools/test_artifact_tools.py`
- **Update** `test_artifact_draft_calls_respond_with_correct_max_tokens`: assert
  `call["max_tokens"] == settings.artifact_draft_max_tokens` (no longer literal 16384).
- **Add** `test_artifact_draft_max_tokens_is_configurable`: monkeypatch
  `artifact_tools.settings.artifact_draft_max_tokens` (or patch the resolver) to a sentinel value
  and assert `respond()` receives it.
- **Add** `test_artifact_draft_logs_output_cap_hit_when_cap_binds`: fake sub-agent returns
  `usage.completion_tokens == cap`; assert a `artifact_draft_output_cap_hit` warning is emitted
  (capture via `structlog` testing capture / caplog or a log spy consistent with existing tests).
- **Add** `test_artifact_draft_no_cap_hit_log_under_cap`: usage below cap → no cap-hit warning.

Run (expect failures): `make test-k K=artifact_draft`

### Step 2 — Config setting
File: `src/personal_agent/config/settings.py` (artifact substrate block, ~line 544)
```python
artifact_draft_max_tokens: int = Field(
    default=32768,
    ge=1024,
    le=65536,
    description=(
        "Max output tokens for the artifact-draft HTML sub-agent (ADR-0077). "
        "Raised from 16384 (FRE-478): observed artifacts reached ~31k output "
        "tokens (trace a0a07227), forcing an unintended cap-hit + continuation "
        "call. 32768 fits a typical artifact in one generation and matches the "
        "cloud claude-sonnet-4-6 model-def ceiling. Reservation is unaffected "
        "(cost_estimator uses min(max_tokens, default_output_tokens))."
    ),
)
```
(Bounded `ge=1024, le=65536` per Codex review — `65536` is the headroom ceiling above the
cloud sonnet `max_tokens: 32768` declaration; `AGENT_` env prefix wiring is automatic via the
`BaseSettings` base.)

### Default value rationale (owner-confirmed, 2026-06-05)
Worst-case input ≈ **4,700 tokens** (system prompt ~504 + plan ≤ `_MAX_PLAN_CHARS` 16000c ≈ 4000 +
title/summary/wrapper ~200). **Owner confirmed the local SLM server is loaded with a real ~65k
context window** (the `sub_agent.context_length: 32768` in `config/models.yaml` is stale metadata,
not the served value), and the cloud path is effectively unbounded. So a flat **32768** output cap
is safe on both: local 32768 + ~5k input ≈ 37k < 65k; cloud fits the observed 31,219-token artifact
and matches its model-def `max_tokens`. No context-aware clamp — it would only read the stale 32768
config value and *under*-utilize the real local context. The cap-hit warning (Step 4) is the
trip-wire: if 32768 still binds often, raise the cloud ceiling toward Sonnet's 64k or do structural
sectioning (FRE-476). The `models.yaml` context_length drift is noted, not changed here
(deploy-affecting, separate concern).

### Step 3 — Resolver + call sites
File: `src/personal_agent/tools/artifact_tools.py`
- Remove `_DRAFT_MAX_TOKENS = 16384` (line 647); add resolver near `_draft_timeout_s()`:
```python
def _draft_max_tokens() -> int:
    """Output-token ceiling for the artifact-draft HTML sub-agent (FRE-478).

    Resolved from config at call time so the cap is env-overridable without an
    import-time freeze. See ``settings.artifact_draft_max_tokens`` for the value
    rationale.

    Returns:
        Maximum output tokens for the generation call.
    """
    return int(settings.artifact_draft_max_tokens)
```
- Compute `draft_max_tokens = _draft_max_tokens()` once near `draft_timeout = _draft_timeout_s()`.
- Replace both `max_tokens=_DRAFT_MAX_TOKENS` (the `artifact_draft_sub_agent_start` log + the
  `respond()` call) with `max_tokens=draft_max_tokens`.

### Step 4 — Cap-hit observability
File: `src/personal_agent/tools/artifact_tools.py` (after `html_content` extraction, ~line 1206)
- Read `output_tokens = (response.get("usage") or {}).get("completion_tokens")` (response is the
  `LLMResponse` TypedDict; `usage: dict[str, Any]`).
- Add `output_tokens=output_tokens` to the success `artifact_draft_sub_agent_complete` log.
- Guard the type (Codex review — `usage` is `dict[str, Any]` and may be `{}` / non-int): warn only
  when `isinstance(output_tokens, int) and output_tokens >= draft_max_tokens`. Emit:
```python
log.warning(
    "artifact_draft_output_cap_hit",
    trace_id=trace_id,
    session_id=session_id,
    span_id=span_id,
    task_id=task_id,
    output_tokens=output_tokens,
    max_tokens=draft_max_tokens,
)
```
(ADR-0074: trace_id/session_id/span_id threaded.)

### Step 5 — Quality gates
- `make test-k K=artifact_draft` (green) → `make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py` → `make test`
- `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`

## Acceptance criteria mapping
- **AC1** (max_tokens parameterized via settings, value justified) → Steps 2–3.
- **AC2** (re-run reports output_tokens per round; completes without unintended cap-hit *or*
  deterministic continuation) → Step 4 adds the per-round output_tokens + cap-hit signal; the
  **live re-run is a post-deploy verification** (master's role — Linear comment after merge, not
  the PR checklist, per lifecycle-rules).
- **AC3** (`make test` / `make mypy` clean) → Step 5.

## Out of scope
- Structural sectioning / decomposition of the artifact → **FRE-476**.
- Deterministic continuation-from-marker handling → deferred unless cap-hit telemetry shows the
  32768 cap still binds in practice (would be a follow-up ticket).
```
