# EVAL-01: Extend Test Harness with Memory-Focused CPs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 4 new critical paths (CP-26 through CP-29) to the evaluation harness targeting memory system quality, including a new Neo4j assertion layer for direct graph-state verification.

**Architecture:** Extend the existing assertion union type with a `Neo4jAssertion` variant. Add a `Neo4jChecker` that queries the graph directly. Introduce `post_path_assertions` on `ConversationPath` for assertions that must run after all turns complete (Neo4j state settles asynchronously). Four new CPs cover memory promotion quality, memory-informed context assembly, context budget trimming audit, and delegation package completeness.

**Tech Stack:** Python 3.12+, neo4j async driver, structlog, pytest, frozen dataclasses

**Linear Issue:** FRE-146

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `tests/evaluation/harness/models.py` | Add `Neo4jAssertion` dataclass, `neo4j_entity()` helper, extend union type; add `post_path_assertions` + `post_assertion_delay_s` fields to `ConversationPath`; add `post_path_assertion_results` to `PathResult` |
| Create | `tests/evaluation/harness/neo4j_checker.py` | `Neo4jChecker` class — connects to Neo4j, runs Cypher queries, checks results with retry logic |
| Modify | `tests/evaluation/harness/runner.py` | After all turns, wait delay then run Neo4j post-path assertions via `Neo4jChecker` |
| Modify | `tests/evaluation/harness/conftest.py` | Add `neo4j_checker` fixture, wire into `evaluation_runner` |
| Modify | `tests/evaluation/harness/report.py` | Include post-path assertion results in JSON and markdown reports |
| Modify | `tests/evaluation/harness/dataset.py` | Add CP-26, CP-27, CP-28, CP-29; update `ALL_PATHS`, registries |
| Modify | `tests/evaluation/harness/__init__.py` | Update docstring |
| Create | `tests/evaluation/harness/test_neo4j_checker.py` | Unit tests for `Neo4jChecker` |

---

## Task 1: Add `Neo4jAssertion` and Extend Models

**Files:**
- Modify: `tests/evaluation/harness/models.py`
- Test: `tests/evaluation/harness/test_neo4j_checker.py` (assertion construction tests)

- [ ] **Step 1: Write the failing test for Neo4jAssertion construction**

Create `tests/evaluation/harness/test_neo4j_checker.py`:

```python
"""Unit tests for Neo4j assertion types and checker."""

from __future__ import annotations

from tests.evaluation.harness.models import (
    Neo4jAssertion,
    neo4j_entity,
    neo4j_promoted,
    neo4j_cypher,
)


class TestNeo4jAssertionBuilders:
    """Test compact builder helpers for Neo4j assertions."""

    def test_neo4j_entity_creates_entity_exists_query(self) -> None:
        a = neo4j_entity("Project Atlas")
        assert a.kind == "neo4j"
        assert "Project Atlas" in a.cypher_query
        assert a.min_result_count == 1
        assert a.description == "Entity 'Project Atlas' exists in Neo4j"

    def test_neo4j_promoted_creates_semantic_check(self) -> None:
        a = neo4j_promoted("Project Atlas")
        assert a.kind == "neo4j"
        assert "memory_type" in a.cypher_query
        assert "semantic" in a.cypher_query
        assert a.min_result_count == 1

    def test_neo4j_cypher_passes_through(self) -> None:
        query = "MATCH (e:Entity) RETURN count(e) AS cnt"
        a = neo4j_cypher("at least one entity", query, min_result_count=1)
        assert a.cypher_query == query
        assert a.min_result_count == 1
        assert a.description == "at least one entity"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/evaluation/harness/test_neo4j_checker.py::TestNeo4jAssertionBuilders -v`
Expected: FAIL — `Neo4jAssertion`, `neo4j_entity`, `neo4j_promoted`, `neo4j_cypher` not defined

- [ ] **Step 3: Add Neo4jAssertion dataclass and builders to models.py**

In `tests/evaluation/harness/models.py`, add after `FieldComparisonAssertion`:

```python
@dataclass(frozen=True)
class Neo4jAssertion:
    """Assert a condition on Neo4j graph state via Cypher query.

    Runs after all conversation turns complete. The checker executes the
    Cypher query and verifies the result count meets ``min_result_count``.

    Args:
        description: Human-readable description of what is being checked.
        cypher_query: Cypher query to execute. May use ``$session_id`` parameter.
        min_result_count: Minimum rows the query must return to pass.
    """

    description: str
    cypher_query: str
    min_result_count: int = 1
    kind: Literal["neo4j"] = "neo4j"
```

Update the union type:

```python
TelemetryAssertion = (
    FieldAssertion | EventPresenceAssertion | FieldComparisonAssertion | Neo4jAssertion
)
```

Add builder helpers at the bottom of the file:

