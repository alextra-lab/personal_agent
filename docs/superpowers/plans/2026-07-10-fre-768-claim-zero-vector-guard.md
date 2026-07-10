# FRE-768 — Claim write path bakes zero-vector embeddings during embedder outage

Mirrors FRE-659 (entity path fix, `01ef2cd0`) onto the Claim substrate
(`memory/service.py::assert_claim`, `MemoryService.backfill_missing_embeddings`).

## Scope

1. **Write-path guard** in `assert_claim` (~line 1649): never persist a zero-vector
   `cl.embedding`. During an embedder outage, write the Claim without an embedding
   property (absent, not zeroed) rather than poisoning `claim_embedding` similarity/
   recall.
2. **Backfill extension**: `MemoryService.backfill_missing_embeddings` currently only
   repairs `Entity` nodes. Extend it to also repair `Claim` nodes with missing/zero
   embeddings, re-embedding from `cl.content` (matching how `assert_claim` computes the
   embedding today), under the same idempotent/outage-safe/guarded-write shape as the
   entity path. No new scheduler wiring needed — the existing hourly call in
   `brainstem/scheduler.py` already invokes this one method; it now covers both node
   types.
3. **Tests** (mocked driver, no live Neo4j — same pattern as
   `test_zero_vector_embedding_guard.py`): a new
   `tests/personal_agent/memory/test_claim_zero_vector_embedding_guard.py` covering:
   - `assert_claim` skips a zero-vector embedding (property absent from the write).
   - `assert_claim` persists a non-zero embedding.
   - Backfill populates a missing/zero Claim embedding once the embedder is back.
   - Backfill is a no-op (skips) when the embedder is still down.
   - Backfill's Claim write is guarded against a concurrent fresher write (same
     `WHERE cl.embedding IS NULL OR none(...)` guard as the entity path).
   - (Documentation test, codex finding) a zero-vector new-claim embedding produces no
     `matching_candidates` match even against an identical-content current Claim —
     proves/pins the known outage-mode duplicate-current limitation rather than
     silently relying on unverified behavior.
4. **Docs**: bump the scheduler comment (`brainstem/scheduler.py` ~line 575-577) and the
   `embedding_backfill_enabled` setting description (`config/settings.py` ~line 589) to
   say "entities and Claims" instead of just "entities" — the method they describe now
   covers both.
