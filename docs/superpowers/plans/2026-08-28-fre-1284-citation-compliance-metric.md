# FRE-1284 — Per-model citation-compliance metric (ADR-0138 D5)

**Ticket:** FRE-1284 (Approved, `stream:build1`, `Tier-1:Opus`)
**Backing ADR:** ADR-0138 D5 (metric definition, confounding, staleness)
**Related:** ADR-0087 (measurement program) · FRE-1282 (verification — Done, supplies the
outcome) · FRE-1285 (enforcement selection — the consumer, blocked by this)

---

## Scope (3–5 bullets)

- Define the D5 compliance metric as a **pure, testable function** over per-turn grounding
  outcomes: numerator = turns where *every* non-exempt span passed on first generation with no
  D4 retry; denominator = turns with ≥1 non-exempt span **and retrieval not pre-forced**.
- Give it a **durable per-model observation store** (Postgres), because FRE-1285 must read the
  rate inline before generation and an ES/capture read cannot serve a control-plane signal.
- Windowing: rolling window of configured size, configured **minimum sample count**, and
  **maximum window age** — under either bound the model is `unmeasured`, never a rate.
- Pre-register the bars in committed config (AC-6), so the parameters exist in git history
  before any result is seen.
- Wire the observation write into the executor's existing `_record_grounding`, best-effort and
  off the critical path.