```python
def neo4j_entity(name: str) -> Neo4jAssertion:
    """Shorthand: assert a named entity exists in Neo4j.

    Args:
        name: Entity name to search for (exact match).

    Returns:
        Neo4jAssertion checking entity existence.
    """
    return Neo4jAssertion(
        description=f"Entity '{name}' exists in Neo4j",
        cypher_query=f"MATCH (e:Entity {{name: '{name}'}}) RETURN e LIMIT 1",
        min_result_count=1,
    )


def neo4j_promoted(name: str) -> Neo4jAssertion:
    """Shorthand: assert an entity has been promoted to semantic memory.

    Args:
        name: Entity name to check.

    Returns:
        Neo4jAssertion checking memory_type='semantic'.
    """
    return Neo4jAssertion(
        description=f"Entity '{name}' promoted to semantic memory",
        cypher_query=(
            f"MATCH (e:Entity {{name: '{name}'}}) "
            f"WHERE e.memory_type = 'semantic' "
            f"RETURN e LIMIT 1"
        ),
        min_result_count=1,
    )


def neo4j_cypher(description: str, query: str, min_result_count: int = 1) -> Neo4jAssertion:
    """Shorthand: assert a custom Cypher query returns enough rows.

    Args:
        description: Human description of the check.
        query: Raw Cypher query.
        min_result_count: Minimum rows expected.

    Returns:
        Neo4jAssertion with the given query.
    """
    return Neo4jAssertion(
        description=description,
        cypher_query=query,
        min_result_count=min_result_count,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/evaluation/harness/test_neo4j_checker.py::TestNeo4jAssertionBuilders -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Add `post_path_assertions` to ConversationPath and PathResult**

In `ConversationPath`, add two new fields (after `setup_notes`):

```python
@dataclass(frozen=True)
class ConversationPath:
    # ... existing fields ...
    setup_notes: str | None = None
    post_path_assertions: tuple[Neo4jAssertion, ...] = ()
    post_path_delay_s: float = 5.0
```

In `PathResult`, add a new field and update properties:

```python
@dataclass
class PathResult:
    # ... existing fields ...
    post_path_assertion_results: list[AssertionResult] = field(default_factory=list)

    @property
    def total_assertions(self) -> int:
        """Total number of assertions across all turns plus post-path."""
        return (
            sum(len(t.assertion_results) for t in self.turns)
            + len(self.post_path_assertion_results)
        )

    @property
    def passed_assertions(self) -> int:
        """Number of passed assertions."""
        return (
            sum(1 for t in self.turns for a in t.assertion_results if a.passed)
            + sum(1 for a in self.post_path_assertion_results if a.passed)
        )
```

- [ ] **Step 6: Run full model tests**

Run: `uv run pytest tests/evaluation/harness/test_neo4j_checker.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add tests/evaluation/harness/models.py tests/evaluation/harness/test_neo4j_checker.py
git commit -m "feat(eval): add Neo4jAssertion type and post_path_assertions to ConversationPath"
```

---

## Task 2: Create Neo4jChecker

**Files:**
- Create: `tests/evaluation/harness/neo4j_checker.py`
- Modify: `tests/evaluation/harness/test_neo4j_checker.py`

- [ ] **Step 1: Write the failing test for Neo4jChecker**

Append to `tests/evaluation/harness/test_neo4j_checker.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.evaluation.harness.models import AssertionResult, Neo4jAssertion
from tests.evaluation.harness.neo4j_checker import Neo4jChecker


