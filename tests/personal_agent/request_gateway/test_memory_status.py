"""The memory-context status: four states, one precedence (FRE-1476, ADR-0148 D1/D2/D3).

Every criterion on FRE-1476 asserts an *exact* state. A test here that accepted "not
POPULATED" would pass for three different reasons, which is the collapse the whole ticket
exists to end.
"""

from __future__ import annotations

from typing import Any

from personal_agent.request_gateway.memory_status import (
    MemoryStatus,
    MemoryStatusReport,
    RecallOutcome,
    RecallStageReport,
    RenderStageReport,
    classify_recall_admission,
)


def _entity(name: str, description: str | None) -> dict[str, Any]:
    return {"type": "entity", "name": name, "description": description}


def _episode(turn_id: str, summary: str | None) -> dict[str, Any]:
    return {"type": "episode", "conversation_id": turn_id, "summary": summary}


def _session(session_id: str, summary: str | None) -> dict[str, Any]:
    return {"type": "session", "session_id": session_id, "summary": summary}


def _stance(target: str, affect: str) -> dict[str, Any]:
    return {"type": "stance", "target": target, "affect": affect}


def _behavioural(target: str, affect: str) -> dict[str, Any]:
    return {"type": "behavioural_stance", "target": target, "affect": affect}


def _completed(items: list[dict[str, Any]] | None, **kwargs: Any) -> MemoryStatusReport:
    """A report whose recall path ran to completion over ``items``."""
    return MemoryStatusReport(
        recall=RecallStageReport(outcome=RecallOutcome.COMPLETED),
        admission=classify_recall_admission(items),
        **kwargs,
    )


class TestAc1UnreportedStatus:
    """AC-1: a path that reports no status yields exactly UNAVAILABLE."""

    def test_unreported_path_with_items_is_unavailable(self) -> None:
        items = [_entity("Kubernetes", "the orchestrator"), _entity("R2", "object store")]
        report = MemoryStatusReport(admission=classify_recall_admission(items))

        assert report.recall.outcome is RecallOutcome.NOT_REPORTED
        assert report.status is MemoryStatus.UNAVAILABLE

    def test_same_items_are_populated_once_the_path_reports(self) -> None:
        """The companion: the fixture is otherwise POPULATED, so AC-1 cannot pass for free."""
        items = [_entity("Kubernetes", "the orchestrator"), _entity("R2", "object store")]

        assert _completed(items).status is MemoryStatus.POPULATED

    def test_status_is_never_inferred_from_the_item_count(self) -> None:
        many = [_entity(f"e{i}", "described") for i in range(20)]

        assert MemoryStatusReport(admission=classify_recall_admission(many)).status is (
            MemoryStatus.UNAVAILABLE
        )


class TestAc2StandingStanceLayer:
    """AC-2: the standing behavioural-stance layer never makes the status POPULATED."""

    def test_behavioural_stances_alone_are_nothing_relevant(self) -> None:
        items = [_behavioural("Artifact", "prefers an artifact"), _behavioural("Health", "private")]

        assert _completed(items).status is MemoryStatus.NOTHING_RELEVANT

    def test_behavioural_stances_are_not_counted_as_admitted(self) -> None:
        admission = classify_recall_admission([_behavioural("Artifact", "prefers an artifact")])

        assert admission.admitted == 0
        assert admission.contentless == 0


class TestAc3EnrichmentInherits:
    """AC-3: enrichment stances inherit their parent, they never qualify independently."""

    def test_parentless_stance_does_not_raise_the_status(self) -> None:
        """A stance whose target entity is absent is not admitted at all."""
        items = [_stance("Kubernetes", "finds it heavy")]

        assert _completed(items).status is MemoryStatus.NOTHING_RELEVANT

    def test_stance_on_a_blank_described_parent_is_content_that_reached_the_model(self) -> None:
        """The renderer emits no entity line and one stance line, so the turn is POPULATED.

        Excluding stances outright would compose NOTHING_RELEVANT while recall-derived
        content did reach the model (codex plan-review finding).
        """
        items = [_entity("Kubernetes", ""), _stance("Kubernetes", "finds it heavy")]

        assert _completed(items).status is MemoryStatus.POPULATED

    def test_blank_affect_stance_carries_no_content(self) -> None:
        items = [_entity("Kubernetes", ""), _stance("Kubernetes", "   ")]

        assert _completed(items).status is MemoryStatus.NOTHING_RELEVANT


