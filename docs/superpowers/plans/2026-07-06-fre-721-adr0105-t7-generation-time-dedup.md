# FRE-721 (ADR-0105 T7) — Generation-time read-before-emit + fallback dedup

**Ticket:** FRE-721 · **Backing ADR:** ADR-0105 D9/D10 · **Depends on:** FRE-720 (Done), FRE-714 (Done), FRE-717 (Awaiting Deploy)

## Resolved input: FRE-720's probe decision

`scripts/eval/fre720_insights_separation/probe_result.json["decision"] == "fallback"`.
No clean cosine floor on the insights corpus (5/24 negatives ≥ lowest positive). Per D10,
this ships **category+facet grouping over sysgraph edges**, never vector clustering. AC-10
(no reranker, no laptop-GPU dependency) is trivially satisfied — the fallback path never
touches an embedder at all.

## Scope decisions (flagging for review, not silently assuming)

1. **Fallback grain = `(source, category)`, no facet.** No facet taxonomy exists yet in the
   schema or codebase (`sysgraph.proposal` has no facet column; `is_kind_decided`/`get_signal`
   from FRE-717 already operate at `(source, category)` grain only). D9's own text permits this
   explicitly: *"D9 keys on (source, category, facet) where facets exist, else (source,
   category) with conservative matching."* Building a facet taxonomy (required facets per
   category, extraction source, stored as sysgraph attributes/edges) is a materially separate
   design effort — out of this ticket's scope. This ticket ships the coarser, already-precedented
   grain and says so honestly rather than claiming facet-level precision it doesn't have.
2. **Scope limited to the two producers ADR-0105's Implementation Notes name explicitly:**
   `insights/engine.py` (`statistical_detector`) and `captains_log/reflection.py`
   (`reflection`). The other `CONFIG_PROPOSAL`-emitting event handlers in
   `events/pipeline_handlers.py` (error-pattern, compaction-quality, graph-quality) build
   entries directly from typed events, not through these two producers, and are not named in
   the ADR's Implementation Notes — wiring them in would be scope creep.
3. **`sysgraph.proposal` must be writable at generation time, not just at promotion time.**
   Today only `PromotionPipeline._record_sysgraph_linkage` (post-Linear-issue-creation) writes
   proposal rows. D9 needs to detect duplicates in the ~1,800-strong *awaiting_approval* pile
   that never reaches promotion (throttled by the ADR-0040 budget gate) — so the read-before-emit
   path must be able to upsert a proposal row independent of any ticket. The schema already
   supports this (`sysgraph.proposal` has no FK to `ticket`); this only needs a new repository
   method, no migration.
