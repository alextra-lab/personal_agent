# FRE-374: Cross-Fact Constraint Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four measured memory-pipeline harms — empty descriptions in prompt, cross-contaminated descriptions, redundant relationship types, wasted context-budget — via a render-time fix, quality-monitor signals, a backfill replay script, and an ADR documenting the full design.

**Architecture:** Four independent deliverables land in sequence: (1) ADR-0073 documents the design; (2) `executor.py` stops emitting blank entity lines; (3) `quality_monitor.py` gains two new anomaly types; (4) a new `scripts/replay_sessions_to_neo4j.py` script lets the operator purge the polluted graph and replay real Postgres sessions through the current extractor. Description provenance (append-and-version schema) and relationship semantic dedup are explicitly deferred — they need performance measurement before commitment, which Task 5 provides.

**Tech Stack:** Python 3.12, Neo4j 5.26, PostgreSQL 17, pydantic v2, structlog, asyncpg, pytest-asyncio, `apoc.merge.relationship`

**Linear:** [FRE-374](https://linear.app/frenchforest/issue/FRE-374) · Approved · Tier-1:Opus · blocks FRE-178/179/180

---

## Scope decisions

| Item | This plan | Reason |
|---|---|---|
| Render-time empty-description filter | ✅ | 3 lines, huge UX win, zero risk |
| Quality monitor: redundant-edge + empty-description signals | ✅ | Cypher-only, safe |
| Backfill replay script (Postgres → extractor → Neo4j) | ✅ | Needed for empty-rate AC |
| Provenance on-write performance measurement | ✅ | Required gate before schema migration |
| Description provenance array schema migration | ❌ deferred | Needs perf measurement first |
| Relationship semantic dedup | ❌ deferred | Needs type-ontology design |

---

## Critical file map

| File | Action | Responsibility |
|---|---|---|
| `src/personal_agent/orchestrator/executor.py:1729-1732` | Modify | Skip entity lines with empty/None description |
| `src/personal_agent/second_brain/quality_monitor.py` | Modify | Add `empty_description_entity_count`, `redundant_relationship_pairs` to `GraphHealthReport`; add two anomaly types |
| `scripts/replay_sessions_to_neo4j.py` | Create | Query Postgres sessions, build TaskCaptures, call consolidator; `--dry-run`, `--since`, `--limit`, `--confirm-prod` |
| `docs/architecture_decisions/ADR-0073-cross-fact-constraint-layer.md` | Create | ADR with Status: Proposed |
| `tests/personal_agent/orchestrator/test_memory_render_filter.py` | Create | Unit tests for empty-description filter |
| `tests/personal_agent/second_brain/test_quality_monitor_new_signals.py` | Create | Unit tests for two new anomaly types |

---

## Task 1: Write ADR-0073

**Files:**
- Create: `docs/architecture_decisions/ADR-0073-cross-fact-constraint-layer.md`

- [ ] **Step 1: Create ADR-0073**

Create `docs/architecture_decisions/ADR-0073-cross-fact-constraint-layer.md` with the following content (write the file verbatim):

```markdown
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
outside TEST env).

Pre-requisite operator steps (manual, documented here, not coded):
1. Take Neo4j snapshot: `docker exec seshat-neo4j neo4j-admin dump --to=/backups/neo4j-pre-fre374-$(date +%F).dump`
2. Clear the graph: `MATCH (n) DETACH DELETE n` (only after snapshot confirmed)
3. Run the replay script: `uv run python scripts/replay_sessions_to_neo4j.py --since 2025-01-01 --confirm-prod`
4. Re-run Probe scripts 1, 2, 5, 6.

### D4 — Description provenance (deferred — perf measurement required)

Replacing the single `description` string with an append-and-version array
`e.descriptions = [{text, turn_id, extractor_role, ts}]` requires every MATCH that
reads `e.description` to instead compute a canonical view. Before committing, a
benchmark must measure the write overhead on a graph of 4,000+ entities. Task 5 of
the FRE-374 implementation plan runs this benchmark; a follow-up issue will ship the
schema migration once the result is in hand.

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
- D3 replay is a destructive operation on the production graph. The snapshot + guard
  (`--confirm-prod`) mitigate risk; data is recoverable from Postgres.
- D4 deferred means descriptions remain single-value first-write-wins until the follow-up.

## Verification

After the backfill replay (D3):
- Probe 1 re-run: top-15 entity empty-description count should be ≤ 2 (down from 7).
- Probe 2 re-run: redundant-relationship-pair count should be ≤ 50 (baseline: 237).
- Probe 5 re-run: memory-injected turns should inject ≥ 12 non-empty lines per context.
- Probe 6 re-run: `test_turns` (session_id NULL) should be 0 for the last 7 days.
```

- [ ] **Step 2: Commit**

```bash
cd /opt/seshat
git add docs/architecture_decisions/ADR-0073-cross-fact-constraint-layer.md
git commit -m "docs(fre-374): ADR-0073 cross-fact constraint layer — Status: Proposed"
```

---

## Task 2: Render-time empty-description filter

**Files:**
- Modify: `src/personal_agent/orchestrator/executor.py:1729-1733`
- Test: `tests/personal_agent/orchestrator/test_memory_render_filter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/personal_agent/orchestrator/test_memory_render_filter.py`:

```python
"""Tests for FRE-374: executor skips entity lines with empty/None descriptions."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_ctx(entities: list[dict]) -> MagicMock:
    """Build a minimal GatewayOutput-like ctx with memory_context."""
    ctx = MagicMock()
    ctx.memory_context = entities
    ctx.session_history = []
    ctx.has_session_history = False
    ctx.task_type = MagicMock()
    ctx.task_type.value = "general"
    return ctx


class TestMemoryRenderFilter:
    """Render-time empty-description filter (FRE-374 D1)."""

    def _render_memory_section(self, entities: list[dict]) -> str:
        """Extract just the memory section from executor rendering logic."""
        from personal_agent.orchestrator.executor import _render_memory_section
        return _render_memory_section(entities)

    def test_entity_with_description_is_included(self) -> None:
        """Entities with a non-empty description are rendered normally."""
        lines = self._render_memory_section([
            {"type": "entity", "name": "Neo4j", "entity_type": "Technology",
             "description": "Graph database system.", "mentions": 100},
        ])
        assert "Neo4j" in lines
        assert "Graph database system." in lines

    def test_entity_with_none_description_is_skipped(self) -> None:
        """Entities with description=None are not emitted."""
        lines = self._render_memory_section([
            {"type": "entity", "name": "Paris", "entity_type": "LOCATION",
             "description": None, "mentions": 328},
        ])
        assert "Paris" not in lines

    def test_entity_with_empty_string_description_is_skipped(self) -> None:
        """Entities with description='' are not emitted."""
        lines = self._render_memory_section([
            {"type": "entity", "name": "London", "entity_type": "LOCATION",
             "description": "", "mentions": 168},
        ])
        assert "London" not in lines

    def test_entity_with_whitespace_only_description_is_skipped(self) -> None:
        """Entities with description='   ' (whitespace only) are not emitted."""
        lines = self._render_memory_section([
            {"type": "entity", "name": "Venice", "entity_type": "Location",
             "description": "   ", "mentions": 21},
        ])
        assert "Venice" not in lines

    def test_mixed_entities_only_described_appear(self) -> None:
        """Only entities with substantive descriptions are rendered."""
        lines = self._render_memory_section([
            {"type": "entity", "name": "Paris", "entity_type": "LOCATION",
             "description": None, "mentions": 328},
            {"type": "entity", "name": "Neo4j", "entity_type": "Technology",
             "description": "Graph database.", "mentions": 287},
            {"type": "entity", "name": "London", "entity_type": "LOCATION",
             "description": "", "mentions": 168},
        ])
        assert "Paris" not in lines
        assert "London" not in lines
        assert "Neo4j" in lines

    def test_empty_memory_context_produces_no_section(self) -> None:
        """Empty entity list produces empty string (no header, no footer)."""
        lines = self._render_memory_section([])
        assert lines == ""

    def test_all_empty_descriptions_produces_no_section(self) -> None:
        """When ALL entities have empty descriptions, the whole section is suppressed."""
        lines = self._render_memory_section([
            {"type": "entity", "name": "Paris", "entity_type": "LOCATION",
             "description": None, "mentions": 328},
            {"type": "entity", "name": "London", "entity_type": "LOCATION",
             "description": "", "mentions": 168},
        ])
        assert "Your Memory Graph" not in lines
        assert "Do NOT say you have no memory" not in lines
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
cd /opt/seshat && make test-file FILE=tests/personal_agent/orchestrator/test_memory_render_filter.py 2>&1 | tail -5
```

Expected: `ImportError: cannot import name '_render_memory_section'` or similar — test file exists, helper function doesn't yet.

- [ ] **Step 3: Extract a helper function and apply the filter**

In `src/personal_agent/orchestrator/executor.py`, find lines 1725–1739 (the broad-recall render block). Extract the entity-line building into a module-level helper and apply the empty-description filter.

Add this function BEFORE the class definition (near other module-level helpers in the file, or at the bottom of the module — check the file structure first):

```python
def _render_memory_section(entity_items: list[dict]) -> str:
    """Build the ## Your Memory Graph entity section string.

    Skips entities with None or blank descriptions (FRE-374 D1) so the
    LLM does not receive empty lines like '- [LOCATION] Paris:  (mentioned 328x)'.

    Args:
        entity_items: List of entity dicts from memory_context.

    Returns:
        Formatted memory section string, or empty string if no described entities.
    """
    described = [
        m for m in entity_items[:15]
        if (m.get("description") or "").strip()
    ]
    if not described:
        return ""
    entity_lines = [
        f"- [{m.get('entity_type', '')}] {m.get('name', '')}: {m.get('description', '').strip()} "
        f"(mentioned {m.get('mentions', 1)}x)"
        for m in described
    ]
    section = "\n\n## Your Memory Graph — Known Entities\n"
    section += "\n".join(entity_lines)
    section += (
        "\n\nUse this list to directly answer questions about what the user "
        "has previously discussed. Do NOT say you have no memory."
    )
    return section
```

Then replace lines 1729–1739 in the existing render block:

```python
# Before (lines 1729-1739):
entity_lines = [
    f"- [{m.get('entity_type', '')}] {m.get('name', '')}: {m.get('description', '')} "
    f"(mentioned {m.get('mentions', 1)}x)"
    for m in entity_items[:15]
]
memory_section = "\n\n## Your Memory Graph — Known Entities\n"
memory_section += "\n".join(entity_lines)
memory_section += (
    "\n\nUse this list to directly answer questions about what the user "
    "has previously discussed. Do NOT say you have no memory."
)

# After (lines 1729-1731):
memory_section = _render_memory_section(entity_items)
if not memory_section:
    pass  # No described entities — skip the section entirely
```

Wait — the `memory_section` variable is appended to `system_prompt` later. If it's empty, nothing is added. Check how `memory_section` is used after line 1739 in the actual file and make sure an empty string works correctly (it should — string concatenation with `""` is a no-op).

- [ ] **Step 4: Run tests**

```bash
cd /opt/seshat && make test-file FILE=tests/personal_agent/orchestrator/test_memory_render_filter.py 2>&1 | tail -5
```

Expected: `7 passed`

- [ ] **Step 5: Run existing executor tests to confirm no regression**

```bash
cd /opt/seshat && make test-k "executor" 2>&1 | tail -5
```

Expected: all executor-related tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/orchestrator/executor.py tests/personal_agent/orchestrator/test_memory_render_filter.py
git commit -m "fix(fre-374): skip empty-description entities in memory section render"
```

---

## Task 3: Quality monitor — new signals

**Files:**
- Modify: `src/personal_agent/second_brain/quality_monitor.py`
- Test: `tests/personal_agent/second_brain/test_quality_monitor_new_signals.py`

- [ ] **Step 1: Write failing tests**

Create `tests/personal_agent/second_brain/test_quality_monitor_new_signals.py`:

```python
"""Tests for FRE-374: redundant-relationship and empty-description quality signals."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from personal_agent.second_brain.quality_monitor import (
    ConsolidationQualityMonitor,
    GraphHealthReport,
    Anomaly,
)


def _make_monitor_with_mocked_service() -> tuple[ConsolidationQualityMonitor, MagicMock]:
    """Build a monitor with a mocked MemoryService (connected, with mock driver)."""
    mock_service = MagicMock()
    mock_service.connected = True
    mock_service.driver = MagicMock()
    mock_session = AsyncMock()
    mock_service.driver.session = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    mock_queries = MagicMock()
    monitor = ConsolidationQualityMonitor(mock_service, mock_queries)
    return monitor, mock_service


class TestGraphHealthReportNewFields:
    """GraphHealthReport must include the two new fields."""

    def test_graph_health_report_has_empty_description_entity_count(self) -> None:
        """GraphHealthReport has empty_description_entity_count field."""
        report = GraphHealthReport(
            total_nodes=100,
            conversation_nodes=10,
            entity_nodes=90,
            relationship_count=50,
            relationship_density=0.55,
            orphaned_entities=5,
            orphaned_entity_rate=0.055,
            clustered_entity_rate=0.8,
            max_temporal_gap_hours=24.0,
            empty_description_entity_count=15,
            redundant_relationship_pairs=10,
        )
        assert report.empty_description_entity_count == 15
        assert report.redundant_relationship_pairs == 10

    def test_graph_health_report_defaults_to_zero(self) -> None:
        """New fields default to 0 for backward compatibility."""
        # Construct without the new fields
        report = GraphHealthReport(
            total_nodes=0, conversation_nodes=0, entity_nodes=0,
            relationship_count=0, relationship_density=0.0,
            orphaned_entities=0, orphaned_entity_rate=0.0,
            clustered_entity_rate=0.0, max_temporal_gap_hours=0.0,
        )
        assert report.empty_description_entity_count == 0
        assert report.redundant_relationship_pairs == 0


class TestNewAnomalyTypes:
    """detect_anomalies fires new anomaly types when thresholds breached."""

    @pytest.mark.asyncio
    async def test_empty_description_rate_high_fires_when_threshold_exceeded(self) -> None:
        """empty_description_rate_high anomaly fires when >10% entities have no description."""
        monitor, _ = _make_monitor_with_mocked_service()

        # Mock check_entity_extraction_quality and check_graph_health
        mock_quality = MagicMock()
        mock_quality.entities_per_conversation_ratio = 1.0
        mock_quality.duplicate_rate = 0.0
        mock_quality.extraction_failure_rate = 0.0

        mock_health = MagicMock()
        mock_health.relationship_density = 1.5
        mock_health.entity_nodes = 100
        mock_health.relationship_count = 150
        mock_health.empty_description_entity_count = 20  # 20% of 100 entities
        mock_health.redundant_relationship_pairs = 0

        with (
            patch.object(monitor, "check_entity_extraction_quality", AsyncMock(return_value=mock_quality)),
            patch.object(monitor, "check_graph_health", AsyncMock(return_value=mock_health)),
            patch.object(monitor, "_queries") as mock_queries,
        ):
            mock_queries.get_daily_event_counts = AsyncMock(return_value=[])
            anomalies = await monitor.detect_anomalies(days=7)

        anomaly_types = [a.anomaly_type for a in anomalies]
        assert "empty_description_rate_high" in anomaly_types

    @pytest.mark.asyncio
    async def test_empty_description_rate_high_does_not_fire_below_threshold(self) -> None:
        """empty_description_rate_high does not fire when ≤10% entities have no description."""
        monitor, _ = _make_monitor_with_mocked_service()

        mock_quality = MagicMock()
        mock_quality.entities_per_conversation_ratio = 1.0
        mock_quality.duplicate_rate = 0.0
        mock_quality.extraction_failure_rate = 0.0

        mock_health = MagicMock()
        mock_health.relationship_density = 1.5
        mock_health.entity_nodes = 100
        mock_health.relationship_count = 150
        mock_health.empty_description_entity_count = 5  # 5% — below threshold
        mock_health.redundant_relationship_pairs = 0

        with (
            patch.object(monitor, "check_entity_extraction_quality", AsyncMock(return_value=mock_quality)),
            patch.object(monitor, "check_graph_health", AsyncMock(return_value=mock_health)),
            patch.object(monitor, "_queries") as mock_queries,
        ):
            mock_queries.get_daily_event_counts = AsyncMock(return_value=[])
            anomalies = await monitor.detect_anomalies(days=7)

        anomaly_types = [a.anomaly_type for a in anomalies]
        assert "empty_description_rate_high" not in anomaly_types

    @pytest.mark.asyncio
    async def test_redundant_relationship_pairs_high_fires_above_threshold(self) -> None:
        """redundant_relationship_pairs_high fires when >50 pairs."""
        monitor, _ = _make_monitor_with_mocked_service()

        mock_quality = MagicMock()
        mock_quality.entities_per_conversation_ratio = 1.0
        mock_quality.duplicate_rate = 0.0
        mock_quality.extraction_failure_rate = 0.0

        mock_health = MagicMock()
        mock_health.relationship_density = 1.5
        mock_health.entity_nodes = 100
        mock_health.relationship_count = 150
        mock_health.empty_description_entity_count = 0
        mock_health.redundant_relationship_pairs = 237  # current prod baseline

        with (
            patch.object(monitor, "check_entity_extraction_quality", AsyncMock(return_value=mock_quality)),
            patch.object(monitor, "check_graph_health", AsyncMock(return_value=mock_health)),
            patch.object(monitor, "_queries") as mock_queries,
        ):
            mock_queries.get_daily_event_counts = AsyncMock(return_value=[])
            anomalies = await monitor.detect_anomalies(days=7)

        anomaly_types = [a.anomaly_type for a in anomalies]
        assert "redundant_relationship_pairs_high" in anomaly_types

    @pytest.mark.asyncio
    async def test_redundant_relationship_pairs_high_does_not_fire_below_threshold(self) -> None:
        """redundant_relationship_pairs_high does not fire when ≤50 pairs."""
        monitor, _ = _make_monitor_with_mocked_service()

        mock_quality = MagicMock()
        mock_quality.entities_per_conversation_ratio = 1.0
        mock_quality.duplicate_rate = 0.0
        mock_quality.extraction_failure_rate = 0.0

        mock_health = MagicMock()
        mock_health.relationship_density = 1.5
        mock_health.entity_nodes = 100
        mock_health.relationship_count = 150
        mock_health.empty_description_entity_count = 0
        mock_health.redundant_relationship_pairs = 30  # below threshold

        with (
            patch.object(monitor, "check_entity_extraction_quality", AsyncMock(return_value=mock_quality)),
            patch.object(monitor, "check_graph_health", AsyncMock(return_value=mock_health)),
            patch.object(monitor, "_queries") as mock_queries,
        ):
            mock_queries.get_daily_event_counts = AsyncMock(return_value=[])
            anomalies = await monitor.detect_anomalies(days=7)

        anomaly_types = [a.anomaly_type for a in anomalies]
        assert "redundant_relationship_pairs_high" not in anomaly_types
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /opt/seshat && make test-file FILE=tests/personal_agent/second_brain/test_quality_monitor_new_signals.py 2>&1 | tail -5
```

Expected: failures because `GraphHealthReport` has no `empty_description_entity_count` field.

- [ ] **Step 3: Add new fields to `GraphHealthReport`**

In `src/personal_agent/second_brain/quality_monitor.py`, find `GraphHealthReport` (line 35). Add two new fields with defaults of 0:

```python
# Current class ends at line 46:
@dataclass(frozen=True)
class GraphHealthReport:
    """Knowledge graph structural health metrics."""

    total_nodes: int
    conversation_nodes: int
    entity_nodes: int
    relationship_count: int
    relationship_density: float
    orphaned_entities: int
    orphaned_entity_rate: float
    clustered_entity_rate: float
    max_temporal_gap_hours: float
    # FRE-374: content quality signals
    empty_description_entity_count: int = 0
    redundant_relationship_pairs: int = 0
```

- [ ] **Step 4: Add Cypher queries to `check_graph_health()`**

In `check_graph_health()` (around line 265, after `max_temporal_gap_hours` is computed), add two scalar queries and include their results in the `GraphHealthReport` constructor:

```python
# Add these two queries after line 265 (after max_temporal_gap_hours = ...):
empty_description_count = int(
    await self._run_scalar_query(
        """
        MATCH (e:Entity)
        WHERE e.description IS NULL OR e.description = ''
        RETURN count(e) AS value
        """
    )
)
redundant_relationship_pairs = int(
    await self._run_scalar_query(
        """
        MATCH (a:Entity)-[r]-(b:Entity)
        WHERE id(a) < id(b)
        WITH a, b, collect(distinct type(r)) AS types
        WHERE size(types) > 1
        RETURN count(*) AS value
        """
    )
)
```

Then update the `GraphHealthReport(...)` constructor call (currently ending at line 276) to add the new fields:

```python
report = GraphHealthReport(
    total_nodes=total_nodes,
    conversation_nodes=conversation_nodes,
    entity_nodes=entity_nodes,
    relationship_count=relationship_count,
    relationship_density=relationship_density,
    orphaned_entities=orphaned_entities,
    orphaned_entity_rate=orphaned_rate,
    clustered_entity_rate=clustered_ratio,
    max_temporal_gap_hours=max_temporal_gap_hours,
    empty_description_entity_count=empty_description_count,   # FRE-374
    redundant_relationship_pairs=redundant_relationship_pairs, # FRE-374
)
```

Also update the `log.info("quality_monitor_graph_report", ...)` call to include the new fields.

- [ ] **Step 5: Add two new anomaly type constants**

Near the existing threshold constants at the top of the file (lines 12–16), add:

```python
EMPTY_DESCRIPTION_RATE_TARGET_MAX = 0.10   # FRE-374: >10% empty descriptions is anomalous
REDUNDANT_RELATIONSHIP_PAIRS_TARGET_MAX = 50  # FRE-374: >50 redundant pairs is anomalous
```

- [ ] **Step 6: Add anomaly detection in `detect_anomalies()`**

In `detect_anomalies()` (around line 345, after the existing `no_relationships_created` check), add:

```python
# FRE-374: empty-description rate anomaly
if graph.entity_nodes > 0:
    empty_rate = graph.empty_description_entity_count / graph.entity_nodes
    if empty_rate > EMPTY_DESCRIPTION_RATE_TARGET_MAX:
        anomalies.append(
            Anomaly(
                anomaly_type="empty_description_rate_high",
                severity="medium",
                message=(
                    f"Empty-description entity rate {empty_rate:.1%} exceeds "
                    f"{EMPTY_DESCRIPTION_RATE_TARGET_MAX:.0%} target "
                    f"({graph.empty_description_entity_count} of {graph.entity_nodes} entities)."
                ),
                observed_value=empty_rate,
                expected_range=(0.0, EMPTY_DESCRIPTION_RATE_TARGET_MAX),
            )
        )

# FRE-374: redundant relationship pairs anomaly
if graph.redundant_relationship_pairs > REDUNDANT_RELATIONSHIP_PAIRS_TARGET_MAX:
    anomalies.append(
        Anomaly(
            anomaly_type="redundant_relationship_pairs_high",
            severity="medium",
            message=(
                f"Redundant-relationship-type pairs ({graph.redundant_relationship_pairs}) "
                f"exceeds threshold of {REDUNDANT_RELATIONSHIP_PAIRS_TARGET_MAX}."
            ),
            observed_value=float(graph.redundant_relationship_pairs),
            expected_range=(0.0, float(REDUNDANT_RELATIONSHIP_PAIRS_TARGET_MAX)),
        )
    )
```

- [ ] **Step 7: Run tests**

```bash
cd /opt/seshat && make test-file FILE=tests/personal_agent/second_brain/test_quality_monitor_new_signals.py 2>&1 | tail -5
```

Expected: `6 passed`

- [ ] **Step 8: Run existing quality monitor tests**

```bash
cd /opt/seshat && make test-k "quality_monitor" 2>&1 | tail -5
```

Expected: all existing tests still pass.

- [ ] **Step 9: Commit**

```bash
git add src/personal_agent/second_brain/quality_monitor.py tests/personal_agent/second_brain/test_quality_monitor_new_signals.py
git commit -m "feat(fre-374): quality monitor — empty_description_rate_high + redundant_relationship_pairs_high"
```

---

## Task 4: Backfill replay script

**Files:**
- Create: `scripts/replay_sessions_to_neo4j.py`

- [ ] **Step 1: Read the `SecondBrainConsolidator._process_capture` signature**

Before coding, check the exact signature of `_process_capture` in `src/personal_agent/second_brain/consolidator.py`. Also check `TaskCapture`'s required fields in `src/personal_agent/captains_log/capture.py`. Both should be available from Task 3 context.

Key fields for a synthetic `TaskCapture`:
- `trace_id: str` — use `str(uuid4())`
- `session_id: str` — from Postgres `session_id`
- `timestamp: datetime` — from message timestamp
- `user_message: str` — from messages JSONB
- `assistant_response: str | None` — from messages JSONB (next message if role=assistant)
- `outcome: str` — use `"completed"`
- `user_id: UUID` — from session metadata or owner fallback

- [ ] **Step 2: Create `scripts/replay_sessions_to_neo4j.py`**

```python
#!/usr/bin/env python3
"""Replay Postgres sessions into Neo4j via the entity extractor (FRE-374 D3).

Reads all sessions from Postgres (messages JSONB), constructs TaskCapture objects
from user/assistant message pairs, and processes each through the consolidator to
re-populate entity descriptions from the current extractor model.

Usage:
    uv run python scripts/replay_sessions_to_neo4j.py --help
    uv run python scripts/replay_sessions_to_neo4j.py --dry-run --since 2026-01-01
    uv run python scripts/replay_sessions_to_neo4j.py --since 2025-01-01 --confirm-prod

IMPORTANT: Run this ONLY after:
  1. Taking a Neo4j snapshot (see ADR-0073 §D3)
  2. Optionally clearing the graph: MATCH (n) DETACH DELETE n

The script respects AGENT_* env vars — point AGENT_NEO4J_URI at the desired target.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import structlog

log = structlog.get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--since",
        default="2025-01-01",
        help="Replay sessions created on or after this date (YYYY-MM-DD). Default: 2025-01-01",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max sessions to process. 0 = no limit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log what would be processed without calling extractor or writing to Neo4j.",
    )
    parser.add_argument(
        "--confirm-prod",
        action="store_true",
        default=False,
        help="Required when AGENT_ENVIRONMENT is not 'test'. Confirms intent to write to production substrate.",
    )
    return parser.parse_args()


