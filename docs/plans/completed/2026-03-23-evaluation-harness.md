# Evaluation Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an automated test harness that executes the 25 conversation paths from `docs/research/EVALUATION_DATASET.md` against the live agent API, verifies telemetry assertions via Elasticsearch, and produces a structured results report.

**Architecture:** HTTP client sends conversation turns to `POST /chat`, captures `trace_id` from each response, then queries Elasticsearch `agent-logs-*` indices to verify telemetry events (intent classification, decomposition strategy, tool calls, expansion events). Results are aggregated into JSON + markdown reports. The harness runs as both a pytest suite (with `@pytest.mark.evaluation` marker) and a standalone CLI script.

**Tech Stack:** Python 3.12, httpx (async HTTP), elasticsearch-py (async ES), pytest + pytest-asyncio, structlog

**Spec:** `docs/research/EVALUATION_DATASET.md`

---

## File Structure

| File | Purpose | Creates/Modifies |
|------|---------|-----------------|
| `tests/evaluation/harness/__init__.py` | Package init | Create |
| `tests/evaluation/harness/models.py` | Frozen dataclasses: Path, Turn, Assertion, Result types | Create |
| `tests/evaluation/harness/telemetry.py` | ES query helpers — fetch events by trace_id, check assertions | Create |
| `tests/evaluation/harness/runner.py` | Core engine — send turns via HTTP, check telemetry, collect results | Create |
| `tests/evaluation/harness/report.py` | JSON + markdown report generation | Create |
| `tests/evaluation/harness/dataset.py` | All 25 conversation paths as Python data structures | Create |
| `tests/evaluation/harness/conftest.py` | Pytest fixtures (httpx client, ES client, agent readiness) | Create |
| `tests/evaluation/harness/test_paths.py` | Parameterized pytest tests — one test per conversation path | Create |
| `tests/evaluation/harness/run.py` | Standalone CLI entry point with argparse | Create |
| `tests/evaluation/harness/test_unit.py` | Unit tests for models, telemetry checker, runner (mocked) | Create |
| `pyproject.toml` | Add `evaluation` pytest marker | Modify |

---

## Task Summary

| Task | Description | Model |
|------|-------------|-------|
| 1 | Data model — frozen dataclasses for paths, assertions, results | Sonnet |
| 2 | Telemetry checker — ES queries with retry/wait | Sonnet |
| 3 | Runner — HTTP client, assertion checking, result collection | Sonnet |
| 4 | Report generator — JSON + markdown output | Sonnet |
| 5 | Dataset — Intent Classification paths (CP-01 to CP-07) | Sonnet |
| 6 | Dataset — Decomposition & Memory paths (CP-08 to CP-15) | Sonnet |
| 7 | Dataset — Expansion, Context, Tools, Edge Cases (CP-16 to CP-25) | Sonnet |
| 8 | Unit tests for harness components | Sonnet |
| 9 | Pytest integration + CLI entry point | Sonnet |

---

## Task 1: Data Model

**Files:**
- Create: `tests/evaluation/harness/__init__.py`
- Create: `tests/evaluation/harness/models.py`

- [ ] **Step 1: Create package directory**

```bash
mkdir -p tests/evaluation/harness
```

- [ ] **Step 2: Create `__init__.py`**

```python
"""Evaluation harness for testing the 25 conversation paths."""
```

- [ ] **Step 3: Write `models.py`**

```python
"""Data model for evaluation conversation paths and results.

Frozen dataclasses representing conversation paths, telemetry assertions,
and execution results. All types are immutable for safety.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


# ---------------------------------------------------------------------------
# Assertion types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldAssertion:
    """Assert a field value in a specific telemetry event type.

    Args:
        event_type: The ES event_type to search for (e.g., "intent_classified").
        field_name: The field within that event (e.g., "task_type").
        expected: The expected value (string or numeric).
    """

    event_type: str
    field_name: str
    expected: str | float | int
    kind: Literal["field"] = "field"


@dataclass(frozen=True)
class EventPresenceAssertion:
    """Assert that a telemetry event type exists or does not exist.

    Args:
        event_type: The ES event_type to search for.
        present: True if event must exist, False if it must NOT exist.
    """

    event_type: str
    present: bool
    kind: Literal["presence"] = "presence"


@dataclass(frozen=True)
class FieldComparisonAssertion:
    """Assert a numeric comparison on a telemetry field.

    Args:
        event_type: The ES event_type to search for.
        field_name: The field to compare.
        operator: One of ">=", "<=", ">", "<".
        threshold: The numeric threshold.
    """

    event_type: str
    field_name: str
    operator: Literal[">=", "<=", ">", "<"]
    threshold: float | int
    kind: Literal["comparison"] = "comparison"


TelemetryAssertion = FieldAssertion | EventPresenceAssertion | FieldComparisonAssertion


# ---------------------------------------------------------------------------
# Conversation structure
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConversationTurn:
    """A single turn in a conversation path.

    Args:
        user_message: The exact message to send to the agent.
        expected_behavior: Human-readable description of expected behavior.
        assertions: Machine-verifiable telemetry assertions for this turn.
    """

    user_message: str
    expected_behavior: str
    assertions: tuple[TelemetryAssertion, ...] = ()


@dataclass(frozen=True)
class ConversationPath:
    """A complete multi-turn conversation path for evaluation.

    Args:
        path_id: Identifier like "CP-01".
        name: Human-readable name.
        category: Category from the capability matrix.
        objective: What this path tests.
        turns: Ordered sequence of conversation turns.
        quality_criteria: Human evaluation checkboxes.
        setup_notes: Optional setup instructions (e.g., for CP-18 resource pressure).
    """

    path_id: str
    name: str
    category: str
    objective: str
    turns: tuple[ConversationTurn, ...]
    quality_criteria: tuple[str, ...] = ()
    setup_notes: str | None = None


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AssertionResult:
    """Result of checking one telemetry assertion.

    Args:
        assertion: The original assertion that was checked.
        passed: Whether the assertion passed.
        actual_value: The actual value found (or None if event missing).
        message: Human-readable explanation.
    """

    assertion: TelemetryAssertion
    passed: bool
    actual_value: str | float | int | None
    message: str


@dataclass(frozen=True)
class TurnResult:
    """Result of executing one conversation turn.

    Args:
        turn_index: 0-based index within the path.
        user_message: The message that was sent.
        response_text: The agent's response.
        trace_id: The trace_id from the response.
        assertion_results: Results of all telemetry assertions.
        response_time_ms: Time to receive the response.
    """

    turn_index: int
    user_message: str
    response_text: str
    trace_id: str
    assertion_results: tuple[AssertionResult, ...]
    response_time_ms: float


@dataclass  # NOT frozen: runner appends turns incrementally during execution
class PathResult:
    """Result of executing a complete conversation path.

    Args:
        path_id: The path identifier.
        path_name: Human-readable name.
        category: Category from the capability matrix.
        session_id: The session used for this path.
        turns: Results for each turn.
        quality_criteria: Criteria for human evaluation (not scored by harness).
        all_assertions_passed: Whether all telemetry assertions passed.
        started_at: When execution started.
        completed_at: When execution completed.
    """

    path_id: str
    path_name: str
    category: str
    session_id: str
    turns: list[TurnResult] = field(default_factory=list)
    quality_criteria: tuple[str, ...] = ()
    all_assertions_passed: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def total_assertions(self) -> int:
        """Total number of telemetry assertions across all turns."""
        return sum(len(t.assertion_results) for t in self.turns)

    @property
    def passed_assertions(self) -> int:
        """Number of passed telemetry assertions."""
        return sum(
            1 for t in self.turns for a in t.assertion_results if a.passed
        )

    @property
    def failed_assertions(self) -> int:
        """Number of failed telemetry assertions."""
        return self.total_assertions - self.passed_assertions

    @property
    def total_time_ms(self) -> float:
        """Total response time across all turns."""
        return sum(t.response_time_ms for t in self.turns)


# ---------------------------------------------------------------------------
# Assertion builder helpers (compact syntax for dataset.py)
# ---------------------------------------------------------------------------

def fld(event: str, key: str, value: str | float | int) -> FieldAssertion:
    """Shorthand for FieldAssertion."""
    return FieldAssertion(event_type=event, field_name=key, expected=value)


def present(event: str) -> EventPresenceAssertion:
    """Shorthand: assert event IS present."""
    return EventPresenceAssertion(event_type=event, present=True)


def absent(event: str) -> EventPresenceAssertion:
    """Shorthand: assert event is NOT present."""
    return EventPresenceAssertion(event_type=event, present=False)


def gte(event: str, key: str, threshold: float | int) -> FieldComparisonAssertion:
    """Shorthand: assert field >= threshold."""
    return FieldComparisonAssertion(
        event_type=event, field_name=key, operator=">=", threshold=threshold,
    )
```

- [ ] **Step 4: Commit**

```bash
git add tests/evaluation/harness/__init__.py tests/evaluation/harness/models.py
git commit -m "feat(eval): add data model for evaluation harness"
```

---

## Task 2: Telemetry Checker

**Files:**
- Create: `tests/evaluation/harness/telemetry.py`
- Reference: `src/personal_agent/telemetry/queries.py` (ES query patterns)

- [ ] **Step 1: Write `telemetry.py`**

