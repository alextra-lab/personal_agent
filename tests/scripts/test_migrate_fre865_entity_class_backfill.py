"""Unit tests for the FRE-865 entity-class backfill orchestration.

These exercise the migration ALGORITHM against an in-memory fake graph + a deterministic fake
BATCH classifier — no Neo4j, no LLM, so they run in ``make test`` as the CI-gating AC proof.
The real Cypher (:class:`_Neo4jGraph`) is exercised by
``test_migrate_fre865_entity_class_backfill_integration.py``.

Unlike FRE-772's Concept-type classifier (fail-CLOSED — an unparseable node stays untouched and is
retried), this backfill is fail-OPEN per ADR-0115 D4: every candidate resolves to an outcome this
run, defaulting to ``output_kind=knowledge, class=World`` on any parse/format/exception anomaly.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Sequence

import pytest
from scripts.migrate_fre865_entity_class_backfill import (
    _CLASSIFIER_PREFIX,
    BackfillReport,
    BatchClassifyResult,
    ClassifyResult,
    EntityCandidate,
    _build_batch_prompt,
    _parse_args,
    _parse_batch_classification,
    _print_summary,
    run_backfill,
    run_rollback,
)

from personal_agent.cost_gate import CostGate, get_default_gate_or_none, set_default_gate

_RUN_ID = "fre865-test"
_NOW = "2026-07-12T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeGraph:
    """In-memory graph seam over a list of node dicts; counts every mutation.

    A node dict may carry: ``name``, ``eid``, ``entity_type``, ``description``, ``class``
    (World/Personal/None), ``class_backfill_output_kind`` (marked-for-dispatch), ``last_seen``
    (for the rollback concurrency guard).
    """

    def __init__(self, nodes: list[dict[str, object]]) -> None:
        self.nodes = nodes
        self.writes = 0

    @staticmethod
    def _is_candidate(node: dict[str, object]) -> bool:
        return node.get("class") is None and node.get("class_backfill_output_kind") is None

    async def count_by_class(self) -> dict[str, int]:
        return dict(Counter(n.get("class") or "(unset)" for n in self.nodes))

    async def count_unclassified(self) -> int:
        return sum(1 for n in self.nodes if self._is_candidate(n))

    async def fetch_candidates(self, cursor: str | None, limit: int) -> list[EntityCandidate]:
        candidates = sorted(
            (n for n in self.nodes if self._is_candidate(n)), key=lambda n: n["eid"]
        )
        if cursor is not None:
            candidates = [n for n in candidates if n["eid"] > cursor]
        return [
            EntityCandidate(
                element_id=n["eid"],
                name=n["name"],
                entity_type=n.get("entity_type", ""),
                description=n.get("description", ""),
            )
            for n in candidates[:limit]
        ]

    async def set_class(
        self,
        element_id: str,
        class_value: str,
        *,
        fail_open: bool,
        run_id: str,
        now: str,
    ) -> None:
        for n in self.nodes:
            if n["eid"] == element_id:
                n["class"] = class_value
                n["class_backfill_run_id"] = run_id
                n["class_backfill_at"] = now
                if fail_open:
                    n["class_backfill_fail_open"] = True
                self.writes += 1

    async def mark_for_dispatch(
        self, element_id: str, output_kind: str, *, run_id: str, now: str
    ) -> None:
        for n in self.nodes:
            if n["eid"] == element_id:
                n["class_backfill_output_kind"] = output_kind
                n["class_backfill_run_id"] = run_id
                n["class_backfill_at"] = now
                self.writes += 1

    async def restore_by_run_id(self, run_id: str) -> tuple[int, list[str]]:
        restored = 0
        skipped: list[str] = []
        for n in self.nodes:
            if n.get("class_backfill_run_id") != run_id:
                continue
            backfilled_at = n.get("class_backfill_at")
            if (
                n.get("last_seen") is not None
                and backfilled_at is not None
                and n["last_seen"] > backfilled_at
            ):
                skipped.append(n["name"])
                continue
            for key in (
                "class",
                "class_backfill_run_id",
                "class_backfill_at",
                "class_backfill_output_kind",
                "class_backfill_fail_open",
            ):
                n.pop(key, None)
            restored += 1
            self.writes += 1
        return restored, skipped


def _batch_classifier(
    mapping: dict[str, tuple[str, str | None]],
    *,
    cost: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    raise_error: bool = False,
):
    """Fake BATCH classifier: name -> (output_kind, class|None) from ``mapping``.

    A name absent from ``mapping`` produces an unparseable/off-vocabulary result (fail-open).
    """

    async def classify(nodes: Sequence[EntityCandidate]) -> BatchClassifyResult:
        if raise_error:
            raise RuntimeError("simulated classifier outage")
        results = []
        for n in nodes:
            if n.name in mapping:
                output_kind, cls = mapping[n.name]
                results.append(
                    ClassifyResult(output_kind=output_kind, knowledge_class=cls, fail_open=False)
                )
            else:
                results.append(
                    ClassifyResult(
                        output_kind="knowledge",
                        knowledge_class="World",
                        fail_open=True,
                        reason="out_of_set",
                    )
                )
        return BatchClassifyResult(
            results=results,
            cost_usd=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        )

    return classify


async def _run(graph: FakeGraph, classifier, **kw):
    return await run_backfill(
        graph,
        classifier,
        run_id=_RUN_ID,
        now=_NOW,
        prompt_version="fre865-test-v1",
        classifier_model="fake",
        **kw,
    )


def _node(eid: str, name: str, **kw) -> dict[str, object]:
    return {
        "eid": eid,
        "name": name,
        "entity_type": "Unknown",
        "description": "",
        "class": None,
        **kw,
    }


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classifies_world_and_personal() -> None:
    graph = FakeGraph(
        [
            _node("1", "Neo4j", description="a graph database"),
            _node("2", "Dr. Chen", description="my cardiologist"),
        ]
    )
    report = await _run(
        graph,
        _batch_classifier({"Neo4j": ("knowledge", "World"), "Dr. Chen": ("knowledge", "Personal")}),
    )
    by_name = {n["name"]: n["class"] for n in graph.nodes}
    assert by_name == {"Neo4j": "World", "Dr. Chen": "Personal"}
    assert report.classified_world == 1
    assert report.classified_personal == 1
    assert report.fail_open_count == 0
    assert report.success is True


@pytest.mark.asyncio
async def test_system_natured_marked_not_classed() -> None:
    graph = FakeGraph([_node("1", "Postgres", description="the agent's own database healthcheck")])
    report = await _run(graph, _batch_classifier({"Postgres": ("finding", None)}))
    node = graph.nodes[0]
    assert node["class"] is None  # never classed
    assert node["class_backfill_output_kind"] == "finding"
    assert report.marked_for_dispatch == {"finding": 1}
    assert report.classified_world == 0
    assert report.classified_personal == 0


@pytest.mark.asyncio
async def test_ephemeral_marked_not_classed() -> None:
    graph = FakeGraph([_node("1", "test-scaffold-node", description="throwaway test fixture")])
    await _run(graph, _batch_classifier({"test-scaffold-node": ("ephemeral", None)}))
    node = graph.nodes[0]
    assert node["class"] is None
    assert node["class_backfill_output_kind"] == "ephemeral"


@pytest.mark.asyncio
async def test_fail_open_on_unparseable_response() -> None:
    """An off-vocabulary/unmapped classification defaults to knowledge/World, never left unresolved."""
    graph = FakeGraph([_node("1", "mystery-thing", description="ambiguous")])
    report = await _run(graph, _batch_classifier({}))  # nothing mapped -> fail-open
    node = graph.nodes[0]
    assert node["class"] == "World"
    assert node["class_backfill_fail_open"] is True
    assert report.fail_open_count == 1
    assert report.classified_world == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_rerun_excludes_classified_and_marked_nodes() -> None:
    graph = FakeGraph(
        [
            _node("1", "Neo4j", description="a graph database"),
            _node("2", "Postgres", description="healthcheck"),
        ]
    )
    classifier = _batch_classifier({"Neo4j": ("knowledge", "World"), "Postgres": ("finding", None)})
    first = await _run(graph, classifier)
    assert first.total_candidates_this_run == 2
    writes_after_first = graph.writes

    second = await _run(graph, classifier)
    assert graph.writes == writes_after_first  # no new writes
    assert second.total_candidates_this_run == 0
    assert second.model_calls == 0


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_but_previews() -> None:
    graph = FakeGraph([_node("1", "Neo4j", description="a graph database")])
    before = await graph.count_by_class()
    report = await _run(graph, _batch_classifier({"Neo4j": ("knowledge", "World")}), dry_run=True)
    assert graph.writes == 0
    assert await graph.count_by_class() == before
    assert report.classified_world == 1  # previewed, not written
    assert graph.nodes[0]["class"] is None


@pytest.mark.asyncio
async def test_dry_run_success_reflects_fail_open_valve_not_forced_false() -> None:
    """A healthy dry-run preview must report success=True — that's the whole point of previewing."""
    graph = FakeGraph([_node(f"{i:02d}", f"e{i}", description="") for i in range(20)])
    mapping = {f"e{i}": ("knowledge", "World") for i in range(20)}  # 0% fail-open
    report = await _run(
        graph,
        _batch_classifier(mapping),
        dry_run=True,
        fail_open_threshold=0.5,
        fail_open_min_sample=20,
    )
    assert report.success is True