4. **Fail-open "counter" = a structured log event**, matching the only precedent this codebase
   has for degrade signals (`promotion_signal_read_failed`, `sysgraph_linkage_write_failed`,
   `outcome_ingestion_sysgraph_connect_failed` — all `log.warning`, no in-process `Counter`
   object anywhere in the tree). ES aggregation over the event name is the "counter"; a Kibana
   panel is out of scope for this ticket (ADR-0090's telemetry-surface project owns dashboards).
5. **No new settings flag.** `build_consolidation_promotion_handler` wires `sysgraph_repo`
   unconditionally (best-effort connect, fail open) with no separate enable flag — this ticket
   follows the same precedent rather than inventing a `sysgraph_read_before_emit_enabled` flag.

## Implementation

### 1. `src/personal_agent/sysgraph/repository.py`
- Extract the existing upsert-proposal SQL (currently inlined in `record_promotion`) into a new
  public method `upsert_proposal(proposal: ProposalRecord) -> UUID`. `record_promotion` calls it
  internally — no behavior change for the promotion path.
- New `ProposalMatch` frozen dataclass: `id: UUID`, `fingerprint: str`, `seen_count: int`.
- New method `find_awaiting_proposal(source, category) -> ProposalMatch | None` — most recent
  `sysgraph.proposal` row for `(source, category)`. Only called after `is_kind_decided` has
  already returned `False`, so no additional decided-filtering needed inside the query.
- New method `reinforce_proposal(proposal_id: UUID) -> None` —
  `UPDATE sysgraph.proposal SET seen_count = seen_count + 1, updated_at = NOW() WHERE id = $1`.

### 2. New module `src/personal_agent/sysgraph/dedup.py` (D9/D10 read-before-emit)
```python
class ReadBeforeEmitDecision(str, Enum):
    DECIDED_SKIP = "decided_skip"
    REINFORCED = "reinforced"
    GENERATE_NEW = "generate_new"
    DEGRADED_GENERATE_NEW = "degraded_generate_new"

@dataclass(frozen=True)
class ReadBeforeEmitResult:
    decision: ReadBeforeEmitDecision
    proposal_id: UUID | None

async def check_before_emit(
    repo: SysgraphRepository | None,
    *,
    source: str,
    category: str,
    proposal: ProposalRecord,
    trace_id: str | None = None,
) -> ReadBeforeEmitResult: ...
```
- `repo is None` → `GENERATE_NEW` (feature not wired at this call site; unchanged behavior).
- `is_kind_decided(source, category)` → `DECIDED_SKIP`, no write, `log.info`.
- else `find_awaiting_proposal` hit → `reinforce_proposal`, `REINFORCED`, `log.info`.
- else → `upsert_proposal`, `GENERATE_NEW`.
- any exception in the try (`repo.pool is None`, connection error, etc.) → `DEGRADED_GENERATE_NEW`,
  `log.warning("sysgraph_read_before_emit_degraded", ...)` — never raises, never blocks generation.
- Module-level D10 branch assertion: read `scripts/eval/fre720_insights_separation/probe_result.json`
  once, assert `["decision"] == "fallback"` in a unit test (not at import time — import-time I/O
  in a hot-path module is unnecessary risk); the module itself performs no embedding/vector call
  by construction (AC-10).

### 3. `src/personal_agent/insights/engine.py`
- `InsightsEngine.__init__` gains `sysgraph_repo: SysgraphRepository | None = None`.
- In `create_captain_log_proposals`, before appending each `CaptainLogEntry`, call
  `check_before_emit(self._sysgraph_repo, source="statistical_detector", category=category.value,
  proposal=ProposalRecord(...))`. `DECIDED_SKIP`/`REINFORCED` → skip this insight (no entry
  appended). `GENERATE_NEW`/`DEGRADED_GENERATE_NEW` → unchanged (append as today).

### 4. `src/personal_agent/captains_log/reflection.py`
- `generate_reflection_entry` gains `sysgraph_repo: SysgraphRepository | None = None`.
- After `_build_proposed_change(...)` produces a non-`None` `proposed_change` with a resolved
  `category` (both the DSPy-success and manual-parse-success return paths), call
  `check_before_emit(...)`. `DECIDED_SKIP`/`REINFORCED` → set `entry.proposed_change = None`
  before returning (rationale/metrics still recorded — "at most an annotation" per AC-9).

### 5. Wiring at call sites (best-effort connect/disconnect, mirrors existing precedent)
- `events/pipeline_handlers.py::build_consolidation_insights_handler` — connect a
  `SysgraphRepository` the same way `build_consolidation_promotion_handler` already does
  (try/except around `connect()`, `sysgraph_repo=None` on failure, `finally: disconnect()`).
- `orchestrator/executor.py::_trigger_captains_log_reflection` — same best-effort
  connect/disconnect pattern (mirrors `outcome_ingestion.py`), already inside the existing
  try/except that swallows all reflection failures.

## Testing (TDD — failing test first)

- `tests/personal_agent/sysgraph/test_dedup.py` (new):
  - decided kind → `DECIDED_SKIP`, no row written/changed.
  - awaiting kind (existing row, not decided) → `REINFORCED`, `seen_count` incremented, no 2nd row.
  - no existing row, not decided → `GENERATE_NEW`, new row created with `seen_count=1`.
  - `repo=None` → `GENERATE_NEW` (control: proves the *would-be duplicate* is created when the
    read is disabled — the AC-9 control-comparison requirement).
  - repo raises (simulated) → `DEGRADED_GENERATE_NEW`, warning logged, never raises.
  - AC-8/D10 mechanical check: `probe_result.json["decision"] == "fallback"`.
  - AC-10 dependency scan: `sysgraph.dedup`/`sysgraph.repository` modules import nothing from
    `personal_agent.memory` (the User-KG reranker/recall stack).
- `tests/personal_agent/sysgraph/test_repository.py` (extend): `upsert_proposal`,
  `find_awaiting_proposal`, `reinforce_proposal` against the test Postgres substrate (`sysgraph_pool`
  fixture, :5433).
- `tests/personal_agent/insights/test_engine.py` (extend): decided/awaiting insight suppressed;
  generate-new/degraded insight still produces a `CaptainLogEntry`.
- `tests/test_captains_log/test_reflection_source_adr_0105.py` (extend, since it already covers
  ADR-0105 source-discriminator behavior): decided/awaiting reflection proposal nulled;
  generate-new/degraded unaffected.

## Quality gates
`make test` (module: `test-file FILE=tests/personal_agent/sysgraph/test_dedup.py`, then full) ·
`make mypy` · `make ruff-check`/`format` · `pre-commit run --all-files`.

## Revisions after codex plan-review (2026-07-06)

Codex (`codex:codex-rescue`) reviewed the plan above against the live code and flagged six
issues. All are folded in below; the "Implementation" section above is superseded by this list
where they conflict.

1. **Fallback grain widened to `(source, category, scope)`.** Category-only would treat every
   awaiting proposal in a category as "equivalent," over-suppressing distinct ideas. `scope`
   already exists on `ProposedChange` for both producers at zero extraction cost — no new
   taxonomy needed, just one more matched column. New migration `0018_sysgraph_proposal_scope.sql`
   adds a nullable `sysgraph.proposal.scope TEXT` column (nullable so historical promotion-only
   rows written before this ticket aren't invalidated). `is_kind_decided`/`get_signal` (FRE-717,
   already shipped) intentionally stay at `(source, category)` grain — that's a coarser,
   stronger, rarer signal ("this whole category is decided") and changing its grain is a
   separate, riskier migration this ticket does not need. Only the new awaiting/generate-new
   check adds `scope` to its match key.
2. **Producer scope widened to every direct proposal-recording site**, not just the two files
   ADR-0105's Implementation Notes name. D9's own text says "before a **reflection or
   statistical producer** records a proposal" — the three direct `CONFIG_PROPOSAL` emitters in
   `events/pipeline_handlers.py` (`_handle_graph_quality_anomaly`, `_handle_staleness_reviewed`,
   `build_error_pattern_captain_log_handler`'s handler, `build_compaction_quality_captain_log_handler`'s
   handler) all construct `source=ProposalSource.STATISTICAL_DETECTOR` entries directly, bypassing
   `InsightsEngine.create_captain_log_proposals`. Excluding them would leave a real AC-9 gap.
   Each gets the same `check_before_emit` call immediately before its `_manager.save_entry(entry)`
   call; `DECIDED_SKIP`/`REINFORCED` → skip the save (call `reinforce_proposal` on the matched
   row instead when `REINFORCED`).
3. **Atomicity fixed with a transactional advisory lock.** The original two-call
   `find_awaiting_proposal` → `reinforce_proposal` sequence race if two producers hit the same
   key concurrently (no unique constraint exists on `(source, category, scope)`, only on
   `fingerprint`). Replaced with one repository method,
   `read_before_emit(source, category, scope, proposal) -> ReadBeforeEmitResult`, that does the
   whole decided-check + awaiting-lookup + reinforce-or-insert inside a single
   `async with conn.transaction()` guarded by
   `SELECT pg_advisory_xact_lock(hashtext($1))` keyed on `f"{source}:{category}:{scope or ''}"` —
   serializes concurrent producers on the same key without a new unique index.
4. **`seen_count` overwrite bug fixed by NOT sharing SQL between promotion and generation paths.**
   `_RECORD_PROMOTION_UPSERT_PROPOSAL`'s `ON CONFLICT (fingerprint) DO UPDATE SET seen_count =
   EXCLUDED.seen_count` is correct only for `record_promotion` (called once, with the
   already-accumulated authoritative count from the source `CaptainLogEntry`). The new
   generation-time insert path uses its own query,
   `ON CONFLICT (fingerprint) DO UPDATE SET seen_count = sysgraph.proposal.seen_count + 1`, so a
   repeat generation-time detection of the exact same fingerprint increments rather than
   clobbering a previously-recorded higher count. `record_promotion` is untouched.
5. **Exception handling narrowed.** `check_before_emit`/`read_before_emit` catch
   `(OSError, asyncpg.PostgresError)` plus an explicit `repo is None or repo.pool is None`
   guard — not bare `Exception`. A programming error (bad enum, malformed `ProposalRecord`)
   must raise and fail the test/call, not silently degrade to "sysgraph unreachable." Added a
   unit test asserting a `TypeError` from a caller bug propagates rather than degrading.
6. **Reflection's per-turn call site uses a shared singleton, not per-turn connect/disconnect.**
   `_trigger_captains_log_reflection` runs after *every* task; a fresh `asyncpg` pool
   (`min_size=1, max_size=5`) per turn is real, avoidable connection churn on the hottest call
   site in this ticket. Mirrors the existing `CaptainLogManager._default_es_handler` singleton
   pattern exactly: new `personal_agent.sysgraph.set_default_sysgraph_repo()` /
   `get_default_sysgraph_repo()` module functions; `service/app.py`'s `lifespan()` connects one
   `SysgraphRepository` at startup (best-effort, matching the existing ES/Neo4j graceful-degradation
   blocks around line 606-651) and disconnects it at shutdown (matching lines ~1205-1215).
   `_trigger_captains_log_reflection` reads the shared instance via the getter — no connect/disconnect
   in the per-turn path at all. The `events/pipeline_handlers.py` handlers (consolidation-triggered,
   not per-turn) keep their existing best-effort per-invocation connect/disconnect — that cadence
   (per consolidation run) is the same order of magnitude as the existing promotion handler, so
   codex's concern doesn't apply there.

**Correction found while implementing:** `tests/personal_agent/sysgraph/test_isolation.py::test_no_recall_path_imports_sysgraph`
(AC-2c, already shipped by FRE-714) asserts an AST-level import scan that
`memory/orchestrator/tools` modules never import `personal_agent.sysgraph` — and
`_trigger_captains_log_reflection` lives in `orchestrator/executor.py`. So executor.py must
**not** import `personal_agent.sysgraph` (directly or via a new parameter forcing the caller
to resolve it). Fix: `generate_reflection_entry` (in `captains_log/reflection.py`, which is
*not* an excluded root) resolves the shared singleton internally via
`personal_agent.sysgraph.get_default_sysgraph_repo()` when no repo is explicitly injected —
`orchestrator/executor.py`'s call site and imports are untouched by this ticket.

Residual scope decisions carried forward (flagged, not silently resolved):
- **AC-9's "counter... surfaces on the funnel or alert signal"** is satisfied via a structured
  `log.warning("sysgraph_read_before_emit_degraded", ...)` event, identical in kind to every other
  fail-open signal in this codebase. A dedicated Kibana panel is explicitly out of scope (owned by
  the separate ADR-0090 telemetry-surface project) — noted for master at the PR gate rather than
  silently assumed sufficient.
- **AC-10's literal wording** ("integration test with the tunnel unreachable and a reranker-failing
  test double") is satisfied structurally here — the fallback path never imports or calls any
  embedder/reranker code at all, proven by the dependency-scan test, plus a runtime test that runs
  `check_before_emit` successfully while embedder/reranker settings are deliberately misconfigured/
  unreachable, showing no coupling exists to make unreachable in the first place.
