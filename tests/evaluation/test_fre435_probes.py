"""FRE-488 — probe-set schema + loader tests.

These tests are pure (no substrate, no LLM). They cover the YAML schema, the
``relevant_ids`` namespacing, the non-degenerate (anti-vacuous-green) guard, and
the LongMemEval stub.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.eval.fre435_memory_recall.probes import (
    ProbeCase,
    ProbeSetError,
    load_longmemeval,
    load_probe_set,
    parse_probe_cases,
)

SEED_PATH = Path("scripts/eval/fre435_memory_recall/seed_probe.yaml")


# ---------------------------------------------------------------------------
# Schema / namespacing
# ---------------------------------------------------------------------------


def test_relevant_ids_are_namespaced_and_normalised() -> None:
    """Relevant ids are namespaced and normalised."""
    case = parse_probe_cases(
        [
            {
                "case_id": "c1",
                "query": "q",
                "expected": {"entity_names": ["Diffraction Limit", "  neo4j "]},
            }
        ]
    )[0]
    assert case.relevant_ids == frozenset({"entity:diffraction limit", "entity:neo4j"})


def test_expected_defaults_must_not_deny_true() -> None:
    """Expected defaults must not deny true."""
    # Second case carries a relevant set so the non-degenerate guard is satisfied;
    # assert defaults on the first (label-less) case.
    case = parse_probe_cases(
        [
            {"case_id": "c1", "query": "q"},
            {"case_id": "c2", "query": "q2", "expected": {"entity_names": ["X"]}},
        ]
    )[0]
    assert case.expected.must_not_deny is True
    assert case.relevant_ids == frozenset()


def test_seed_entities_and_relationships_parse() -> None:
    """Seed entities and relationships parse."""
    case = parse_probe_cases(
        [
            {
                "case_id": "c1",
                "query": "q",
                "seed_entities": [
                    {"name": "Diffraction Limit", "entity_type": "concept", "description": "d"}
                ],
                "seed_relationships": [
                    {"source": "Diffraction Limit", "rel_type": "RELATES_TO", "target": "Optics"}
                ],
                "expected": {"entity_names": ["Diffraction Limit"]},
            }
        ]
    )[0]
    assert case.seed_entities[0].name == "Diffraction Limit"
    assert case.seed_entities[0].entity_type == "concept"
    assert case.seed_relationships[0].target == "Optics"


def test_history_turns_parse_for_extract_mode() -> None:
    """History turns parse for extract mode."""
    case = parse_probe_cases(
        [
            {
                "case_id": "c1",
                "query": "q",
                "history": [{"user": "u", "assistant": "a"}],
                "expected": {"entity_names": ["X"]},
            }
        ]
    )[0]
    assert case.history[0].user == "u"
    assert case.history[0].assistant == "a"


def test_missing_required_field_raises() -> None:
    """Missing required field raises."""
    with pytest.raises(ProbeSetError):
        parse_probe_cases([{"query": "no case_id"}])


# ---------------------------------------------------------------------------
# Non-degenerate (anti-vacuous-green) guard — codex Q2
# ---------------------------------------------------------------------------


def test_all_empty_relevant_is_rejected() -> None:
    """All empty relevant is rejected."""
    with pytest.raises(ProbeSetError, match="degenerate"):
        parse_probe_cases(
            [
                {"case_id": "c1", "query": "q"},
                {"case_id": "c2", "query": "q2"},
            ]
        )


def test_at_least_one_relevant_passes() -> None:
    """At least one relevant passes."""
    cases = parse_probe_cases(
        [
            {"case_id": "c1", "query": "q"},
            {"case_id": "c2", "query": "q2", "expected": {"entity_names": ["X"]}},
        ]
    )
    assert len(cases) == 2


# ---------------------------------------------------------------------------
# Seed file on disk
# ---------------------------------------------------------------------------


def test_seed_probe_yaml_loads_and_is_non_degenerate() -> None:
    """Seed probe yaml loads and is non degenerate."""
    cases = load_probe_set(SEED_PATH)
    assert len(cases) >= 2
    assert all(isinstance(c, ProbeCase) for c in cases)
    # At least one case with a non-empty relevant set (the gate metric needs it).
    assert any(c.relevant_ids for c in cases)
    # At least one case carrying an expected write (replay seed) — write-completeness.
    assert any(c.seed_entities for c in cases)
    # Tags the ADR/gate require are represented.
    all_tags = {t for c in cases for t in c.tags}
    assert "false-negative" in all_tags
    assert "pedagogical" in all_tags


# ---------------------------------------------------------------------------
# LongMemEval adapter stub — FRE-490
# ---------------------------------------------------------------------------


def test_longmemeval_is_stub() -> None:
    """Longmemeval is stub."""
    with pytest.raises(NotImplementedError, match="FRE-490"):
        load_longmemeval(Path("anywhere.json"))
