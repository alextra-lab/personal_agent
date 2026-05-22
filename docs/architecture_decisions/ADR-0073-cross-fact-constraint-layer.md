# ADR-0073: Cross-Fact Constraint Layer for Memory Pipeline

**Status:** Proposed
**Date:** 2026-05-22
**Issue:** FRE-374
**Supersedes:** —
**Related:** ADR-0071 (two-source one-gate memory model), ADR-0072 (test/prod substrate isolation)

## Context

Four independent harms were measured on the live VPS memory pipeline (Probes 1–6,
`docs/research/2026-05-21-memory-integration-probe-report.md`):

1. **Token waste:** 76.9% of gateway turns (1,343/1,747 in 30 days) inject memory
   context. Empty-description entity lines like `- [LOCATION] Paris:  (mentioned 328x)`
   pass through to the LLM with no informational value.

2. **Empty descriptions:** Top-mention entities (Paris: 328x, London: 168x) have no
   descriptions, despite the system's stated purpose of helping with frequently-discussed
   topics.

3. **Cross-contaminated descriptions:** `Neo4j` is described as "Query language used to
   interact with Neo4j" (Cypher's definition). `Postgres` and `Redis Streams` carry
   similar misattributions. These reach the prompt verbatim.

4. **Redundant relationship types:** 237 of 2,541 entity pairs (9.3%) carry 2–5
   duplicate edge types (e.g., Docker ↔ Neo4j: PART_OF×2, RELATED_TO, USES×2).
   The quality monitor did not detect this.

Root causes:
- `service.py:605` applied `SET e.description = $description` unconditionally (fixed by
  FRE-375 to first-write-wins CASE WHEN, but historical contamination persists).
- 87% of last-7d Turn nodes had `session_id: NULL` — synthetic eval traffic wrote fake
  descriptions that overwrote real ones (also fixed by FRE-375).
- No render-time guard skips empty or known-bad lines.
- `apoc.merge.relationship()` deduplicates exact-type edges but not semantically
  overlapping types.
- Quality monitor measured only structural metrics, not content quality.

## Decisions

### D1 — Render-time empty-description filter (implement now)

Skip entity lines where `description` is None or empty string before rendering the
memory section in `executor.py`. Do not emit a placeholder like "(description pending)"
— silence is better than noise. This is a 3-line change with zero schema risk.

### D2 — Quality monitor: redundant-edge and empty-description signals (implement now)

Add two fields to `GraphHealthReport`:
- `empty_description_entity_count: int` — entities with `description IS NULL OR description = ''`
- `redundant_relationship_pairs: int` — entity pairs carrying more than one distinct
  relationship type between them

Add two corresponding anomaly types to `detect_anomalies()`:
- `"empty_description_rate_high"` (threshold: >10% of entities, severity: `"medium"`)
- `"redundant_relationship_pairs_high"` (threshold: >50 pairs, severity: `"medium"`)

### D3 — Backfill replay script (implement now)

New `scripts/replay_sessions_to_neo4j.py` queries all Postgres sessions by
`created_at` ASC, extracts user/assistant message pairs, constructs `TaskCapture`
objects, and calls `SecondBrainConsolidator._process_capture()` for each. Flags:
`--dry-run` (log only), `--since YYYY-MM-DD`, `--limit N`, `--confirm-prod` (required
outside TEST env), `--sleep-ms` (rate-limiting between captures).

Pre-requisite operator steps (manual, documented here, not coded):
1. Take Neo4j snapshot: `docker exec seshat-neo4j neo4j-admin dump --to=/backups/neo4j-pre-fre374-$(date +%F).dump`
2. Clear the graph: `MATCH (n) DETACH DELETE n` (only after snapshot confirmed)
3. Run the replay script: `uv run python scripts/replay_sessions_to_neo4j.py --since 2025-01-01 --confirm-prod`
4. Re-run Probe scripts 1, 2, 5, 6.

### D4 — Description provenance (deferred — redesign required)