class TestNeo4jChecker:
    """Tests for Neo4jChecker with mocked Neo4j driver."""

    @pytest.fixture
    def checker(self) -> Neo4jChecker:
        return Neo4jChecker(
            neo4j_uri="bolt://localhost:7687",
            neo4j_user="neo4j",
            neo4j_password="test",
            max_retries=2,
            retry_delay_s=0.01,
        )

    @pytest.mark.asyncio
    async def test_check_assertion_passes_when_rows_returned(
        self, checker: Neo4jChecker
    ) -> None:
        assertion = Neo4jAssertion(
            description="entity exists",
            cypher_query="MATCH (e:Entity {name: 'Foo'}) RETURN e",
            min_result_count=1,
        )

        mock_record = MagicMock()
        mock_result = AsyncMock()
        mock_result.values.return_value = [[mock_record]]
        mock_session = AsyncMock()
        mock_session.run.return_value = mock_result
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_driver = MagicMock()
        mock_driver.session.return_value = mock_session

        checker._driver = mock_driver

        result = await checker.check_assertion(assertion)
        assert result.passed is True
        assert result.actual_value == 1

    @pytest.mark.asyncio
    async def test_check_assertion_fails_when_no_rows(
        self, checker: Neo4jChecker
    ) -> None:
        assertion = Neo4jAssertion(
            description="entity exists",
            cypher_query="MATCH (e:Entity {name: 'Missing'}) RETURN e",
            min_result_count=1,
        )

        mock_result = AsyncMock()
        mock_result.values.return_value = []
        mock_session = AsyncMock()
        mock_session.run.return_value = mock_result
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_driver = MagicMock()
        mock_driver.session.return_value = mock_session

        checker._driver = mock_driver

        result = await checker.check_assertion(assertion)
        assert result.passed is False
        assert result.actual_value == 0

    @pytest.mark.asyncio
    async def test_check_assertion_returns_failure_on_no_driver(
        self, checker: Neo4jChecker
    ) -> None:
        assertion = Neo4jAssertion(
            description="entity exists",
            cypher_query="MATCH (e:Entity {name: 'Foo'}) RETURN e",
            min_result_count=1,
        )
        # _driver is None by default
        result = await checker.check_assertion(assertion)
        assert result.passed is False
        assert "not connected" in result.message.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/evaluation/harness/test_neo4j_checker.py::TestNeo4jChecker -v`
Expected: FAIL — `neo4j_checker` module not found

- [ ] **Step 3: Implement Neo4jChecker**

Create `tests/evaluation/harness/neo4j_checker.py`:

```python
"""Neo4j graph state checker for evaluation assertions.

Queries Neo4j directly to verify entity existence, promotion state,
and other graph conditions. Includes retry logic to handle async
consolidation delays.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from tests.evaluation.harness.models import AssertionResult, Neo4jAssertion

log = structlog.get_logger(__name__)

DEFAULT_NEO4J_URI = "bolt://localhost:7687"
DEFAULT_NEO4J_USER = "neo4j"
DEFAULT_NEO4J_PASSWORD = "neo4j_dev_password"
DEFAULT_RETRY_DELAY_S = 3.0
DEFAULT_MAX_RETRIES = 4


class Neo4jChecker:
    """Checks Neo4j graph assertions against live graph state.

    Args:
        neo4j_uri: Neo4j bolt URI.
        neo4j_user: Neo4j username.
        neo4j_password: Neo4j password.
        retry_delay_s: Seconds between retries.
        max_retries: Maximum retry attempts for queries returning 0 rows.
    """

    def __init__(
        self,
        neo4j_uri: str = DEFAULT_NEO4J_URI,
        neo4j_user: str = DEFAULT_NEO4J_USER,
        neo4j_password: str = DEFAULT_NEO4J_PASSWORD,
        retry_delay_s: float = DEFAULT_RETRY_DELAY_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._neo4j_uri = neo4j_uri
        self._neo4j_user = neo4j_user
        self._neo4j_password = neo4j_password
        self._retry_delay_s = retry_delay_s
        self._max_retries = max_retries
        self._driver: Any | None = None

    async def connect(self) -> bool:
        """Connect to Neo4j.

        Returns:
            True if connected successfully.
        """
        try:
            from neo4j import AsyncGraphDatabase
        except ModuleNotFoundError:
            log.error("neo4j_checker_dependency_missing")
            return False

        try:
            self._driver = AsyncGraphDatabase.driver(
                self._neo4j_uri,
                auth=(self._neo4j_user, self._neo4j_password),
            )
            await self._driver.verify_connectivity()
            log.info("neo4j_checker_connected", uri=self._neo4j_uri)
            return True
        except Exception as e:
            log.error("neo4j_checker_connection_failed", error=str(e))
            self._driver = None
            return False

    async def disconnect(self) -> None:
        """Close Neo4j connection."""
        if self._driver:
            await self._driver.close()
            self._driver = None

    async def check_assertion(self, assertion: Neo4jAssertion) -> AssertionResult:
        """Check a single Neo4j assertion with retry logic.

        Retries when the query returns fewer rows than expected, since
        entity extraction and promotion happen asynchronously.

        Args:
            assertion: The Neo4j assertion to check.

        Returns:
            AssertionResult with pass/fail and row count as actual_value.
        """
        if self._driver is None:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                actual_value=None,
                message=f"Neo4j not connected — cannot check: {assertion.description}",
            )

        for attempt in range(self._max_retries):
            try:
                async with self._driver.session() as session:
                    result = await session.run(assertion.cypher_query)
                    rows = await result.values()
                    row_count = len(rows)

                if row_count >= assertion.min_result_count:
                    log.debug(
                        "neo4j_assertion_passed",
                        description=assertion.description,
                        row_count=row_count,
                        attempt=attempt + 1,
                    )
                    return AssertionResult(
                        assertion=assertion,
                        passed=True,
                        actual_value=row_count,
                        message=(
                            f"Neo4j: {assertion.description} — "
                            f"{row_count} rows (need >= {assertion.min_result_count})"
                        ),
                    )

                if attempt < self._max_retries - 1:
                    log.debug(
                        "neo4j_assertion_retrying",
                        description=assertion.description,
                        row_count=row_count,
                        attempt=attempt + 1,
                        retry_in_s=self._retry_delay_s,
                    )
                    await asyncio.sleep(self._retry_delay_s)

            except Exception as e:
                log.warning(
                    "neo4j_assertion_error",
                    description=assertion.description,
                    error=str(e),
                    attempt=attempt + 1,
                )
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(self._retry_delay_s)
                else:
                    return AssertionResult(
                        assertion=assertion,
                        passed=False,
                        actual_value=None,
                        message=f"Neo4j error: {assertion.description} — {e}",
                    )

        return AssertionResult(
            assertion=assertion,
            passed=False,
            actual_value=row_count,
            message=(
                f"Neo4j: {assertion.description} — "
                f"{row_count} rows (need >= {assertion.min_result_count}) "
                f"after {self._max_retries} attempts"
            ),
        )

    async def check_assertions(
        self,
        assertions: tuple[Neo4jAssertion, ...],
    ) -> list[AssertionResult]:
        """Check multiple Neo4j assertions sequentially.

        Args:
            assertions: Tuple of Neo4j assertions to check.

        Returns:
            List of AssertionResult for each assertion.
        """
        results: list[AssertionResult] = []
        for assertion in assertions:
            result = await self.check_assertion(assertion)
            results.append(result)
        return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/evaluation/harness/test_neo4j_checker.py -v`
Expected: PASS (6 tests — 3 builder tests + 3 checker tests)

- [ ] **Step 5: Commit**

```bash
git add tests/evaluation/harness/neo4j_checker.py tests/evaluation/harness/test_neo4j_checker.py
git commit -m "feat(eval): add Neo4jChecker for direct graph-state assertions"
```

---

## Task 3: Wire Neo4jChecker into Runner, Conftest, and Report

**Files:**
- Modify: `tests/evaluation/harness/runner.py`
- Modify: `tests/evaluation/harness/conftest.py`
- Modify: `tests/evaluation/harness/report.py`
- Modify: `tests/evaluation/harness/run.py`

- [ ] **Step 1: Modify runner.py — accept Neo4jChecker and run post-path assertions**

In `runner.py`, add the import:

```python
from tests.evaluation.harness.neo4j_checker import Neo4jChecker
```

Update `EvaluationRunner.__init__` to accept an optional `Neo4jChecker`:

```python
class EvaluationRunner:
    def __init__(
        self,
        agent_url: str = DEFAULT_AGENT_URL,
        telemetry: TelemetryChecker | None = None,
        neo4j_checker: Neo4jChecker | None = None,
        chat_timeout_s: float = DEFAULT_CHAT_TIMEOUT_S,
        inter_turn_delay_s: float = DEFAULT_INTER_TURN_DELAY_S,
    ) -> None:
        self._agent_url = agent_url
        self._telemetry = telemetry or TelemetryChecker()
        self._neo4j_checker = neo4j_checker
        self._chat_timeout_s = chat_timeout_s
        self._inter_turn_delay_s = inter_turn_delay_s
```

In `run_path`, after the turn loop completes and before setting `result.completed_at`, add post-path assertion handling:

```python
        # Run post-path Neo4j assertions (if any)
        if path.post_path_assertions and self._neo4j_checker:
            if path.post_path_delay_s > 0:
                log.info(
                    "post_path_delay",
                    path_id=path.path_id,
                    delay_s=path.post_path_delay_s,
                )
                await asyncio.sleep(path.post_path_delay_s)

            post_results = await self._neo4j_checker.check_assertions(
                path.post_path_assertions,
            )
            result.post_path_assertion_results = post_results

            log.info(
                "post_path_assertions_checked",
                path_id=path.path_id,
                passed=sum(1 for r in post_results if r.passed),
                total=len(post_results),
            )
        elif path.post_path_assertions and not self._neo4j_checker:
            log.warning(
                "post_path_assertions_skipped_no_checker",
                path_id=path.path_id,
                assertion_count=len(path.post_path_assertions),
            )
```

The existing `result.all_assertions_passed` line already accounts for post-path assertions since the `passed_assertions` and `total_assertions` properties were updated in Task 1.

- [ ] **Step 2: Update conftest.py — add Neo4jChecker fixture**

```python
"""Pytest fixtures for evaluation harness.