```python
"""Elasticsearch telemetry checker for evaluation assertions.

Queries ES for telemetry events by trace_id, then checks assertions
against the returned events. Includes retry logic to handle async
indexing delays.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import structlog
from elasticsearch import AsyncElasticsearch

from tests.evaluation.harness.models import (
    AssertionResult,
    EventPresenceAssertion,
    FieldAssertion,
    FieldComparisonAssertion,
    TelemetryAssertion,
)

log = structlog.get_logger(__name__)

# ES event: structlog JSON documents with string keys
TelemetryEvent = dict[str, object]

# Default ES config
DEFAULT_ES_URL = "http://localhost:9200"
DEFAULT_INDEX_PATTERN = "agent-logs-*"
DEFAULT_RETRY_DELAY_S = 1.5
DEFAULT_MAX_RETRIES = 4


class TelemetryChecker:
    """Checks telemetry assertions against Elasticsearch events.

    Args:
        es_url: Elasticsearch URL.
        index_pattern: Index pattern to query.
        retry_delay_s: Seconds to wait between retries.
        max_retries: Maximum number of retries for missing events.
    """

    def __init__(
        self,
        es_url: str = DEFAULT_ES_URL,
        index_pattern: str = DEFAULT_INDEX_PATTERN,
        retry_delay_s: float = DEFAULT_RETRY_DELAY_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._es_url = es_url
        self._index_pattern = index_pattern
        self._retry_delay_s = retry_delay_s
        self._max_retries = max_retries

    async def fetch_events(
        self, trace_id: str,
    ) -> list[TelemetryEvent]:
        """Fetch all telemetry events for a given trace_id.

        Retries if no events found (ES indexing may be async).

        Args:
            trace_id: The trace_id from the agent's response.

        Returns:
            List of ES document source dicts.
        """
        es = AsyncElasticsearch([self._es_url], request_timeout=10)
        try:
            for attempt in range(self._max_retries):
                response = await es.search(
                    index=self._index_pattern,
                    query={
                        "bool": {
                            "filter": [
                                {"term": {"trace_id": trace_id}},
                            ]
                        }
                    },
                    size=200,
                    sort=[{"@timestamp": "asc"}],
                )
                hits = response.get("hits", {}).get("hits", [])
                events = [h["_source"] for h in hits]

                if events:
                    log.debug(
                        "telemetry_events_fetched",
                        trace_id=trace_id,
                        count=len(events),
                        attempt=attempt + 1,
                    )
                    return events

                if attempt < self._max_retries - 1:
                    log.debug(
                        "telemetry_events_not_yet_indexed",
                        trace_id=trace_id,
                        attempt=attempt + 1,
                        retry_in_s=self._retry_delay_s,
                    )
                    await asyncio.sleep(self._retry_delay_s)

            log.warning(
                "telemetry_events_not_found",
                trace_id=trace_id,
                max_retries=self._max_retries,
            )
            return []
        finally:
            await es.close()

    def check_assertions(
        self,
        events: list[TelemetryEvent],
        assertions: Sequence[TelemetryAssertion],
    ) -> list[AssertionResult]:
        """Check a list of assertions against fetched telemetry events.

        Args:
            events: Telemetry events from ES.
            assertions: Assertions to check.

        Returns:
            List of AssertionResult for each assertion.
        """
        results: list[AssertionResult] = []
        for assertion in assertions:
            match assertion:
                case FieldAssertion():
                    results.append(self._check_field(events, assertion))
                case EventPresenceAssertion():
                    results.append(self._check_presence(events, assertion))
                case FieldComparisonAssertion():
                    results.append(self._check_comparison(events, assertion))
        return results

    def _find_events_by_type(
        self, events: list[dict], event_type: str,
    ) -> list[TelemetryEvent]:
        """Filter events by event_type field."""
        return [
            e for e in events
            if e.get("event_type") == event_type or e.get("event") == event_type
        ]

    def _check_field(
        self, events: list[dict], assertion: FieldAssertion,
    ) -> AssertionResult:
        """Check a FieldAssertion."""
        matching = self._find_events_by_type(events, assertion.event_type)
        if not matching:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                actual_value=None,
                message=f"No '{assertion.event_type}' event found",
            )

        # Check the most recent matching event
        event = matching[-1]
        actual = event.get(assertion.field_name)

        if actual is None:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                actual_value=None,
                message=(
                    f"Field '{assertion.field_name}' not found in "
                    f"'{assertion.event_type}' event"
                ),
            )

        # Normalize for comparison (ES may return strings for enums)
        expected_str = str(assertion.expected).lower()
        actual_str = str(actual).lower()
        passed = expected_str == actual_str

        return AssertionResult(
            assertion=assertion,
            passed=passed,
            actual_value=actual,
            message=(
                f"{assertion.event_type}.{assertion.field_name}: "
                f"expected={assertion.expected}, actual={actual}"
            ),
        )

    def _check_presence(
        self, events: list[dict], assertion: EventPresenceAssertion,
    ) -> AssertionResult:
        """Check an EventPresenceAssertion."""
        matching = self._find_events_by_type(events, assertion.event_type)
        found = len(matching) > 0

        if assertion.present:
            return AssertionResult(
                assertion=assertion,
                passed=found,
                actual_value=len(matching) if found else 0,
                message=(
                    f"Event '{assertion.event_type}': "
                    f"{'found' if found else 'NOT found'} "
                    f"(expected: present)"
                ),
            )
        else:
            return AssertionResult(
                assertion=assertion,
                passed=not found,
                actual_value=len(matching),
                message=(
                    f"Event '{assertion.event_type}': "
                    f"{'found' if found else 'not found'} "
                    f"(expected: absent)"
                ),
            )

    def _check_comparison(
        self, events: list[dict], assertion: FieldComparisonAssertion,
    ) -> AssertionResult:
        """Check a FieldComparisonAssertion."""
        matching = self._find_events_by_type(events, assertion.event_type)
        if not matching:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                actual_value=None,
                message=f"No '{assertion.event_type}' event found",
            )

        event = matching[-1]
        actual = event.get(assertion.field_name)

        if actual is None:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                actual_value=None,
                message=(
                    f"Field '{assertion.field_name}' not found in "
                    f"'{assertion.event_type}' event"
                ),
            )

        try:
            actual_num = float(actual)
        except (ValueError, TypeError):
            return AssertionResult(
                assertion=assertion,
                passed=False,
                actual_value=actual,
                message=(
                    f"Field '{assertion.field_name}' is not numeric: {actual}"
                ),
            )

        ops = {
            ">=": actual_num >= assertion.threshold,
            "<=": actual_num <= assertion.threshold,
            ">": actual_num > assertion.threshold,
            "<": actual_num < assertion.threshold,
        }
        passed = ops[assertion.operator]

        return AssertionResult(
            assertion=assertion,
            passed=passed,
            actual_value=actual_num,
            message=(
                f"{assertion.event_type}.{assertion.field_name}: "
                f"{actual_num} {assertion.operator} {assertion.threshold} "
                f"= {'PASS' if passed else 'FAIL'}"
            ),
        )
```

- [ ] **Step 2: Commit**

```bash
git add tests/evaluation/harness/telemetry.py
git commit -m "feat(eval): add telemetry checker with ES query + retry"
```

---

## Task 3: Runner

**Files:**
- Create: `tests/evaluation/harness/runner.py`
- Reference: `src/personal_agent/service/app.py` (API schemas)
- Reference: `src/personal_agent/ui/service_client.py` (httpx patterns)

- [ ] **Step 1: Write `runner.py`**

```python
"""Core evaluation runner — sends conversation turns to the agent API
and checks telemetry assertions via Elasticsearch.

Usage:
    runner = EvaluationRunner(agent_url="http://localhost:9000")
    result = await runner.run_path(path)
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import datetime, timezone

import httpx
import structlog

from tests.evaluation.harness.models import (
    ConversationPath,
    PathResult,
    TelemetryAssertion,
    TurnResult,
)
from tests.evaluation.harness.telemetry import TelemetryChecker

log = structlog.get_logger(__name__)

DEFAULT_AGENT_URL = "http://localhost:9000"
DEFAULT_CHAT_TIMEOUT_S = 120.0
DEFAULT_INTER_TURN_DELAY_S = 2.0


class EvaluationRunner:
    """Executes conversation paths against the live agent API.

    Args:
        agent_url: Base URL of the agent service.
        telemetry: TelemetryChecker instance for assertion verification.
        chat_timeout_s: Timeout for POST /chat requests.
        inter_turn_delay_s: Delay between turns to allow ES indexing.
    """

    def __init__(
        self,
        agent_url: str = DEFAULT_AGENT_URL,
        telemetry: TelemetryChecker | None = None,
        chat_timeout_s: float = DEFAULT_CHAT_TIMEOUT_S,
        inter_turn_delay_s: float = DEFAULT_INTER_TURN_DELAY_S,
    ) -> None:
        self._agent_url = agent_url
        self._telemetry = telemetry or TelemetryChecker()
        self._chat_timeout_s = chat_timeout_s
        self._inter_turn_delay_s = inter_turn_delay_s

    async def check_agent_health(self) -> bool:
        """Verify the agent service is running and healthy.

        Returns:
            True if agent is reachable and healthy.
        """
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                resp = await client.get(f"{self._agent_url}/health")
                resp.raise_for_status()
                data = resp.json()
                return data.get("status") == "healthy"
            except (httpx.HTTPError, httpx.ConnectError):
                return False

    async def create_session(self) -> str:
        """Create a new session for a conversation path.

        Returns:
            The session_id string.
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._agent_url}/sessions",
                json={"channel": "CHAT", "mode": "NORMAL", "metadata": {}},
            )
            resp.raise_for_status()
            data = resp.json()
            return str(data["session_id"])

    async def run_path(self, path: ConversationPath) -> PathResult:
        """Execute a complete conversation path.

        Creates a fresh session, sends each turn sequentially,
        checks telemetry assertions after each turn.

        Args:
            path: The conversation path to execute.

        Returns:
            PathResult with all turn results and assertion outcomes.
        """
        session_id = await self.create_session()
        result = PathResult(
            path_id=path.path_id,
            path_name=path.name,
            category=path.category,
            session_id=session_id,
            quality_criteria=path.quality_criteria,
            started_at=datetime.now(tz=timezone.utc),
        )

        log.info(
            "path_execution_started",
            path_id=path.path_id,
            path_name=path.name,
            session_id=session_id,
            turn_count=len(path.turns),
        )

        async with httpx.AsyncClient(
            timeout=self._chat_timeout_s,
        ) as client:
            for i, turn in enumerate(path.turns):
                turn_result = await self._execute_turn(
                    client=client,
                    session_id=session_id,
                    turn_index=i,
                    user_message=turn.user_message,
                    assertions=turn.assertions,
                )
                result.turns.append(turn_result)

                # Delay between turns to allow ES indexing
                if i < len(path.turns) - 1:
                    await asyncio.sleep(self._inter_turn_delay_s)

                log.info(
                    "turn_executed",
                    path_id=path.path_id,
                    turn=i + 1,
                    trace_id=turn_result.trace_id,
                    assertions_passed=sum(
                        1 for a in turn_result.assertion_results if a.passed
                    ),
                    assertions_total=len(turn_result.assertion_results),
                    response_time_ms=turn_result.response_time_ms,
                )

        result.completed_at = datetime.now(tz=timezone.utc)
        result.all_assertions_passed = all(
            a.passed
            for t in result.turns
            for a in t.assertion_results
        )

        log.info(
            "path_execution_completed",
            path_id=path.path_id,
            all_passed=result.all_assertions_passed,
            passed=result.passed_assertions,
            failed=result.failed_assertions,
            total_time_ms=result.total_time_ms,
        )

        return result

    async def run_paths(
        self, paths: Sequence[ConversationPath],
    ) -> list[PathResult]:
        """Execute multiple conversation paths sequentially.

        Args:
            paths: Conversation paths to execute.

        Returns:
            List of PathResult for each path.
        """
        results: list[PathResult] = []
        for path in paths:
            result = await self.run_path(path)
            results.append(result)
        return results

    async def _execute_turn(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        turn_index: int,
        user_message: str,
        assertions: tuple[TelemetryAssertion, ...],
    ) -> TurnResult:
        """Execute a single conversation turn.

        Sends the message, waits for indexing, then checks assertions.

        Args:
            client: httpx client.
            session_id: Current session.
            turn_index: 0-based turn index.
            user_message: Message to send.
            assertions: Telemetry assertions to check.

        Returns:
            TurnResult with response and assertion outcomes.
        """
        start = time.monotonic()
        resp = await client.post(
            f"{self._agent_url}/chat",
            params={"message": user_message, "session_id": session_id},
        )
        resp.raise_for_status()
        elapsed_ms = (time.monotonic() - start) * 1000

        data = resp.json()
        response_text = data.get("response", "")
        trace_id = data.get("trace_id", "")

        # Check telemetry assertions
        assertion_results = ()
        if assertions:
            events = await self._telemetry.fetch_events(trace_id)
            assertion_results = tuple(
                self._telemetry.check_assertions(events, assertions)
            )

        return TurnResult(
            turn_index=turn_index,
            user_message=user_message,
            response_text=response_text,
            trace_id=trace_id,
            assertion_results=assertion_results,
            response_time_ms=elapsed_ms,
        )
```