@pytest.mark.asyncio
async def test_dry_run_unhealthy_valve_still_flags_unsuccessful() -> None:
    """An unhealthy dry-run preview (classifier looks broken) must still report success=False."""
    graph = FakeGraph([_node(f"{i:02d}", f"e{i}", description="") for i in range(25)])
    report = await _run(
        graph,
        _batch_classifier({}),  # nothing mapped -> 100% fail-open
        dry_run=True,
        fail_open_threshold=0.5,
        fail_open_min_sample=20,
    )
    assert report.success is False
    assert graph.writes == 0  # still a true preview — no writes despite the unhealthy signal


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batching_reduces_calls() -> None:
    graph = FakeGraph([_node(f"{i:02d}", f"e{i}", description="") for i in range(10)])
    report = await _run(
        graph,
        _batch_classifier({f"e{i}": ("knowledge", "World") for i in range(10)}),
        classify_batch_size=5,
    )
    assert report.total_candidates_this_run == 10
    assert report.batch_count == 2
    assert report.model_calls == 2
    assert report.batch_count < report.total_candidates_this_run


@pytest.mark.asyncio
async def test_report_aggregates_batch_metrics() -> None:
    graph = FakeGraph([_node(f"{i:02d}", f"e{i}", description="") for i in range(3)])
    report = await _run(
        graph,
        _batch_classifier(
            {f"e{i}": ("knowledge", "World") for i in range(3)},
            cost=0.01,
            input_tokens=100,
            output_tokens=20,
            cached_tokens=80,
        ),
        classify_batch_size=2,
    )
    assert report.model_calls == 2
    assert report.cost_usd == pytest.approx(0.02)
    assert report.input_tokens == 200
    assert report.output_tokens == 40
    assert report.cached_tokens == 160
    assert report.prompt_version == "fre865-test-v1"
    assert report.classifier_model == "fake"


