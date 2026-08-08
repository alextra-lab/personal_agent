"""Tests for the ADR-0133 governed telemetry vocabulary (FRE-1177)."""

from __future__ import annotations

import pytest

from personal_agent.exceptions import VocabularyViolationError
from personal_agent.telemetry.vocabulary import (
    DECLARED_TYPES,
    NEAR_MISS_EXCEPTIONS,
    NEAR_MISS_THRESHOLD,
    RETIRED_SPELLINGS,
    validate_document,
)


@pytest.mark.parametrize("retired_spelling", sorted(RETIRED_SPELLINGS))
def test_every_retired_spelling_is_rejected(retired_spelling: str) -> None:
    """AC-1: every entry in the committed retired-spelling table fails, enumerated."""
    with pytest.raises(VocabularyViolationError) as exc_info:
        validate_document({retired_spelling: "some_value"})
    assert exc_info.value.field == retired_spelling
    assert exc_info.value.rule == "retired_spelling"


@pytest.mark.parametrize("governed_name,declared_type", sorted(DECLARED_TYPES.items()))
def test_every_governed_type_rejects_wrong_type(governed_name: str, declared_type: type) -> None:
    """AC-2: every governed name carrying a declared type rejects a wrong-typed value."""
    wrong_value: object = "not-an-int" if declared_type is int else 12345
    with pytest.raises(VocabularyViolationError) as exc_info:
        validate_document({governed_name: wrong_value})
    assert exc_info.value.field == governed_name
    assert exc_info.value.rule == "declared_type"


def test_int_declared_field_rejects_a_bool() -> None:
    """Rule 3: bool is a subclass of int, so isinstance alone would let it through.

    Codex review (2026-08-08): ``input_tokens=True`` must fail exactly like
    any other wrong-typed value, not silently pass because Python considers
    ``bool`` an ``int``.
    """
    with pytest.raises(VocabularyViolationError) as exc_info:
        validate_document({"input_tokens": True})
    assert exc_info.value.rule == "declared_type"


def test_a_null_governed_field_passes() -> None:
    """Rule 3: a governed field with no value is not a type violation.

    ``es_logger.log_event`` legitimately writes ``trace_id``/``span_id`` as
    ``None`` when no trace context exists — Rule 3 must not treat that as a
    declared-type failure.
    """
    validate_document({"trace_id": None, "span_id": None})  # must not raise


def test_near_miss_threshold_is_085() -> None:
    """AC-3: the near-miss threshold is decided at 0.85, not left to implementation."""
    assert NEAR_MISS_THRESHOLD == 0.85


def test_near_miss_catches_tarce_id_at_the_advertised_boundary() -> None:
    """AC-3: tarce_id (0.875 vs trace_id) raises — pins the threshold from above.

    A threshold set too high would still catch sesion_id (0.947) and pass a
    check that only planted the easy one; this is the boundary case.
    """
    with pytest.raises(VocabularyViolationError) as exc_info:
        validate_document({"tarce_id": "abc123"})
    assert exc_info.value.rule == "near_miss"


def test_near_miss_catches_sesion_id() -> None:
    """AC-3: sesion_id (0.947 vs session_id) raises."""
    with pytest.raises(VocabularyViolationError) as exc_info:
        validate_document({"sesion_id": "abc123"})
    assert exc_info.value.rule == "near_miss"


def test_component_is_the_committed_exception_and_passes() -> None:
    """AC-3: component (0.857 vs component_id) is the sole committed exception."""
    validate_document({"component": "es_handler"})  # must not raise

    exc = NEAR_MISS_EXCEPTIONS["component"]
    assert exc.matched_governed_name == "component_id"
    assert exc.similarity == 0.857
    assert exc.reason


def test_unrecognised_key_passes_cleanly() -> None:
    """AC-4: a key in no vocabulary and above no near-miss threshold passes.

    Proves the 178 family-private names named in ADR-0133's Context keep the
    freedom ADR-0128 D3 granted them — a reject-everything validator fails this.
    """
    validate_document({"queue_depth": 42})  # must not raise


def test_a_clean_document_with_every_governed_field_passes() -> None:
    """Sanity: a document assembled entirely from correctly-typed governed fields passes."""
    validate_document(
        {
            "@timestamp": "2026-08-08T00:00:00",
            "event_type": "task_started",
            "trace_id": "trace-abc",
            "span_id": "span-abc",
            "session_id": "sess-abc",
            "component_id": "orchestrator",
            "user_id": "user-abc",
            "input_tokens": 10,
            "output_tokens": 20,
        }
    )