**Explicitly out of scope (FRE-1285's ticket):** light/heavy selection, probation sampling,
the promote/demote hysteresis band, cooldown. This ticket produces the *reading*; FRE-1285
decides what to do with it.

## Codex plan-review — findings and dispositions (2026-08-28)

| # | Finding | Disposition |
|---|---|---|
| 1 | **AC-1 as planned is circular** — a hand-authored `GroundingRecord` fixture scores the extractor's own output; ADR-0138 AC-1 requires spans "identified by the **independent labelling** of AC-7's corpus, not by the system's own extractor". | **Accepted, plan changed.** New corpus `scripts/eval/fre1284_compliance/`, dev/heldout partitioned. The harness builds a `SpanExtraction` **from the corpus's independent labels**, then runs the **real** `parse_citations` → `verify_turn` → `build_grounding_record` → metric. `verify_turn` is pure and synchronous, so this is deterministic and needs no LLM. See D-e. |
| 2 | Query ordering not pinned — an index does not define SQL result order. | **Accepted.** `ORDER BY observed_at DESC, id DESC`. |
| 3 | Plan calls the store "durable" but writes are best-effort/background — a dropped write silently shrinks the denominator. | **Accepted, partially.** A failed write is logged at ERROR and never swallowed. Not fail-closed: a DB write failure is uncorrelated with whether the turn complied, so a dropped observation is missing-at-random and does not inflate; failing the user's turn over our own bookkeeping is the reasoning `enforcement.py` already rejects. |
| 4 | **Answering-model misattribution** — `resolve_role_target(role)` is the *role* key, so an attachment-routed turn credits one model's turns to another. | **Accepted.** `ctx.answering_model_key` is stamped at the answering call site with the `effective_model_key` actually used. Recomputing at finalize was rejected: `_effective_attachment_routing_key` can raise. |
| 5 | Excluding verification-unavailable turns may not be missing-at-random. | **Accepted as telemetry.** They cannot be counted — there is no verdict to count — but every skip is logged with its reason and model key, so a per-model exclusion rate is observable rather than argued. |
| 6 | `ctx.grounding_record` is overwritten on each D4 retry, losing attempt-1 evidence. | **No change — the design already handles it.** The observation is written inside `_record_grounding`, per attempt, so attempt 1's eligible record is recorded *before* attempt 2 overwrites it. This is the reason the write lives there and not at capture time; now stated in the docstring. |
| 7 | No one-row-per-turn invariant / idempotency key. | **Accepted.** `trace_id` is `NOT NULL UNIQUE`, written `ON CONFLICT DO NOTHING`. Safe: `_record_grounding` has exactly one call site, sub-agents never reach it, and captures are already keyed one-file-per-`trace_id`. A turn with no trace id is skipped and logged. |
| 8 | `observed_at` would measure insertion time, not verification time. | **Accepted.** Passed explicitly from the turn, not left to `DEFAULT NOW()`. |
| 9 | No model/verifier revision versioning. | **Declined, recorded.** `max_window_age` already bounds how long an observation from a superseded verifier survives, and a revision column is FRE-1285's problem only if it ever pins a window across a deploy. Recorded as accepted risk, not designed around. |
| 10 | Partial-refresh staleness case untested. | **Accepted.** Added to the AC-4 test set. |

One further decision the review provoked, settled against its suggestion: **degraded-extraction turns are counted, not excluded.** A degraded extraction fails *safe* — it can only depress the rate — so excluding those turns is the choice that inflates. Inflation is the failure that matters here, so they stay in.

## Design decisions

**D-a — the eligibility predicate is the whole of AC-2.** A `GroundingRecord` contributes an
observation iff `available` (verification actually ran — a denied budget or a broken extractor
is a fact about Seshat, not evidence about the model), `non_exempt_count >= 1` (D5's
denominator, literally), and `retrieval_forced` is False.

`retrieval_forced` already exists on `GroundingRecord` and is set today by
`build_grounding_record(retrieval_forced=ctx.grounding_attempts > 1)`. FRE-1285 will widen the
same flag to cover heavy enforcement's *pre-generation* forcing — the field's docstring already
says so. **No new flag is added here**: the predicate consumes the existing field, and AC-2 is
proven directly against a record carrying `retrieval_forced=True`. Adding a
`pre_forced` context field now would be building FRE-1285's seam before FRE-1285.

**D-b — window, then freshness, then minimum.** `classify()` takes observations newest-first:

1. `window = observations[:window_size]`.
2. `fresh = [o for o in window if now - o.observed_at <= max_age]`.
3. `len(window) == 0` → `unmeasured(no_observations)`.
4. `len(fresh) < min_samples <= len(window)` → `unmeasured(stale_window)` — the window aged
   past its maximum without sufficient new observations (AC-4).
5. `len(fresh) < min_samples` → `unmeasured(insufficient_samples)` (AC-5).
6. else → `measured(rate = compliant(fresh) / len(fresh))`.

The rate is computed over `fresh`, never over `window`: compliance is re-earned, never banked,
so an observation older than the maximum age must not contribute to the rate either.

Distinguishing `stale_window` from `insufficient_samples` costs one branch and tells an
operator whether a model went quiet or was never measured — two entirely different remedies.

**D-c — Postgres, not Elasticsearch.** The offline entailment arm (FRE-1286) emits to the
structured log because nothing reads it inline. This signal *is* read inline (FRE-1285, once
per turn, before generation). One indexed `LIMIT <window_size>` read on
`(model_key, observed_at DESC)` is the cheap, deterministic substrate; an ES query in the turn
path is neither.

**D-e — AC-1 is scored against independent labels, through the real pipeline.**
`scripts/eval/fre1284_compliance/corpus.yaml` carries, per document: the reply text, its
citation markers, the turn's source set, **hand-labelled non-exempt spans**, and a
**hand-authored turn-level compliance label** — all authored from the raw reply, never from
extractor output. `harness.py` turns each document into a `SpanExtraction` built *from the
labels*, a real `SourceRegistry`, and a real `parse_citations` result, then runs the real
`verify_turn` → `build_grounding_record` → `is_unconfounded_observation` → `classify` path and
compares the per-turn verdict against the hand label. Tolerance is **zero disagreement**: the
derivation is deterministic, so any divergence is a defect rather than noise.

The corpus must contain the killer case ADR-0138 names — a turn with one passing citation *and*
one failing span, hand-labelled non-compliant — so an "at least one citation" implementation
fails AC-1 rather than passing it.

**D-d — the bar is single here, banded in FRE-1285.** `grounding_compliance_bar` answers
"does this rate meet the contract?" AC-6's broken-baseline check needs exactly that. FRE-1285
adds the promote/demote hysteresis *around* it; this ticket must not pre-empt the band.

## Files

| # | File | Change |
|---|------|--------|
| 1 | `docker/postgres/migrations/0029_grounding_compliance_observations.sql` | new table + index + `grafana_ro` grant |
| 2 | `docker/postgres/init.sql` | same DDL for fresh installs |
| 3 | `src/personal_agent/service/models.py` | `GroundingComplianceObservationModel` |
| 4 | `src/personal_agent/service/repositories/grounding_compliance_repository.py` | `record()` / `recent()` |
| 5 | `src/personal_agent/grounding/compliance.py` | predicate, types, `classify()` |
| 6 | `src/personal_agent/grounding/__init__.py` | exports |
| 7 | `src/personal_agent/config/settings.py` | 4 pre-registered parameters |
| 8 | `src/personal_agent/orchestrator/executor.py` | `ctx.answering_model_key`; record the observation in `_record_grounding` |
| 9 | `src/personal_agent/orchestrator/types.py` | `answering_model_key` field |
| 10 | `scripts/eval/fre1284_compliance/{corpus.yaml,corpus.py,harness.py,README.md,__init__.py}` | AC-1's independently-labelled corpus + scorer |
| 11 | `tests/personal_agent/grounding/test_compliance.py` | AC-2…AC-6 |
| 12 | `tests/personal_agent/grounding/test_fre1284_compliance_corpus.py` | AC-1 agreement, both partitions |
| 13 | `tests/personal_agent/service/repositories/test_grounding_compliance_repository.py` | store contract (skips without :5433) |

## Schema

```sql
CREATE TABLE IF NOT EXISTS grounding_compliance_observations (
    id           BIGSERIAL PRIMARY KEY,
    observed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_key    VARCHAR(255) NOT NULL,
    compliant    BOOLEAN NOT NULL,
    trace_id     VARCHAR(64) NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_grounding_compliance_model_time
    ON grounding_compliance_observations(model_key, observed_at DESC);
GRANT SELECT ON public.grounding_compliance_observations TO grafana_ro;
```

`trace_id` is kept because AC-1's method — independent labelling of the same turns — needs a
join key back to the turn record. Nothing else is stored: the span-level detail already lives
on the capture's `GroundingRecord`, and a second copy would be a second thing to disagree with.

## Config (pre-registered — AC-6)

| Setting | Default | Why |
|---|---|---|
| `grounding_compliance_window_size` | 100 | Rolling window, in observations. |
| `grounding_compliance_min_samples` | 30 | Below this, `unmeasured`. |
| `grounding_compliance_max_window_age_hours` | 336 (14d) | Staleness bound. |
| `grounding_compliance_bar` | 0.95 | The contract bar the rate is read against. |

Committed defaults, in git, before any live reading exists — which is what AC-6's "recorded
before results were seen" means operationally.

## Steps

1. **Failing test first** — write `tests/personal_agent/grounding/test_compliance.py` with all
   six AC classes against the not-yet-existing `personal_agent.grounding.compliance`. Confirm
   it fails on import. → verify: `make test-file FILE=tests/personal_agent/grounding/test_compliance.py` errors on collection.
2. **`grounding/compliance.py`** — `ComplianceObservation`, `UnmeasuredReason`,
   `ModelCompliance`, `is_unconfounded_observation(record)`, `classify(...)`. Pure; no I/O.
   → verify: the AC-1…AC-6 tests pass.
3. **Settings** — the four fields, with `Field(...)` descriptions carrying the rationale.
   → verify: `make test-k K=settings`.
4. **Schema** — migration 0029 + `init.sql` + `GroundingComplianceObservationModel`.
   → verify: `grep` parity between the three; migration applied against :5433 test stack.
5. **Repository** — `record()` / `recent()`, plus its real-DB test.
   → verify: `make test-file FILE=tests/personal_agent/service/repositories/test_grounding_compliance_repository.py`.
6. **Executor wiring** — in `_record_grounding`, when the record is unconfounded, write the
   observation in the background against the answering model key, reusing the existing
   `resolve_role_target(answering_role, model_key=get_current_selection(answering_role))`
   idiom from `_schedule_offline_entailment`. Best-effort, logged on failure, never raises into
   the turn. → verify: an executor test asserting the write is attempted for an eligible turn
   and skipped for a pre-forced one.
7. **Gates** — `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files`; then self-review (code-reviewer + security-review).

## Acceptance criteria → evidence

| AC | Criterion | Test / probe |
|---|---|---|
| AC-1 | Agrees with independent labelling; **every** non-exempt span must pass | `TestAC1Agreement` — a hand-labelled fixture corpus of turn records scored both ways, tolerance 0. Includes the killer case: a turn with one passing citation and one failing span, labelled non-compliant. An "at least one citation" implementation fails it. |
| AC-2 | Pre-forced turns absent from the denominator | `TestAC2Unconfounded` — `retrieval_forced=True` records are rejected by `is_unconfounded_observation` and never reach `classify`'s denominator. |
| AC-3 | Rate moves in both directions | `TestAC3Responsive` — feed non-compliant then compliant observations; assert the rate falls and rises within the window definition. |
| AC-4 | Staleness → `unmeasured` | `TestAC4Staleness` — a full, favourable window aged past `max_window_age` reverts to `unmeasured(stale_window)`. |
| AC-5 | Minimum sample enforced | `TestAC5MinSamples` — `min_samples - 1` observations report `unmeasured(insufficient_samples)`, not a rate. |
| AC-6 | Bars pre-registered; reject a broken baseline | `TestAC6PreRegisteredBars` — the committed defaults are asserted as constants, and a seeded always-non-compliant model (rate 0.0) is classified below the bar under those exact defaults. |

## Risks

- **Row growth.** One row per eligible turn. Low volume; no reaper in this ticket. Recorded,
  not mitigated.
- **`model_` pydantic namespace.** `model_key` collides with pydantic's protected prefix;
  `ConfigDict(protected_namespaces=())`, as `settings.py:50` already does.
- **Deploy ordering.** `Base.metadata.create_all` may build the table before migration 0029
  runs; the SQLAlchemy column carries `server_default=func.now()` to match the DDL, per the
  `SessionModelSelectionModel` precedent.