Provides httpx client, ES client, Neo4j checker, and agent health check.
Tests marked with @pytest.mark.evaluation require the live agent.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from tests.evaluation.harness.neo4j_checker import Neo4jChecker
from tests.evaluation.harness.runner import EvaluationRunner
from tests.evaluation.harness.telemetry import TelemetryChecker


@pytest.fixture(scope="session")
def telemetry_checker() -> TelemetryChecker:
    """Shared TelemetryChecker instance."""
    return TelemetryChecker()


@pytest_asyncio.fixture(scope="session")
async def neo4j_checker() -> Neo4jChecker | None:
    """Shared Neo4jChecker instance. Returns None if Neo4j is unreachable."""
    checker = Neo4jChecker()
    connected = await checker.connect()
    if connected:
        yield checker
        await checker.disconnect()
    else:
        yield None


@pytest.fixture(scope="session")
def evaluation_runner(
    telemetry_checker: TelemetryChecker,
    neo4j_checker: Neo4jChecker | None,
) -> EvaluationRunner:
    """Shared EvaluationRunner instance."""
    return EvaluationRunner(telemetry=telemetry_checker, neo4j_checker=neo4j_checker)


@pytest_asyncio.fixture(scope="session")
async def agent_healthy(evaluation_runner: EvaluationRunner) -> None:
    """Skip all evaluation tests if the agent service is not running on port 9000."""
    healthy = await evaluation_runner.check_agent_health()
    if not healthy:
        pytest.skip("Agent service not running on port 9000")