**Original proposal:** Replace the single `description` string with an append-and-version
array `e.descriptions = [{text, turn_id, extractor_role, ts}]`.

**2026-05-22 perf-probe finding:** This schema is **architecturally incompatible with
Neo4j**. Running `scripts/research/fre374_provenance_perf_probe.py` against the test
stack, Pattern A (current `CASE WHEN`) succeeded; Pattern B (descriptions[] array of
maps) failed with:

```
neo4j.exceptions.CypherTypeError: Property values can only be of primitive types
or arrays thereof. Encountered: Map{text -> ..., ts -> ...}
```

Neo4j property values may only be primitives (string, int, float, bool, datetime,
spatial) or **arrays of primitives** — not arrays of maps. The proposed schema cannot
be implemented as a node property.

**Two viable alternatives surface from this finding:**

1. **JSON-string array** — `e.descriptions = COALESCE(e.descriptions, []) + [$json_str]`
   where `$json_str = json.dumps({text, ts, extractor_role, turn_id})`. Cypher cannot
   query nested fields natively, but Python deserializes the strings on read. Lowest
   schema change; canonical-view computation in Python.

2. **Separate `:DescriptionVersion` nodes** — `(:Entity)-[:HAS_DESCRIPTION]->(:DescriptionVersion {text, ts, extractor_role, turn_id})`.
   Proper graph model. Fully queryable from Cypher. Adds one relationship traversal
   per description read; canonical view becomes a Cypher pattern with `ORDER BY` +
   `LIMIT 1`.

**Status:** Deferred pending design decision between (1) and (2). Perf measurement of
the new approach (whichever is chosen) is the next gate; the original "measure array
of maps" probe is moot.

A follow-up issue will choose the approach and ship the migration.

### D5 — Relationship semantic dedup (deferred — type ontology required)

`apoc.merge.relationship()` already deduplicates exact-type edges (idempotent). The
9.3% redundant-type problem requires either a type-normalization map
(`USES → PART_OF → RELATED_TO` consolidation) or an LLM-assisted dedup pass. Neither
is trivially correct without a defined type ontology. Deferred to a follow-up issue
after D2's quality-monitor signal provides a live count baseline.

## Consequences

**Positive:**
- Empty-description entities disappear from prompts immediately after D1 lands.
- The quality monitor gains coverage of the two most harmful graph conditions (D2).
- After the backfill replay (D3), the production graph has extractor-stamped
  descriptions from the current gpt-5.4-mini model for all real sessions.
- Performance data from D4 benchmark informs the schema migration decision.

**Negative / tradeoffs:**
- D1 reduces the size of the memory section for entities that have no description yet
  — the LLM sees fewer entities until the backfill lands. Acceptable: blank lines were
  not helping anyway.
- **D1 creates a transient "no memory" window during D3.** When the graph is cleared
  before replay starts, every entity has an empty description, so `_render_memory_section`
  returns `""` and the "Do NOT say you have no memory" instruction disappears with it.
  The LLM will correctly behave as if it has no memory during this window (minutes to
  hours depending on replay speed). Mitigate by running the replay immediately after
  clearing, ideally in a maintenance window.
- D3 replay is a destructive operation on the production graph. The snapshot + guard
  (`--confirm-prod`) mitigate risk; data is recoverable from Postgres.
- D3 replay makes ~3,000–5,000 gpt-5.4-mini calls (1,025 sessions × ~3–5 pairs each).
  If the FRE-303 weekly budget cap is close to its limit, the replay may be interrupted.
  Check remaining budget before starting and use `--limit N` to batch if needed.
- D4 deferred means descriptions remain single-value first-write-wins until the follow-up.

## Verification

After the backfill replay (D3):
- Probe 1 re-run: top-15 entity empty-description count should be ≤ 2 (down from 7).
- Probe 2 re-run: redundant-relationship-pair count should be ≤ 50 (baseline: 237).
- Probe 5 re-run: memory-injected turns should inject ≥ 12 non-empty lines per context.
- Probe 6 re-run: `test_turns` (session_id NULL) should be 0 for the last 7 days.