async def _fetch_sessions(since_date: str, limit: int) -> list[dict[str, Any]]:
    """Query Postgres for sessions with at least one user message.

    Args:
        since_date: ISO date string (YYYY-MM-DD).
        limit: Max sessions to return; 0 = no limit.

    Returns:
        List of dicts with keys: session_id, created_at, messages, metadata.
    """
    import asyncpg
    from personal_agent.config import get_settings

    settings = get_settings()

    # Convert asyncpg URL format
    db_url = settings.database_url
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    conn = await asyncpg.connect(db_url)
    try:
        limit_clause = f"LIMIT {limit}" if limit > 0 else ""
        rows = await conn.fetch(
            f"""
            SELECT session_id, created_at, messages, metadata
            FROM sessions
            WHERE created_at >= $1
              AND jsonb_array_length(messages) > 0
            ORDER BY created_at ASC
            {limit_clause}
            """,
            datetime.fromisoformat(since_date).replace(tzinfo=timezone.utc),
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


def _extract_message_pairs(session: dict[str, Any]) -> list[tuple[str, str, datetime]]:
    """Extract (user_message, assistant_response, timestamp) pairs from session messages JSONB.

    Args:
        session: Session dict with 'messages' as a list of dicts.

    Returns:
        List of (user_message, assistant_response, timestamp) tuples.
        assistant_response may be empty string if no following assistant message.
    """
    import json
    messages = session.get("messages") or []
    if isinstance(messages, str):
        messages = json.loads(messages)

    pairs: list[tuple[str, str, datetime]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "user":
            user_text = msg.get("content", "").strip()
            ts_raw = msg.get("timestamp") or session.get("created_at")
            ts = datetime.fromisoformat(str(ts_raw)).replace(tzinfo=timezone.utc) if ts_raw else datetime.now(timezone.utc)
            # Look ahead for assistant response
            assistant_text = ""
            if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
                assistant_text = messages[i + 1].get("content", "").strip()
                i += 1  # skip assistant message in outer loop
            if user_text:
                pairs.append((user_text, assistant_text, ts))
        i += 1
    return pairs


async def _replay_session(
    session: dict[str, Any],
    consolidator: Any,
    dry_run: bool,
) -> dict[str, int]:
    """Process one session through the consolidator.

    Args:
        session: Session dict from Postgres.
        consolidator: SecondBrainConsolidator instance.
        dry_run: If True, skip actual processing.

    Returns:
        Dict with counts: turns_processed, entities_created, relationships_created, errors.
    """
    from personal_agent.captains_log.capture import TaskCapture

    session_id = str(session["session_id"])
    metadata = session.get("metadata") or {}
    user_id_raw = metadata.get("user_id") or metadata.get("owner_id")
    try:
        user_id = UUID(str(user_id_raw)) if user_id_raw else uuid4()
    except ValueError:
        user_id = uuid4()

    pairs = _extract_message_pairs(session)
    if not pairs:
        return {"turns_processed": 0, "entities_created": 0, "relationships_created": 0, "errors": 0}

    counts = {"turns_processed": 0, "entities_created": 0, "relationships_created": 0, "errors": 0}

    for user_msg, assistant_msg, ts in pairs:
        if dry_run:
            log.info(
                "replay_dry_run_pair",
                session_id=session_id,
                user_message_preview=user_msg[:80],
            )
            counts["turns_processed"] += 1
            continue

        capture = TaskCapture(
            trace_id=str(uuid4()),
            session_id=session_id,
            timestamp=ts,
            user_message=user_msg,
            assistant_response=assistant_msg or None,
            outcome="completed",
            user_id=user_id,
            tools_used=[],
            duration_ms=0,
        )
        try:
            result = await consolidator._process_capture(capture)
            counts["turns_processed"] += 1
            counts["entities_created"] += result.get("entities_created", 0)
            counts["relationships_created"] += result.get("relationships_created", 0)
        except Exception as exc:
            log.warning("replay_capture_failed", session_id=session_id, error=str(exc))
            counts["errors"] += 1

    return counts


async def main() -> None:
    """Main entrypoint for the replay script."""
    from personal_agent.config import get_settings
    from personal_agent.config.env_loader import Environment
    from personal_agent.memory.service import MemoryService
    from personal_agent.second_brain.consolidator import SecondBrainConsolidator

    args = _parse_args()
    settings = get_settings()

    # Prod gate
    if settings.environment != Environment.TEST and not args.confirm_prod:
        print(
            "ERROR: Running against non-TEST environment without --confirm-prod.\n"
            "This script writes to the Neo4j substrate.\n"
            "Re-run with --confirm-prod to confirm intent.",
            file=sys.stderr,
        )
        sys.exit(2)

    log.info(
        "replay_starting",
        since=args.since,
        limit=args.limit,
        dry_run=args.dry_run,
        neo4j_uri=settings.neo4j_uri,
    )

    # Connect to Neo4j
    memory_service = MemoryService()
    connected = await memory_service.connect()
    if not connected:
        log.error("replay_neo4j_connect_failed")
        sys.exit(1)

    consolidator = SecondBrainConsolidator(memory_service=memory_service)

    # Fetch sessions
    sessions = await _fetch_sessions(args.since, args.limit)
    log.info("replay_sessions_fetched", count=len(sessions))

    # Process
    total: dict[str, int] = {"turns_processed": 0, "entities_created": 0, "relationships_created": 0, "errors": 0}
    for i, session in enumerate(sessions, 1):
        log.info("replay_session_start", n=i, total=len(sessions), session_id=str(session["session_id"]))
        result = await _replay_session(session, consolidator, args.dry_run)
        for k, v in result.items():
            total[k] += v

    await memory_service.disconnect()

    log.info(
        "replay_complete",
        sessions_processed=len(sessions),
        **total,
    )


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: Smoke-test the script in dry-run mode**

On the VPS (where Postgres and Neo4j are running):

```bash
cd /opt/seshat
APP_ENV=prod uv run python scripts/replay_sessions_to_neo4j.py --dry-run --since 2026-01-01 --limit 5 --confirm-prod 2>&1 | head -30
```

Expected: logs showing `replay_sessions_fetched count=5` (or however many sessions exist since 2026-01-01), then `replay_dry_run_pair` log entries, then `replay_complete`. No Neo4j writes.

If Postgres connection fails: check `AGENT_DATABASE_URL` points to the running Postgres.

- [ ] **Step 4: Run mypy on the script**

```bash
cd /opt/seshat && uv run mypy scripts/replay_sessions_to_neo4j.py --ignore-missing-imports 2>&1 | tail -5
```

Expected: `Success` or minor warnings (script uses `Any` for consolidator — acceptable).

- [ ] **Step 5: Commit**

```bash
git add scripts/replay_sessions_to_neo4j.py
git commit -m "feat(fre-374): backfill replay script — replay Postgres sessions through extractor into Neo4j"
```

---

## Task 5: Provenance-on-write performance probe

**Files:**
- Create: `scripts/research/fre374_provenance_perf_probe.py`

This task answers: "Is appending to a `descriptions[]` array on every entity write materially slower than the current single-field write?" The answer gates the ADR moving from Proposed → Accepted for D4.

- [ ] **Step 1: Create the probe script**

```python
#!/usr/bin/env python3
"""FRE-374 D4 gate: measure write latency for descriptions[] append vs. single-field set.

Compares two MERGE patterns on a test Neo4j graph:
  A. Current: SET e.description = CASE WHEN ... THEN $desc ELSE e.description END
  B. Proposed: SET e.descriptions = COALESCE(e.descriptions, []) + [{text: $desc, ts: datetime()}]

Runs 500 entity writes for each pattern and reports p50/p95/p99 latency.

Usage:
    APP_ENV=test uv run python scripts/research/fre374_provenance_perf_probe.py
"""

from __future__ import annotations

import asyncio
import statistics
import time
from uuid import uuid4

import structlog

log = structlog.get_logger(__name__)

ITERATIONS = 500
TEST_ENTITY_PREFIX = f"perf-probe-{uuid4().hex[:8]}"


async def _run_benchmark(driver: Any, pattern: str, desc: str) -> list[float]:
    """Run ITERATIONS writes of the given pattern. Returns latencies in milliseconds."""
    latencies: list[float] = []
    for i in range(ITERATIONS):
        name = f"{TEST_ENTITY_PREFIX}-{i}"
        start = time.perf_counter()
        async with driver.session() as session:
            await session.run(pattern, name=name, description=desc + f" {i}")
        latencies.append((time.perf_counter() - start) * 1000)
    return latencies


async def _cleanup(driver: Any) -> None:
    async with driver.session() as session:
        await session.run(
            "MATCH (e:Entity) WHERE e.name STARTS WITH $prefix DETACH DELETE e",
            prefix=TEST_ENTITY_PREFIX,
        )


PATTERN_A = """
MERGE (e:Entity {name: $name})
SET e.description = CASE WHEN e.description IS NULL OR e.description = ''
                    THEN $description ELSE e.description END,
    e.last_seen = datetime()
"""

PATTERN_B = """
MERGE (e:Entity {name: $name})
SET e.descriptions = COALESCE(e.descriptions, []) + [{text: $description, ts: datetime()}],
    e.last_seen = datetime()
"""


async def main() -> None:
    from personal_agent.config import get_settings
    from neo4j import AsyncGraphDatabase

    settings = get_settings()
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    await driver.verify_connectivity()
    log.info("probe_connected", uri=settings.neo4j_uri)

    try:
        log.info("running_pattern_a", iterations=ITERATIONS)
        lat_a = await _run_benchmark(driver, PATTERN_A, "A description for perf testing")
        log.info("running_pattern_b", iterations=ITERATIONS)
        lat_b = await _run_benchmark(driver, PATTERN_B, "A description for perf testing")
    finally:
        await _cleanup(driver)
        await driver.close()

    def _stats(lats: list[float], label: str) -> None:
        lats_sorted = sorted(lats)
        print(
            f"{label}: p50={statistics.median(lats_sorted):.1f}ms "
            f"p95={lats_sorted[int(0.95 * len(lats_sorted))]:.1f}ms "
            f"p99={lats_sorted[int(0.99 * len(lats_sorted))]:.1f}ms "
            f"mean={statistics.mean(lats_sorted):.1f}ms"
        )

    print(f"\n=== FRE-374 provenance-on-write benchmark ({ITERATIONS} iterations each) ===")
    _stats(lat_a, "Pattern A (current CASE WHEN)")
    _stats(lat_b, "Pattern B (append to descriptions[])")
    overhead_pct = (statistics.median(sorted(lat_b)) - statistics.median(sorted(lat_a))) / statistics.median(sorted(lat_a)) * 100
    print(f"\nMedian overhead of B vs A: {overhead_pct:+.1f}%")
    print("\nRecord these numbers in ADR-0073 before moving D4 from deferred to planned.")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run the benchmark against the test Neo4j (must be running)**

```bash
cd /opt/seshat
make test-infra-up  # start test stack: neo4j on :7688
# Wait ~30s for Neo4j to start
APP_ENV=test uv run python scripts/research/fre374_provenance_perf_probe.py 2>&1
```

Expected output:
```
=== FRE-374 provenance-on-write benchmark (500 iterations each) ===
Pattern A (current CASE WHEN): p50=X.Xms p95=Y.Yms p99=Z.Zms mean=X.Xms
Pattern B (append to descriptions[]): p50=X.Xms p95=Y.Yms p99=Z.Zms mean=X.Xms

Median overhead of B vs A: +X.X%
```

Record the output in `docs/research/fre374-provenance-perf-probe-results.md`.

**Decision rule:** If median overhead of B vs A is < 25%, proceed with D4 description provenance schema migration in a follow-up issue. If ≥ 25%, evaluate server-side list-append performance options before committing.

- [ ] **Step 3: Commit**

```bash
git add scripts/research/fre374_provenance_perf_probe.py
git commit -m "research(fre-374): provenance-on-write perf probe script for D4 gate"
```

---

## Task 6: Verification — full test suite + probes

- [ ] **Step 1: Run full test suite**

```bash
cd /opt/seshat && make test 2>&1 | tail -5
```

Expected: 2490+ passed, 0 failed.

- [ ] **Step 2: mypy + ruff**

```bash
cd /opt/seshat && make mypy 2>&1 | tail -3
cd /opt/seshat && cd /opt/seshat && uv run --project /opt/seshat ruff check src/ 2>&1 | tail -3
```

Expected: `Success` for mypy, `All checks passed!` for ruff.

- [ ] **Step 3: Pre-commit hook**

```bash
cd /opt/seshat && uv run pre-commit run --all-files 2>&1 | tail -5
```

Expected: both hooks pass.

- [ ] **Step 4: Run replay script dry-run (VPS or with test infra up)**

```bash
cd /opt/seshat
APP_ENV=test make test-infra-up
APP_ENV=test AGENT_NEO4J_URI=bolt://localhost:7688 AGENT_DATABASE_URL=postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent uv run python scripts/replay_sessions_to_neo4j.py --dry-run --limit 3 2>&1 | tail -10
```

Expected: 3 sessions processed, 0 Neo4j writes.

### Post-deploy steps (operator, on VPS after merge)

```bash
# 1. Snapshot
docker exec seshat-neo4j neo4j-admin dump --to=/var/lib/neo4j/import/pre-fre374-$(date +%F).dump

# 2. Verify snapshot exists
docker exec seshat-neo4j ls -lh /var/lib/neo4j/import/

# 3. OPTIONAL: clear existing polluted graph (irreversible without snapshot)
# docker exec seshat-neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "MATCH (n) DETACH DELETE n"

# 4. Run replay (all sessions since launch)
APP_ENV=prod uv run python scripts/replay_sessions_to_neo4j.py --since 2025-01-01 --confirm-prod 2>&1 | tee /tmp/replay-$(date +%F).log

# 5. Re-run probe 1 (description quality)
uv run python scripts/research/memory_integration_probe/probe_1_entity_attribute_drift.py

# 6. Re-run probe 2 (redundant relationships)
uv run python scripts/research/memory_integration_probe/probe_2_relationship_accumulation.py

# 7. Re-run probe 5 (impact path)
uv run python scripts/research/memory_integration_probe/probe_5_impact_path.py

# 8. Re-run probe 6 (production-only slice)
uv run python scripts/research/memory_integration_probe/probe_6_recent_production_only.py
```

---

## Self-review

### Spec coverage

| AC from FRE-374 | Task |
|---|---|
| FRE-375 shipped (blocking) | ✅ already done |
| ADR drafted with Status: Proposed | Task 1 |
| Snapshot of current Neo4j before destructive action | Task 6 post-deploy step 1 |
| Migration / replay plan from Postgres sessions.messages JSONB | Task 4 |
| Performance impact of provenance-on-write measured | Task 5 |
| Probe 1, 2, 5, 6 re-run after implementation | Task 6 post-deploy steps 5-8 |
| empty-description rate trends toward 0 for top-15 | Backfill replay execution (Task 6 step 4 + post-deploy) |

### Type consistency

- `TaskCapture` fields match `src/personal_agent/captains_log/capture.py` (trace_id, session_id, timestamp, user_message, assistant_response, outcome, user_id, tools_used, duration_ms)
- `GraphHealthReport` new fields use `int` with `= 0` defaults (frozen dataclass, backward compatible)
- `_render_memory_section` takes `list[dict]` (matches `entity_items` type in executor.py)

### Deferred items (not in this plan)

- Description provenance array (`descriptions: list[dict]`) — gated by Task 5 measurement
- Relationship semantic dedup — requires type ontology, separate issue