class TestAc4Precedence:
    """AC-4: the precedence order resolves every mixed case to exactly one state."""

    def test_one_arm_raises_while_another_returns_items(self) -> None:
        report = MemoryStatusReport(
            recall=RecallStageReport(RecallOutcome.FAILED, "recall_arms_failed:dense"),
            admission=classify_recall_admission([_entity("R2", "object store")]),
        )

        assert report.status is MemoryStatus.UNAVAILABLE

    def test_budget_drops_a_partial_context(self) -> None:
        items = [_entity("R2", "object store")]

        assert _completed(items, budget_dropped_recall_items=True).status is MemoryStatus.WITHHELD

    def test_every_arm_completes_but_the_renderer_drops_every_item(self) -> None:
        items = [_entity("R2", "object store")]
        report = _completed(items, render=RenderStageReport(ran=True, recall_emitted=0))

        assert report.status is MemoryStatus.WITHHELD

    def test_recall_admits_nothing_while_stances_inject_items(self) -> None:
        items = [_behavioural("Artifact", "prefers an artifact")]

        assert _completed(items).status is MemoryStatus.NOTHING_RELEVANT

    def test_memory_is_unwired_while_stances_render(self) -> None:
        report = MemoryStatusReport(
            recall=RecallStageReport(RecallOutcome.FAILED, "memory_not_connected"),
            admission=classify_recall_admission([_behavioural("Artifact", "prefers an artifact")]),
        )

        assert report.status is MemoryStatus.UNAVAILABLE

    def test_a_rendered_recall_line_keeps_the_turn_populated(self) -> None:
        """The companion to the renderer case: a render that emitted is not WITHHELD."""
        items = [_entity("R2", "object store")]
        report = _completed(items, render=RenderStageReport(ran=True, recall_emitted=1))

        assert report.status is MemoryStatus.POPULATED


class TestAc5PartialFailureIsNeverAbsence:
    """AC-5: a partial failure never yields absence."""

    def test_partial_failure_outranks_the_surviving_items(self) -> None:
        report = MemoryStatusReport(
            recall=RecallStageReport(RecallOutcome.FAILED, "recall_arms_failed:dense"),
            admission=classify_recall_admission([_entity("R2", "object store")]),
        )

        assert report.status is not MemoryStatus.NOTHING_RELEVANT
        assert report.status is not MemoryStatus.POPULATED
        assert report.status is MemoryStatus.UNAVAILABLE

    def test_partial_failure_with_no_items_is_still_unavailable(self) -> None:
        report = MemoryStatusReport(
            recall=RecallStageReport(RecallOutcome.FAILED, "recall_arms_failed:dense"),
            admission=classify_recall_admission([]),
        )

        assert report.status is MemoryStatus.UNAVAILABLE


class TestAc6AdmissionRequiresContent:
    """AC-6: a contentless item is not admitted; unsupported real content is."""

    def test_blank_description_entities_only_is_nothing_relevant(self) -> None:
        items = [_entity("Kubernetes", ""), _entity("R2", None), _entity("Neo4j", "   ")]
        report = _completed(items)

        assert report.admission.admitted == 0
        assert report.admission.contentless == 3
        assert report.status is MemoryStatus.NOTHING_RELEVANT

    def test_session_items_with_real_content_are_withheld(self) -> None:
        items = [_session("s1", "we discussed the deploy"), _session("s2", "and the reranker")]
        report = _completed(items)

        assert report.admission.admitted == 2
        assert report.admission.with_content_renderable == 0
        assert report.status is MemoryStatus.WITHHELD

    def test_an_episode_falls_back_to_its_user_message_for_content(self) -> None:
        items = [{"type": "episode", "conversation_id": "t1", "user_message": "how do I deploy?"}]

        assert _completed(items).status is MemoryStatus.POPULATED

    def test_a_blank_episode_carries_no_content(self) -> None:
        assert _completed([_episode("t1", "   ")]).status is MemoryStatus.NOTHING_RELEVANT


class TestClassification:
    """The classifier itself, since every criterion above reads through it."""

    def test_none_context_admits_nothing(self) -> None:
        admission = classify_recall_admission(None)

        assert admission.admitted == 0
        assert admission.contentless == 0

    def test_mixed_context_is_counted_per_kind(self) -> None:
        items = [
            _entity("R2", "object store"),
            _entity("Neo4j", ""),
            _session("s1", "a real summary"),
            _stance("R2", "likes it"),
            _behavioural("Artifact", "prefers an artifact"),
        ]
        admission = classify_recall_admission(items)

        assert admission.with_content_renderable == 2  # the entity and its stance
        assert admission.with_content_unsupported_kind == 1  # the session
        assert admission.contentless == 1  # the blank entity
        assert admission.admitted == 3

    def test_unknown_shapes_are_not_admitted(self) -> None:
        admission = classify_recall_admission([{"type": "nonsense", "text": "hello"}, "not a dict"])

        assert admission.admitted == 0
        assert admission.contentless == 0
