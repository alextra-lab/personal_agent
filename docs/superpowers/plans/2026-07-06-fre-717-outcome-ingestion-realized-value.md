# FRE-717 — ADR-0105 T4: Close the loop (outcome ingestion + realized-value signal)

**Backing:** ADR-0105 D7 (`docs/architecture_decisions/ADR-0105-convergent-self-improvement-pipeline-and-system-graph.md`).
**Depends on (landed):** FRE-714 (sysgraph schema, PR #399), FRE-715 (source discriminator, PR #403),
FRE-716 (bidirectional linkage, PR #407).
**Blocks:** FRE-721 (T7 — generation-time read-before-emit, D9/D10; AC-9 seam).

## Scope (from the ticket's own AC — not the full ADR AC-6/AC-9 conflation)

The ticket body states three checks (ADR-0105 AC-6, scoped to this child):

1. For one real ticket that reached an outcome, an outcome node is linked to its source proposal
   (not orphaned).
2. The source's realized value changes by the expected outcome weight (measured before/after).
3. The next promotion run's ordering or suppression reflects the changed value (read, not merely
   written).

**Explicitly out of scope for this ticket** (belongs to FRE-721 / T7, gated on the FRE-720 probe):
D9's generation-time read-before-emit branch, and semantic (vector) dedup. This ticket writes the
signal and makes *promotion* read it; FRE-721 is the one that makes *producers* read it before
emitting.

**D7's "decided" stamp — resolved, not silently dropped** (codex review flagged this as ambiguous in
the first draft): this ticket adds a read-only `is_kind_decided(source, category)` method (derived
from outcome existence, not a persisted stamp — see §3 below) so the fact is queryable end-to-end as
part of "closing the loop." FRE-721 remains the ticket that *consumes* it inside the producers'
generation-time branch.

**Also explicitly out of scope:** synthesizing a `deferred` outcome from ticket state. The ticket's
own "What" section only names shipped / canceled-as-noise / owner-rejected as ingestion triggers;
`deferred` appears only in the weight table (for completeness / a future consumer) — no existing
code path today marks a ticket "deferred" as a terminal state (the existing `Defer` label handler in
`feedback.py` only records `defer_noted`, it does not close the ticket). Synthesizing one here would
be exactly the kind of speculative branch CLAUDE.md says not to build. If FRE-721 or a later ticket
needs it, it can add the trigger without touching this ticket's mechanism (weight=0 is already
supported by the formula).

## Current-state facts this plan depends on (verified live in this worktree)

- `sysgraph.outcome` (result CHECK IN shipped/owner-rejected/canceled-as-noise/deferred) and
  `sysgraph.produced` (Ticket→Outcome, `UNIQUE(ticket_id, outcome_id)`) tables **already exist**
  (migration `0014_sysgraph_schema.sql`) — FRE-714 built them ahead of need. No new node/edge tables
  needed; only a new suppression-cooldown table.
- `SysgraphRepository.proposal_lineage()` already traverses proposal→ticket→outcome (depth 2) — once
  an outcome is written, existing traversal code already reaches it. No change needed there.