- [ ] **Step 2: Commit**

```bash
git add tests/evaluation/harness/runner.py
git commit -m "feat(eval): add evaluation runner with HTTP client and assertion checking"
```

---

## Task 4: Report Generator

**Files:**
- Create: `tests/evaluation/harness/report.py`

- [ ] **Step 1: Write `report.py`**

```python
"""Report generator for evaluation results.

Produces JSON (machine-readable) and markdown (human-readable) reports
from PathResult data.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from tests.evaluation.harness.models import PathResult


def generate_json_report(
    results: Sequence[PathResult],
    output_path: Path | None = None,
) -> dict:
    """Generate a JSON report from evaluation results.

    Args:
        results: List of PathResult from runner.
        output_path: Optional path to write JSON file.

    Returns:
        Report dictionary.
    """
    total_assertions = sum(r.total_assertions for r in results)
    passed_assertions = sum(r.passed_assertions for r in results)
    paths_passed = sum(1 for r in results if r.all_assertions_passed)

    report = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "summary": {
            "total_paths": len(results),
            "paths_passed": paths_passed,
            "paths_failed": len(results) - paths_passed,
            "path_pass_rate": (
                paths_passed / len(results) if results else 0.0
            ),
            "total_assertions": total_assertions,
            "assertions_passed": passed_assertions,
            "assertions_failed": total_assertions - passed_assertions,
            "assertion_pass_rate": (
                passed_assertions / total_assertions
                if total_assertions > 0
                else 0.0
            ),
            "total_response_time_ms": sum(r.total_time_ms for r in results),
            "avg_response_time_ms": (
                sum(r.total_time_ms for r in results) / sum(
                    len(r.turns) for r in results
                )
                if any(r.turns for r in results)
                else 0.0
            ),
        },
        "by_category": _group_by_category(results),
        "paths": [_serialize_path(r) for r in results],
    }

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, default=str))

    return report


def generate_markdown_report(
    results: Sequence[PathResult],
    output_path: Path | None = None,
) -> str:
    """Generate a markdown report from evaluation results.

    Args:
        results: List of PathResult from runner.
        output_path: Optional path to write markdown file.

    Returns:
        Markdown string.
    """
    total_assertions = sum(r.total_assertions for r in results)
    passed_assertions = sum(r.passed_assertions for r in results)
    paths_passed = sum(1 for r in results if r.all_assertions_passed)

    lines: list[str] = []
    lines.append("# Evaluation Results Report")
    lines.append("")
    lines.append(
        f"**Generated:** {datetime.now(tz=timezone.utc).isoformat()}"
    )
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Paths Passed | {paths_passed}/{len(results)} |")
    lines.append(
        f"| Assertions Passed | {passed_assertions}/{total_assertions} |"
    )
    rate = (
        passed_assertions / total_assertions * 100
        if total_assertions > 0
        else 0.0
    )
    lines.append(f"| Assertion Pass Rate | {rate:.1f}% |")
    avg_ms = (
        sum(r.total_time_ms for r in results)
        / sum(len(r.turns) for r in results)
        if any(r.turns for r in results)
        else 0.0
    )
    lines.append(f"| Avg Response Time | {avg_ms:.0f} ms |")
    lines.append("")

    # Results by category
    categories = _group_by_category(results)
    lines.append("## Results by Category")
    lines.append("")
    lines.append("| Category | Passed | Failed | Pass Rate |")
    lines.append("|----------|--------|--------|-----------|")
    for cat_name, cat_data in categories.items():
        p = cat_data["passed"]
        t = cat_data["total"]
        pct = p / t * 100 if t > 0 else 0.0
        lines.append(f"| {cat_name} | {p} | {t - p} | {pct:.0f}% |")
    lines.append("")

    # Per-path details
    lines.append("## Path Details")
    lines.append("")
    for r in results:
        status = "✅" if r.all_assertions_passed else "❌"
        lines.append(f"### {status} {r.path_id}: {r.path_name}")
        lines.append("")
        lines.append(f"**Category:** {r.category} | **Session:** `{r.session_id}`")
        lines.append(
            f"**Assertions:** {r.passed_assertions}/{r.total_assertions} passed"
        )
        lines.append("")

        for turn in r.turns:
            lines.append(f"**Turn {turn.turn_index + 1}** ({turn.response_time_ms:.0f} ms)")
            lines.append(f"- **Sent:** {turn.user_message[:100]}...")
            lines.append(f"- **Trace:** `{turn.trace_id}`")

            for a in turn.assertion_results:
                icon = "✅" if a.passed else "❌"
                lines.append(f"  - {icon} {a.message}")
            lines.append("")

        # Quality criteria (for human eval)
        if r.quality_criteria:
            lines.append("**Quality Criteria (Human Eval):**")
            for criterion in r.quality_criteria:
                lines.append(f"- [ ] {criterion}")
            lines.append("")

        lines.append("---")
        lines.append("")

    report_text = "\n".join(lines)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_text)

    return report_text


def _group_by_category(
    results: Sequence[PathResult],
) -> dict[str, dict]:
    """Group results by category with pass/fail counts."""
    categories: dict[str, dict] = {}
    for r in results:
        if r.category not in categories:
            categories[r.category] = {"total": 0, "passed": 0}
        categories[r.category]["total"] += 1
        if r.all_assertions_passed:
            categories[r.category]["passed"] += 1
    return categories


def _serialize_path(r: PathResult) -> dict:
    """Serialize a PathResult to a JSON-compatible dict."""
    return {
        "path_id": r.path_id,
        "path_name": r.path_name,
        "category": r.category,
        "session_id": r.session_id,
        "all_passed": r.all_assertions_passed,
        "assertions_passed": r.passed_assertions,
        "assertions_total": r.total_assertions,
        "total_time_ms": r.total_time_ms,
        "turns": [
            {
                "turn_index": t.turn_index,
                "user_message": t.user_message,
                "response_text": t.response_text[:500],
                "trace_id": t.trace_id,
                "response_time_ms": t.response_time_ms,
                "assertions": [
                    {
                        "passed": a.passed,
                        "message": a.message,
                        "actual_value": a.actual_value,
                    }
                    for a in t.assertion_results
                ],
            }
            for t in r.turns
        ],
        "quality_criteria": list(r.quality_criteria),
    }
```

- [ ] **Step 2: Commit**

```bash
git add tests/evaluation/harness/report.py
git commit -m "feat(eval): add JSON + markdown report generator"
```

---

## Task 5: Dataset — Intent Classification (CP-01 to CP-07)

**Files:**
- Create: `tests/evaluation/harness/dataset.py`
- Reference: `docs/research/EVALUATION_DATASET.md`

- [ ] **Step 1: Write dataset.py header and CP-01 through CP-07**

