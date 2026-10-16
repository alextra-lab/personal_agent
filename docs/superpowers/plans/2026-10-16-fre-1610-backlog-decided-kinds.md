# FRE-1610 — Backlog acts on decided kinds (plan)

Ticket: FRE-1610 (self-analysis 3/4, BACKLOG). Backing design: ADR-0105 D7 (outcome weights, decided
kinds) and D9 (fail-open). Spec: `docs/specs/SELF_IMPROVEMENT_FEEDBACK_LOOP_SPEC.md` §6.4.

D7 weights do not change. No schema change. No ADR amendment is needed for the weights. The bounded
skip refines D7's interim key `(source, category, facet)`, so this PR adds a one-line amendment note
to ADR-0105 (same style as the FRE-1354 note).

## The rule (written down here, in the spec, in the guide)

1. **R1 — a cancel suppresses its fingerprint.** A ticket in state `Canceled` suppresses its
   fingerprint for `feedback_suppression_days` (30), with or without the `Rejected` label. States
   `Duplicate` and the `Defer` label do not suppress.
2. **R2 — a decided skip is bounded.** A terminal outcome other than `deferred` skips new proposals
   of the same `(source, category, scope)` for `decided_skip_days` (new setting, default 90, equal to
   `signal_window_days`), counted from `outcome.observed_at`. A proposal with another scope is not
   skipped. After expiry a proposal is not skipped. Skipped proposals write no row, so the evidence
   after the window starts at `seen_count` 1 and must earn the normal bar (3 sightings, 7 days).
3. **R3 — the scan checks both.** Before ranking, promotion drops an entry whose fingerprint is
   suppressed (R1) or whose kind is decided (R2). A sysgraph error keeps the entry (fail-open, D9).

Deliberately unchanged: `signal_suppression_min_n` (5). R1 and R2 carry the six cancellations
without a category-level count. Outcome classification (`Duplicate` still scores
`canceled-as-noise`) is unchanged.

## Steps

Every step starts with its failing test. Commands run one at a time.

### 1. Settings
- `src/personal_agent/config/settings.py`: add `decided_skip_days: int = Field(default=90, ge=1)`
  beside `signal_window_days`.
- `.env.example`: add `# AGENT_DECIDED_SKIP_DAYS=90`.
- `docs/reference/CONFIG_INVENTORY.md`: run `uv run python scripts/audit/config_inventory.py generate`.
  `tests/scripts/test_config_inventory.py` fails without the row.

### 2. Repository: bounded decided check, scope on promotion
File: `src/personal_agent/sysgraph/repository.py`.
- `_IS_KIND_DECIDED_QUERY` gains `AND p.scope IS NOT DISTINCT FROM $3 AND o.observed_at >= $4`.
- `is_kind_decided(source, category, scope=None)` computes the cutoff from `settings.decided_skip_days`.
- `read_before_emit` passes `scope` and the cutoff to the same query.
- `_RECORD_PROMOTION_UPSERT_PROPOSAL` inserts `scope` and keeps an existing scope
  (`scope = COALESCE(sysgraph.proposal.scope, EXCLUDED.scope)`). `record_promotion` passes
  `proposal.scope`. Without this, a row created at promotion has a NULL scope and escapes R2.
- Tests (`tests/personal_agent/sysgraph/test_signal.py`, `test_read_before_emit.py`, integration, test
  Postgres only):
  - decided, same scope, inside window → `decided_skip`, no row (existing test, seed gains scope).
  - decided, other scope, inside window → `generate_new` (AC-2).
  - decided, same scope, outcome older than the window → `generate_new` (AC-2).
  - `record_promotion` with a scope stores it, and a second call keeps the first scope.

### 3. Outcome ingestion: a cancel suppresses
File: `src/personal_agent/brainstem/jobs/outcome_ingestion.py`.
- New pure function `fingerprint_to_suppress(issue) -> str | None`: returns the fingerprint parsed
  from the description when the state is `Canceled`, else `None`.
- In `run_outcome_ingestion`, call `record_rejection_suppression` BEFORE `record_outcome`. If the
  write raises, the per-ticket handler logs it and the ticket stays in the queue for a retry. The
  write is idempotent.