- `LinearClient.get_issue()` returns `state: {name: str}` (**not** `state.type`) and a flattened
  `labels: list[{name: str}]` (`linear_client.py:413-428`, `_normalize_issue_node`). The outcome
  classifier must match on the workflow **state name** (`Done`, `Canceled`, `Duplicate`, …, per
  `.claude/skills/lifecycle-rules.md`'s state lifecycle), not `state.type`.
- `handle_rejected` (`feedback.py:222-236`) already sets `state="Canceled"` for the **pre-promotion,
  proposal-review** "Rejected" label path (ADR-0040 label channel) — this is a *different* signal
  from a *post-ship* ticket outcome, but it means a Linear ticket in state `Canceled` **can** carry a
  `Rejected` label. The classifier below uses exactly that: `Canceled` + `Rejected` label →
  `owner-rejected`; `Canceled` (or `Duplicate`) without it → `canceled-as-noise`.
- `PromotionPipeline._finalize_promotion` (`promotion.py:572-596`) already writes the proposal↔ticket
  `PROMOTED_TO` edge via `_record_sysgraph_linkage` on every promotion (FRE-716). This ticket adds a
  **read** of the signal earlier in `run()`, before the existing `capped = entries[:...]` line
  (`promotion.py:346`) — it does not touch `_finalize_promotion` at all.
- `BrainstemScheduler` already runs a daily-gated Linear job (`feedback_poller`, gated by
  `feedback_polling_hour_utc`, `_last_feedback_date`) — the outcome-ingestion job follows that exact
  pattern (new hour setting, new `_last_*_date`), not a new mechanism.
- `build_consolidation_promotion_handler` (`events/pipeline_handlers.py:266-282`) shows the
  established idiom for a **per-run** `SysgraphRepository` (construct → `connect()` best-effort →
  use → `disconnect()` in `finally`, never persisted on a long-lived object). The new job follows the
  same idiom rather than adding a persistent pool to the scheduler.

## Design

### 1. Realized-value signal is computed, not stored

The windowed value `v = Σweights / (n + 2)` over a **trailing 90-day window** is fully determined by
the outcome rows joined back to their source proposal's `(source, category)`. Storing `v` as a mutable
running total would let it drift from the window definition (an outcome ages out of the 90-day window
with no corresponding write to "un-count" it). So `v`/`n` are **recomputed on read** via a join query,
not persisted — this matches D2's own reasoning for using recursive CTEs over a flat cache.

The **suppression cooldown**, however, genuinely needs persisted state: once triggered (`v ≤ −0.4`
over `n ≥ 5`), it holds for a fixed 30-day window *independent of whether `v` recovers* in the
meantime (parallel structure to the existing fingerprint suppression in `suppression.py`, which is
also a fixed-duration timer, not a live recompute). That is the one new table.

### 2. New migration — `docker/postgres/migrations/0017_sysgraph_signal.sql`

Following the `0016` idempotent convention (`SET ROLE sysgraph_role` / `RESET ROLE`, `IF NOT EXISTS`):

```sql
CREATE TABLE IF NOT EXISTS sysgraph.signal (
    source           TEXT NOT NULL CHECK (source IN ('statistical_detector', 'reflection')),
    category         TEXT NOT NULL,
    suppressed_until TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, category)
);

-- Concurrency fix (codex review): one terminal outcome per ticket, enforced at the DB layer —
-- record_outcome's ON CONFLICT (ticket_id) DO NOTHING depends on this existing.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_constraint WHERE conname = 'sysgraph_produced_ticket_unique'
    ) THEN
        ALTER TABLE sysgraph.produced ADD CONSTRAINT sysgraph_produced_ticket_unique UNIQUE (ticket_id);
    END IF;
END
$$;

-- Supports get_signal's windowed (observed_at) filter without a seq scan as outcome rows grow.
CREATE INDEX IF NOT EXISTS idx_sysgraph_outcome_observed_at ON sysgraph.outcome(observed_at);
```

No FK to `proposal`/`ticket` — `(source, category)` is a derived key over many proposals, not a
single node. **Mirror into `init.sql`** (codex review flagged this — the existing `produced` table
there must get `UNIQUE(ticket_id)` inline in its `CREATE TABLE`, plus the new `sysgraph.signal` table
and the `observed_at` index, so a fresh install matches an upgraded one). Checked while touching this
section: migration `0016`'s fingerprint uniqueness (FRE-716) is **already** present in `init.sql` —
`sysgraph.proposal.fingerprint` is declared `TEXT NOT NULL UNIQUE` inline (an anonymously-named
constraint, functionally equivalent to `0016`'s named `sysgraph_proposal_fingerprint_key`). No gap;
retracting the earlier draft's claim to the contrary.

### 3. `src/personal_agent/sysgraph/repository.py` — new methods

```python
@dataclass(frozen=True)
class SignalValue:
    value: float
    n: int
    suppressed: bool
```

- `async def tickets_awaiting_outcome(self) -> list[dict[str, str]]` — tickets with a `PROMOTED_TO`
  edge but no `produced` edge yet:
  ```sql
  SELECT DISTINCT t.linear_issue_id
  FROM sysgraph.ticket t
  JOIN sysgraph.promoted_to pt ON pt.ticket_id = t.id
  WHERE NOT EXISTS (SELECT 1 FROM sysgraph.produced pr WHERE pr.ticket_id = t.id);
  ```
- `async def ticket_source_kind(self, linear_issue_id: str) -> tuple[str, str] | None` — resolves
  `(source, category)` for a ticket via its `PROMOTED_TO` edge (reuses the same join as
  `ticket_source_proposal`, adding `category`). If a ticket has more than one linked proposal (the
  dedup-matched-existing-issue path), pick the most-recently-created proposal — log at INFO if more
  than one exists so it is observable, not silently arbitrary.
- `async def record_outcome(self, linear_issue_id: str, result: Literal["shipped", "owner-rejected", "canceled-as-noise"]) -> bool` —
  **atomic, DB-constraint-backed** (revised after codex review — a bare check-then-insert inside a
  transaction does *not* prevent two concurrent callers each inserting a distinct `outcome_id` for the
  same ticket, since `sysgraph.produced`'s only existing constraint is `UNIQUE(ticket_id, outcome_id)`,
  which permits multiple outcomes per ticket). New migration 0017 adds
  `ALTER TABLE sysgraph.produced ADD CONSTRAINT sysgraph_produced_ticket_unique UNIQUE (ticket_id)`
  (mirrored inline in `init.sql`'s `produced` `CREATE TABLE` for fresh installs). `record_outcome` then:
  resolve `ticket_id`; if none, log `sysgraph_outcome_skipped_no_ticket`, return `False`; else, in one
  transaction, insert `sysgraph.outcome` (`RETURNING id`), then
  `INSERT INTO sysgraph.produced (ticket_id, outcome_id) VALUES ($1, $2) ON CONFLICT (ticket_id) DO
  NOTHING RETURNING id`. If the conflict fires (`produced_id is None`), raise a local sentinel
  exception to roll back the just-inserted (now-orphaned) `outcome` row within the same transaction,
  caught outside as "already recorded" → return `False`. If it commits, return `True`. This makes the
  one-outcome-per-ticket invariant hold at the database layer, not by hoping nothing races.
- `async def is_kind_decided(self, source: str, category: str) -> bool` — **new, added per codex
  review** to resolve the D7/ADR:94-96 ambiguity the plan originally left silent: whether this ticket
  implements the "kind marked decided" stamp at all. Decision: **derive it on read**, the same
  "compute, don't persist" philosophy as `v` — `decided` is true iff any outcome with
  `result != 'deferred'` exists for `(source, category)` (no time window; a terminal decision doesn't
  age out), via the same `produced → ticket → promoted_to → proposal` join. No new column/stamp is
  written; this keeps `is_kind_decided` always consistent with the outcome data instead of risking a
  stale stamp. **Scope note for the ticket comment:** this method exists so FRE-721 (T7, D9's
  generation-time read) has something to call — FRE-721 is still the ticket that wires it into the
  producers' read-before-emit branch; FRE-717 only makes the fact queryable.
- `async def get_signal(self, source: str, category: str) -> SignalValue` — computes `v`/`n` from
  outcome rows in the last `signal_window_days` (default 90) joined via
  `produced → ticket → promoted_to → proposal` filtered on `(source, category)`; weights via a
  Python dict (`{"shipped": 1.0, "owner-rejected": -1.0, "canceled-as-noise": -0.5, "deferred": 0.0}`,
  matching D7 verbatim); `v = sum(weights) / (n + signal_smoothing_prior)`. Reads current
  `suppressed_until` from `sysgraph.signal` and sets `suppressed = suppressed_until is not None and
  suppressed_until > now()`.
- `async def compute_and_apply_signal(self, source: str, category: str) -> SignalValue` — calls
  `get_signal`, and if `value <= signal_suppression_threshold and n >= signal_suppression_min_n`,
  upserts `sysgraph.signal.suppressed_until = now() + signal_suppression_cooldown_days` (does *not*
  clear an existing suppression early if the condition no longer holds — a cooldown, once started,
  runs its course, matching the fixed-duration-timer precedent). Returns the `SignalValue` computed
  before the upsert (so callers get "current v", and the suppression state reflects what will apply
  to the *next* read).

All new settings knobs are read from `settings` at call time (constructor already takes none —
`SysgraphRepository.__init__` takes only `dsn`; settings values are read directly inside the new
methods, matching how `get_signal`'s window/prior constants would otherwise be hardcoded — this keeps
the "retune without reopening the ADR" property the ADR asks for).

### 4. New settings (`src/personal_agent/config/settings.py`, alongside the existing ADR-0040 block)

```python
outcome_ingestion_enabled: bool = Field(default=True, ...)
outcome_ingestion_hour_utc: int = Field(default=8, ge=0, le=23, ...)  # distinct from feedback_polling_hour_utc=7
signal_window_days: int = Field(default=90, ge=1, ...)
signal_smoothing_prior: float = Field(default=2.0, ge=0, ...)
signal_priority_clamp: float = Field(default=0.5, ge=0, le=1.0, ...)
signal_suppression_threshold: float = Field(default=-0.4, ...)
signal_suppression_min_n: int = Field(default=5, ge=1, ...)
signal_suppression_cooldown_days: int = Field(default=30, ge=1, ...)
```

### 5. New job — `src/personal_agent/brainstem/jobs/outcome_ingestion.py`

Mirrors `freshness_review.py`'s shape (standalone `async def run_...`, its own settings gate,
structured logging).

```python
_STATE_TO_RESULT = {"Done": "shipped"}  # Canceled/Duplicate resolved by _classify_outcome below

def _classify_outcome(issue: dict) -> Literal["shipped", "owner-rejected", "canceled-as-noise"] | None:
    """Pure function — no I/O — unit-testable in isolation."""
    state_name = (issue.get("state") or {}).get("name")
    if state_name == "Done":
        return "shipped"
    if state_name in ("Canceled", "Duplicate"):
        labels = LinearClient.labels_from_issue(issue)
        return "owner-rejected" if "Rejected" in labels else "canceled-as-noise"
    return None  # still open (Approved/In Progress/In Review/Awaiting Deploy/Verify Failed) — not decided


async def run_outcome_ingestion(linear_client: LinearClient, trace_id: str) -> None:
    cfg = get_settings()
    if not cfg.outcome_ingestion_enabled:
        log.debug("outcome_ingestion_skipped_disabled", trace_id=trace_id)
        return

    from personal_agent.sysgraph import SysgraphRepository

    repo = SysgraphRepository(cfg.sysgraph_database_url)
    try:
        await repo.connect()
    except Exception as exc:
        log.warning("outcome_ingestion_sysgraph_connect_failed", error=str(exc), trace_id=trace_id)
        return

    try:
        pending = await repo.tickets_awaiting_outcome()
        ingested = 0
        for linear_issue_id in pending:
            try:
                issue = await linear_client.get_issue(linear_issue_id)
                result = _classify_outcome(issue)
                if result is None:
                    continue
                kind = await repo.ticket_source_kind(linear_issue_id)
                before = await repo.get_signal(*kind) if kind else None
                recorded = await repo.record_outcome(linear_issue_id, result)
                if recorded and kind:
                    after = await repo.compute_and_apply_signal(*kind)
                    log.info(
                        "sysgraph_outcome_ingested",
                        linear_issue_id=linear_issue_id,
                        result=result,
                        source=kind[0],
                        category=kind[1],
                        value_before=before.value if before else None,
                        value_after=after.value,
                        suppressed=after.suppressed,
                        trace_id=trace_id,
                    )
                    ingested += 1
            except Exception as exc:
                log.warning(
                    "outcome_ingestion_ticket_failed",
                    linear_issue_id=linear_issue_id,
                    error=str(exc),
                    trace_id=trace_id,
                )
        log.info("outcome_ingestion_completed", scanned=len(pending), ingested=ingested, trace_id=trace_id)
    finally:
        await repo.disconnect()
```

### 6. Scheduler wiring — `src/personal_agent/brainstem/scheduler.py`

New instance state (`__init__`, alongside `_last_feedback_date`):
```python
self._last_outcome_ingestion_date: date | None = None
self.outcome_ingestion_hour_utc = settings.outcome_ingestion_hour_utc
```

New daily-gated block in `_lifecycle_loop()`, placed directly after the existing feedback-polling
block (same `today`/`iteration_trace_id` already in scope):
```python
if (
    self._linear_client is not None
    and getattr(settings, "outcome_ingestion_enabled", True)
    and now.hour == self.outcome_ingestion_hour_utc
    and (self._last_outcome_ingestion_date is None or self._last_outcome_ingestion_date != today)
):
    try:
        from personal_agent.brainstem.jobs.outcome_ingestion import run_outcome_ingestion

        await run_outcome_ingestion(self._linear_client, trace_id=iteration_trace_id)
        self._last_outcome_ingestion_date = today
    except Exception as exc:
        log.warning("outcome_ingestion_failed", error=str(exc), exc_info=True, trace_id=iteration_trace_id)
```

### 7. Promotion-time read — `src/personal_agent/captains_log/promotion.py`

New private method, called from `run()` right after `scan_promotable_entries()` and before the
existing budget-gate block:

```python
async def _apply_signal_ranking(self, entries: list[CaptainLogEntry]) -> list[CaptainLogEntry]:
    """Rank by realized value and drop suppressed (source, category) pairs (ADR-0105 D7/AC-6)."""
    if self._sysgraph_repo is None:
        return entries  # fail open — identical to today's behavior when sysgraph is unavailable

    scored: list[tuple[float, CaptainLogEntry]] = []
    for entry in entries:
        pc = entry.proposed_change
        if pc is None or pc.source is None or pc.category is None:
            scored.append((float(pc.seen_count) if pc else 0.0, entry))
            continue
        try:
            signal = await self._sysgraph_repo.get_signal(pc.source.value, pc.category.value)
        except Exception as exc:
            log.warning("promotion_signal_read_failed", entry_id=entry.entry_id, error=str(exc))
            scored.append((float(pc.seen_count), entry))
            continue
        if signal.suppressed:
            log.info(
                "promotion_suppressed_by_signal",
                entry_id=entry.entry_id,
                source=pc.source.value,
                category=pc.category.value,
                value=signal.value,
            )
            continue  # dropped from this run entirely
        clamp = settings.signal_priority_clamp
        modulation = 1.0 + max(-clamp, min(clamp, signal.value))
        scored.append((pc.seen_count * modulation, entry))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in scored]
```

`run()` changes from:
```python
entries = self.scan_promotable_entries()
if not entries:
    ...
```
to:
```python
entries = self.scan_promotable_entries()
if not entries:
    ...
entries = await self._apply_signal_ranking(entries)
if not entries:  # everything in this run was suppressed
    log.info("promotion_pipeline_all_suppressed")
    return []
```
`capped = entries[: self.criteria.max_existing_linear_issues]` (line 346) is unchanged — it now caps
an already-ranked, already-suppression-filtered list, which is exactly "next promotion run's ordering
or suppression reflects the changed value."

## Files touched

1. `docker/postgres/migrations/0017_sysgraph_signal.sql` (new) — `sysgraph.signal` table +
   `sysgraph.produced` ticket-unique constraint + `outcome.observed_at` index
2. `docker/postgres/init.sql` — mirror all three (fresh-install parity, per codex review)
3. `src/personal_agent/sysgraph/repository.py` — `SignalValue` dataclass + 6 new methods
   (`tickets_awaiting_outcome`, `ticket_source_kind`, `record_outcome`, `is_kind_decided`,
   `get_signal`, `compute_and_apply_signal`)
4. `src/personal_agent/config/settings.py` — 7 new settings fields
5. `src/personal_agent/brainstem/jobs/outcome_ingestion.py` (new) — `_classify_outcome`, `run_outcome_ingestion`
6. `src/personal_agent/brainstem/scheduler.py` — `__init__` state + one new daily-gated block
7. `src/personal_agent/captains_log/promotion.py` — `_apply_signal_ranking`, one call site in `run()`

## Tests (TDD — failing first)

**Integration (real test Postgres, `@pytest.mark.integration`, `sysgraph_pool`/`sysgraph_repo`
fixtures — extend `tests/personal_agent/sysgraph/test_repository.py` or add
`tests/personal_agent/sysgraph/test_signal.py`):**
- `record_outcome` creates outcome+produced for a seeded promoted ticket; returns `True`.
- `record_outcome` called twice for the same ticket is idempotent — second call returns `False`, still
  exactly one `sysgraph.outcome` row linked.
- `record_outcome` for a `linear_issue_id` with no `sysgraph.ticket` row returns `False`, no rows
  written.
- `get_signal` computes `v = Σweights / (n+2)` correctly over seeded outcomes for one `(source,
  category)`; outcomes older than `signal_window_days` are excluded from the sum (seed one inside, one
  outside the window via a direct `observed_at` override).
- `compute_and_apply_signal` sets `suppressed_until` ≈ now + 30 days when 5 seeded `owner-rejected`
  outcomes push `v` below −0.4; `get_signal` on the same key afterward returns `suppressed=True`.
- **AC-6 direct proof**: seed one full arc via `record_promotion` (proposal→ticket), call `get_signal`
  (before, `v==0, n==0`), call `record_outcome` + `compute_and_apply_signal` (after) — assert `v`
  changed by exactly the expected weight for the recorded result. This is the literal "measured
  before/after" the AC asks for.
- `ticket_source_kind` resolves `(source, category)` for a promoted ticket.
- `is_kind_decided` is `False` before any outcome, `True` after a `shipped`/`owner-rejected`/
  `canceled-as-noise` outcome is recorded, and stays `False` if only a `deferred` outcome exists
  (never synthesized by this ticket's ingestion job, but exercised directly at the repository level so
  the method itself is proven independent of the ingestion job's scope cut).
- **Concurrency (codex review)**: two concurrent `record_outcome` calls for the same
  `linear_issue_id` (`asyncio.gather` over two calls, or two separate connections against the same
  seeded ticket) — assert exactly one returns `True`, the other `False`, and exactly one
  `sysgraph.outcome` row ends up linked via `produced` (no orphaned `outcome` row from the rolled-back
  loser). Proves the `UNIQUE(ticket_id)` constraint + rollback-on-conflict actually holds under a race,
  not just under sequential calls.

**Unit (mocked `LinearClient`/`SysgraphRepository`, no real I/O — same idiom as
`TestAdr0105BidirectionalLinkage` in `tests/test_captains_log/test_promotion.py`):**
- `tests/test_brainstem/test_outcome_ingestion.py` (new):
  - `_classify_outcome`: `state.name == "Done"` → `"shipped"`; `"Canceled"` + `Rejected` label →
    `"owner-rejected"`; `"Canceled"` without it → `"canceled-as-noise"`; `"Duplicate"` → same rule;
    any open state (`Approved`, `In Progress`, `In Review`, `Awaiting Deploy`) → `None`.
  - `run_outcome_ingestion`: given `tickets_awaiting_outcome` returns one id, `get_issue` returns a
    `Done`-state issue → asserts `record_outcome("shipped")` called, `compute_and_apply_signal` called
    with the resolved `(source, category)`.
  - `run_outcome_ingestion` with `_classify_outcome` returning `None` (still open) → `record_outcome`
    never called for that ticket.
  - `outcome_ingestion_enabled=False` → returns immediately, no repo connection attempted.
- `tests/test_captains_log/test_promotion.py`:
  - **Update `TestAdr0105BidirectionalLinkage`'s existing `sysgraph_repo` mocks** (codex review — these
    currently mock only `record_promotion`; once `_apply_signal_ranking` calls `get_signal` on every
    entry with a non-`None` repo, an un-mocked `MagicMock().get_signal(...)` is not awaitable. The
    broad `except Exception` in `_apply_signal_ranking` would swallow the resulting `TypeError` and
    fail open — so these tests would not *break*, but they'd be silently exercising the exception path
    instead of the real one. Add `sysgraph_repo.get_signal = AsyncMock(return_value=SignalValue(0.0,
    0, False))` to each so they exercise the intended no-signal-yet path explicitly.)
  - New `TestAdr0105SignalReadInPromotion`:
  - `sysgraph_repo=None` → `_apply_signal_ranking` returns entries unchanged (fail-open, matches
    today's behavior for every other sysgraph best-effort call in this file).
  - A suppressed `(source, category)` entry is excluded from `promoted` / never reaches
    `_create_linear_issue`.
  - Two candidate entries with the same `seen_count` but different signal values are promoted in the
    value-ranked order (higher `v` first) when both are inside the same run but the cap would
    otherwise only admit one — proves ranking, not just filtering.
  - `get_signal` raising an exception for one entry degrades that entry to its unmodulated
    `seen_count` score and does not block the run (fail open, matches the existing
    `promotion_budget_check_failed`/`promotion_linear_dedup_query_failed` try/except precedent already
    in this file).

## Test commands

```
make test-infra-up   # once, if not already running
make test-file FILE=tests/personal_agent/sysgraph/test_signal.py
make test-file FILE=tests/personal_agent/sysgraph/test_repository.py
make test-file FILE=tests/test_brainstem/test_outcome_ingestion.py
make test-file FILE=tests/test_captains_log/test_promotion.py
make test
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Acceptance-criteria mapping (what master's gate reads)

| Ticket AC | Proof |
|---|---|
| Outcome node linked to source proposal, not orphaned | `test_record_outcome_creates_produced_edge` (integration) + existing `proposal_lineage` traversal already reaches `outcome` nodes (FRE-714, unchanged) |
| Realized value changes by expected weight, measured before/after | `test_get_signal_before_after_matches_expected_weight` (integration, direct before/after assertion) |
| Next promotion run's ordering/suppression reflects the changed value | `TestAdr0105SignalReadInPromotion` (unit, mocked repo) proving both the suppression-drop and the rank-reordering paths |

## Risk classification

**Standard/Complex** — touches `src/` business logic across 7 files, a new Postgres migration mirrored
into `init.sql`, and a new ADR-0105 implementation seam (AC-6). Per the build skill, this requires a
codex plan-review before implementation — completed above, plan revised accordingly.