```python
"""All 25 evaluation conversation paths as Python data structures.

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
"""

from __future__ import annotations

from tests.evaluation.harness.models import (
    ConversationPath,
    ConversationTurn,
    absent,
    fld,
    gte,
    present,
)

# ============================================================================
# Category 1: Intent Classification (CP-01 to CP-07)
# ============================================================================

CP_01 = ConversationPath(
    path_id="CP-01",
    name="Conversational Intent",
    category="Intent Classification",
    objective=(
        "Verify that simple conversational messages fall through all "
        "pattern banks to the default CONVERSATIONAL classification"
    ),
    turns=(
        ConversationTurn(
            user_message="Hey, how's it going?",
            expected_behavior=(
                "Responds conversationally. No tool calls. No sub-agents."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                fld("intent_classified", "confidence", 0.7),
                fld("decomposition_assessed", "complexity", "simple"),
                fld("decomposition_assessed", "strategy", "single"),
                absent("tool_call_completed"),
                absent("hybrid_expansion_start"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Tell me something interesting you've learned recently."
            ),
            expected_behavior=(
                "Continues conversational tone. May draw on general "
                "knowledge. No tool calls."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                absent("tool_call_completed"),
            ),
        ),
    ),
    quality_criteria=(
        "Response is natural and engaging, not robotic",
        "Appropriate length (not a one-word answer, not an essay)",
        "No unnecessary tool invocations or system introspection",
        "Turn 2 response demonstrates personality or knowledge",
    ),
)

CP_02 = ConversationPath(
    path_id="CP-02",
    name="Memory Recall Intent",
    category="Intent Classification",
    objective=(
        "Verify that 'have we discussed' triggers MEMORY_RECALL "
        "classification and broad recall"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "I've been thinking about building a recommendation "
                "engine using collaborative filtering."
            ),
            expected_behavior=(
                "Responds to the topic. Entities like 'recommendation "
                "engine' and 'collaborative filtering' should be captured."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "What have we discussed in our conversations so far?"
            ),
            expected_behavior=(
                "Triggers memory recall. Should reference the "
                "recommendation engine topic from Turn 1."
            ),
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                fld("intent_classified", "confidence", 0.9),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 2 response references the recommendation engine topic",
        "If no prior history, gracefully acknowledges limited history",
        "Response is structured (not a wall of text)",
        "Does not hallucinate conversations that never happened",
    ),
)

CP_03 = ConversationPath(
    path_id="CP-03",
    name="Analysis Intent",
    category="Intent Classification",
    objective="Verify that 'Analyze' triggers ANALYSIS classification",
    turns=(
        ConversationTurn(
            user_message=(
                "Analyze the trade-offs between REST and GraphQL "
                "for a small team building internal APIs."
            ),
            expected_behavior=(
                "Provides structured analysis comparing REST vs GraphQL. "
                "Addresses team size constraint."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("intent_classified", "confidence", 0.8),
                fld("decomposition_assessed", "complexity", "simple"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message="Which would you lean toward for our case and why?",
            expected_behavior=(
                "Provides a recommendation grounded in the prior analysis."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 1 covers at least 3 trade-off dimensions",
        "Addresses the 'small team' constraint specifically",
        "Turn 2 recommendation is consistent with Turn 1 analysis",
        "Structured format (bullets, headers, or numbered points)",
    ),
)

CP_04 = ConversationPath(
    path_id="CP-04",
    name="Planning Intent",
    category="Intent Classification",
    objective="Verify that 'Plan' triggers PLANNING classification",
    turns=(
        ConversationTurn(
            user_message=(
                "Plan the next steps for adding user authentication "
                "to our API service."
            ),
            expected_behavior=(
                "Produces a structured plan with discrete steps, "
                "rough ordering, and considerations."
            ),
            assertions=(
                fld("intent_classified", "task_type", "planning"),
                fld("intent_classified", "confidence", 0.8),
                fld("decomposition_assessed", "complexity", "simple"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "What should we tackle first, and what can we defer?"
            ),
            expected_behavior=(
                "Prioritizes the steps with reasoning."
            ),
            assertions=(),
        ),
    ),
    quality_criteria=(
        "Plan includes at least 4 concrete steps",
        "Steps have a logical ordering",
        "Addresses auth method choices (OAuth, JWT, session-based)",
        "Turn 2 provides clear prioritization with reasoning",
    ),
)

CP_05 = ConversationPath(
    path_id="CP-05",
    name="Delegation Intent (Explicit and Implicit)",
    category="Intent Classification",
    objective=(
        "Verify both explicit delegation ('Use Claude Code to...') and "
        "implicit delegation ('Write a function...') trigger DELEGATION"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Use Claude Code to write a function that parses nested "
                "JSON configuration files with schema validation and "
                "returns structured error messages for each validation "
                "failure."
            ),
            expected_behavior=(
                "Classifies as DELEGATION. Should compose a "
                "DelegationPackage with target_agent='claude-code'."
            ),
            assertions=(
                fld("intent_classified", "task_type", "delegation"),
                fld("intent_classified", "confidence", 0.85),
                fld("decomposition_assessed", "strategy", "delegate"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Write unit tests for the edge cases — circular "
                "references, missing required keys, and deeply nested "
                "structures beyond 10 levels."
            ),
            expected_behavior=(
                "Follow-up delegation. Enriches the task with test "
                "requirements."
            ),
            assertions=(
                fld("intent_classified", "task_type", "delegation"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "What context would you include in the handoff to make "
                "sure Claude Code doesn't need to ask follow-up questions?"
            ),
            expected_behavior=(
                "Explains DelegationPackage contents: relevant_files, "
                "conventions, known_pitfalls."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 1: Agent composes a DelegationPackage rather than writing code",
        "Turn 1: task_description is clear for an agent with no prior context",
        "Turn 2: acceptance_criteria includes the three edge cases",
        "Turn 3: Demonstrates awareness of what external agents need",
        "Package is sufficient for Claude Code without follow-up questions",
    ),
)

CP_06 = ConversationPath(
    path_id="CP-06",
    name="Self-Improvement Intent",
    category="Intent Classification",
    objective=(
        "Verify that self-referential improvement questions trigger "
        "SELF_IMPROVE classification"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "What improvements would you suggest to your own "
                "memory and recall system?"
            ),
            expected_behavior=(
                "Discusses potential improvements to its own architecture."
            ),
            assertions=(
                fld("intent_classified", "task_type", "self_improve"),
                fld("intent_classified", "confidence", 0.85),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Which of those would have the biggest impact on "
                "your usefulness to me?"
            ),
            expected_behavior="Prioritizes suggestions with reasoning.",
            assertions=(),
        ),
    ),
    quality_criteria=(
        "Suggestions reference actual system capabilities",
        "Does not hallucinate features the system doesn't have",
        "Turn 2 prioritization is grounded, not generic",
        "Demonstrates self-awareness about current limitations",
    ),
)

CP_07 = ConversationPath(
    path_id="CP-07",
    name="Tool Use Intent",
    category="Intent Classification",
    objective=(
        "Verify that explicit tool-use language triggers TOOL_USE "
        "classification"
    ),
    turns=(
        ConversationTurn(
            user_message="List the tools you currently have access to.",
            expected_behavior=(
                "Enumerates available tools (search_memory, "
                "system_metrics_snapshot, self_telemetry_query, "
                "read_file, list_directory, plus any MCP tools)."
            ),
            assertions=(
                fld("intent_classified", "task_type", "tool_use"),
                fld("intent_classified", "confidence", 0.8),
                fld("decomposition_assessed", "complexity", "simple"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Read the system log and tell me if anything "
                "looks concerning."
            ),
            expected_behavior=(
                "Calls self_telemetry_query or reads log output. "
                "Reports findings."
            ),
            assertions=(
                fld("intent_classified", "task_type", "tool_use"),
                present("tool_call_completed"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 1 lists tools accurately",
        "Turn 2 actually calls a tool (not just describes it)",
        "Tool results are interpreted and summarized, not dumped raw",
        "If system is healthy, says so; if issues found, highlights them",
    ),
)
```

- [ ] **Step 2: Commit**

```bash
git add tests/evaluation/harness/dataset.py
git commit -m "feat(eval): add intent classification paths CP-01 to CP-07"
```

---

## Task 6: Dataset — Decomposition & Memory (CP-08 to CP-15)

**Files:**
- Modify: `tests/evaluation/harness/dataset.py` (append)

- [ ] **Step 1: Append CP-08 through CP-15 to dataset.py**

