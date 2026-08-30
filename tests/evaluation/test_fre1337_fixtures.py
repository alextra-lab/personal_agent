"""Fixture-set validity, and the AC-5 seeded-agreement fixture's ground truth.

The seeded-agreement fixture's ``expected_task_type`` must match what the deterministic
classifier *actually returns today* — this locks the claim in as a real assertion against
live code, not an aspirational label that could silently drift out of sync with
``intent.py``.
"""

from __future__ import annotations

from scripts.eval.fre1337_intent_probe.fixtures import load_fixtures

from personal_agent.request_gateway.intent import classify_intent


def test_at_least_four_fixtures_load() -> None:
    fixtures = load_fixtures()
    assert len(fixtures) >= 4


def test_fixture_labels_are_unique() -> None:
    fixtures = load_fixtures()
    labels = [f.label for f in fixtures]
    assert len(labels) == len(set(labels))


def test_gpsr_research_fixture_present() -> None:
    fixtures = load_fixtures()
    assert any(f.label == "gpsr_research" for f in fixtures)


def test_fixtures_with_an_expected_type_match_the_deterministic_classifier() -> None:
    """Locks in every seeded expectation against the real classifier, not just AC-5's."""
    fixtures = load_fixtures()
    checked = 0
    for fixture in fixtures:
        if fixture.expected_task_type is None:
            continue
        result = classify_intent(fixture.message)
        assert result.task_type.value == fixture.expected_task_type, (
            f"{fixture.label}: expected {fixture.expected_task_type}, got {result.task_type.value}"
        )
        checked += 1
    assert checked >= 1, "no fixture exercises the deterministic ground-truth assertion"


def test_seeded_agreement_fixture_is_conversational() -> None:
    """The specific AC-5 fixture: deterministic must call it conversational."""
    fixtures = load_fixtures()
    seeded = next(f for f in fixtures if f.label == "plain_greeting")
    assert seeded.expected_task_type == "conversational"
    assert classify_intent(seeded.message).task_type.value == "conversational"