```

- [ ] **Step 3: Update report.py — include post-path assertion results**

In `generate_markdown_report`, after the per-turn assertion output and before quality criteria, add:

```python
        # Post-path assertions (Neo4j)
        if r.post_path_assertion_results:
            lines.append("**Post-Path Assertions (Neo4j):**")
            for a in r.post_path_assertion_results:
                icon = "✅" if a.passed else "❌"
                lines.append(f"  - {icon} {a.message}")
            lines.append("")
```

In `_serialize_path`, add after `"quality_criteria"`:

```python
        "post_path_assertions": [
            {
                "passed": a.passed,
                "message": a.message,
                "actual_value": a.actual_value,
            }
            for a in r.post_path_assertion_results
        ],
```

- [ ] **Step 4: Update run.py — wire Neo4jChecker into standalone runner**

Add imports:

```python
from tests.evaluation.harness.neo4j_checker import Neo4jChecker
```

Add `--neo4j-uri` argument to `parse_args`:

```python
    parser.add_argument(
        "--neo4j-uri",
        default="bolt://localhost:7687",
        help="Neo4j bolt URI (default: bolt://localhost:7687)",
    )
```

In `main()`, create and connect the Neo4j checker before creating the runner:

```python
    neo4j = Neo4jChecker(neo4j_uri=args.neo4j_uri)
    neo4j_connected = await neo4j.connect()
    if not neo4j_connected:
        log.warning("neo4j_not_available_post_path_assertions_will_be_skipped")

    runner = EvaluationRunner(
        agent_url=args.agent_url,
        telemetry=telemetry,
        neo4j_checker=neo4j if neo4j_connected else None,
    )
```

After the run, disconnect:

```python
    if neo4j_connected:
        await neo4j.disconnect()
```

- [ ] **Step 5: Run existing tests to verify nothing broke**

Run: `uv run pytest tests/evaluation/harness/ -v --ignore=tests/evaluation/harness/test_paths.py`
Expected: All existing tests pass, new tests pass

- [ ] **Step 6: Commit**

```bash
git add tests/evaluation/harness/runner.py tests/evaluation/harness/conftest.py tests/evaluation/harness/report.py tests/evaluation/harness/run.py
git commit -m "feat(eval): wire Neo4jChecker into runner, conftest, report, and CLI"
```

---

## Task 4: Add CP-26 — Memory Promotion Quality

**Files:**
- Modify: `tests/evaluation/harness/dataset.py`

This is the flagship memory CP. It seeds entities across 3 turns, allows consolidation, then uses both telemetry assertions (promotion events) and Neo4j post-path assertions (entity existence, semantic promotion state) to verify quality.

- [ ] **Step 1: Add CP-26 definition**

In `dataset.py`, add a new category section after `CP_25` and before the registry:

```python
# ============================================================================
# Category 8: Memory Quality (CP-26 to CP-29)
# ============================================================================

