"""Tests for the ADR-0114 D7/D8 baseline harness (FRE-840).

Unit-level: fake settings objects and a stubbed ``MemoryServiceAdapter`` — no
real Neo4j. The one integration test connects to the real study sandbox and
is skipped (not failed) when it is unreachable, matching
``test_run_ingest_integration.py``'s established convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from scripts.study.baseline_harness import (
    MULTIPATH_FLOOR,
    STUDY_NEO4J_URI,
    StudyTargetMismatchError,
    connect_baseline_service,
    run_baseline_recall,
    set_prod_multipath_config,
)


class _FakeSettings:
    """A bare namespace mimicking the flag surface ``set_prod_multipath_config`` mutates."""

    def __init__(self) -> None:
        self.multipath_recall_enabled = False
        self.lexical_arm_enabled = False
        self.multiquery_arm_enabled = False
        self.recall_similarity_floor = 0.0
        self.relevance_bounded_recall_enabled = True  # deliberately "wrong" pre-state
        self.structural_arm_enabled = False


def test_set_prod_multipath_config_enables_the_named_arms() -> None:
    fake = _FakeSettings()
    set_prod_multipath_config(fake)
    assert fake.multipath_recall_enabled is True
    assert fake.lexical_arm_enabled is True
    assert fake.multiquery_arm_enabled is True
    assert fake.recall_similarity_floor == MULTIPATH_FLOOR


def test_set_prod_multipath_config_pins_relevance_bounded_recall_off() -> None:
    """Codex review: pin explicitly, don't rely on the field's own default."""
    fake = _FakeSettings()
    set_prod_multipath_config(fake)
    assert fake.relevance_bounded_recall_enabled is False


def test_set_prod_multipath_config_leaves_structural_arm_untouched() -> None:
    """The ADR's arm A is dense+lexical+multi-query only -- not structural."""
    fake = _FakeSettings()
    fake.structural_arm_enabled = True  # pre-existing value must survive untouched
    set_prod_multipath_config(fake)
    assert fake.structural_arm_enabled is True


@dataclass
class _FakeRecallResult:
    episodes: list[dict[str, Any]] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    relevance_scores: dict[str, float] = field(default_factory=dict)


class _FakeAdapter:
    def __init__(self, result: _FakeRecallResult) -> None:
        self._result = result
        self.last_query: Any = None
        self.last_trace_id: str | None = None

    async def recall(self, query: Any, trace_id: str) -> _FakeRecallResult:
        self.last_query = query
        self.last_trace_id = trace_id
        return self._result


@pytest.mark.asyncio
async def test_run_baseline_recall_flattens_the_adapter_result() -> None:
    result = _FakeRecallResult(
        entities=[{"name": "Arterial calcification"}, {"name": "Hypertension"}],
        relevance_scores={"Arterial calcification": 0.9, "Hypertension": 0.5},
    )
    adapter = _FakeAdapter(result)

    retrieved = await run_baseline_recall(adapter, "health issues", k=20, trace_id="t1")

    assert retrieved[0] == "entity:arterial calcification"
    assert "entity:hypertension" in retrieved
    assert adapter.last_query.query_text == "health issues"
    assert adapter.last_query.limit == 20
    assert adapter.last_trace_id == "t1"
    # Discovered running the real harness against the sandbox: the frozen
    # corpus's entities all carry visibility='group', which the FRE-229
    # visibility filter only admits for authenticated requests.
    assert adapter.last_query.authenticated is True


@pytest.mark.asyncio
async def test_connect_baseline_service_raises_on_study_target_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.study.baseline_harness as baseline_harness

    monkeypatch.setattr(baseline_harness.settings, "neo4j_uri", "bolt://localhost:9999")
    with pytest.raises(StudyTargetMismatchError):
        await connect_baseline_service()


def test_study_neo4j_uri_matches_study_bolt_port() -> None:
    assert STUDY_NEO4J_URI == "bolt://localhost:7691"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_connect_and_recall_against_the_real_study_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end proof of mechanism against the real frozen corpus.

    Skipped (not failed) when the sandbox is unreachable or the study
    credential is not configured, matching
    ``test_run_ingest_integration.py``'s established convention.
    Monkeypatches the ``personal_agent.config.settings`` singleton's
    ``neo4j_uri``/``neo4j_user``/``neo4j_password`` directly (reverted by
    monkeypatch after the test) rather than env vars, since the shared
    pytest process's singleton is already constructed pointing at the
    :7688 test stack by the root conftest -- this is the same pattern
    ``test_connect_baseline_service_raises_on_study_target_mismatch`` above
    uses in reverse.

    Codex review: ``connect_baseline_service`` also mutates the singleton's
    multipath recall flags (``set_prod_multipath_config``) with no
    restoration -- under ``make test-integration`` all ``integration``-marked
    tests share this one process, so a later test reading these flags
    without setting them itself would silently see this test's values.
    Pre-snapshotting each flag onto its own current value via monkeypatch
    (before calling ``connect_baseline_service``) makes monkeypatch restore
    the true pre-test value afterwards, regardless of what the call mutates
    it to.
    """
    from scripts.study.baseline_harness import settings
    from scripts.study.config import StudySettings

    try:
        study_settings = StudySettings()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"STUDY_NEO4J_PASSWORD not configured ({exc})")

    monkeypatch.setattr(settings, "neo4j_uri", study_settings.neo4j_uri)
    monkeypatch.setattr(settings, "neo4j_user", study_settings.neo4j_user)
    monkeypatch.setattr(settings, "neo4j_password", study_settings.neo4j_password)
    for _flag in (
        "multipath_recall_enabled",
        "lexical_arm_enabled",
        "multiquery_arm_enabled",
        "recall_similarity_floor",
        "relevance_bounded_recall_enabled",
    ):
        monkeypatch.setattr(settings, _flag, getattr(settings, _flag))

    try:
        service = await connect_baseline_service()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"study Neo4j not reachable ({exc}) -- run `make study-infra-up` first")

    try:
        from personal_agent.memory.protocol_adapter import MemoryServiceAdapter

        adapter = MemoryServiceAdapter(service)
        retrieved = await run_baseline_recall(
            adapter, "arterial calcification", k=20, trace_id="t1"
        )
        assert isinstance(retrieved, tuple)
    finally:
        await service.disconnect()