5. **Identity-threading allowlist**: `scripts/identity_threading_allowlist.yaml` pins
   `memory/service.py` line 1419 (dynamic entity `on_create_clauses` MERGE exemption).
   Inserting code above that line shifts it — bump the pinned `line:` to match after
   edits land (same pattern as FRE-659's own +5 bump).
6. **Known limitation, documented not fixed (codex review finding)**: during an
   embedder outage the new Claim's own embedding is also a zero vector, so
   `matching_candidates` (cosine against a zero vector) never matches an existing
   current Claim — the new Claim is always treated as unrelated-and-new rather than a
   possible supersession. Two Claims about the same fact-slot asserted during the same
   outage can both end up "current". The backfill this ticket adds only repairs
   embeddings; it does not re-run adjudication, so it will not retroactively collapse
   that duplicate-current state. This is a real gap but out of scope for this ticket
   (mirroring FRE-659's guard+backfill shape, not a supersession-engine change) — add a
   test that documents/proves the behavior explicitly, and file a follow-up
   Needs-Approval ticket for outage-mode re-adjudication (genuinely separate,
   ADR-adjacent design decision, per build skill §5).

## Implementation detail

### 1. `assert_claim` write-path guard

Current write is a single static Cypher string with `embedding: $embedding` inlined into
the `CREATE (o)-[:HAS_FACT]->(cl:Claim {...})` map literal — unlike the entity path,
which builds `SET` clauses dynamically and can just omit one. To keep the same
test-observable shape FRE-659 established (`"embedding" not in kwargs`,
`"cl.embedding" not in cypher` when zero), restructure the Claim CREATE to build the
query as list-joined segments, appending a conditional `SET cl.embedding = $embedding`
line (and the `embedding` param) only when `any(x != 0.0 for x in embedding)` — mirrors
the entity path's `set_clauses.append(...)` pattern instead of relying on Neo4j's
implicit null-in-map-drops-property behavior (correct, but not what the existing test
style asserts against).

### 2. Backfill extension

Split the current single-purpose method into two private helpers so each substrate's
read/write shape stays simple and independently testable, with the public method
summing their counts:

```python
async def backfill_missing_embeddings(self, *, batch_size=100, trace_id=None) -> int:
    if not self.connected or not self.driver:
        return 0
    entity_filled = await self._backfill_entity_embeddings(batch_size=batch_size, trace_id=trace_id)
    claim_filled = await self._backfill_claim_embeddings(batch_size=batch_size, trace_id=trace_id)
    return entity_filled + claim_filled
```

`_backfill_entity_embeddings` = today's method body, unchanged, renamed.

`_backfill_claim_embeddings` = same shape, Claim-specific:
- Read: `MATCH (cl:Claim) WHERE cl.content IS NOT NULL AND cl.content <> '' AND (cl.embedding IS NULL OR none(x IN cl.embedding WHERE x <> 0.0)) RETURN cl.claim_id AS claim_id, cl.content AS content ORDER BY cl.claim_id LIMIT $batch_size`
- Embed texts = `[c["content"] for c in candidates]` (matches `assert_claim`'s
  `generate_embedding(claim.content)` — no name-prefixing, unlike Entity's
  `f"{name}: {description}"`).
- Guarded write keyed by `claim_id` (Claim's natural unique key, in place of Entity's
  `name`): `UNWIND $updates AS u MATCH (cl:Claim {claim_id: u.claim_id}) WHERE cl.embedding IS NULL OR none(x IN cl.embedding WHERE x <> 0.0) SET cl.embedding = u.embedding RETURN count(*) AS filled`
- Distinct log event names (codex-flagged decision — deliberately not reusing FRE-659's
  entity event names, so ES telemetry can tell substrates apart without a new field
  convention): `claim_embedding_backfill_completed` /
  `claim_embedding_backfill_skipped_embedder_down` / `claim_embedding_backfill_error`.

Both helpers reuse the existing `generate_embeddings_batch` import — no new imports.

Docstring on the public `backfill_missing_embeddings` will be updated to state
`batch_size` bounds *each* substrate independently (codex finding: a single call can
now repair up to `2 * batch_size` nodes total, not `batch_size` — this is intentional,
just needs to be documented so the scheduler's cost-per-tick assumption stays accurate).

## Files touched

- `src/personal_agent/memory/service.py` — guard + backfill split/extension
- `tests/personal_agent/memory/test_claim_zero_vector_embedding_guard.py` — new
- `src/personal_agent/brainstem/scheduler.py` — comment only
- `src/personal_agent/config/settings.py` — description string only
- `scripts/identity_threading_allowlist.yaml` — bump pinned line number

## Acceptance criteria (from ticket)

- AC-1: No zero-vector Claim embeddings are ever persisted (write-path guard test).
- AC-2: Claims written during an embedder outage are recoverable by the backfill
  without manual intervention (backfill-populates test).
- AC-3 (implicit): backfill is outage-safe/idempotent and never clobbers a concurrent
  fresher write (skip-when-down test + guarded-write test) — same proof shape FRE-659
  used for the entity path, since there is no live vector index in the unit-test
  substrate to directly prove "a vector query then recalls the Claim"; the guarded
  non-zero write is the mechanism that enables it.

## Test commands

```bash
make test-file FILE=tests/personal_agent/memory/test_claim_zero_vector_embedding_guard.py
make test-file FILE=tests/personal_agent/memory/test_zero_vector_embedding_guard.py  # no regression
make mypy
make ruff-check
pre-commit run --all-files
```