# ---------------------------------------------------------------------------
# Parser anomalies (mirroring FRE-772's dedicated parser tests) — fail-open, never unresolved
# ---------------------------------------------------------------------------


def test_parse_batch_classification_numbered() -> None:
    content = "1. knowledge | World\n2. knowledge | Personal\n3. finding"
    results = _parse_batch_classification(content, 3)
    assert results[0] == ClassifyResult(
        output_kind="knowledge", knowledge_class="World", fail_open=False
    )
    assert results[1] == ClassifyResult(
        output_kind="knowledge", knowledge_class="Personal", fail_open=False
    )
    assert results[2] == ClassifyResult(
        output_kind="finding", knowledge_class=None, fail_open=False
    )


def test_parse_batch_duplicate_index_fails_open() -> None:
    content = "1. knowledge | World\n1. knowledge | Personal\n2. finding"
    results = _parse_batch_classification(content, 2)
    assert results[0].fail_open is True
    assert results[0].output_kind == "knowledge"
    assert results[0].knowledge_class == "World"
    assert results[0].reason == "ambiguous_index"
    assert results[1] == ClassifyResult(
        output_kind="finding", knowledge_class=None, fail_open=False
    )


def test_parse_batch_missing_index_fails_open() -> None:
    content = "2. finding"
    results = _parse_batch_classification(content, 3)
    assert results[0].fail_open is True
    assert results[0].reason == "missing"
    assert results[1] == ClassifyResult(
        output_kind="finding", knowledge_class=None, fail_open=False
    )
    assert results[2].fail_open is True
    assert results[2].reason == "missing"