- Tests (`tests/test_brainstem/test_outcome_ingestion.py`):
  - `Canceled` with no label and a fingerprint → fingerprint suppressed (AC-1, uses the real
    suppression file under `tmp_path`).
  - `Canceled` with the `Rejected` label → suppressed.
  - `Duplicate`, `Done`, open states → not suppressed.
  - suppression write failure → outcome is not recorded.

### 4. Promotion: the scan checks (AC-1, AC-3)
File: `src/personal_agent/captains_log/promotion.py`.
- `scan_promotable_entries`: skip an entry whose fingerprint `is_fingerprint_suppressed`.
- New `_drop_decided_kinds(entries)`, called in `run()` before `_apply_signal_ranking`. It calls
  `sysgraph_repo.is_kind_decided(source, category, scope)` per entry. The category is
  `pc.category.value if pc.category else "unknown"`, the same value `_record_sysgraph_linkage`
  stores (Codex finding 3). An entry with source `LEGACY_UNATTRIBUTABLE` is skipped, because sysgraph
  cannot hold it. Error → keep the entry and log a warning.
- `_record_sysgraph_linkage` passes `scope=pc.scope.value if pc.scope else None`.
- Tests (`tests/test_captains_log/test_promotion.py`):
  - suppressed fingerprint → absent from the scan (AC-1).
  - decided kind → create function not awaited (AC-3).
  - undecided kind → still promotes.
  - `is_kind_decided` raises → entry still promotes (fail-open).

### 5. AC-4 replay script
File: `scripts/fre1610_replay_cancellations.py`. The default mode is a dry run: read-only on Linear,
writes nothing.
- Fetch FRE-1373, 1378, 1404, 1412, 1418, 1429 from Linear. Print state, outcome class, fingerprint
  and "would suppress". Exit non-zero if any ticket would not be suppressed.
- Uses `fingerprint_to_suppress` from step 3, so the replay runs the production decision.
- Flag `--apply` writes the suppression for the tickets that qualify. Reason (Codex finding 1): the
  outcome queue holds only tickets with a sysgraph linkage and no outcome. The six old tickets are
  not in it after the FRE-1609 reset, so the queue never reaches them. Master runs `--apply` once
  after deploy. This PR does not run it.
- Test: `tests/scripts/test_fre1610_replay.py` with a stub client (suppressed and not suppressed;
  dry run writes nothing; `--apply` writes).

### Codex plan review (2026-10-16) — disposition
- Finding 1 (queue misses old tickets): accepted in part. `--apply` added. A general Linear sweep is
  not added, because every new ticket enters the queue through `record_promotion`.
- Finding 2 (NULL scope is a category-wide bucket): rejected. A NULL scope is its own group, the same
  key `read_before_emit` uses for awaiting proposals. Documented in the rule. Promotion now stores
  scope, so new rows do not have a NULL scope.
- Finding 3 (category-less entries): accepted, see step 4.
- Finding 4 (check-then-act race): rejected. The window is seconds inside one run, and no
  acceptance criterion needs it.

### 6. Docs
- `docs/guides/LINEAR_FEEDBACK_LOOP.md`: fix the label table (`Duplicate`, `Defer` do not suppress),
  add the cancel rule and the bounded skip.
- `docs/specs/SELF_IMPROVEMENT_FEEDBACK_LOOP_SPEC.md` §6.4: Guard 2 gains the cancel path; add Guard 4
  (bounded decided skip); fix the "Gating of decided kinds is FRE-1610" sentence; update the table.
- `docs/architecture_decisions/ADR-0105-...md`: one-line amendment note under D7.

## Gates
`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
Integration tests for steps 2 only: `PERSONAL_AGENT_INTEGRATION=1 uv run python -m pytest -m integration tests/personal_agent/sysgraph -q`
(test Postgres :5433 only, no LLM).

## Acceptance criteria → proof

| AC | Proof |
|----|-------|
| AC-1 | step 3 + step 4 tests; a canceled ticket without a label suppresses, and the scan drops the entry |
| AC-2 | step 2 tests: other scope and expired window both `generate_new`; rule written in spec §6.4 |
| AC-3 | step 4 test: decided kind, create function not awaited |
| AC-4 | step 5 script output for the six real tickets |
