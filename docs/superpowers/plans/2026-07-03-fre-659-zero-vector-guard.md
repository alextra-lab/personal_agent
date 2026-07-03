# FRE-659 — Zero-vector embedding write-guard + idempotent backfill

**Ticket:** FRE-659 (Approved, Memory Recall Quality project, Tier-2)
**Backing context:** FRE-656 (tiered/failover embedder) — latent correctness bug, independent of that work.
**Branch:** `fre-659-zero-vector-guard`

## Problem

When the embedder is unreachable, `generate_embedding` degrades to a **zero vector**
(`embeddings.py:103` — returns `[0.0]*dim` on exception). `create_entity` then persists it:
the guard at `service.py:1373` is only `if embedding is not None`, so a zero vector (which is
*not* `None`) is written to `e.embedding` (`:1374`). The non-zero guard at `:1303` protects only
the dedup similarity check, not persistence.

**Effect:** every entity created during an embedder outage gets a zero-vector embedding baked into
the `entity_embedding` index. A zero vector has no meaningful cosine to any query, so those entities
are unrecallable by vector — and nothing re-embeds them when the embedder returns. Silent, permanent
corruption until a manual re-embed.

## Acceptance criteria (from the ticket — the definition of done)

- **AC-1** No zero-vector embeddings are ever persisted by the write path.
- **AC-2** Entities written during an embedder outage are recoverable by the backfill **without
  manual intervention** (i.e. an automatic periodic task, not a hand-run script).

## Scope (surgical — entity write path only)

The Claim write path (`service.py:~1648`, `embedding: $embedding` in the `CREATE (cl:Claim {...})`)
has the **identical** unconditional-persist bug on the Claim substrate — **out of scope** for this
entity-scoped ticket; filed as a Step-5 follow-up.

## Changes

### 1. Write-path guard — `src/personal_agent/memory/service.py:1373`

```python
# BEFORE
if embedding is not None:
    set_clauses.append("e.embedding = $embedding")
    params["embedding"] = embedding

# AFTER
# FRE-659: never persist a zero-vector embedding (baked when the embedder is
# unreachable — generate_embedding degrades to a zero vector). Persist only a
# real vector; a missing embedding is repaired by the periodic backfill once
# the embedder returns.
if embedding is not None and any(x != 0.0 for x in embedding):
    set_clauses.append("e.embedding = $embedding")
    params["embedding"] = embedding
```

During an outage the entity is created **without** an embedding (absent, not zeroed) → simply missing
from the vector index rather than poisoning it. Consistent with the existing dedup skip at `:1303`.

### 2. Idempotent backfill method — `MemoryService.backfill_missing_embeddings`

New async method on `MemoryService` (next to `create_entity`, reuses `self.driver` +
`generate_embeddings_batch` — one batch embed call, not N sequential; keeps the serial
`_lifecycle_loop` from blocking on 100 HTTP round-trips):

```python
async def backfill_missing_embeddings(
    self,
    *,
    batch_size: int = 100,
    trace_id: str | None = None,
) -> int:
    """Re-embed entities whose embedding is missing or zero-vectored (FRE-659).

    Idempotent remediation for the zero-vector corruption. An entity created while
    the embedder was unreachable is either missing ``e.embedding`` (post-fix path) or
    carries a baked-in zero vector (pre-fix corruption). This pass finds such entities
    (bounded to ``batch_size``, deterministic ``ORDER BY e.name`` so runs converge and
    do not re-select the same page), batch-regenerates their embeddings, and persists
    ONLY the non-zero results — so a run during a continuing outage is a safe no-op
    rather than re-poisoning the index.

    The write is guarded (``WHERE e.embedding IS NULL OR none(...)``) so it never
    clobbers a fresher non-zero embedding a concurrent ``create_entity`` may have
    written between the read and the write.
    ...
    """
```

Read (deterministic, materialized before mutating):

```cypher
MATCH (e:Entity)
WHERE e.description IS NOT NULL AND e.description <> ''
  AND (e.embedding IS NULL OR none(x IN e.embedding WHERE x <> 0.0))
RETURN e.name AS name, e.description AS description
ORDER BY e.name
LIMIT $batch_size
```

Embed the batch (`generate_embeddings_batch([f"{name}: {description}" ...])`, same text format as
`create_entity:1296`), keep only non-zero results as `updates=[{name, embedding}, ...]`. If none
survive (embedder still down) → log `embedding_backfill_skipped_embedder_down`, return 0. Otherwise
one guarded UNWIND write:

```cypher
UNWIND $updates AS u
MATCH (e:Entity {name: u.name})
WHERE e.embedding IS NULL OR none(x IN e.embedding WHERE x <> 0.0)
SET e.embedding = u.embedding
RETURN count(*) AS filled
```

`filled` (RETURN count) is the actual number written under the guard. Not-connected → return 0;
wrap in try/except → log `embedding_backfill_error` with `trace_id`, return 0.

**Behaviour notes (codex plan-review):** empty-list embedding is treated as zero-vector and re-embedded
(intentional). A *legitimately* all-zero embedding is indistinguishable from outage degradation — safe
under the model's dense-vector invariant (real embeddings are never all-zero). Malformed/scalar
`e.embedding` values are out of scope (data invariant: embeddings are float lists). Single gateway
process on the VPS → no distributed-lock concern. Large existing corruption converges over
`ceil(N/batch_size)` hourly runs once the embedder is up (bounded by the outage window; no catch-up
mode by design).

### 3. Scheduler wiring — `src/personal_agent/brainstem/scheduler.py`