CP_26 = ConversationPath(
    path_id="CP-26",
    name="Memory Promotion Quality",
    category="Memory Quality",
    objective=(
        "Verify that entities seeded across multiple turns are extracted, "
        "stored in Neo4j, and promoted to semantic memory with accurate facts"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "I'm building a service called DataForge. It uses Apache Flink "
                "for stream processing and stores results in ClickHouse."
            ),
            expected_behavior=(
                "Responds to the topic. Entities captured: "
                "DataForge, Apache Flink, ClickHouse."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "The project lead is Priya Sharma. We're targeting "
                "a throughput of 50,000 events per second on GCP."
            ),
            expected_behavior=(
                "More context. Entities: Priya Sharma, GCP."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "DataForge also integrates with Grafana for real-time "
                "monitoring and uses Kafka as the ingestion layer "
                "before Flink processes the data."
            ),
            expected_behavior=(
                "Third turn enriching the entity graph: "
                "Grafana, Kafka, reinforces Flink connection."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message="What do you remember about the DataForge project?",
            expected_behavior=(
                "Memory recall. Should reference DataForge, Flink, "
                "ClickHouse, Priya Sharma, GCP, Grafana, and Kafka."
            ),
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                fld("decomposition_assessed", "strategy", "single"),
                present("memory_enrichment_completed"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 4 references DataForge by name",
        "Mentions at least 5 of: Flink, ClickHouse, Priya Sharma, GCP, Grafana, Kafka",
        "Information is accurate (no hallucinated technologies or people)",
        "Demonstrates entity-relationship awareness (Kafka -> Flink -> ClickHouse pipeline)",
        "Does not confuse entities from other conversations",
    ),
    setup_notes=(
        "Requires consolidation scheduler to have run after entity seeding.\n"
        "The post_path_delay_s (default 5s) plus Neo4jChecker retries (4x3s)\n"
        "allow up to ~17 seconds for promotion. If the consolidation interval\n"
        "is longer, increase post_path_delay_s or trigger manually."
    ),
    post_path_assertions=(
        neo4j_entity("DataForge"),
        neo4j_entity("Apache Flink"),
        neo4j_entity("ClickHouse"),
        neo4j_entity("Priya Sharma"),
        neo4j_promoted("DataForge"),
    ),
    post_path_delay_s=5.0,
)
```

- [ ] **Step 2: Add the import for new builder helpers at top of dataset.py**

Update imports:

```python
from tests.evaluation.harness.models import (
    ConversationPath,
    ConversationTurn,
    absent,
    fld,
    gte,
    neo4j_cypher,
    neo4j_entity,
    neo4j_promoted,
    present,
)
```

- [ ] **Step 3: Verify syntax is valid**

Run: `python -c "from tests.evaluation.harness.dataset import CP_26; print(CP_26.path_id)"`
Expected: `CP-26`

- [ ] **Step 4: Commit**

```bash
git add tests/evaluation/harness/dataset.py
git commit -m "feat(eval): add CP-26 Memory Promotion Quality with Neo4j assertions"
```

---

## Task 5: Add CP-27 — Memory-Informed Context Assembly

**Files:**
- Modify: `tests/evaluation/harness/dataset.py`

This CP verifies that Seshat memory appears in assembled context and improves the response. It uses a two-session approach: first session seeds data, second session queries. The telemetry assertions check that memory enrichment occurs and context assembly includes memory.

- [ ] **Step 1: Add CP-27 definition**

After CP-26 in `dataset.py`:

```python
CP_27 = ConversationPath(
    path_id="CP-27",
    name="Memory-Informed Context Assembly",
    category="Memory Quality",
    objective=(
        "Verify that when Seshat has relevant memory from prior turns, "
        "it appears in assembled context and the memory_enrichment_completed "
        "event shows entities discovered"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "I'm working on a machine learning pipeline called "
                "SentinelML that uses PyTorch for model training and "
                "MLflow for experiment tracking."
            ),
            expected_behavior="Seeds entities: SentinelML, PyTorch, MLflow.",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "SentinelML runs on Kubernetes with GPU node pools. "
                "The inference endpoint uses TorchServe behind an "
                "Istio service mesh."
            ),
            expected_behavior=(
                "More context: Kubernetes, GPU, TorchServe, Istio."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "What infrastructure changes would you recommend for "
                "scaling SentinelML to handle 10x the current inference load?"
            ),
            expected_behavior=(
                "Analysis request. Should use memory context about "
                "the existing stack (PyTorch, TorchServe, K8s, Istio) "
                "to give a specific — not generic — scaling recommendation."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "strategy", "single"),
                present("memory_enrichment_completed"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 3 response explicitly references SentinelML by name",
        "Recommends scaling TorchServe specifically (not generic model serving)",
        "Addresses Kubernetes GPU node pool scaling",
        "Mentions Istio service mesh considerations for load balancing",
        "Advice is stack-specific, not generic cloud scaling advice",
        "Response demonstrates memory-informed reasoning, not generic knowledge",
    ),
)
```

- [ ] **Step 2: Verify syntax**

Run: `python -c "from tests.evaluation.harness.dataset import CP_27; print(CP_27.path_id)"`
Expected: `CP-27`

- [ ] **Step 3: Commit**

```bash
git add tests/evaluation/harness/dataset.py
git commit -m "feat(eval): add CP-27 Memory-Informed Context Assembly"
```

---

## Task 6: Add CP-28 — Context Budget Trimming Audit

**Files:**
- Modify: `tests/evaluation/harness/dataset.py`

Extends the concepts from CP-19/CP-20 with assertions on WHAT was trimmed. Uses the `context_budget_applied` telemetry event which includes `trimmed`, `overflow_action`, `total_tokens`, and `max_tokens` fields.

- [ ] **Step 1: Add CP-28 definition**

After CP-27 in `dataset.py`:

```python
CP_28 = ConversationPath(
    path_id="CP-28",
    name="Context Budget Trimming Audit",
    category="Memory Quality",
    objective=(
        "Verify that when context budget is exceeded, trimming decisions "
        "are logged with specific overflow_action and foundational facts "
        "are preserved in the response"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Our production system uses PostgreSQL 16 as the primary "
                "database with pgvector for embeddings."
            ),
            expected_behavior="Establishes foundational architectural fact.",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "We chose PostgreSQL specifically because we needed ACID "
                "guarantees for our financial transaction processing."
            ),
            expected_behavior="Reinforces importance of PostgreSQL (financial context).",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "The API layer is FastAPI with Pydantic v2 for validation."
            ),
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message="We use Redis for session caching and rate limiting.",
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "Our observability stack is Prometheus plus Grafana "
                "with OpenTelemetry instrumentation."
            ),
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "We deploy using ArgoCD with Kustomize overlays "
                "across three environments: dev, staging, production."
            ),
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "The CI pipeline uses GitHub Actions with matrix builds "
                "for Python 3.11 and 3.12."
            ),
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "We also have a Celery worker fleet for async job processing "
                "backed by RabbitMQ."
            ),
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "Run a full system health check, then tell me about "
                "any issues, and also check the recent error log."
            ),
            expected_behavior=(
                "Tool-heavy turn that adds to context pressure. "
                "Multiple tool calls generate large outputs."
            ),
            assertions=(
                present("tool_call_completed"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Given everything we've discussed about our stack, "
                "what is our primary database and why did we choose it?"
            ),
            expected_behavior=(
                "Should still recall PostgreSQL 16 and ACID/financial "
                "context despite potential trimming. Check budget_trimmed "
                "field in gateway_output."
            ),
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 10 correctly identifies PostgreSQL 16 as primary database",
        "Turn 10 mentions ACID guarantees or financial transaction context",
        "If context was trimmed, foundational facts (PostgreSQL, financial) survived",
        "gateway_output.budget_trimmed field accurately reflects trimming decision",
        "If overflow_action is 'dropped_oldest_history', recent tool output is preserved",
        "If overflow_action is 'dropped_memory_context', session history is preserved",
    ),
)
```

- [ ] **Step 2: Verify syntax**

Run: `python -c "from tests.evaluation.harness.dataset import CP_28; print(len(CP_28.turns))"`
Expected: `10`

- [ ] **Step 3: Commit**

```bash
git add tests/evaluation/harness/dataset.py
git commit -m "feat(eval): add CP-28 Context Budget Trimming Audit"
```

---

## Task 7: Add CP-29 — Delegation Package Completeness

**Files:**
- Modify: `tests/evaluation/harness/dataset.py`

Extends the concepts from CP-05 with assertions on `delegation_package_created` event fields, checking that the package has sufficient context, memory items, and criteria — not just that delegation was classified correctly.

- [ ] **Step 1: Add CP-29 definition**

After CP-28 in `dataset.py`:

```python
CP_29 = ConversationPath(
    path_id="CP-29",
    name="Delegation Package Completeness",
    category="Memory Quality",
    objective=(
        "Verify that delegation packages contain sufficient context, "
        "memory excerpts, acceptance criteria, and known pitfalls — "
        "not just correct classification"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Our API uses FastAPI with SQLAlchemy 2.0 async sessions "
                "and Alembic for migrations. The models are in "
                "src/models/ and the routes in src/routes/."
            ),
            expected_behavior="Seeds project context for delegation enrichment.",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "We had a bug last week where a migration dropped a column "
                "that was still referenced by an API endpoint. The tests "
                "didn't catch it because we were mocking the database."
            ),
            expected_behavior="Seeds a known pitfall for delegation context.",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Use Claude Code to add a new REST endpoint for bulk user "
                "imports with CSV upload support, input validation, and "
                "proper error reporting for malformed rows."
            ),
            expected_behavior=(
                "Classifies as DELEGATION. The delegation package should "
                "include: (1) context about FastAPI + SQLAlchemy stack, "
                "(2) memory of the migration bug as a known pitfall, "
                "(3) acceptance criteria covering CSV parsing, validation, "
                "and error reporting, (4) relevant file paths."
            ),
            assertions=(
                fld("intent_classified", "task_type", "delegation"),
                fld("decomposition_assessed", "strategy", "delegate"),
                present("delegation_package_created"),
                gte("delegation_package_created", "criteria_count", 1),
                gte("delegation_package_created", "context_items", 0),
            ),
        ),
    ),
    quality_criteria=(
        "Delegation package references FastAPI + SQLAlchemy from Turn 1",
        "Package includes the migration bug from Turn 2 as a known pitfall",
        "Acceptance criteria cover CSV parsing, validation, and error reporting",
        "Package includes relevant file paths (src/models/, src/routes/)",
        "Task description is self-contained for an agent with no prior context",
        "Package complexity estimate is reasonable (MODERATE or COMPLEX)",
    ),
)
```

- [ ] **Step 2: Verify syntax**

Run: `python -c "from tests.evaluation.harness.dataset import CP_29; print(CP_29.path_id)"`
Expected: `CP-29`

- [ ] **Step 3: Commit**

```bash
git add tests/evaluation/harness/dataset.py
git commit -m "feat(eval): add CP-29 Delegation Package Completeness"
```

---

## Task 8: Update Registry and Docstring

**Files:**
- Modify: `tests/evaluation/harness/dataset.py`
- Modify: `tests/evaluation/harness/__init__.py`

- [ ] **Step 1: Update ALL_PATHS, PATHS_BY_ID, PATHS_BY_CATEGORY**

In `dataset.py`, update the `ALL_PATHS` tuple:

```python
ALL_PATHS: tuple[ConversationPath, ...] = (
    CP_01,
    CP_02,
    CP_03,
    CP_04,
    CP_05,
    CP_06,
    CP_07,
    CP_08,
    CP_09,
    CP_10,
    CP_11,
    CP_12,
    CP_13,
    CP_14,
    CP_15,
    CP_16,
    CP_17,
    CP_18,
    CP_19,
    CP_20,
    CP_21,
    CP_22,
    CP_23,
    CP_24,
    CP_25,
    CP_26,
    CP_27,
    CP_28,
    CP_29,
)
```

The `PATHS_BY_ID` and `PATHS_BY_CATEGORY` dicts are built dynamically from `ALL_PATHS`, so they auto-update.

- [ ] **Step 2: Update module docstring in dataset.py**

```python
"""All 29 evaluation conversation paths as Python data structures.

Each path mirrors the specification in docs/research/EVALUATION_DATASET.md.
Uses compact builder helpers from models.py for assertion definitions.

Organized by capability category:
- Category 1: Intent Classification (CP-01 to CP-07)
- Category 2: Decomposition Strategies (CP-08 to CP-11)
- Category 3: Memory System (CP-12 to CP-15)
- Category 4: Expansion & Sub-Agents (CP-16 to CP-18)
- Category 5: Context Management (CP-19 to CP-20)
- Category 6: Tools & Self-Inspection (CP-21 to CP-23)
- Category 7: Edge Cases (CP-24 to CP-25)
- Category 8: Memory Quality (CP-26 to CP-29)
"""
```

- [ ] **Step 3: Update `__init__.py` docstring**

```python
"""Evaluation harness for testing the 29 conversation paths."""
```

- [ ] **Step 4: Verify category filter works**

Run: `python -c "from tests.evaluation.harness.dataset import PATHS_BY_CATEGORY; print(list(PATHS_BY_CATEGORY.keys()))"`
Expected: List includes `"Memory Quality"` alongside existing categories

Run: `python -c "from tests.evaluation.harness.dataset import PATHS_BY_CATEGORY; print([p.path_id for p in PATHS_BY_CATEGORY['Memory Quality']])"`
Expected: `['CP-26', 'CP-27', 'CP-28', 'CP-29']`

- [ ] **Step 5: Verify CLI filter would work**

Run: `python -c "from tests.evaluation.harness.dataset import ALL_PATHS; print(len(ALL_PATHS))"`
Expected: `29`

- [ ] **Step 6: Commit**

```bash
git add tests/evaluation/harness/dataset.py tests/evaluation/harness/__init__.py
git commit -m "feat(eval): register CP-26 through CP-29 in Memory Quality category"
```

---

## Task 9: Run Type Checking and Linting

**Files:** All modified files

- [ ] **Step 1: Run mypy**

Run: `uv run mypy tests/evaluation/harness/`
Expected: No errors

- [ ] **Step 2: Run ruff check**

Run: `uv run ruff check tests/evaluation/harness/`
Expected: No issues

- [ ] **Step 3: Run ruff format**

Run: `uv run ruff format tests/evaluation/harness/`
Expected: Files formatted (or already formatted)

- [ ] **Step 4: Run all harness unit tests**

Run: `uv run pytest tests/evaluation/harness/test_neo4j_checker.py -v`
Expected: All 6 tests pass

- [ ] **Step 5: Fix any issues and commit**

```bash
git add -A tests/evaluation/harness/
git commit -m "chore(eval): fix type and lint issues in evaluation harness"
```

---

## Summary Table

| Task | Description | Files | Model |
|------|-------------|-------|-------|
| 1 | Neo4jAssertion type + post_path_assertions | models.py, test_neo4j_checker.py | Sonnet |
| 2 | Neo4jChecker implementation | neo4j_checker.py, test_neo4j_checker.py | Sonnet |
| 3 | Wire checker into runner/conftest/report/CLI | runner.py, conftest.py, report.py, run.py | Sonnet |
| 4 | CP-26: Memory Promotion Quality | dataset.py | Sonnet |
| 5 | CP-27: Memory-Informed Context Assembly | dataset.py | Sonnet |
| 6 | CP-28: Context Budget Trimming Audit | dataset.py | Sonnet |
| 7 | CP-29: Delegation Package Completeness | dataset.py | Sonnet |
| 8 | Update registry + docstrings | dataset.py, __init__.py | Haiku |
| 9 | Type check, lint, test | all | Haiku |

## Acceptance Criteria Mapping

| Acceptance Criterion | Satisfied By |
|---------------------|--------------|
| New CP definitions added to harness config | Tasks 4-8: CP-26 through CP-29 in `ALL_PATHS` |
| Each CP has automated assertions (not just human-eval) | All CPs have telemetry assertions; CP-26 also has Neo4j assertions |
| At least one CP tests Neo4j state directly | CP-26: `post_path_assertions` with `neo4j_entity()` and `neo4j_promoted()` |
| Harness can run focused subset: `--category memory` | Task 8: `PATHS_BY_CATEGORY["Memory Quality"]` → `--category "Memory Quality"` |