```python
# ============================================================================
# Category 2: Decomposition Strategies (CP-08 to CP-11)
# ============================================================================

CP_08 = ConversationPath(
    path_id="CP-08",
    name="SINGLE Strategy (Simple Question)",
    category="Decomposition Strategies",
    objective=(
        "Verify that a simple, short question results in SINGLE strategy"
    ),
    turns=(
        ConversationTurn(
            user_message="What is dependency injection?",
            expected_behavior=(
                "Clear, concise explanation. No sub-agents. Single LLM call."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                fld("decomposition_assessed", "complexity", "simple"),
                fld("decomposition_assessed", "strategy", "single"),
                absent("hybrid_expansion_start"),
            ),
        ),
        ConversationTurn(
            user_message="Can you give me a quick example in Python?",
            expected_behavior="Another simple response. Still SINGLE.",
            assertions=(
                fld("decomposition_assessed", "strategy", "single"),
                absent("hybrid_expansion_start"),
            ),
        ),
    ),
    quality_criteria=(
        "Explanation is clear and accurate",
        "Appropriate depth for a definitional question",
        "Python example in Turn 2 is correct and illustrative",
        "Fast response time (no expansion overhead)",
    ),
)

CP_09 = ConversationPath(
    path_id="CP-09",
    name="HYBRID Strategy (Moderate Analysis)",
    category="Decomposition Strategies",
    objective=(
        "Verify that a moderate-complexity analysis triggers HYBRID "
        "with sub-agent expansion"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Research the advantages of event sourcing versus CRUD "
                "for session storage, and evaluate their suitability "
                "for a PostgreSQL-backed system."
            ),
            expected_behavior=(
                "HYBRID expansion triggered. Sub-agents research "
                "individual aspects. Final response synthesizes."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "complexity", "moderate"),
                fld("decomposition_assessed", "strategy", "hybrid"),
                present("hybrid_expansion_start"),
                gte("hybrid_expansion_start", "sub_agent_count", 1),
                present("hybrid_expansion_complete"),
                gte("hybrid_expansion_complete", "successes", 1),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Given what you found, which approach would you "
                "recommend for our use case?"
            ),
            expected_behavior=(
                "Single follow-up referencing Turn 1 analysis."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
    ),
    quality_criteria=(
        "Response covers both event sourcing AND CRUD approaches",
        "PostgreSQL-specific considerations addressed",
        "Sub-agent contributions synthesized coherently",
        "Turn 2 recommendation grounded in Turn 1 analysis",
        "Quality noticeably better than a single-pass response",
    ),
)

CP_10 = ConversationPath(
    path_id="CP-10",
    name="DECOMPOSE Strategy (Complex Multi-Part Analysis)",
    category="Decomposition Strategies",
    objective=(
        "Verify that a complex multi-part request with 3+ action verbs "
        "triggers DECOMPOSE"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Compare three approaches to distributed caching, "
                "evaluate their performance under load, analyze the "
                "cost implications for each, and recommend which fits "
                "a system handling ten thousand requests per second."
            ),
            expected_behavior=(
                "Full decomposition. Multiple sub-agents. "
                "Comprehensive synthesized output."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "complexity", "complex"),
                fld("decomposition_assessed", "strategy", "decompose"),
                present("hybrid_expansion_start"),
                gte("hybrid_expansion_start", "sub_agent_count", 2),
                gte("hybrid_expansion_complete", "successes", 2),
            ),
        ),
    ),
    quality_criteria=(
        "At least 3 caching approaches compared",
        "Performance evaluation includes metrics or benchmarks",
        "Cost analysis is concrete, not vague",
        "Recommendation is specific with clear reasoning",
        "Response well-structured with sections for each part",
    ),
)

CP_11 = ConversationPath(
    path_id="CP-11",
    name="Complexity Escalation Across Turns",
    category="Decomposition Strategies",
    objective=(
        "Verify that each turn is classified independently — "
        "a simple first question doesn't lock the strategy"
    ),
    turns=(
        ConversationTurn(
            user_message="What is a knowledge graph?",
            expected_behavior="Simple definitional answer. SINGLE strategy.",
            assertions=(
                fld("decomposition_assessed", "complexity", "simple"),
                fld("decomposition_assessed", "strategy", "single"),
                absent("hybrid_expansion_start"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Compare Neo4j and Dgraph for entity storage, and "
                "evaluate their query performance and Python ecosystem "
                "support."
            ),
            expected_behavior=(
                "Moderate analysis. HYBRID strategy. Sub-agents spawned."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "complexity", "moderate"),
                fld("decomposition_assessed", "strategy", "hybrid"),
                present("hybrid_expansion_start"),
                gte("hybrid_expansion_start", "sub_agent_count", 1),
            ),
        ),
        ConversationTurn(
            user_message="Based on that comparison, which should we use?",
            expected_behavior="Simple follow-up. Back to SINGLE strategy.",
            assertions=(
                fld("decomposition_assessed", "complexity", "simple"),
                fld("decomposition_assessed", "strategy", "single"),
                absent("hybrid_expansion_start"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 1 is concise and accurate",
        "Turn 2 is noticeably more detailed (HYBRID effect)",
        "Turn 2 covers both databases across both dimensions",
        "Turn 3 recommendation references Turn 2 analysis",
        "No classification bleed-over between turns",
    ),
)

# ============================================================================
# Category 3: Memory System (CP-12 to CP-15)
# ============================================================================

CP_12 = ConversationPath(
    path_id="CP-12",
    name="Entity Seeding and Targeted Recall",
    category="Memory System",
    objective=(
        "Verify that entities mentioned in conversation are captured "
        "and can be recalled"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "I've been working on a project called Project Atlas. "
                "It's a data pipeline that processes satellite imagery "
                "using Apache Kafka and Apache Spark."
            ),
            expected_behavior=(
                "Responds to the topic. Entities captured: "
                "Project Atlas, Apache Kafka, Apache Spark."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "The team lead is Maria Chen and we're deploying to AWS "
                "with a target of processing 500 images per hour."
            ),
            expected_behavior=(
                "More context. Entities: Maria Chen, AWS."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message="What do you know about Project Atlas?",
            expected_behavior=(
                "Triggers MEMORY_RECALL. Should reference the data "
                "pipeline, Kafka, Spark, Maria Chen, and AWS."
            ),
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                fld("intent_classified", "confidence", 0.9),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 3 references Project Atlas by name",
        "Mentions at least 3 of: pipeline, imagery, Kafka, Spark, Maria Chen, AWS",
        "Information is accurate (not hallucinated)",
        "Demonstrates synthesis, not just parroting",
    ),
)

CP_13 = ConversationPath(
    path_id="CP-13",
    name="Broad Recall",
    category="Memory System",
    objective=(
        "Verify that open-ended recall questions trigger the broad "
        "recall path and return grouped results"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "I've been evaluating Django and FastAPI for our new "
                "web service. FastAPI seems faster but Django has more "
                "batteries included."
            ),
            expected_behavior="Responds to the framework comparison.",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "We also need to decide between PostgreSQL and MongoDB "
                "for the storage layer. Our data is mostly relational "
                "but we have some document-like structures."
            ),
            expected_behavior="Responds to the database discussion.",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message="What topics have we covered in this conversation?",
            expected_behavior=(
                "MEMORY_RECALL with broad recall. Lists both the "
                "framework and database topics."
            ),
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
    ),
    quality_criteria=(
        "Identifies at least 2 distinct topics (web frameworks, databases)",
        "Mentions specific technologies (Django, FastAPI, PostgreSQL, MongoDB)",
        "Response is organized — groups topics",
        "Captures key considerations (speed vs batteries, relational vs document)",
        "Does not hallucinate topics not discussed",
    ),
)

CP_14 = ConversationPath(
    path_id="CP-14",
    name="Multi-Entity Tracking",
    category="Memory System",
    objective=(
        "Verify that when multiple entities are introduced, the agent "
        "recalls the correct one"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Alice on our team is building a CI/CD automation tool "
                "called BuildBot. She's using Python and GitHub Actions."
            ),
            expected_behavior="Responds about Alice and BuildBot.",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Bob is working on a deployment tool called DeployTool. "
                "He's focused on Terraform and AWS infrastructure."
            ),
            expected_behavior="Responds about Bob and DeployTool.",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message="What do you know about Alice and her work?",
            expected_behavior=(
                "Recalls Alice + BuildBot + Python + GitHub Actions. "
                "Should NOT conflate with Bob's work."
            ),
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                fld("intent_classified", "confidence", 0.9),
            ),
        ),
    ),
    quality_criteria=(
        "Correctly associates Alice with BuildBot, Python, GitHub Actions",
        "Does NOT mention Bob, DeployTool, Terraform, or AWS",
        "Demonstrates entity-relationship awareness",
        "Clean separation between the two people",
    ),
)

CP_15 = ConversationPath(
    path_id="CP-15",
    name="Memory-Informed Response",
    category="Memory System",
    objective=(
        "Verify that earlier context shapes later responses, "
        "not just generic knowledge"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "I'm building a real-time dashboard using WebSockets "
                "and React to monitor IoT sensor data produced by "
                "industrial equipment."
            ),
            expected_behavior="Acknowledges the project details.",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "What technology stack would you recommend for the "
                "backend of this project?"
            ),
            expected_behavior=(
                "Recommendations compatible with WebSockets, IoT, "
                "and real-time requirements. Not a generic answer."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
    ),
    quality_criteria=(
        "Recommendation explicitly references WebSockets from Turn 1",
        "Addresses IoT/real-time requirements (not generic web stack)",
        "Technologies compatible with stated stack",
        "Does not recommend conflicting technologies",
        "Feels like a conversation, not two isolated questions",
    ),
)
```

- [ ] **Step 2: Commit**

```bash
git add tests/evaluation/harness/dataset.py
git commit -m "feat(eval): add decomposition and memory paths CP-08 to CP-15"
```

---

## Task 7: Dataset — Expansion, Context, Tools, Edge Cases (CP-16 to CP-25)

**Files:**
- Modify: `tests/evaluation/harness/dataset.py` (append)

- [ ] **Step 1: Append CP-16 through CP-25 to dataset.py**