def test_parse_batch_off_vocabulary_output_kind_fails_open() -> None:
    content = "1. nonsense_kind | World"
    results = _parse_batch_classification(content, 1)
    assert results[0].fail_open is True
    assert results[0].output_kind == "knowledge"
    assert results[0].knowledge_class == "World"
    assert results[0].reason == "out_of_set"


def test_parse_batch_off_vocabulary_class_fails_open() -> None:
    content = "1. knowledge | Alien"
    results = _parse_batch_classification(content, 1)
    assert results[0].fail_open is True
    assert results[0].output_kind == "knowledge"
    assert results[0].knowledge_class == "World"
    assert results[0].reason == "out_of_set"


def test_parse_batch_whole_batch_unnumbered_all_fail_open() -> None:
    content = "knowledge | World\nfinding"
    results = _parse_batch_classification(content, 2)
    assert all(r.fail_open for r in results)
    assert all(r.output_kind == "knowledge" and r.knowledge_class == "World" for r in results)
    assert all(r.reason == "missing" for r in results)


def test_batch_prompt_prefix_is_stable() -> None:
    a = _build_batch_prompt([EntityCandidate("1", "SENTINEL_ALPHA", "Unknown", "zzq_desc_a")])
    b = _build_batch_prompt(
        [
            EntityCandidate("9", "SENTINEL_BETA", "Unknown", "zzq_desc_b"),
            EntityCandidate("8", "SENTINEL_GAMMA", "Unknown", "q"),
        ]
    )
    assert a.startswith(_CLASSIFIER_PREFIX)
    assert b.startswith(_CLASSIFIER_PREFIX)
    assert a[: len(_CLASSIFIER_PREFIX)] == b[: len(_CLASSIFIER_PREFIX)] == _CLASSIFIER_PREFIX
    assert "SENTINEL_ALPHA" not in a[: len(_CLASSIFIER_PREFIX)]
    assert "SENTINEL_BETA" not in b[: len(_CLASSIFIER_PREFIX)]


# ---------------------------------------------------------------------------
# Fail-open safety valve (report-and-flag, not abort — ADR-0115 D4 wins over early-stop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_open_ratio_above_threshold_flags_unsuccessful() -> None:
    graph = FakeGraph([_node(f"{i:02d}", f"e{i}", description="") for i in range(30)])
    # nothing mapped -> every candidate fails open
    report = await _run(
        graph, _batch_classifier({}), fail_open_threshold=0.5, fail_open_min_sample=20
    )
    assert report.total_candidates_this_run == 30
    assert report.fail_open_count == 30
    assert report.success is False
    # every candidate still got a real outcome — D4 preserved, nothing dropped
    assert all(n["class"] == "World" for n in graph.nodes)


@pytest.mark.asyncio
async def test_fail_open_ratio_below_sample_floor_still_succeeds() -> None:
    graph = FakeGraph([_node(f"{i:02d}", f"e{i}", description="") for i in range(5)])
    report = await _run(
        graph, _batch_classifier({}), fail_open_threshold=0.5, fail_open_min_sample=20
    )
    assert report.fail_open_count == 5
    assert report.success is True  # sample too small to trigger the valve


@pytest.mark.asyncio
async def test_fail_open_ratio_below_threshold_succeeds() -> None:
    graph = FakeGraph([_node(f"{i:02d}", f"e{i}", description="") for i in range(20)])
    mapping = {f"e{i}": ("knowledge", "World") for i in range(18)}  # 2/20 fail open = 10%
    report = await _run(
        graph, _batch_classifier(mapping), fail_open_threshold=0.5, fail_open_min_sample=20
    )
    assert report.fail_open_count == 2
    assert report.success is True


@pytest.mark.asyncio
async def test_whole_batch_exception_fails_open_and_counts_toward_valve() -> None:
    graph = FakeGraph([_node(f"{i:02d}", f"e{i}", description="") for i in range(25)])
    report = await _run(
        graph,
        _batch_classifier({}, raise_error=True),
        classify_batch_size=25,
        fail_open_threshold=0.5,
        fail_open_min_sample=20,
    )
    assert report.fail_open_count == 25
    assert report.success is False
    assert all(n["class"] == "World" for n in graph.nodes)  # still resolved, never dropped


def test_print_summary_warns_on_unsuccessful_dry_run(capsys) -> None:
    """The WARNING must print for an unsuccessful run even when it was a dry run."""
    report = BackfillReport(
        run_id="x",
        dry_run=True,
        prompt_version="v",
        classifier_model="m",
        started_at="t",
        success=False,
    )
    _print_summary(report)
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "preview" in out.lower()


