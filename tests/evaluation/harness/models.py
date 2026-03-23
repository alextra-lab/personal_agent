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
        """Total number of telemetry assertions across all turns.

        Returns:
            Sum of assertion counts across all TurnResults.
        """
        return sum(len(t.assertion_results) for t in self.turns)

    @property
    def passed_assertions(self) -> int:
        """Number of passed telemetry assertions.

        Returns:
            Count of AssertionResults where passed is True.
        """
        return sum(1 for t in self.turns for a in t.assertion_results if a.passed)

    @property
    def failed_assertions(self) -> int:
        """Number of failed telemetry assertions.

        Returns:
            total_assertions minus passed_assertions.
        """
        return self.total_assertions - self.passed_assertions

    @property
    def total_time_ms(self) -> float:
        """Total response time across all turns.

        Returns:
            Sum of response_time_ms for all TurnResults.
        """
        return sum(t.response_time_ms for t in self.turns)


# ---------------------------------------------------------------------------
# Assertion builder helpers (compact syntax for dataset.py)
# ---------------------------------------------------------------------------


def fld(event: str, key: str, value: str | float | int) -> FieldAssertion:
    """Shorthand for FieldAssertion.

    Args:
        event: The ES event_type to search for.
        key: The field name to check.
        value: The expected value.

    Returns:
        A FieldAssertion with the given parameters.
    """
    return FieldAssertion(event_type=event, field_name=key, expected=value)


def present(event: str) -> EventPresenceAssertion:
    """Shorthand: assert event IS present.

    Args:
        event: The ES event_type to search for.

    Returns:
        An EventPresenceAssertion requiring the event to exist.
    """
    return EventPresenceAssertion(event_type=event, present=True)


def absent(event: str) -> EventPresenceAssertion:
    """Shorthand: assert event is NOT present.

    Args:
        event: The ES event_type that must not exist.

    Returns:
        An EventPresenceAssertion requiring the event to be absent.
    """
    return EventPresenceAssertion(event_type=event, present=False)


def gte(event: str, key: str, threshold: float | int) -> FieldComparisonAssertion:
    """Shorthand: assert field >= threshold.

    Args:
        event: The ES event_type to search for.
        key: The field name to compare.
        threshold: The numeric lower bound (inclusive).

    Returns:
        A FieldComparisonAssertion with operator ">=".
    """
    return FieldComparisonAssertion(
        event_type=event,
        field_name=key,
        operator=">=",
        threshold=threshold,
    )