```python
# ============================================================================
# Category 4: Expansion & Sub-Agents (CP-16 to CP-18)
# ============================================================================

CP_16 = ConversationPath(
    path_id="CP-16",
    name="HYBRID Synthesis Quality",
    category="Expansion & Sub-Agents",
    objective=(
        "Verify that HYBRID expansion produces a synthesized response "
        "better than a single-pass answer"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Research microservices communication patterns and "
                "evaluate the trade-offs between synchronous HTTP, "
                "asynchronous messaging, and gRPC."
            ),
            expected_behavior=(
                "HYBRID expansion triggered. Sub-agents research "
                "different patterns. Primary agent synthesizes."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "complexity", "moderate"),
                fld("decomposition_assessed", "strategy", "hybrid"),
                present("hybrid_expansion_start"),
                gte("hybrid_expansion_start", "sub_agent_count", 1),
                present("hybrid_expansion_complete"),
                gte("hybrid_expansion_complete", "successes", 1),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Which pattern would you recommend for a system with "
                "both low-latency and high-throughput requirements?"
            ),
            expected_behavior=(
                "Follow-up referencing Turn 1 analysis. SINGLE strategy."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
    ),
    quality_criteria=(
        "All three communication patterns covered (HTTP, async, gRPC)",
        "Trade-offs are concrete (latency, complexity, tooling)",
        "Response feels unified — not three stitched answers",
        "Synthesis adds value (comparison table, decision framework)",
        "Turn 2 recommendation grounded in Turn 1 analysis",
    ),
)

CP_17 = ConversationPath(
    path_id="CP-17",
    name="Sub-Agent Concurrency",
    category="Expansion & Sub-Agents",
    objective=(
        "Verify that DECOMPOSE spawns multiple sub-agents and "
        "synthesizes all results"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Compare the performance characteristics of Redis, "
                "Memcached, and Hazelcast for distributed caching. "
                "Analyze their memory management approaches and "
                "evaluate operational complexity. Recommend which "
                "fits our workload of ten thousand requests per second."
            ),
            expected_behavior=(
                "DECOMPOSE triggered. Multiple sub-agents. "
                "All results synthesized."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "complexity", "complex"),
                fld("decomposition_assessed", "strategy", "decompose"),
                present("hybrid_expansion_start"),
                gte("hybrid_expansion_start", "sub_agent_count", 2),
                gte("hybrid_expansion_complete", "successes", 2),
            ),
        ),
    ),
    quality_criteria=(
        "All three caching systems compared",
        "Performance includes throughput, latency, memory efficiency",
        "Memory management differences explained",
        "Operational complexity addressed",
        "Final recommendation is specific and justified",
    ),
)

CP_18 = ConversationPath(
    path_id="CP-18",
    name="Expansion Budget Enforcement",
    category="Expansion & Sub-Agents",
    objective=(
        "Verify that expansion_budget forces SINGLE under resource "
        "pressure"
    ),
    setup_notes=(
        "Requires system resource pressure. Before running:\n"
        "1. Run `stress --cpu 4 --timeout 60s` to push CPU above 70%\n"
        "2. OR set expansion_budget=0 in governance for testing\n"
        "3. Monitor expansion_budget_computed events\n"
        "Run same message WITHOUT load as control comparison."
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Research the advantages of container orchestration "
                "and evaluate Kubernetes versus Docker Swarm for "
                "small engineering teams."
            ),
            expected_behavior=(
                "Normally HYBRID (2 action verbs, ANALYSIS). Under "
                "resource pressure, forced to SINGLE."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                # Under load: strategy forced to single
                # Under normal: strategy would be hybrid
                # Assert on what's expected given the test conditions
            ),
        ),
    ),
    quality_criteria=(
        "Under load: provides reasonable response (graceful degradation)",
        "Under load: response less detailed than HYBRID version",
        "Budget enforcement transparent in telemetry",
        "Compare quality: SINGLE vs HYBRID version of same question",
    ),
)

# ============================================================================
# Category 5: Context Management (CP-19 to CP-20)
# ============================================================================

CP_19 = ConversationPath(
    path_id="CP-19",
    name="Long Conversation Trimming",
    category="Context Management",
    objective=(
        "Verify that long conversations are trimmed intelligently — "
        "important context preserved"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Let's talk about our system architecture. We use a "
                "microservices pattern with FastAPI services "
                "communicating over HTTP."
            ),
            expected_behavior="Establishes foundational context.",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Our primary database is PostgreSQL for transactional "
                "data."
            ),
            expected_behavior="Adds more context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "We also use Elasticsearch for logging and Neo4j for "
                "our knowledge graph."
            ),
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "The deployment is on Docker Compose locally and "
                "Kubernetes in production."
            ),
            expected_behavior="More context.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "We've been having issues with service discovery "
                "between containers."
            ),
            expected_behavior="Introduces a problem.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "I tried using Consul but it added too much "
                "operational overhead."
            ),
            expected_behavior="Adds history.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "We're now evaluating DNS-based service discovery "
                "versus Envoy sidecar proxies."
            ),
            expected_behavior="Current state.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "The team is leaning toward Envoy because it also "
                "handles load balancing."
            ),
            expected_behavior="Team preference.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "But I'm worried about the memory overhead of running "
                "Envoy sidecars on every service."
            ),
            expected_behavior="Concern.",
            assertions=(),
        ),
        ConversationTurn(
            user_message=(
                "Going back to the beginning — what was our primary "
                "database again?"
            ),
            expected_behavior=(
                "Should still know PostgreSQL despite potential "
                "context trimming."
            ),
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 10: correctly identifies PostgreSQL as primary database",
        "If trimmed, important foundational facts were retained",
        "Conversation feels coherent throughout",
        "Agent doesn't forget mid-conversation",
    ),
)

CP_20 = ConversationPath(
    path_id="CP-20",
    name="Progressive Token Budget Management",
    category="Context Management",
    objective=(
        "Verify that tool-heavy conversations manage token budgets "
        "correctly"
    ),
    turns=(
        ConversationTurn(
            user_message="Run the system health check.",
            expected_behavior=(
                "Calls self_telemetry_query(health). Large tool output."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                present("tool_call_completed"),
            ),
        ),
        ConversationTurn(
            user_message="Now show me the recent error details.",
            expected_behavior=(
                "Calls self_telemetry_query(errors). More tool output."
            ),
            assertions=(
                present("tool_call_completed"),
            ),
        ),
        ConversationTurn(
            user_message="Also check the system metrics.",
            expected_behavior=(
                "Calls system_metrics_snapshot. Even more tool output."
            ),
            assertions=(
                present("tool_call_completed"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Summarize everything you've found — is the system "
                "healthy overall?"
            ),
            expected_behavior=(
                "Synthesizes all three tool results. Context may "
                "need trimming."
            ),
            assertions=(),
        ),
    ),
    quality_criteria=(
        "Each tool call returns valid data",
        "Turn 4 synthesizes findings coherently",
        "If trimmed, most recent tool results preserved",
        "Agent identifies any genuine issues",
    ),
)

# ============================================================================
# Category 6: Tools & Self-Inspection (CP-21 to CP-23)
# ============================================================================

CP_21 = ConversationPath(
    path_id="CP-21",
    name="System Metrics (Natural Language)",
    category="Tools & Self-Inspection",
    objective=(
        "Verify the agent calls system_metrics_snapshot even when "
        "intent is CONVERSATIONAL (natural language doesn't match "
        "TOOL_USE patterns)"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "How is the system doing right now? I want to know "
                "about CPU and memory usage."
            ),
            expected_behavior=(
                "Calls system_metrics_snapshot tool despite "
                "CONVERSATIONAL classification."
            ),
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                present("tool_call_completed"),
            ),
        ),
        ConversationTurn(
            user_message="Is that normal for our setup?",
            expected_behavior=(
                "Interprets metrics with context."
            ),
            assertions=(),
        ),
    ),
    quality_criteria=(
        "Agent calls the tool (doesn't just describe it)",
        "Response includes actual CPU %, memory %, disk % values",
        "Values are interpreted, not just dumped",
        "Turn 2 provides context-aware interpretation",
    ),
)

CP_22 = ConversationPath(
    path_id="CP-22",
    name="Self-Telemetry Query",
    category="Tools & Self-Inspection",
    objective=(
        "Verify the agent can introspect its own operational health"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Show me your error rate and performance metrics "
                "over the past hour."
            ),
            expected_behavior=(
                "Calls self_telemetry_query with query_type='health' "
                "or 'performance' and window='1h'."
            ),
            assertions=(
                present("tool_call_completed"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Are there any specific errors I should be worried "
                "about?"
            ),
            expected_behavior=(
                "Calls self_telemetry_query with query_type='errors'."
            ),
            assertions=(
                present("tool_call_completed"),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 1 reports success rate, latency, or throughput",
        "Turn 2 reports specific error types or confirms no errors",
        "Data is interpreted, not raw JSON dumped",
        "Demonstrates genuine self-awareness about operational state",
    ),
)

CP_23 = ConversationPath(
    path_id="CP-23",
    name="Search Memory Tool (Explicit)",
    category="Tools & Self-Inspection",
    objective=(
        "Verify that the agent uses the search_memory tool when "
        "explicitly asked"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "I've been learning about distributed systems, "
                "particularly consensus algorithms like Raft and Paxos."
            ),
            expected_behavior="Establishes context for memory.",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "I'm also interested in how CRDTs enable conflict-free "
                "replication."
            ),
            expected_behavior="More context.",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Search your memory for anything related to distributed "
                "systems."
            ),
            expected_behavior=(
                "Triggers search_memory tool."
            ),
            assertions=(
                fld("intent_classified", "task_type", "tool_use"),
                present("tool_call_completed"),
            ),
        ),
    ),
    quality_criteria=(
        "Agent actually calls search_memory tool",
        "Results reference distributed systems topics",
        "If no prior data, gracefully indicates this",
        "Distinguishes memory data vs. session context",
    ),
)

# ============================================================================
# Category 7: Edge Cases (CP-24 to CP-25)
# ============================================================================

CP_24 = ConversationPath(
    path_id="CP-24",
    name="Ambiguous Intent",
    category="Edge Cases",
    objective=(
        "Verify that priority-ordered classification handles "
        "ambiguous messages correctly"
    ),
    turns=(
        ConversationTurn(
            user_message=(
                "Can you look into why our unit tests keep failing "
                "and fix the flaky ones in the authentication module?"
            ),
            expected_behavior=(
                "Multiple signals: 'fix' + 'unit test' → DELEGATION "
                "(priority 3 beats analysis at priority 5)."
            ),
            assertions=(
                fld("intent_classified", "task_type", "delegation"),
                fld("intent_classified", "confidence", 0.85),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Actually, before fixing anything, just analyze the "
                "failure patterns first."
            ),
            expected_behavior=(
                "Clearer intent: 'analyze' → ANALYSIS. "
                "Demonstrates user can redirect."
            ),
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("intent_classified", "confidence", 0.8),
            ),
        ),
    ),
    quality_criteria=(
        "Turn 1: treats as delegation/coding task",
        "Turn 2: shifts to analysis mode — investigates patterns",
        "Transition between intents is smooth",
        "No carry-over of Turn 1 intent into Turn 2",
    ),
)

CP_25 = ConversationPath(
    path_id="CP-25",
    name="Intent Shift Mid-Conversation",
    category="Edge Cases",
    objective=(
        "Verify that the gateway classifies each turn independently "
        "— no bleed-over from prior turns"
    ),
    turns=(
        ConversationTurn(
            user_message="Hey there, how are you doing today?",
            expected_behavior="Conversational greeting.",
            assertions=(
                fld("intent_classified", "task_type", "conversational"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Analyze the impact of adding a caching layer between "
                "our API and database."
            ),
            expected_behavior="Analysis request.",
            assertions=(
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "Write a function that implements a simple LRU cache "
                "in Python."
            ),
            expected_behavior=(
                "Delegation request. Different intent from Turn 2."
            ),
            assertions=(
                fld("intent_classified", "task_type", "delegation"),
                fld("decomposition_assessed", "strategy", "delegate"),
            ),
        ),
        ConversationTurn(
            user_message=(
                "What have we discussed about caching in this "
                "conversation?"
            ),
            expected_behavior=(
                "Memory recall. References Turns 2 and 3."
            ),
            assertions=(
                fld("intent_classified", "task_type", "memory_recall"),
                fld("decomposition_assessed", "strategy", "single"),
            ),
        ),
    ),
    quality_criteria=(
        "Each turn's response matches its intent",
        "Turn 2 provides genuine analysis",
        "Turn 3 produces code (or delegation package)",
        "Turn 4 recalls the caching discussion from Turns 2-3",
        "No classification bleed-over between turns",
    ),
)

# ============================================================================
# Registry: all paths for easy import
# ============================================================================

ALL_PATHS: tuple[ConversationPath, ...] = (
    CP_01, CP_02, CP_03, CP_04, CP_05, CP_06, CP_07,
    CP_08, CP_09, CP_10, CP_11,
    CP_12, CP_13, CP_14, CP_15,
    CP_16, CP_17, CP_18,
    CP_19, CP_20,
    CP_21, CP_22, CP_23,
    CP_24, CP_25,
)

PATHS_BY_ID: dict[str, ConversationPath] = {p.path_id: p for p in ALL_PATHS}

PATHS_BY_CATEGORY: dict[str, tuple[ConversationPath, ...]] = {}
for _p in ALL_PATHS:
    _cat = _p.category
    if _cat not in PATHS_BY_CATEGORY:
        PATHS_BY_CATEGORY[_cat] = ()
    PATHS_BY_CATEGORY[_cat] = (*PATHS_BY_CATEGORY[_cat], _p)
```

