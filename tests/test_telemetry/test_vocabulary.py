"""Tests for the ADR-0133 governed telemetry vocabulary (FRE-1177, FRE-1178)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from personal_agent.config.env_loader import Environment
from personal_agent.exceptions import VocabularyViolationError
from personal_agent.telemetry import vocabulary
from personal_agent.telemetry.vocabulary import (
    DECLARED_TYPES,
    NEAR_MISS_EXCEPTIONS,
    NEAR_MISS_THRESHOLD,
    RETIRED_SPELLINGS,
    VocabularyCounts,
    reset_counts,
    snapshot_counts,
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


# ---------------------------------------------------------------------------
# FRE-1178: production behaviour — never drop, publish violations against a
# validated denominator (ADR-0133 D4)
# ---------------------------------------------------------------------------


def _production_settings() -> MagicMock:
    mock = MagicMock()
    mock.environment = Environment.PRODUCTION
    return mock


def test_ac2_denominator_and_numerator_match_a_mixed_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-2: N records emitted, M violating — validated == N, violations == M.

    M is strictly between 0 and N (2 of 5), which is what distinguishes the
    two counters — N == M or M == 0 would not.
    """
    monkeypatch.setattr(vocabulary, "settings", _production_settings())
    reset_counts()

    docs = [
        {"queue_depth": 1},  # clean
        {"duration_ms": 1},  # violation: retired spelling
        {"session_id": "s1"},  # clean
        {"input_tokens": "not-an-int"},  # violation: declared type
        {"component": "es_handler"},  # clean (Rule 2 exception)
    ]
    for doc in docs:
        validate_document(doc)  # production mode: never raises

    assert snapshot_counts() == VocabularyCounts(validated=5, violations=2)


def test_ac3_a_rule_evaluation_failure_increments_neither_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3: rule evaluation failing internally counts neither validated nor violations.

    Simulated by making ``_check_rules`` itself raise something that is not
    ``VocabularyViolationError`` — a bug in the validator, not a governed-
    vocabulary violation. The record's rules never ran, so it must not
    present as coverage either way.
    """
    reset_counts()

    def _broken_check_rules(doc: object) -> None:
        raise RuntimeError("rule evaluation blew up")

    monkeypatch.setattr(vocabulary, "_check_rules", _broken_check_rules)

    with pytest.raises(RuntimeError):
        validate_document({"session_id": "s1"})

    assert snapshot_counts() == VocabularyCounts(validated=0, violations=0)


def test_ac1_production_mode_never_raises_but_still_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-1 (validator level): a violation is counted, not raised, in production."""
    monkeypatch.setattr(vocabulary, "settings", _production_settings())
    reset_counts()

    validate_document({"duration_ms": 12})  # must not raise

    assert snapshot_counts() == VocabularyCounts(validated=1, violations=1)


def test_outside_production_a_violation_still_raises_after_counting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0133 D4: the development-time guarantee is unaffected by the new counters."""
    reset_counts()

    with pytest.raises(VocabularyViolationError):
        validate_document({"duration_ms": 12})

    assert snapshot_counts() == VocabularyCounts(validated=1, violations=1)
