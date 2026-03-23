"""Elasticsearch telemetry checker for evaluation assertions.

Queries ES for telemetry events by trace_id, then checks assertions
against the returned events. Includes retry logic to handle async
indexing delays.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import cast

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

    def __init__(  # noqa: D107
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
        self,
        trace_id: str,
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
                case _:
                    log.warning(  # type: ignore[unreachable]
                        "unknown_assertion_type",
                        assertion_type=type(assertion).__name__,
                    )
        return results

    def _find_events_by_type(
        self,
        events: list[TelemetryEvent],
        event_type: str,
    ) -> list[TelemetryEvent]:
        """Filter events by event_type field.

        Args:
            events: All telemetry events for a trace.
            event_type: The event type string to match.

        Returns:
            Filtered list of events matching event_type.
        """
        return [
            e for e in events if e.get("event_type") == event_type or e.get("event") == event_type
        ]

    def _check_field(
        self,
        events: list[TelemetryEvent],
        assertion: FieldAssertion,
    ) -> AssertionResult:
        """Check a FieldAssertion.

        Args:
            events: All telemetry events for a trace.
            assertion: The assertion to check.

        Returns:
            AssertionResult with pass/fail and actual value.
        """
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
                    f"Field '{assertion.field_name}' not found in '{assertion.event_type}' event"
                ),
            )

        # Normalize for comparison (ES may return strings for enums)
        expected_str = str(assertion.expected).lower()
        actual_str = str(actual).lower()
        passed = expected_str == actual_str
        actual_typed = cast("str | float | int | None", actual)

        return AssertionResult(
            assertion=assertion,
            passed=passed,
            actual_value=actual_typed,
            message=(
                f"{assertion.event_type}.{assertion.field_name}: "
                f"expected={assertion.expected}, actual={actual}"
            ),
        )

    def _check_presence(
        self,
        events: list[TelemetryEvent],
        assertion: EventPresenceAssertion,
    ) -> AssertionResult:
        """Check an EventPresenceAssertion.

        Args:
            events: All telemetry events for a trace.
            assertion: The assertion to check.

        Returns:
            AssertionResult with pass/fail and event count as actual_value.
        """
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
        self,
        events: list[TelemetryEvent],
        assertion: FieldComparisonAssertion,
    ) -> AssertionResult:
        """Check a FieldComparisonAssertion.

        Args:
            events: All telemetry events for a trace.
            assertion: The assertion to check.

        Returns:
            AssertionResult with pass/fail and numeric actual_value.
        """
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
                    f"Field '{assertion.field_name}' not found in '{assertion.event_type}' event"
                ),
            )

        actual_typed = cast("str | float | int | None", actual)
        try:
            actual_num = float(cast("str | float | int", actual))
        except (ValueError, TypeError):
            return AssertionResult(
                assertion=assertion,
                passed=False,
                actual_value=actual_typed,
                message=(f"Field '{assertion.field_name}' is not numeric: {actual}"),
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