- [ ] **Step 2: Commit**

```bash
git add tests/evaluation/harness/dataset.py
git commit -m "feat(eval): add expansion, context, tools, edge case paths CP-16 to CP-25"
```

---

## Task 8: Unit Tests for Harness Components

**Files:**
- Create: `tests/evaluation/harness/test_unit.py`

- [ ] **Step 1: Write unit tests**

```python
"""Unit tests for evaluation harness components.

Tests the data model, telemetry checker (mocked ES), and runner (mocked HTTP).
These do NOT require a running agent — they verify harness correctness.
"""

from __future__ import annotations

import pytest

from tests.evaluation.harness.models import (
    AssertionResult,
    ConversationPath,
    ConversationTurn,
    EventPresenceAssertion,
    FieldAssertion,
    FieldComparisonAssertion,
    PathResult,
    TurnResult,
    absent,
    fld,
    gte,
    present,
)
from tests.evaluation.harness.telemetry import TelemetryChecker


# ---------------------------------------------------------------------------
# Data model tests
# ---------------------------------------------------------------------------

class TestModels:
    """Tests for frozen dataclasses and builder helpers."""

    def test_field_assertion_creation(self) -> None:
        a = fld("intent_classified", "task_type", "analysis")
        assert isinstance(a, FieldAssertion)
        assert a.event_type == "intent_classified"
        assert a.field_name == "task_type"
        assert a.expected == "analysis"
        assert a.kind == "field"

    def test_presence_assertion_creation(self) -> None:
        a = present("hybrid_expansion_start")
        assert isinstance(a, EventPresenceAssertion)
        assert a.event_type == "hybrid_expansion_start"
        assert a.present is True
        assert a.kind == "presence"

    def test_absence_assertion_creation(self) -> None:
        a = absent("tool_call_completed")
        assert isinstance(a, EventPresenceAssertion)
        assert a.event_type == "tool_call_completed"
        assert a.present is False

    def test_comparison_assertion_creation(self) -> None:
        a = gte("hybrid_expansion_complete", "successes", 1)
        assert isinstance(a, FieldComparisonAssertion)
        assert a.operator == ">="
        assert a.threshold == 1

    def test_conversation_path_is_frozen(self) -> None:
        path = ConversationPath(
            path_id="CP-TEST",
            name="Test Path",
            category="Test",
            objective="Test objective",
            turns=(
                ConversationTurn(
                    user_message="Hello",
                    expected_behavior="Responds",
                    assertions=(fld("intent_classified", "task_type", "conversational"),),
                ),
            ),
        )
        with pytest.raises(AttributeError):
            path.name = "Changed"  # type: ignore[misc]

    def test_path_result_properties(self) -> None:
        result = PathResult(
            path_id="CP-01",
            path_name="Test",
            category="Test",
            session_id="abc-123",
        )
        # Add a turn with 2 assertions (1 pass, 1 fail)
        result.turns.append(
            TurnResult(
                turn_index=0,
                user_message="Hello",
                response_text="Hi",
                trace_id="trace-1",
                assertion_results=(
                    AssertionResult(
                        assertion=fld("x", "y", "z"),
                        passed=True,
                        actual_value="z",
                        message="ok",
                    ),
                    AssertionResult(
                        assertion=fld("x", "y", "z"),
                        passed=False,
                        actual_value="w",
                        message="fail",
                    ),
                ),
                response_time_ms=150.0,
            )
        )
        assert result.total_assertions == 2
        assert result.passed_assertions == 1
        assert result.failed_assertions == 1
        assert result.total_time_ms == 150.0


# ---------------------------------------------------------------------------
# Telemetry checker tests (mocked ES)
# ---------------------------------------------------------------------------

class TestTelemetryChecker:
    """Tests for TelemetryChecker.check_assertions (no ES needed)."""

    def setup_method(self) -> None:
        self.checker = TelemetryChecker()

    def test_field_assertion_passes(self) -> None:
        events = [
            {"event_type": "intent_classified", "task_type": "analysis", "confidence": 0.8}
        ]
        results = self.checker.check_assertions(
            events,
            [fld("intent_classified", "task_type", "analysis")],
        )
        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].actual_value == "analysis"

    def test_field_assertion_fails_wrong_value(self) -> None:
        events = [
            {"event_type": "intent_classified", "task_type": "conversational"}
        ]
        results = self.checker.check_assertions(
            events,
            [fld("intent_classified", "task_type", "analysis")],
        )
        assert results[0].passed is False
        assert results[0].actual_value == "conversational"

    def test_field_assertion_fails_missing_event(self) -> None:
        events = [
            {"event_type": "other_event", "some_field": "value"}
        ]
        results = self.checker.check_assertions(
            events,
            [fld("intent_classified", "task_type", "analysis")],
        )
        assert results[0].passed is False
        assert results[0].actual_value is None

    def test_field_assertion_case_insensitive(self) -> None:
        events = [
            {"event_type": "intent_classified", "task_type": "ANALYSIS"}
        ]
        results = self.checker.check_assertions(
            events,
            [fld("intent_classified", "task_type", "analysis")],
        )
        assert results[0].passed is True

    def test_presence_assertion_found(self) -> None:
        events = [
            {"event_type": "hybrid_expansion_start", "sub_agent_count": 2}
        ]
        results = self.checker.check_assertions(
            events, [present("hybrid_expansion_start")],
        )
        assert results[0].passed is True

    def test_presence_assertion_not_found(self) -> None:
        events = [{"event_type": "other_event"}]
        results = self.checker.check_assertions(
            events, [present("hybrid_expansion_start")],
        )
        assert results[0].passed is False

    def test_absence_assertion_not_found(self) -> None:
        events = [{"event_type": "other_event"}]
        results = self.checker.check_assertions(
            events, [absent("hybrid_expansion_start")],
        )
        assert results[0].passed is True

    def test_absence_assertion_found(self) -> None:
        events = [{"event_type": "hybrid_expansion_start"}]
        results = self.checker.check_assertions(
            events, [absent("hybrid_expansion_start")],
        )
        assert results[0].passed is False

    def test_comparison_assertion_passes(self) -> None:
        events = [
            {"event_type": "hybrid_expansion_complete", "successes": 3}
        ]
        results = self.checker.check_assertions(
            events, [gte("hybrid_expansion_complete", "successes", 2)],
        )
        assert results[0].passed is True

    def test_comparison_assertion_fails(self) -> None:
        events = [
            {"event_type": "hybrid_expansion_complete", "successes": 1}
        ]
        results = self.checker.check_assertions(
            events, [gte("hybrid_expansion_complete", "successes", 2)],
        )
        assert results[0].passed is False

    def test_multiple_assertions(self) -> None:
        events = [
            {"event_type": "intent_classified", "task_type": "analysis", "confidence": 0.8},
            {"event_type": "decomposition_assessed", "strategy": "hybrid"},
            {"event_type": "hybrid_expansion_start", "sub_agent_count": 2},
        ]
        results = self.checker.check_assertions(
            events,
            [
                fld("intent_classified", "task_type", "analysis"),
                fld("decomposition_assessed", "strategy", "hybrid"),
                present("hybrid_expansion_start"),
                absent("tool_call_completed"),
            ],
        )
        assert len(results) == 4
        assert all(r.passed for r in results)

    def test_event_field_fallback(self) -> None:
        """Verify checker also matches on 'event' field (not just 'event_type')."""
        events = [
            {"event": "intent_classified", "task_type": "analysis"}
        ]
        results = self.checker.check_assertions(
            events,
            [fld("intent_classified", "task_type", "analysis")],
        )
        assert results[0].passed is True
```

- [ ] **Step 2: Run unit tests**

