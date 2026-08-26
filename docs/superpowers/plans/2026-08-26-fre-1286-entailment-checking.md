# FRE-1286 — Entailment checking: inline for entity-free predicates, sampled offline for the rest

**ADR:** [ADR-0138](../../architecture_decisions/ADR-0138-the-model-may-generate-but-may-not-assert.md)
D3(c)/(d), rejected Option 5, Consequences (residual failure mode) ·
[ADR-0087](../../architecture_decisions/ADR-0087-memory-recall-quality-measurement-program.md) owns the offline arm.
**Blocked by:** FRE-1282 (merged, #960) — `ContainmentOutcome.ENTAILMENT_REQUIRED` and
`CheckOutcome.ENTAILMENT_REQUIRED` already exist and already route here.

## What FRE-1282 left standing

`containment.check_containment` escalates a span whose claim unit has **no entity and no
figure** to `ENTAILMENT_REQUIRED`, *after* containment passes. `verification._verify_span`
maps that straight to `CheckOutcome.ENTAILMENT_REQUIRED` with the detail
`"…D3(d) must (FRE-1286, not yet available)"`. Under `enforce` that span blocks the turn
today — fail-closed, but it blocks *every* entity-free claim regardless of whether the
source supports it. This ticket makes the class decidable.

Two arms, per the ADR:

| Arm | Class | Where | Blocking |
|---|---|---|---|
| **Inline** | entity-free, figure-free spans (`ENTAILMENT_REQUIRED`) | turn path, D3(d) | yes, under `enforce` |
| **Offline, sampled** | containment-passing spans that are **not** in the inline class | background task after delivery | never — it feeds the eval set |

The two classes must be **disjoint**, and the plan review caught that they were not: once
`apply_entailment` turns an escalated span into `PASSED`, nothing on `SpanVerification`
distinguishes it from a span that passed containment outright, so the sampler would
re-sample the class the inline arm already judged. `SpanVerification` therefore carries
`entity_free_predicate`, copied from `ContainmentResult`, and the sampler takes its
complement.

The offline arm is the instrument for the residue containment cannot see: a correctly-cited
token embedded in a claim the source contradicts (*"not sold in France"* contains every
token of *"sold in France"*; *"some"* passes for *"all"*).

## Design decisions

**D-1 — The judge is a forced-tool model pass, following `ModelSpanExtractor` exactly.**
Same shape for the same reasons (FRE-996: `response_format` hides truncation on the
deployed Anthropic models). Same per-call nonce delimiters: the source content is fetched
web pages, i.e. attacker-influenced, and it is being handed to a model.

**D-2 — Four verdicts, not two.** `SUPPORTED` · `NOT_SUPPORTED` · `CONTRADICTED` ·
`UNDECIDED`. Contradiction is kept apart from plain non-support because AC-3 requires it to
be *detected* and because a wave of each means something different — the same discipline
D3 already applies to `unverifiable` vs `no_source`.

**D-3 — The judge sees a bounded, deterministically-selected excerpt.** A fetched page can
be 200KB. `select_excerpt` picks the window maximising hits of the claim's canonical tokens
(reusing `containment.normalize_tokens`, so the two checks agree on what a token is), so
the passage that made containment pass is the passage the judge reads. Deterministic —
no second model call to choose.

**D-4 — Inline latency is bounded by construction, not by hope.** Four bounds, and the plan
review was right that the first draft had only two of them:

- all escalated spans of one turn are judged in one `asyncio.gather`, so the added latency
  is **one** round-trip rather than N;
- every judge call carries an explicit `timeout_s`, derived from
  `grounding_entailment_latency_budget_ms`. Without it a hung provider call sits on the
  critical path indefinitely — `asyncio.gather` is only as fast as its slowest member;
- `MAX_OUTPUT_TOKENS = 512`, declared for the same reason the extractor declares one: the
  cost gate reserves against whatever ceiling is named;
- the check cap is **cumulative across D4 retries** (`ctx.grounding_entailment_checks`),
  not per pass. A per-pass cap bounds nothing when the loop may run the pass again.

Spans past the cap, and calls that time out or fail, become `ENTAILMENT_UNAVAILABLE` —
fail-closed and recorded, never silently passed.

**D-5 — The budget records, it does not abort.** With a per-call timeout in place the
worst case is already bounded, so an additional mid-flight abort would only convert a slow
provider into a refusal the user did not deserve. The measurement and its excess land on
the `GroundingRecord` and on the `grounding_verification_completed` log line (AC-5).

**D-5b — A judge outage blocks, deliberately, and this is written down.** Every non-`PASSED`
span fails `compliant`, so `ENTAILMENT_UNAVAILABLE` blocks under `enforce`. That is not a
new policy: before this ticket the same class blocked as `ENTAILMENT_REQUIRED`, so
fail-closed is the *status quo* being preserved, and D3 makes the class's inline settlement
part of the contract. It is deliberately unlike `_verify_grounding`'s extractor-failure
path, which delivers — there we know nothing about any span, so blocking would be blocking
on total ignorance; here every other gate has spoken. `enforcement.py`'s docstring records
the exception, and the outcome sits in the machine-undecided family so a wave of it reads
as our malfunction rather than as the model becoming honest.

**D-6 — The offline arm runs in `captains_log.background.run_in_background`, after the turn
is delivered.** "Offline" means *off the critical path*, which that is. Specifics the plan
review was right to demand:

- **Unit:** one span. **Algorithm:** an independent Bernoulli draw per eligible span at
  `grounding_entailment_sample_rate`, `random.random()` — not stratified, not per-turn, so
  a turn with many spans contributes proportionally.
- **Scheduled on the delivery branch only.** The `RETRY_WITH_FORCED_RETRIEVAL` branch
  returns to `LLM_CALL` before the scheduling point is reached, so a retried turn is
  sampled once, against its final reply.
- **Failure is a lost sample, never a lost turn.** `run_in_background` already logs and
  swallows task errors; there is no retry, and dropping a failed sample is unbiased with
  respect to the verdict it would have produced.
- **The emitted row is adjudicable, not just countable.** `grounding_entailment_sample`
  carries `answering_model`, `judge_model`, `verdict`, `miss`, **the claim text, the source
  identifier and the excerpt the judge read**, so the eval program can re-adjudicate a
  disputed row instead of inheriting a bare boolean. `scripts/eval/fre1286_entailment/miss_rate.py`
  runs the per-model aggregation (AC-4's "without hand-computation").

Elasticsearch is where this project's durable evidence already lives — the captain's log
and the ADR-0125 turn records — and AC-4 states its own check as "telemetry over a period",
so a second, file-based eval-set sink is deliberately **not** built here.

**D-7 — No new budget lane.** The real `budget.yaml` is gitignored (FRE-1209), so a new lane
would pass CI against `.example` and then fail `validate_role_totality` at startup on the
box. `entailment` maps to `entity_extraction`, exactly as FRE-1281 did for
`span_extraction`, with the split left as master's config edit.

**D-8 — No separate enable flag.** D3 makes the inline arm part of the contract for its
class; gating it separately would let a deploy run a D3 that is not D3. It runs whenever
`grounding_verification_mode != "off"`, like every other gate.

**Out of scope, deliberately:** promoting entailment inline for *all* spans (that is the
future decision the ADR says this arm's measured rate is evidence for).

## Failure-family placement (AC-6 of FRE-1282 still holds)

| Outcome | Family | Why |
|---|---|---|
| `NOT_ENTAILED` | **neither**, with `NOT_CONTAINED` | citation theatre caught, one level up |
| `CONTRADICTED_BY_SOURCE` | **neither**, with `NOT_CONTAINED` | same, and worse |
| `ENTAILMENT_UNAVAILABLE` | machine-undecided | our machinery could not decide |
| `ENTAILMENT_REQUIRED` | machine-undecided | escalated, judge never ran |

The first draft put the two rejection outcomes in `true_no_source` and the plan review was
right to push back — harder than it did. `true_no_source` means the turn had **no
admissible source at all** (`UNCITED`, `UNRESOLVED`, `SOURCE_NOT_ENTITLED`); a source that
exists, is entitled, is reachable and contains every token but does not support the claim is
not that. It is exactly what `NOT_CONTAINED` already is, found one gate later, and
`NOT_CONTAINED` sits in neither family. Filing it under `no_source_count` would quietly
change what that counter means.

The `unverifiable` property is hard-coded to a single member today; this adds
`_MACHINE_UNDECIDED` beside the existing `_TRUE_NO_SOURCE`, so both families are named sets
rather than one set and one literal. A judge outage must never read as the model becoming
honest.

## Steps

1. **`src/personal_agent/grounding/entailment.py`** (new) — `EntailmentVerdict`,
   `EntailmentJudgement`, `SYSTEM_PROMPT`, `entailment_tool()`/`_choice()`,
   `select_excerpt()`, `parse_judgement()`, `EntailmentJudge` Protocol,
   `ModelEntailmentJudge`.
   *verify:* `uv run pytest tests/personal_agent/grounding/test_entailment.py -q`
2. **`verification.py`** — three new `CheckOutcome` members; `_MACHINE_UNDECIDED` beside
   `_TRUE_NO_SOURCE`; `entity_free_predicate` on `SpanVerification`; three new
   `TurnVerification` fields (`entailment_checks`, `entailment_latency_ms`,
   `entailment_budget_exceeded`); `async apply_entailment(...)`; `build_grounding_record`
   carries the new fields; the stale "not yet available" detail replaced.
   *verify:* `uv run pytest tests/personal_agent/grounding/test_verification.py -q`
3. **`captains_log/turn_evidence.py`** — the three fields on `GroundingRecord`.
4. **`grounding/entailment_sampling.py`** (new) — `select_offline_samples(verification,
   registry, rate)` and `async score_offline_samples(...)` emitting the sample log line.
   *verify:* `uv run pytest tests/personal_agent/grounding/test_entailment_sampling.py -q`
5. **`config/settings.py`** — `grounding_entailment_max_inline_checks` (8, cumulative
   across D4 attempts), `grounding_entailment_latency_budget_ms` (4000, doubling as the
   per-call `timeout_s`), `grounding_entailment_sample_rate` (0.05),
   `grounding_entailment_max_excerpt_chars` (6000).
   Regenerate `docs/reference/CONFIG_INVENTORY.md`.
6. **`llm_client/types.py`** + **`config/model_roles.yaml`** + **`cost_gate/role_map.py`** —
   the `entailment` role, bound to `claude_sonnet`, lane `entity_extraction`.
   *verify:* `uv run pytest tests/test_llm_client/test_types.py tests/personal_agent/cost_gate/test_role_map_totality.py tests/scripts -q`
7. **`orchestrator/executor.py`** — `_verify_grounding` runs `apply_entailment`;
   `_record_grounding` logs the new fields; `step_synthesis` schedules the offline arm in
   the background after the D4 decision.
   *verify:* `uv run pytest tests/personal_agent/orchestrator -k grounding -q`
8. **`scripts/eval/fre1286_entailment/`** — `corpus.yaml` (labelled pairs across
   `supported` / `not_supported` / `contradicted` / `quantifier_reversal`, each carrying a
   `dev` / `heldout` partition), `corpus.py`, `metrics.py` (accuracy, per-class detection
   rate, false-rejection rate, and the preregistered bars **as numbers**), `harness.py`
   (I/O driver), `miss_rate.py` (AC-4 query), `README.md` (bars + ADR-0087 ownership and
   the remediation route when the miss rate moves). Pure core unit-tested; the driver is
   run by hand, the FRE-1281 split.
   *verify:* `uv run pytest tests/personal_agent/grounding/test_entailment_corpus.py -q`
9. **Gates:** `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files`; then self-review (`feature-dev:code-reviewer` +
   `security-review` — the diff parses fetched page content and builds a model prompt from
   it).
10. **Measured run** of the AC-3/AC-6 harness against the real judge, for the handoff.

## Acceptance criteria → evidence

| AC | Evidence |
|---|---|
| **AC-1** non-supporting source rejected inline | `test_verification.py::test_entity_free_span_with_non_supporting_source_is_rejected` — the mercury case, stub judge returning `NOT_SUPPORTED`, asserts `CheckOutcome.NOT_ENTAILED` and that `decide()` blocks it |
| **AC-2** supporting source passes | `…::test_entity_free_span_with_supporting_source_passes` — same span, `SUPPORTED`, asserts `PASSED` and `decide()` delivers. Both arms present, so a reject-everything judge fails AC-2 |
| **AC-3** contradiction + quantifier reversal detected | corpus classes `contradicted` and `quantifier_reversal`, **bars preregistered as numbers** in `metrics.py` (contradiction detection ≥ 0.90, quantifier-reversal detection ≥ 0.80); measured rates from the harness run reported in the handoff; unit tests prove `CONTRADICTED` routes to `CONTRADICTED_BY_SOURCE` |
| **AC-4** offline arm samples at its rate, miss rate queryable per model | `test_entailment_sampling.py`: rate 0.0 → none; 1.0 → every eligible span; **rate 0.5 over 1000 spans against a seeded `Random` → an unbiased per-span draw within tolerance**; the inline class is **excluded**; the emitted row carries model, verdict, miss, claim, identifier and excerpt. Plus `test_executor_grounding.py::test_offline_sample_is_scheduled_on_delivery_not_on_retry`, driving `step_synthesis` and awaiting `wait_for_background_tasks()`. Plus `miss_rate.py` |
| **AC-5** no latency cliff | the bound is structural and asserted: `…::test_judge_call_carries_the_configured_timeout` (a hung judge cannot outlive the budget), `…::test_inline_checks_are_capped_across_retries`, and `…::test_records_latency_and_budget_excess` (0 ms budget → `entailment_budget_exceeded` on record and log line). Worst-case added latency = one round-trip ≤ `timeout_s`, stated in the handoff |
| **AC-6** judge's own error rate measured | `test_entailment_corpus.py` on the pure metrics core; corpus split `dev` / `heldout` as FRE-1281 does, with the held-out partition scored once; measured accuracy and per-class figures from the harness run in the handoff |

## Risks

- **The judge is a model on the critical path** — the exact reason Option 5 was rejected.
  Mitigated by class-restriction (a minority of spans), the concurrency + cap, the latency
  record, and AC-6 measuring it rather than assuming it.
- **Prompt injection through source content** — nonce delimiters, forced-tool enum output,
  explicit instruction that the passage is data.
- **Cost** — `entity_extraction`'s lane absorbs it; `grounding_verification_mode` is `off`
  by default, so nothing changes on deploy until master flips it.
