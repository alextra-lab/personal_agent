# FRE-1570 — Span extraction stops at its 4,096-token output limit

> Ticket: FRE-1570 (Tier-2, bugfix, stream build1). Backing design: ADR-0138 D1 (layer 2 tiles,
> layer 3 fails closed). Diff class: Standard (touches `src/` logic in `grounding/`; no schema,
> no cap value, no deleting code).

## 1. What happens today at the limit (scope item 1)

Measured on the live substrate, 2026-10-10. Postgres `api_costs` (authoritative) and ES
`agent-logs-*` agree exactly on the 7-day window: 154 calls, USD 5.418, 21 calls at 4,096.

| Fact | Evidence |
|------|----------|
| 13.4% of calls stop at the limit (35 of 261 in 30 days; 21 of 154 in 7 days). | `api_costs`, `purpose = 'span_extraction'`, `output_tokens >= 4096`. |
| Thinking tokens are not the cause. | `reasoning_tokens` sum 0, `anthropic_thinking_present` true on 0 calls. The limit binds on tiling output only. |
| The model returns a parseable, partial tiling. | `span_extractor_payload_unparseable` fired 0 times in 30 days. `span_extractor_unanchored_quotes` fired 5 times. |
| The uncovered tail becomes a coverage gap, then a non-exempt span. | `span_policy._fill_gaps` emits a `COVERAGE_GAP` span for each uncovered run of text, and sets `degraded` when that run holds non-whitespace. A stop that happens to leave only whitespace uncovered is not degraded. |
| The gap span is judged as one claim. | `verification._identifier_for` binds the first marker-bound text region that overlaps the span. With no such region the span is `uncited`. |
| Capped turns never settle as compliant. | 35 of 35 `degraded_extraction`, 0 of 35 `first_generation_compliant`. Uncapped: 104 of 226 degraded, 31 of 226 compliant. |
| All 35 capped turns ran in `observe` mode. | Nothing was blocked. The effect is on the FRE-1284 compliance record. In `enforce` mode the same turns would get a cite-only retry. |
| The stop is not attributed. | `extract()` never reads `finish_reason`. `degraded_extraction` is also true on 46% of uncapped turns, so it cannot separate truncation from other gaps. |

So the ticket's guess is half right. The error is not silent in the data model: layer 3 fails
closed and sets `degraded`. But the cause is not recorded anywhere, and the effect is a false
failure on exactly the long answers.

Correction to the ticket: `entailment.py:365` does not use the extractor's constant. It has its
own `MAX_OUTPUT_TOKENS = 512` for one verdict. No change there.

## 2. Options and decision (scope item 2)

Throughput on this role is 144 tok/s median (api_costs, 35 at-limit calls: p50 28 s, max 37 s).

Needed output is about 1.4x the input text in tokens (ratio on uncapped calls: p50 1.37,
p90 1.62, max 2.09, over `input_tokens - 2182` of system prompt). Predicted need of the 35
capped calls: median 5.7k to 6.8k, maximum 15k to 18k tokens.