- Constant near `BACKFILL_INTERVAL_SECONDS` (`:63`):
  `EMBEDDING_BACKFILL_INTERVAL_SECONDS = 3600  # Entity embedding backfill hourly (FRE-659)`
- `__init__`: `self._last_embedding_backfill_run: datetime | None = None`
- `_lifecycle_loop` (after the captains-log backfill block, `~:568`):

```python
# Entity embedding backfill (FRE-659): re-embed entities missing/zeroed
# embeddings when the embedder is reachable — idempotent, outage-safe.
if (
    self.memory_service is not None
    and getattr(settings, "embedding_backfill_enabled", True)
    and (
        self._last_embedding_backfill_run is None
        or (now - self._last_embedding_backfill_run).total_seconds()
        >= EMBEDDING_BACKFILL_INTERVAL_SECONDS
    )
):
    eb_trace_id = _new_scheduler_trace_id("scheduler.embedding_backfill")
    try:
        filled = await self.memory_service.backfill_missing_embeddings(trace_id=eb_trace_id)
        self._last_embedding_backfill_run = now
        if filled:
            log.info("embedding_backfill_completed", entities_filled=filled, trace_id=eb_trace_id)
    except Exception as eb_err:
        log.warning(
            "embedding_backfill_failed", error=str(eb_err), exc_info=True, trace_id=eb_trace_id
        )
```

### 4. Setting — `src/personal_agent/config/settings.py` (recall section, ~:552)

```python
embedding_backfill_enabled: bool = Field(
    default=True,
    description=(
        "FRE-659: periodically re-embed entities whose embedding is missing or "
        "zero-vectored (baked during an embedder outage). Idempotent and outage-safe "
        "(persists only a non-zero vector). Default on; off-switch for the recall substrate."
    ),
)
```

## Tests (TDD, fast unit — mocked driver, no live Neo4j → runs in `make test`)

New file `tests/personal_agent/memory/test_zero_vector_embedding_guard.py`, mock-driver pattern from
`test_neo4j_origination_properties.py`:

1. **`test_create_entity_skips_zero_vector_embedding`** (AC-1) — patch
   `personal_agent.memory.service.generate_embedding` → `[0.0]*dim`; `create_entity(Entity(...))`;
   assert emitted Cypher has **no** `e.embedding = $embedding` and `"embedding" not in kwargs`.
2. **`test_create_entity_persists_nonzero_embedding`** — patch → non-zero vector; assert Cypher
   **includes** `e.embedding = $embedding` and `kwargs["embedding"]` equals the vector.
3. **`test_backfill_populates_missing_embedding_when_embedder_up`** (AC-2) — mock the candidate read
   (`ORDER BY e.name ... LIMIT`) → one row `{name, description}`; patch `generate_embeddings_batch`
   → `[non-zero]`; assert the guarded `UNWIND ... SET e.embedding` write fired with `updates=[{name,
   embedding}]`; return value `== 1` (from mocked `count(*)` → `{"filled": 1}`).
4. **`test_backfill_skips_when_embedder_still_down`** — same candidate; patch batch → `[[0.0]*dim]`;
   assert **no** UNWIND write fired and return `== 0` (outage-safe). Running it twice still `== 0`
   (repeated-batch-under-outage).
5. **`test_backfill_write_is_guarded_against_concurrent_writes`** — assert the write Cypher contains
   `WHERE e.embedding IS NULL OR none(x IN e.embedding WHERE x <> 0.0)` (never clobbers a fresher
   concurrent embedding).

Write test 1 first, confirm it **fails** against current `:1373` (zero vector currently persisted),
then apply the guard.

## Step 5 — follow-up ticket

File (Needs Approval, Memory Recall Quality): *"Claim write path bakes zero-vector embeddings during
embedder outage — same defect as FRE-659, on the Claim substrate"* — `service.py:~1648`
`CREATE (cl:Claim { embedding: $embedding })` persists unconditionally; propose the same non-zero guard
+ extend the backfill to Claims.

## Quality gates

`make test-k K=zero_vector` → `make test` · `make mypy` · `make ruff-check` + `make ruff-format` ·
`pre-commit run --all-files`.

## Master handoff notes

- **No deploy of its own class beyond gateway rebuild** — this is a `seshat-gateway` code change
  (ask-first deploy class). Backfill runs in-process via the brainstem scheduler once deployed.
- **Live remediation:** the first backfill runs ~60s after gateway startup (the `_lifecycle_loop`
  sleeps `LIFECYCLE_CHECK_INTERVAL_SECONDS` before its first iteration), then hourly. Each run repairs
  up to `batch_size=100` corrupted/missing entities, provided the embedder (:8503) is up; large
  backlogs converge over successive hourly runs. Verify: `embedding_backfill_completed filled=N` in
  logs; then a vector query recalls a previously-corrupted entity.
- **Quantify existing damage + verify backlog → 0** (Neo4j :7687 prod / :7688 test):
  ```cypher
  MATCH (e:Entity)
  WHERE e.description IS NOT NULL AND e.description <> ''
    AND (e.embedding IS NULL OR none(x IN e.embedding WHERE x <> 0.0))
  RETURN count(e) AS corrupted
  ```
  Run pre-deploy (baseline) and after a few hourly ticks (should trend to 0 while the embedder is up).
- **Safety:** outage-safe by construction (never writes a zero vector; guarded SET never clobbers a
  fresher concurrent embedding); `embedding_backfill_enabled` off-switch if it misbehaves.