```bash
cd /Users/Alex/Dev/personal_agent/.claude/worktrees/heuristic-khayyam
uv run pytest tests/evaluation/harness/test_unit.py -v
```

Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/evaluation/harness/test_unit.py
git commit -m "test(eval): add unit tests for harness models and telemetry checker"
```

---

## Task 9: Pytest Integration + CLI Entry Point

**Files:**
- Create: `tests/evaluation/harness/conftest.py`
- Create: `tests/evaluation/harness/test_paths.py`
- Create: `tests/evaluation/harness/run.py`
- Modify: `pyproject.toml` (add evaluation marker)

- [ ] **Step 1: Add evaluation marker to pyproject.toml**

In `pyproject.toml`, add to the `markers` list:

```toml
markers = [
    "integration: marks tests as integration tests (deselect with '-m \"not integration\"')",
    "requires_llm_server: marks tests that require LLM server running",
    "evaluation: marks evaluation path tests (require live agent on port 9000)"
]
```

- [ ] **Step 2: Write `conftest.py`**

```python
"""Pytest fixtures for evaluation harness.

Provides httpx client, ES client, and agent health check.
Tests marked with @pytest.mark.evaluation require the live agent.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from tests.evaluation.harness.runner import EvaluationRunner
from tests.evaluation.harness.telemetry import TelemetryChecker


@pytest.fixture(scope="session")
def telemetry_checker() -> TelemetryChecker:
    """Shared TelemetryChecker instance."""
    return TelemetryChecker()


@pytest.fixture(scope="session")
def evaluation_runner(telemetry_checker: TelemetryChecker) -> EvaluationRunner:
    """Shared EvaluationRunner instance."""
    return EvaluationRunner(telemetry=telemetry_checker)


@pytest_asyncio.fixture(scope="session")
async def agent_healthy(evaluation_runner: EvaluationRunner) -> bool:
    """Check that the agent is healthy before running evaluation tests."""
    healthy = await evaluation_runner.check_agent_health()
    if not healthy:
        pytest.skip("Agent service not running on port 9000")
    return healthy
```

- [ ] **Step 3: Write `test_paths.py`**

```python
"""Parameterized pytest tests for all 25 evaluation conversation paths.

Each path runs as a separate test case. Tests are marked with
@pytest.mark.evaluation and require the live agent service.

Run:
    uv run pytest tests/evaluation/harness/test_paths.py -v -m evaluation

Run a single path:
    uv run pytest tests/evaluation/harness/test_paths.py -v -k "CP_01"

Run a category:
    uv run pytest tests/evaluation/harness/test_paths.py -v -k "Intent"
"""

from __future__ import annotations

import pytest

from tests.evaluation.harness.dataset import ALL_PATHS
from tests.evaluation.harness.models import ConversationPath
from tests.evaluation.harness.runner import EvaluationRunner


@pytest.mark.evaluation
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ALL_PATHS,
    ids=[p.path_id for p in ALL_PATHS],
)
async def test_conversation_path(
    path: ConversationPath,
    evaluation_runner: EvaluationRunner,
    agent_healthy: bool,
) -> None:
    """Execute a conversation path and verify telemetry assertions.

    Quality criteria are NOT checked here — they require human judgment.
    This test only verifies machine-checkable telemetry assertions.

    Paths with setup_notes (e.g., CP-18) may need manual setup before
    running. If the path has no telemetry assertions, it passes
    automatically (quality-only paths).
    """
    if path.setup_notes:
        pytest.skip(f"Requires manual setup: {path.setup_notes[:80]}...")

    result = await evaluation_runner.run_path(path)

    # Build detailed failure message
    if not result.all_assertions_passed:
        failures = []
        for turn in result.turns:
            for a in turn.assertion_results:
                if not a.passed:
                    failures.append(
                        f"  Turn {turn.turn_index + 1} "
                        f"(trace={turn.trace_id}): {a.message}"
                    )
        failure_msg = (
            f"{path.path_id} ({path.name}): "
            f"{result.failed_assertions}/{result.total_assertions} "
            f"assertions failed:\n" + "\n".join(failures)
        )
        pytest.fail(failure_msg)
```

- [ ] **Step 4: Write `run.py` (standalone CLI)**

```python
"""Standalone CLI entry point for running evaluation paths.

Usage:
    # Run all paths
    uv run python -m tests.evaluation.harness.run

    # Run specific paths
    uv run python -m tests.evaluation.harness.run --paths CP-01 CP-02 CP-03

    # Run a category
    uv run python -m tests.evaluation.harness.run --category "Intent Classification"

    # Custom agent URL
    uv run python -m tests.evaluation.harness.run --agent-url http://localhost:9000

    # Save reports
    uv run python -m tests.evaluation.harness.run --output-dir telemetry/evaluation
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import structlog

from tests.evaluation.harness.dataset import (
    ALL_PATHS,
    PATHS_BY_CATEGORY,
    PATHS_BY_ID,
)
from tests.evaluation.harness.models import ConversationPath
from tests.evaluation.harness.report import (
    generate_json_report,
    generate_markdown_report,
)
from tests.evaluation.harness.runner import EvaluationRunner
from tests.evaluation.harness.telemetry import TelemetryChecker

log = structlog.get_logger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Run evaluation conversation paths against the live agent",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        help="Specific path IDs to run (e.g., CP-01 CP-02)",
    )
    parser.add_argument(
        "--category",
        help="Run all paths in a category (e.g., 'Intent Classification')",
    )
    parser.add_argument(
        "--agent-url",
        default="http://localhost:9000",
        help="Agent service URL (default: http://localhost:9000)",
    )
    parser.add_argument(
        "--es-url",
        default="http://localhost:9200",
        help="Elasticsearch URL (default: http://localhost:9200)",
    )
    parser.add_argument(
        "--output-dir",
        default="telemetry/evaluation",
        help="Directory for output reports (default: telemetry/evaluation)",
    )
    parser.add_argument(
        "--skip-setup",
        action="store_true",
        help="Skip paths that require manual setup (e.g., CP-18)",
    )
    return parser.parse_args()


def select_paths(args: argparse.Namespace) -> list[ConversationPath]:
    """Select paths based on CLI arguments."""
    if args.paths:
        paths = []
        for pid in args.paths:
            if pid not in PATHS_BY_ID:
                log.error("unknown_path_id", path_id=pid)
                sys.exit(1)
            paths.append(PATHS_BY_ID[pid])
        return paths

    if args.category:
        if args.category not in PATHS_BY_CATEGORY:
            log.error(
                "unknown_category",
                category=args.category,
                available=list(PATHS_BY_CATEGORY.keys()),
            )
            sys.exit(1)
        paths = list(PATHS_BY_CATEGORY[args.category])
    else:
        paths = list(ALL_PATHS)

    if args.skip_setup:
        paths = [p for p in paths if p.setup_notes is None]

    return paths


async def main() -> None:
    """Main entry point."""
    args = parse_args()
    paths = select_paths(args)

    if not paths:
        log.error("no_paths_selected")
        sys.exit(1)

    log.info(
        "evaluation_starting",
        path_count=len(paths),
        path_ids=[p.path_id for p in paths],
    )

    telemetry = TelemetryChecker(es_url=args.es_url)
    runner = EvaluationRunner(
        agent_url=args.agent_url,
        telemetry=telemetry,
    )

    # Health check
    healthy = await runner.check_agent_health()
    if not healthy:
        log.error("agent_not_healthy", url=args.agent_url)
        sys.exit(1)

    # Run paths
    results = await runner.run_paths(paths)

    # Generate reports
    output_dir = Path(args.output_dir)
    json_path = output_dir / "evaluation_results.json"
    md_path = output_dir / "evaluation_results.md"

    report = generate_json_report(results, json_path)
    generate_markdown_report(results, md_path)

    # Summary
    summary = report["summary"]
    log.info(
        "evaluation_complete",
        paths_passed=summary["paths_passed"],
        paths_total=summary["total_paths"],
        assertions_passed=summary["assertions_passed"],
        assertions_total=summary["total_assertions"],
        pass_rate=f"{summary['assertion_pass_rate']:.1%}",
    )

    log.info(
        "evaluation_reports_saved",
        json_path=str(json_path),
        md_path=str(md_path),
    )

    # Exit code: 0 if all passed, 1 if any failed
    if summary["paths_failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 5: Run unit tests to verify no import errors**

```bash
cd /Users/Alex/Dev/personal_agent/.claude/worktrees/heuristic-khayyam
uv run pytest tests/evaluation/harness/test_unit.py -v
```

Expected: All tests PASS.

- [ ] **Step 6: Verify CLI help works**

```bash
cd /Users/Alex/Dev/personal_agent/.claude/worktrees/heuristic-khayyam
uv run python -m tests.evaluation.harness.run --help
```

Expected: Prints usage information.

- [ ] **Step 7: Commit**

```bash
git add tests/evaluation/harness/conftest.py tests/evaluation/harness/test_paths.py tests/evaluation/harness/run.py pyproject.toml
git commit -m "feat(eval): add pytest integration, CLI entry point, and evaluation marker"
```

---

## Post-Implementation Verification

After all tasks are complete, run the full check:

```bash
# 1. Unit tests pass
uv run pytest tests/evaluation/harness/test_unit.py -v

# 2. Type checking passes
uv run mypy tests/evaluation/harness/ --ignore-missing-imports

# 3. Linting passes
uv run ruff check tests/evaluation/harness/
uv run ruff format tests/evaluation/harness/

# 4. CLI help works
uv run python -m tests.evaluation.harness.run --help

# 5. Evaluation tests are discovered (but skipped without agent)
uv run pytest tests/evaluation/harness/test_paths.py --collect-only

# 6. (With live agent) Run a single path
uv run python -m tests.evaluation.harness.run --paths CP-01
```

---

## Usage Reference

### From pytest

```bash
# Run all evaluation paths (requires live agent)
uv run pytest tests/evaluation/harness/test_paths.py -v -m evaluation

# Run a single path
uv run pytest tests/evaluation/harness/test_paths.py -v -k "CP_01"

# Run intent classification paths only
uv run pytest tests/evaluation/harness/test_paths.py -v -k "CP_01 or CP_02 or CP_03 or CP_04 or CP_05 or CP_06 or CP_07"

# Exclude from normal test runs
uv run pytest -m "not evaluation"
```

### From CLI

```bash
# Run all 25 paths with reports
uv run python -m tests.evaluation.harness.run --output-dir telemetry/evaluation

# Run specific paths
uv run python -m tests.evaluation.harness.run --paths CP-01 CP-09 CP-12

# Run a category
uv run python -m tests.evaluation.harness.run --category "Memory System"

# Skip paths needing manual setup (CP-18)
uv run python -m tests.evaluation.harness.run --skip-setup
```

### From Claude Code (the meta-evaluation)

```
"Start the agent on port 9000, then run:
uv run python -m tests.evaluation.harness.run --skip-setup --output-dir telemetry/evaluation
Read the markdown report at telemetry/evaluation/evaluation_results.md
and evaluate the response quality against the quality criteria."
```