| Option | Share of calls still cut (30-day sample) | Latency of the longest call | Verdict |
|--------|------------------------------------------|------------------------------|---------|
| Ceiling 8,192 | 3.8% (p50 ratio) to 4.6% (p90 ratio) | 57 s | Fails AC-4 (2%). |
| Ceiling 12,288 | 0.4% to 2.3% (3.8% at the worst ratio) | 85 s | Borderline. |
| **Ceiling 16,384** | **0.0% to 0.4% (1.5% at the worst ratio)** | **114 s** (no catalog timeout applies; litellm's 600 s default is the only bound) | **Chosen.** |
| Split a long reply into chunks | About 0% | Bounded near 25 s, parallel | More code, a second source of cross-chunk context loss. Held in reserve. |
| Compact tool output (offsets instead of quotes) | Unknown | Lower | Changes the measured FRE-1281 contract (15 of 16 bars). Not a bugfix. |

Reachability (Codex plan review, checked in code): the role has no override, the catalog
ceiling for `claude_sonnet` is 128,000, and `litellm_client` forwards an explicit `max_tokens`
without a clamp. The cloud path uses litellm's HTTP handler, not the Anthropic SDK, so the
SDK's non-streaming size check does not apply. No call above 4,096 tokens has run on this role
yet, so the first one is observed after deploy (section 6).

The change has two parts:

1. `MAX_OUTPUT_TOKENS` 4,096 → 16,384.
2. Detect a stop at the limit and log it (`span_extractor_truncated`). The 0 to 1.5% of calls
   that still stop become an explicit, attributable gap instead of an anonymous one.

Not changed, and why: layer 3 stays fail-closed (ADR-0138 D1). Whether a truncated turn must be
recorded as `unverified` instead of as non-compliant is an ADR question for the owner. It is
named in the handoff.

## 3. Cost (scope item 3, AC-3)

**Reservation does not change.** `cost_estimator.estimate_reservation` reserves
`min(max_tokens, role.default_output_tokens)`. The `span_extraction` lane sets
`default_output_tokens: 1024` in both the example and the live `budget.yaml`. So the reserved
output is `min(16384, 1024) = min(4096, 1024) = 1024` tokens before and after. The comment on
`MAX_OUTPUT_TOKENS` ("reserves against whatever ceiling is declared") is wrong, and the plan
corrects it.

| Per call | Before | After |
|----------|--------|-------|
| Reserved, typical call (3,953 input tokens) | USD 0.0303 | USD 0.0303 |
| Reserved, at-limit call (7,642 input tokens) | USD 0.0414 | USD 0.0414 |

**Spend changes only on calls that were cut.** Extra output is `(needed - 4096) x USD 15 / MTok`:

| Basis | Extra spend, 30 days | Per cut call | Per call overall |
|-------|----------------------|--------------|------------------|
| Ratio p50 (1.37) | USD 1.40 | USD 0.040 | USD 0.0054 |
| Ratio p90 (1.62) | USD 2.01 | USD 0.057 | USD 0.0077 |

Scaled to a week (21 of 35 cut calls): about USD 0.84 to 1.21 per week.

**Cap headroom, flagged for the owner.** No cap value is changed in this PR. The caps in the
running gateway container (`/app/config/governance/budget.yaml`, read 2026-10-10) are USD 5.00
per day and USD 7.00 per week for `span_extraction` (owner-confirmed 2026-08-28). The
worktree copy of that file holds placeholder values (USD 1 and 5) and is not the live source.
The last 7 days spent USD 5.418. The fix adds up to USD 1.21, so a week at today's rate ends
near USD 6.63. A burst day (2026-10-09: USD 2.716) stays under the daily cap. A denial is
`on_denial: deliver`: the turn is delivered and recorded as unverified. If the owner wants more
headroom, that is a cap change that needs the owner's word.

**The gate checks a cap only before the call.** `cost_gate.gate.commit()` adds
`actual - reserved` to every counter with no second cap check (Codex plan review, confirmed in
`gate.py:303`). A call admitted under a cap can finish above it. This already holds at 4,096.
The ceiling is the bound on the overshoot of one call: about USD 0.25 of output at 16,384,
against about USD 0.06 at 4,096. Both are small against USD 5.00 per day. The plan does not
change the gate. Whether a pre-call cap check must reserve the ceiling instead of the lane
default is a governance question for the owner. It is named in the handoff.

## 4. Steps (TDD)

Branch: `fre-1570-span-extraction-output-limit`. One phase, one PR.

### Step 1 — failing tests

File: `tests/personal_agent/grounding/test_extractor.py`.

Extend `StubClient` so a test can set `finish_reason` and `usage` on the response. Default
behaviour stays the same, so existing tests do not change.

New tests:

| Test | Proves |
|------|--------|
| `test_call_is_bounded_and_forced` (edit) | The call sends `MAX_OUTPUT_TOKENS`, not a literal. Passes before the fix by design: it follows the constant. |
| `test_ceiling_is_above_the_measured_cut_off` | `MAX_OUTPUT_TOKENS >= 16_384`. A change-detector: lowering the ceiling must update the evidence too. |
| `test_reservation_does_not_depend_on_the_ceiling` | `estimate_reservation` for `span_extraction` gives the same amount at 4,096 and at the literal 16,384 (AC-3). A guard on the lane default. Passes before the fix by design. Uses `load_budget_config_for_tests()`. |
| `test_stop_at_the_limit_leaves_the_tail_as_a_logged_gap` | AC-1. A stub returns the first segments of a three-sentence reply with `finish_reason="length"`. The tail is a `COVERAGE_GAP` non-exempt span, `degraded` is true, and `span_extractor_truncated` is logged with `trace_id`. |
| `test_cut_off_json_is_a_whole_reply_gap_and_logged` | AC-1. A stub returns JSON cut mid-segment with `finish_reason="length"`. No span is trusted, all text is a gap, truncation is logged. |
| `test_token_count_at_the_ceiling_counts_as_a_stop` | `finish_reason` is absent, `usage.completion_tokens == MAX_OUTPUT_TOKENS`. Truncation is logged (second signal, as in `session_summary._reject_if_truncated`). |
| `test_a_complete_reply_is_not_reported_as_truncated` | Seeded negative. `finish_reason="tool_calls"` or `"stop"`, tokens below the ceiling: no `span_extractor_truncated` event. |
| `test_a_long_reply_is_tiled_completely` | AC-2. A reply of about 6,000 words and a stub that tiles it fully. The extraction covers every character, `degraded` is false, and the request carried a ceiling above 4,096. |

Run: `make test-file FILE=tests/personal_agent/grounding/test_extractor.py`. Expected: the
truncation tests fail (no event, constant is 4,096).

### Step 2 — implementation

File: `src/personal_agent/grounding/extractor.py`.

1. Set `MAX_OUTPUT_TOKENS = 16_384`. Rewrite its docstring: state the measured sizing, state
   that the reservation is `min(ceiling, lane default)` and so does not move, and state why the
   value is bounded (latency and runaway output).
2. Add `_TRUNCATION_FINISH_REASONS = frozenset({"length", "max_tokens"})` and
   `_hit_output_limit(response)` (finish reason, then `usage.completion_tokens`).
3. In `ModelSpanExtractor.extract`, after `apply_policy`, if the call hit the limit, log
   `span_extractor_truncated` at `warning` with `trace_id`, `max_tokens`, `finish_reason`,
   `output_tokens`, and `uncovered_chars` (sum of `COVERAGE_GAP` span lengths). Return the same
   extraction as today.
4. Update the module note on truncation so it names the log event.

No change to `spans.py`, `span_policy.py`, `verification.py` or any config file.

### Step 3 — gates

```
make test-file FILE=tests/personal_agent/grounding/test_extractor.py
make test
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

Then commit, and run `feature-dev:code-reviewer` on `git diff origin/main...HEAD`.

## 5. Acceptance criteria

| AC | Proof on this branch | Proof after deploy |
|----|----------------------|--------------------|
| AC-1 stop behaviour known | `test_stop_at_the_limit_leaves_the_tail_as_a_logged_gap` and `test_cut_off_json_is_a_whole_reply_gap_and_logged` show the tail as `COVERAGE_GAP`, `degraded`, logged. Section 1 gives the live evidence. | `span_extractor_truncated` events in ES. |
| AC-2 long replies covered | `test_a_long_reply_is_tiled_completely` (complete tiling). Residual stops are an explicit `span_extractor_truncated` event, kept by design. | Share of calls at the limit (AC-4). |
| AC-3 cost stated | Section 3 above, with `test_reservation_does_not_depend_on_the_ceiling`. Numbers come from `api_costs`. | `api_costs` spend per call for 7 days after deploy. |
| AC-4 live | Not provable before deploy. Prediction: 0.0% to 0.4% (1.5% at the worst ratio). | See section 6. |

## 6. Post-deploy runbook (master)

Deploy class: `seshat-gateway` rebuild (strict class). Python-only change, no migration.

Verify after 7 days, from Postgres (authoritative):

```
SELECT count(*) AS calls,
       count(*) FILTER (WHERE output_tokens >= 16384) AS at_limit,
       round(100.0 * count(*) FILTER (WHERE output_tokens >= 16384) / count(*), 1) AS pct
FROM api_costs
WHERE purpose = 'span_extraction' AND timestamp > <deploy time>;
```

Expected: `pct` at or below 2.0. Also count `span_extractor_truncated` events in
`agent-logs-*` (provisional: ES loses events, FRE-1051). Also check the p95 of `latency_ms` for
the same calls: expected below 120 s.

## 7. Risks

| Risk | Handling |
|------|----------|
| A 16k tiling runs near 114 s on the turn path, and nothing but litellm's 600 s default bounds it. | The user waits that long on the longest replies, which today wait about 28 s for a broken result. A call that fails returns `unavailable`, recorded as unverified, never as a pass. Rare (under 2% of calls). Chunking is the held option if the post-deploy p95 latency check fails. |
| Spend rises toward the weekly cap. | Section 3. Owner decides on any cap change. |
| The count of calls at exactly the ceiling is a weak "was it cut" signal. | The log uses `finish_reason` first, the token count second. |