# ---------------------------------------------------------------------------
# Pluggable-classifier validation — a BatchClassifier is a Protocol; an out-of-enum
# output_kind/class from ANY implementation must fall open, never write verbatim (correctness,
# not just the LLM-backed classifier's own _parse_one_line validation).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_output_kind_from_classifier_falls_open() -> None:
    graph = FakeGraph([_node("1", "weird", description="")])

    async def classifier(nodes):
        return BatchClassifyResult(
            results=[
                ClassifyResult(output_kind="not_a_real_kind", knowledge_class=None, fail_open=False)
                for _ in nodes
            ]
        )

    report = await _run(graph, classifier)
    assert graph.nodes[0]["class"] == "World"
    assert report.fail_open_count == 1
    assert report.classified_world == 1


@pytest.mark.asyncio
async def test_invalid_knowledge_class_from_classifier_falls_open() -> None:
    graph = FakeGraph([_node("1", "weird", description="")])

    async def classifier(nodes):
        return BatchClassifyResult(
            results=[
                ClassifyResult(output_kind="knowledge", knowledge_class="Alien", fail_open=False)
                for _ in nodes
            ]
        )

    report = await _run(graph, classifier)
    assert graph.nodes[0]["class"] == "World"
    assert report.fail_open_count == 1  # exactly one, not double-counted


@pytest.mark.asyncio
async def test_invalid_output_kind_and_class_together_counts_fail_open_once() -> None:
    graph = FakeGraph([_node("1", "weird", description="")])

    async def classifier(nodes):
        return BatchClassifyResult(
            results=[
                ClassifyResult(output_kind="bogus", knowledge_class="Alien", fail_open=False)
                for _ in nodes
            ]
        )

    report = await _run(graph, classifier)
    assert graph.nodes[0]["class"] == "World"
    assert report.fail_open_count == 1  # two stacked issues, still counted once per candidate
    assert report.total_candidates_this_run == 1


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_restores_class_and_markers() -> None:
    graph = FakeGraph(
        [
            _node("1", "Neo4j", description="a graph database"),
            _node("2", "Postgres", description="healthcheck"),
        ]
    )
    await _run(
        graph, _batch_classifier({"Neo4j": ("knowledge", "World"), "Postgres": ("finding", None)})
    )
    restored, skipped = await run_rollback(graph, _RUN_ID)
    assert restored == 2
    assert skipped == []
    assert all(n.get("class") is None for n in graph.nodes)
    assert all(n.get("class_backfill_output_kind") is None for n in graph.nodes)
    assert all(n.get("class_backfill_run_id") is None for n in graph.nodes)


@pytest.mark.asyncio
async def test_rollback_skips_node_mutated_by_concurrent_live_traffic() -> None:
    graph = FakeGraph([_node("1", "Neo4j", description="a graph database")])
    await _run(graph, _batch_classifier({"Neo4j": ("knowledge", "World")}))
    # Simulate live extraction touching this node after the backfill wrote it.
    graph.nodes[0]["last_seen"] = "2026-07-13T00:00:00+00:00"  # after _NOW
    restored, skipped = await run_rollback(graph, _RUN_ID)
    assert restored == 0
    assert skipped == ["Neo4j"]
    assert graph.nodes[0]["class"] == "World"  # left untouched, not clobbered


# ---------------------------------------------------------------------------
# Parsers / CLI
# ---------------------------------------------------------------------------


def test_cli_parses_backfill_flags(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "--fail-open-threshold", "0.3", "--fail-open-min-sample", "10", "--dry-run"],
    )
    args = _parse_args()
    assert args.fail_open_threshold == 0.3
    assert args.fail_open_min_sample == 10
    assert args.dry_run is True


def test_cli_flags_default(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["prog"])
    args = _parse_args()
    assert args.fail_open_threshold == pytest.approx(0.5)
    assert args.fail_open_min_sample == 20
    assert args.dry_run is False
    assert args.confirm_prod is False


# ---------------------------------------------------------------------------
# Cost-gate registration (FRE-800 regression class, mirrored from FRE-772)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_cost_gate_connects_and_registers_default_gate(monkeypatch) -> None:
    from scripts.migrate_fre865_entity_class_backfill import _setup_cost_gate

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
