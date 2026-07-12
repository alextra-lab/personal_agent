"""Unit tests for the FRE-772 entity-type migration orchestration.

These exercise the migration ALGORITHM against an in-memory fake graph + a deterministic fake
BATCH classifier — no Neo4j, no LLM, so they run in ``make test`` as the CI-gating AC proof.
The real Cypher (:class:`_Neo4jGraph`) is exercised by ``test_migrate_fre772_integration.py``.

FRE-801 batched the Concept classifier (25-50 entities/call, cache-stable prefix) and added an
optional System-class exclusion; the fakes and tests here reflect the batch contract.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from unittest.mock import patch

import pytest
from scripts.migrate_fre772_entity_type_v2 import (
    _CLASSIFIER_PREFIX,
    BatchClassifyResult,
    ClassifyResult,
    ConceptNode,
    MigrationReport,
    _build_batch_prompt,
    _build_llm_batch_classifier,
    _map_speaks_v2,
    _parse_args,
    _parse_batch_classification,
    _parse_classification,
    _print_summary,
    run_migration,
    run_rollback,
)

from personal_agent.cost_gate import CostGate, get_default_gate_or_none, set_default_gate

_RUN_ID = "fre772-test"
_NOW = "2026-07-05T00:00:00+00:00"

_KNOWN_CLASSES = {"World", "Personal", "System"}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeGraph:
    """In-memory :class:`GraphProtocol` over a list of node dicts; counts every mutation.

    Nodes may carry a ``class`` key (World/Personal/System); absent = unpopulated. The
    ``exclude_system`` filter mirrors the real Cypher ``coalesce(e.class,'') <> 'System'``:
    only literal ``System`` is skipped; null/absent class is included.
    """

    def __init__(self, nodes: list[dict[str, str]]) -> None:
        self.nodes = nodes
        self.writes = 0

    @staticmethod
    def _kept(node: dict[str, str], exclude_system: bool) -> bool:
        return not (exclude_system and node.get("class", "") == "System")

    async def count_by_type(self, *, exclude_system: bool = False) -> dict[str, int]:
        return dict(
            Counter(n.get("entity_type", "") for n in self.nodes if self._kept(n, exclude_system))
        )

    async def count_by_class(self) -> dict[str, int]:
        return dict(Counter(n.get("class") or "(unset)" for n in self.nodes))

    async def snapshot(self) -> list[dict[str, str]]:
        return [
            {"name": n["name"], "entity_type": n.get("entity_type", "")}
            for n in self.nodes
            if n.get("name")
        ]

    async def remap_deterministic(
        self, v1: str, v2: str, *, run_id: str, now: str, batch: int, exclude_system: bool = False
    ) -> int:
        count = 0
        for n in self.nodes:
            if n.get("entity_type") == v1 and self._kept(n, exclude_system):
                n["entity_type"] = v2
                n["entity_type_migration"] = run_id
                n["entity_type_migrated_at"] = now
                self.writes += 1
                count += 1
        return count

    async def fetch_concepts(
        self, cursor: str | None, limit: int, *, exclude_system: bool = False
    ) -> list[ConceptNode]:
        concepts = sorted(
            (
                n
                for n in self.nodes
                if n.get("entity_type") == "Concept" and self._kept(n, exclude_system)
            ),
            key=lambda n: n["eid"],
        )
        if cursor is not None:
            concepts = [n for n in concepts if n["eid"] > cursor]
        return [
            ConceptNode(element_id=n["eid"], name=n["name"], description=n.get("description", ""))
            for n in concepts[:limit]
        ]

    async def set_entity_type(self, element_id: str, v2: str, *, run_id: str, now: str) -> None:
        for n in self.nodes:
            if n["eid"] == element_id:
                n["entity_type"] = v2
                n["entity_type_migration"] = run_id
                n["entity_type_migrated_at"] = now
                n.pop("entity_type_migration_error", None)
                self.writes += 1

    async def mark_error(self, element_id: str, reason: str, *, now: str) -> None:
        for n in self.nodes:
            if n["eid"] == element_id:
                n["entity_type_migration_error"] = reason
                self.writes += 1

    async def restore_types(self, rows: Sequence[dict[str, str]], *, batch: int) -> int:
        by_name = {r["name"]: r["entity_type"] for r in rows}
        count = 0
        for n in self.nodes:
            if n.get("name") in by_name:
                n["entity_type"] = by_name[n["name"]]
                for key in (
                    "entity_type_migration",
                    "entity_type_migrated_at",
                    "entity_type_migration_error",
                ):
                    n.pop(key, None)
                self.writes += 1
                count += 1
        return count


def _batch_classifier(
    mapping: dict[str, str],
    *,
    cost: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
):
    """Return a fake BATCH classifier: name→type from ``mapping``, else fail-closed (None).

    ``cost``/``*_tokens`` are per-batch-call metrics (like the real classifier) so aggregation
    tests can multiply by the batch count.
    """

    async def classify(nodes: Sequence[ConceptNode]) -> BatchClassifyResult:
        results = [
            ClassifyResult(entity_type=mapping[n.name])
            if n.name in mapping
            else ClassifyResult(entity_type=None, reason="out_of_set")
            for n in nodes
        ]
        return BatchClassifyResult(
            results=results,
            cost_usd=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        )

    return classify


async def _run(graph: FakeGraph, classifier, **kw):
    return await run_migration(
        graph,
        classifier,
        run_id=_RUN_ID,
        now=_NOW,
        classifier_model="fake",
        **kw,
    )


# ---------------------------------------------------------------------------
# Existing invariants (AC4) — adapted to the batch fake, assertions unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deterministic_remap_and_unchanged_types() -> None:
    graph = FakeGraph(
        [
            {"eid": "1", "name": "Python", "entity_type": "Technology"},
            {"eid": "2", "name": "cosmology", "entity_type": "Topic"},
            {"eid": "3", "name": "Ada Lovelace", "entity_type": "Person"},
            {"eid": "4", "name": "the Big Bang", "entity_type": "Event"},
        ]
    )
    report = await _run(graph, _batch_classifier({}))

    types = {n["name"]: n["entity_type"] for n in graph.nodes}
    assert types == {
        "Python": "TechnicalArtifact",
        "cosmology": "DomainOrTopic",
        "Ada Lovelace": "Person",
        "the Big Bang": "Event",
    }
    marked = {n["name"] for n in graph.nodes if "entity_type_migration" in n}
    assert marked == {"Python", "cosmology"}
    assert report.deterministic == {"Technology->TechnicalArtifact": 1, "Topic->DomainOrTopic": 1}
    assert report.v1_remnants_after == {}
    assert report.success is True


@pytest.mark.asyncio
async def test_concept_happy_path_classifies_into_conceptual_family() -> None:
    graph = FakeGraph(
        [
            {
                "eid": "1",
                "name": "trie",
                "entity_type": "Concept",
                "description": "a data structure",
            },
            {
                "eid": "2",
                "name": "wavelength",
                "entity_type": "Concept",
                "description": "a property",
            },
            {
                "eid": "3",
                "name": "the GoLLIE paper",
                "entity_type": "Concept",
                "description": "a paper",
            },
        ]
    )
    report = await _run(
        graph,
        _batch_classifier(
            {
                "trie": "MethodOrConcept",
                "wavelength": "QuantityMeasure",
                "the GoLLIE paper": "KnowledgeArtifact",
            }
        ),
    )
    types = {n["name"]: n["entity_type"] for n in graph.nodes}
    assert types == {
        "trie": "MethodOrConcept",
        "wavelength": "QuantityMeasure",
        "the GoLLIE paper": "KnowledgeArtifact",
    }
    assert report.concept_total == 3
    assert report.concept_classified == {
        "MethodOrConcept": 1,
        "QuantityMeasure": 1,
        "KnowledgeArtifact": 1,
    }
    assert report.concept_unclassified == []
    assert report.success is True


@pytest.mark.asyncio
async def test_concept_fail_closed_leaves_concept_and_blocks_success() -> None:
    graph = FakeGraph(
        [
            {"eid": "1", "name": "trie", "entity_type": "Concept", "description": ""},
            {"eid": "2", "name": "spacetime", "entity_type": "Concept", "description": ""},
        ]
    )
    report = await _run(graph, _batch_classifier({"trie": "MethodOrConcept"}))

    by_name = {n["name"]: n for n in graph.nodes}
    assert by_name["trie"]["entity_type"] == "MethodOrConcept"
    assert by_name["spacetime"]["entity_type"] == "Concept"  # left, never guessed
    assert by_name["spacetime"]["entity_type_migration_error"] == "out_of_set"
    assert [u["name"] for u in report.concept_unclassified] == ["spacetime"]
    assert report.v1_remnants_after == {"Concept": 1}
    assert report.success is False


@pytest.mark.asyncio
async def test_rerun_is_idempotent_noop() -> None:
    graph = FakeGraph(
        [
            {"eid": "1", "name": "Python", "entity_type": "Technology"},
            {"eid": "2", "name": "trie", "entity_type": "Concept", "description": ""},
        ]
    )
    classifier = _batch_classifier({"trie": "MethodOrConcept"})
    first = await _run(graph, classifier)
    assert first.success is True
    writes_after_first = graph.writes

    second = await _run(graph, classifier)
    assert graph.writes == writes_after_first
    assert second.deterministic == {"Technology->TechnicalArtifact": 0, "Topic->DomainOrTopic": 0}
    assert second.concept_total == 0
    assert second.success is True


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_but_previews() -> None:
    graph = FakeGraph(
        [
            {"eid": "1", "name": "Python", "entity_type": "Technology"},
            {"eid": "2", "name": "trie", "entity_type": "Concept", "description": ""},
        ]
    )
    before = await graph.count_by_type()
    report = await _run(graph, _batch_classifier({"trie": "MethodOrConcept"}), dry_run=True)

    assert graph.writes == 0  # the hard dry-run contract
    assert await graph.count_by_type() == before
    assert report.deterministic == {"Technology->TechnicalArtifact": 1, "Topic->DomainOrTopic": 0}
    assert report.concept_total == 1
    assert report.concept_classified == {"MethodOrConcept": 1}
    assert report.success is False  # a dry run is never a completed migration


@pytest.mark.asyncio
async def test_class_property_is_never_touched() -> None:
    graph = FakeGraph(
        [{"eid": "1", "name": "Python", "entity_type": "Technology", "class": "World"}]
    )
    await _run(graph, _batch_classifier({}))
    assert graph.nodes[0]["class"] == "World"


@pytest.mark.asyncio
async def test_concept_paging_terminates_past_fail_closed_node() -> None:
    graph = FakeGraph(
        [
            {"eid": "a", "name": "trie", "entity_type": "Concept", "description": ""},
            {"eid": "b", "name": "spacetime", "entity_type": "Concept", "description": ""},
            {"eid": "c", "name": "game theory", "entity_type": "Concept", "description": ""},
        ]
    )
    report = await _run(
        graph,
        _batch_classifier({"trie": "MethodOrConcept", "game theory": "DomainOrTopic"}),
        batch_size=1,
    )
    assert report.concept_total == 3
    assert len(report.concept_unclassified) == 1
    assert report.v1_remnants_after == {"Concept": 1}


@pytest.mark.asyncio
async def test_rollback_restores_types_and_strips_markers(tmp_path) -> None:
    snap = tmp_path / "snap.json"
    graph = FakeGraph(
        [
            {"eid": "1", "name": "Python", "entity_type": "Technology"},
            {"eid": "2", "name": "trie", "entity_type": "Concept", "description": ""},
        ]
    )
    await _run(graph, _batch_classifier({"trie": "MethodOrConcept"}), snapshot_path=snap)
    assert {n["name"]: n["entity_type"] for n in graph.nodes} == {
        "Python": "TechnicalArtifact",
        "trie": "MethodOrConcept",
    }

    restored = await run_rollback(graph, snap, batch_size=100)
    assert restored == 2
    assert {n["name"]: n["entity_type"] for n in graph.nodes} == {
        "Python": "Technology",
        "trie": "Concept",
    }
    assert all("entity_type_migration" not in n for n in graph.nodes)


# ---------------------------------------------------------------------------
# FRE-801 Part 1/2 — batching, cache-stable prefix, metrics (AC1, AC2)
# ---------------------------------------------------------------------------


def test_build_llm_batch_classifier_bills_entity_extraction_budget_role() -> None:
    """FRE-869: role is a resolved model key, not a factory role name —
    get_llm_client_for_key must be used (not get_llm_client) with an explicit
    budget_role="entity_extraction" so spend lands in that budget lane instead
    of being silently mis-billed to main_inference.
    """
    with (
        patch(
            "scripts.migrate_fre772_entity_type_v2.resolve_role_model_key",
            return_value="gpt-5.4-mini",
        ),
        patch("personal_agent.llm_client.factory.get_llm_client_for_key") as mock_get_client,
    ):
        _classify, role = _build_llm_batch_classifier()

        assert role == "gpt-5.4-mini"
        mock_get_client.assert_called_once_with("gpt-5.4-mini", budget_role="entity_extraction")


def test_batch_prompt_prefix_is_stable() -> None:
    """AC2: the static prefix is byte-identical regardless of the batch appended."""
    # Sentinel names guaranteed absent from the definition text, so a leak is unambiguous.
    a = _build_batch_prompt([ConceptNode("1", "SENTINEL_ALPHA", "zzq_desc_a")])
    b = _build_batch_prompt(
        [ConceptNode("9", "SENTINEL_BETA", "zzq_desc_b"), ConceptNode("8", "SENTINEL_GAMMA", "q")]
    )
    assert a.startswith(_CLASSIFIER_PREFIX)
    assert b.startswith(_CLASSIFIER_PREFIX)
    assert a[: len(_CLASSIFIER_PREFIX)] == b[: len(_CLASSIFIER_PREFIX)] == _CLASSIFIER_PREFIX
    # nothing variable leaked into the prefix region
    assert "SENTINEL_ALPHA" not in a[: len(_CLASSIFIER_PREFIX)]
    assert "SENTINEL_BETA" not in b[: len(_CLASSIFIER_PREFIX)]


def test_parse_batch_classification_numbered() -> None:
    content = "1. MethodOrConcept\n2. QuantityMeasure\n3. KnowledgeArtifact"
    assert _parse_batch_classification(content, 3) == [
        ("MethodOrConcept", ""),
        ("QuantityMeasure", ""),
        ("KnowledgeArtifact", ""),
    ]


def test_parse_batch_duplicate_index_fails_closed() -> None:
    # A duplicated index cannot be trusted for EITHER line → fail-closed, never guessed.
    content = "1. MethodOrConcept\n1. QuantityMeasure\n2. Phenomenon"
    res = _parse_batch_classification(content, 2)
    assert res[0] == (None, "ambiguous_index")
    assert res[1] == ("Phenomenon", "")


def test_parse_batch_missing_and_out_of_order() -> None:
    # index1 ambiguous line → out_of_set; index2 present but out of order → mapped; index3 absent → missing
    content = "2. Phenomenon\n1. could be MethodOrConcept or DomainOrTopic"
    res = _parse_batch_classification(content, 3)
    assert res[0] == (None, "out_of_set")
    assert res[1] == ("Phenomenon", "")
    assert res[2] == (None, "missing")


def test_parse_batch_whole_batch_unnumbered_all_fail_closed() -> None:
    # No numbered lines at all → every entity fails closed (safe; retried next run), never misassigned.
    content = "MethodOrConcept\nQuantityMeasure"
    res = _parse_batch_classification(content, 2)
    assert res == [(None, "missing"), (None, "missing")]


@pytest.mark.asyncio
async def test_partial_batch_failure_one_bad_entity() -> None:
    """One unparseable entity stays Concept; the rest classify; the batch never fails whole."""
    graph = FakeGraph(
        [
            {"eid": "1", "name": "trie", "entity_type": "Concept", "description": ""},
            {"eid": "2", "name": "spacetime", "entity_type": "Concept", "description": ""},
            {"eid": "3", "name": "game theory", "entity_type": "Concept", "description": ""},
        ]
    )
    report = await _run(
        graph,
        _batch_classifier({"trie": "MethodOrConcept", "game theory": "DomainOrTopic"}),
        classify_batch_size=10,  # all three in one batch call
    )
    assert report.model_calls == 1  # a single batch call handled all three
    by_name = {n["name"]: n["entity_type"] for n in graph.nodes}
    assert by_name["trie"] == "MethodOrConcept"
    assert by_name["game theory"] == "DomainOrTopic"
    assert by_name["spacetime"] == "Concept"
    assert [u["name"] for u in report.concept_unclassified] == ["spacetime"]


@pytest.mark.asyncio
async def test_batching_reduces_calls() -> None:
    """AC1: call count is materially fewer than one per node."""
    graph = FakeGraph(
        [
            {"eid": f"{i:02d}", "name": f"c{i}", "entity_type": "Concept", "description": ""}
            for i in range(10)
        ]
    )
    report = await _run(
        graph,
        _batch_classifier({f"c{i}": "MethodOrConcept" for i in range(10)}),
        classify_batch_size=5,
    )
    assert report.concept_total == 10
    assert report.batch_count == 2  # ceil(10 / 5)
    assert report.model_calls == 2
    assert report.batch_count < report.concept_total


@pytest.mark.asyncio
async def test_report_aggregates_batch_metrics() -> None:
    """AC1: cost + input/output/cached tokens are summed across batch calls."""
    graph = FakeGraph(
        [
            {"eid": f"{i:02d}", "name": f"c{i}", "entity_type": "Concept", "description": ""}
            for i in range(3)
        ]
    )
    report = await _run(
        graph,
        _batch_classifier(
            {f"c{i}": "MethodOrConcept" for i in range(3)},
            cost=0.01,
            input_tokens=100,
            output_tokens=20,
            cached_tokens=80,
        ),
        classify_batch_size=2,  # → 2 batch calls
    )
    assert report.model_calls == 2
    assert report.cost_usd == pytest.approx(0.02)
    assert report.input_tokens == 200
    assert report.output_tokens == 40
    assert report.cached_tokens == 160


# ---------------------------------------------------------------------------
# FRE-801 Part 3 — class breakdown + optional System exclusion (AC3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_class_histogram_in_report() -> None:
    graph = FakeGraph(
        [
            {"eid": "1", "name": "a", "entity_type": "Technology", "class": "World"},
            {"eid": "2", "name": "b", "entity_type": "Topic", "class": "System"},
            {"eid": "3", "name": "c", "entity_type": "Person"},  # unset
        ]
    )
    report = await _run(graph, _batch_classifier({}))
    assert report.class_histogram == {"World": 1, "System": 1, "(unset)": 1}
    assert report.class_populated is True


@pytest.mark.asyncio
async def test_exclude_system_skips_system_when_populated() -> None:
    graph = FakeGraph(
        [
            {"eid": "1", "name": "Python", "entity_type": "Technology", "class": "World"},
            {"eid": "2", "name": "Postgres", "entity_type": "Technology", "class": "System"},
            {
                "eid": "3",
                "name": "trie",
                "entity_type": "Concept",
                "class": "World",
                "description": "",
            },
            {
                "eid": "4",
                "name": "reaper",
                "entity_type": "Concept",
                "class": "System",
                "description": "",
            },
        ]
    )
    report = await _run(
        graph,
        _batch_classifier({"trie": "MethodOrConcept", "reaper": "MethodOrConcept"}),
        exclude_system=True,
    )
    by_name = {n["name"]: n["entity_type"] for n in graph.nodes}
    assert by_name["Python"] == "TechnicalArtifact"  # World remapped
    assert by_name["trie"] == "MethodOrConcept"  # World classified
    assert by_name["Postgres"] == "Technology"  # System untouched
    assert by_name["reaper"] == "Concept"  # System not classified
    assert report.exclude_system is True
    assert report.class_populated is True
    assert report.concept_total == 1  # only the non-System Concept was fetched


@pytest.mark.asyncio
async def test_exclude_system_is_noop_when_unpopulated() -> None:
    graph = FakeGraph(
        [
            {"eid": "1", "name": "Python", "entity_type": "Technology"},  # no class
            {"eid": "2", "name": "trie", "entity_type": "Concept", "description": ""},
        ]
    )
    report = await _run(graph, _batch_classifier({"trie": "MethodOrConcept"}), exclude_system=True)
    assert report.class_populated is False
    assert report.exclude_system is False  # effective flag forced off
    assert report.exclude_system_requested is True
    assert {n["name"]: n["entity_type"] for n in graph.nodes} == {
        "Python": "TechnicalArtifact",
        "trie": "MethodOrConcept",
    }


def test_summary_prints_noop_message(capsys) -> None:
    report = MigrationReport(
        run_id="x",
        dry_run=True,
        prompt_version="x",
        classifier_model="x",
        started_at="x",
        class_populated=False,
        exclude_system_requested=True,
        exclude_system=False,
        class_histogram={"(unset)": 5},
    )
    _print_summary(report)
    out = capsys.readouterr().out
    assert "no-op" in out.lower()
    assert "class" in out.lower()


# ---------------------------------------------------------------------------
# Parsers / CLI / preflight
# ---------------------------------------------------------------------------


def test_parse_classification_requires_unambiguous_single_hit() -> None:
    assert _parse_classification("MethodOrConcept") == "MethodOrConcept"
    assert _parse_classification("  QuantityMeasure\n") == "QuantityMeasure"
    assert _parse_classification("I think DomainOrTopic fits best") == "DomainOrTopic"
    assert _parse_classification("Person") is None  # not a conceptual target
    assert _parse_classification("could be MethodOrConcept or DomainOrTopic") is None  # ambiguous
    assert _parse_classification("") is None


def test_cli_parses_new_flags(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["prog", "--classify-batch-size", "25", "--exclude-system"])
    args = _parse_args()
    assert args.classify_batch_size == 25
    assert args.exclude_system is True


def test_cli_new_flags_default_off(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["prog"])
    args = _parse_args()
    assert args.exclude_system is False
    assert args.classify_batch_size > 1  # a sane batch default


def test_preflight_gate_logic_v1_refuses_v2_opens() -> None:
    v1_map = {"tool": "Technology", "topic": "Topic", "concept": "Concept", "person": "Person"}
    v2_map = {
        "tool": "TechnicalArtifact",
        "topic": "DomainOrTopic",
        "concept": ("MethodOrConcept", "DomainOrTopic", "Phenomenon"),
        "person": "Person",
    }
    assert _map_speaks_v2(v1_map) is False
    assert _map_speaks_v2(v2_map) is True


# ---------------------------------------------------------------------------
# FRE-800 cost-gate regression guards (adapted to the batch classifier builder)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_cost_gate_connects_and_registers_default_gate(monkeypatch) -> None:
    """FRE-800: _setup_cost_gate must connect the gate and register it via set_default_gate."""
    from scripts.migrate_fre772_entity_type_v2 import _setup_cost_gate

    set_default_gate(None)
    connected = False

    async def _fake_connect(self: CostGate) -> None:
        nonlocal connected
        connected = True

    monkeypatch.setattr(CostGate, "connect", _fake_connect)
    try:
        assert get_default_gate_or_none() is None
        gate = await _setup_cost_gate()
        assert connected is True
        assert get_default_gate_or_none() is gate
    finally:
        set_default_gate(None)


@pytest.mark.asyncio
async def test_amain_registers_cost_gate_before_building_classifier(monkeypatch) -> None:
    """FRE-800 regression guard, adapted to the batch classifier builder (FRE-801)."""
    import neo4j
    import scripts.migrate_fre772_entity_type_v2 as mod

    set_default_gate(None)

    class _FakeDriver:
        async def verify_connectivity(self) -> None:
            return None

        async def close(self) -> None:
            return None

    monkeypatch.setattr(neo4j.AsyncGraphDatabase, "driver", lambda *a, **kw: _FakeDriver())
    monkeypatch.setattr(mod, "_consumer_speaks_v2", lambda: True)

    async def _fake_connect(self: CostGate) -> None:
        return None

    async def _fake_reap_stale(self: CostGate) -> int:
        return 0

    monkeypatch.setattr(CostGate, "connect", _fake_connect)
    monkeypatch.setattr(CostGate, "reap_stale", _fake_reap_stale)

    gate_was_registered_at_classifier_build = False

    def _fake_build_llm_batch_classifier():
        nonlocal gate_was_registered_at_classifier_build
        gate_was_registered_at_classifier_build = get_default_gate_or_none() is not None

        async def _classify(nodes: Sequence[ConceptNode]) -> BatchClassifyResult:
            return BatchClassifyResult(results=[ClassifyResult(entity_type=None) for _ in nodes])

        return _classify, "fake-model"

    monkeypatch.setattr(mod, "_build_llm_batch_classifier", _fake_build_llm_batch_classifier)

    captured: dict[str, object] = {}

    async def _fake_run_migration(*args, **kwargs) -> MigrationReport:
        captured.update(kwargs)
        return MigrationReport(
            run_id="x",
            dry_run=True,
            prompt_version="x",
            classifier_model="x",
            started_at="x",
        )

    monkeypatch.setattr(mod, "run_migration", _fake_run_migration)

    args = argparse.Namespace(
        confirm_prod=True,
        dry_run=True,
        rollback=False,
        snapshot_path=None,
        report_path=None,
        batch_size=500,
        classify_batch_size=40,
        exclude_system=True,
        skip_consumer_check=False,
    )
    reap_called = False
    _orig_fake_reap_stale = _fake_reap_stale

    async def _counting_fake_reap_stale(self: CostGate) -> int:
        nonlocal reap_called
        reap_called = True
        return await _orig_fake_reap_stale(self)

    monkeypatch.setattr(CostGate, "reap_stale", _counting_fake_reap_stale)

    try:
        rc = await mod._amain(args)
        assert rc == 0
        assert gate_was_registered_at_classifier_build is True
        assert reap_called is True
        assert get_default_gate_or_none() is None
        # _amain threads the new CLI params into run_migration.
        assert captured["classify_batch_size"] == 40
        assert captured["exclude_system"] is True
    finally:
        set_default_gate(None)
