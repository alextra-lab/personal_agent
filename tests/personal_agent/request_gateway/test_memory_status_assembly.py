"""The status composed over the real assembly path (FRE-1476, ADR-0148 D1/D2/D3).

``test_memory_status.py`` pins the rule. These pin the wiring: that each producing path
reports what it did, that the standing behavioural-stance layer is scoped out, and that
the budget stage reports its own drop. AC-2 and AC-5 fail against the pre-FRE-1476 code by
construction — the report they read did not exist.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.memory.proactive_types import (
    ProactiveMemoryCandidate,
    ProactiveMemorySuggestions,
    ProactiveScoreComponents,
)
from personal_agent.memory.protocol import (
    BroadRecallResult,
    EntityResolutionResult,
    MemoryRecallResult,
)
from personal_agent.request_gateway.budget import apply_budget
from personal_agent.request_gateway.context import assemble_context
from personal_agent.request_gateway.memory_status import (
    MemoryStatus,
    RecallOutcome,
)
from personal_agent.request_gateway.types import Complexity, IntentResult, TaskType

CURATED_STANCE = {"target": "Artifact", "affect": "prefers an artifact", "asserted_by": "user"}


def _intent(task_type: TaskType = TaskType.CONVERSATIONAL) -> IntentResult:
    return IntentResult(
        task_type=task_type, complexity=Complexity.SIMPLE, confidence=0.9, signals=[]
    )


def _adapter(*, stances: list[dict] | None = None) -> MagicMock:
    """An adapter that connects, resolves no entities, and holds the curated stances."""
    adapter = MagicMock()
    adapter.is_connected = AsyncMock(return_value=True)
    adapter.resolve_message_entities = AsyncMock(return_value=EntityResolutionResult(names=[]))
    adapter.get_current_stances = AsyncMock(return_value=stances if stances is not None else [])
    return adapter


async def _assemble(adapter: MagicMock | None, *, task_type: TaskType = TaskType.CONVERSATIONAL):
    return await assemble_context(
        user_message="what do you know about my sailing certificate?",
        session_messages=[],
        intent=_intent(task_type),
        memory_adapter=adapter,
        trace_id="t-1476",
        authenticated=True,
    )


class TestAc2StandingStancesDoNotDefeatAbsence:
    """AC-2: recall admits nothing while the standing layer injects items."""

    @pytest.mark.asyncio
    async def test_status_is_nothing_relevant_and_the_stances_still_render(self) -> None:
        adapter = _adapter(stances=[CURATED_STANCE])

        result = await _assemble(adapter)

        assert result.memory_status.recall.outcome is RecallOutcome.COMPLETED
        assert result.memory_status.status is MemoryStatus.NOTHING_RELEVANT
        # Scoping the status out of the standing layer must not suppress the layer.
        assert result.memory_context is not None
        assert [i["type"] for i in result.memory_context] == ["behavioural_stance"]

    @pytest.mark.asyncio
    async def test_a_turn_with_no_stances_at_all_is_also_nothing_relevant(self) -> None:
        result = await _assemble(_adapter())

        assert result.memory_status.status is MemoryStatus.NOTHING_RELEVANT


class TestAc3EnrichmentInherits:
    """AC-3: an enrichment stance neither survives a rejected parent nor raises alone."""

    @pytest.mark.asyncio
    async def test_a_stance_for_an_unrecalled_entity_never_reaches_the_context(self) -> None:
        """Recall admits nothing, so enrichment has no parent to attach to.

        The stance target here is deliberately outside the curated standing set, so the
        standing layer does not carry it in either: the only route into the context would
        have been enrichment, and enrichment has nothing to enrich.
        """
        adapter = _adapter(stances=[{"target": "Sailing", "affect": "proud of it"}])

        result = await _assemble(adapter)

        assert all(i["type"] != "stance" for i in result.memory_context or [])
        assert result.memory_status.status is MemoryStatus.NOTHING_RELEVANT

    @pytest.mark.asyncio
    async def test_a_stance_on_a_blank_described_parent_does_raise_the_status(self) -> None:
        """Inheriting is not the same as being invisible.

        The renderer drops the blank entity and emits the stance line, so recall-derived
        content does reach the model and the turn is POPULATED.
        """
        adapter = _adapter(stances=[{"target": "Sailing", "affect": "proud of it"}])
        adapter.recall_broad = AsyncMock(
            return_value=BroadRecallResult(
                entities_by_type={
                    "Topic": [
                        {
                            "name": "Sailing",
                            "description": "",
                            "relevance_score": 0.8,
                            "relevance_model": "rerank-2.5",
                        }
                    ]
                },
                recent_sessions=[],
                total_entity_count=1,
                # FRE-1480: a healthy MEMORY_RECALL turn is a *reranked* one. Without the
                # score and this flag the fixture models a path that established no
                # relevance value, which ADR-0148 D4 makes UNAVAILABLE, not POPULATED.
                relevance_scored=True,
            )
        )

        result = await _assemble(adapter, task_type=TaskType.MEMORY_RECALL)

        assert result.memory_context is not None
        assert [i["type"] for i in result.memory_context] == ["entity", "stance"]
        assert result.memory_status.status is MemoryStatus.POPULATED


class TestAc5PartialFailureIsNotAbsence:
    """AC-5: one arm fails while another returns items."""

    @pytest.mark.asyncio
    async def test_broad_recall_with_a_failed_arm_is_unavailable(self) -> None:
        adapter = _adapter()
        adapter.recall_broad = AsyncMock(
            return_value=BroadRecallResult(
                entities_by_type={"Topic": [{"name": "Sailing", "description": "a real record"}]},
                recent_sessions=[],
                total_entity_count=1,
                arms_failed=("dense",),
            )
        )

        result = await _assemble(adapter, task_type=TaskType.MEMORY_RECALL)

        assert result.memory_status.recall.outcome is RecallOutcome.FAILED
        assert result.memory_status.recall.cause == "recall_arms_failed:dense"
        assert result.memory_status.status is MemoryStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_the_same_broad_recall_with_every_arm_intact_is_populated(self) -> None:
        """The companion: without it, the case above could pass for the wrong reason."""
        adapter = _adapter()
        adapter.recall_broad = AsyncMock(
            return_value=BroadRecallResult(
                entities_by_type={
                    "Topic": [
                        {
                            "name": "Sailing",
                            "description": "a real record",
                            "relevance_score": 0.8,
                            "relevance_model": "rerank-2.5",
                        }
                    ]
                },
                recent_sessions=[],
                total_entity_count=1,
                # FRE-1480: see the sibling fixture above -- "every arm intact" means the
                # reranker ran, which is what establishes a relevance value at all.
                relevance_scored=True,
            )
        )

        result = await _assemble(adapter, task_type=TaskType.MEMORY_RECALL)

        assert result.memory_status.status is MemoryStatus.POPULATED

    @pytest.mark.asyncio
    async def test_a_failed_proactive_path_is_not_rescued_by_the_entity_fallback(
        self, monkeypatch
    ) -> None:
        """The proactive failure survives the fall-through to entity match."""
        monkeypatch.setattr(
            "personal_agent.request_gateway.context.settings.proactive_memory_enabled",
            True,
            raising=False,
        )
        adapter = _adapter()
        adapter.resolve_message_entities = AsyncMock(
            return_value=EntityResolutionResult(names=["Sailing"])
        )
        adapter.suggest_relevant = AsyncMock(
            return_value=ProactiveMemorySuggestions(
                candidates=[], failed=True, failure_cause="zero_embedding"
            )
        )
        adapter.recall = AsyncMock(
            return_value=MemoryRecallResult(
                episodes=[],
                entities=[{"name": "Sailing", "description": "a real record"}],
            )
        )

        result = await _assemble(adapter)

        assert result.memory_status.recall.cause == "zero_embedding"
        assert result.memory_status.status is MemoryStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_broad_recall_with_a_failed_multi_query_arm_is_unavailable(self) -> None:
        """FRE-1481, AC-3: the same composition mechanism the dense-arm sibling test
        above proves must also handle "multi_query" as the failed arm name -- proving
        nothing in the composition path hardcodes "dense" specifically. The service-level
        proof that a zero-vector variant actually puts "multi_query" in
        ``MultiPathRecallResult.arms_failed`` lives in
        ``test_multiquery_arm_reports_failure.py``; this is the sibling proof that the
        name, once reported, reaches ``AssembledContext.memory_status.status``.
        """
        adapter = _adapter()
        adapter.recall_broad = AsyncMock(
            return_value=BroadRecallResult(
                entities_by_type={"Topic": [{"name": "Sailing", "description": "a real record"}]},
                recent_sessions=[],
                total_entity_count=1,
                arms_failed=("multi_query",),
            )
        )

        result = await _assemble(adapter, task_type=TaskType.MEMORY_RECALL)

        assert result.memory_status.recall.outcome is RecallOutcome.FAILED
        assert result.memory_status.recall.cause == "recall_arms_failed:multi_query"
        assert result.memory_status.status is MemoryStatus.UNAVAILABLE


class TestEntityResolutionFailureIsNotAbsence:
    """FRE-1481: ``resolve_message_entities`` reporting failure must compose the same
    way a proactive or arm failure already does -- and the two must not overwrite each
    other's cause when both fail (codex plan-review finding).
    """

    @pytest.mark.asyncio
    async def test_a_failed_resolution_is_not_rescued_by_proactive_success(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "personal_agent.request_gateway.context.settings.proactive_memory_enabled",
            True,
            raising=False,
        )
        adapter = _adapter()
        adapter.resolve_message_entities = AsyncMock(
            return_value=EntityResolutionResult(
                names=[], failed=True, failure_cause="entity_resolution_failed"
            )
        )
        adapter.suggest_relevant = AsyncMock(
            return_value=ProactiveMemorySuggestions(
                candidates=[
                    ProactiveMemoryCandidate(
                        kind="entity",
                        payload={"type": "entity", "name": "Sailing", "description": "a record"},
                        relevance_score=0.9,
                        score_components=ProactiveScoreComponents(
                            embedding=0.9, entity_overlap=0.0, recency=0.0, topic_coherence=0.0
                        ),
                    )
                ]
            )
        )

        result = await _assemble(adapter)

        assert result.memory_status.recall.cause == "entity_resolution_failed"
        assert result.memory_status.status is MemoryStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_a_failed_resolution_reaches_the_entity_match_fallthrough(self) -> None:
        """Proactive disabled: the failure must still surface on the fall-through path."""
        adapter = _adapter()
        adapter.resolve_message_entities = AsyncMock(
            return_value=EntityResolutionResult(
                names=[], failed=True, failure_cause="entity_resolution_failed"
            )
        )

        result = await _assemble(adapter)

        assert result.memory_status.recall.cause == "entity_resolution_failed"
        assert result.memory_status.status is MemoryStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_both_resolution_and_proactive_failing_name_both_causes(
        self, monkeypatch
    ) -> None:
        """Codex plan-review: the second failure must not silently overwrite the first
        -- the evidence record should name every stage that failed.
        """
        monkeypatch.setattr(
            "personal_agent.request_gateway.context.settings.proactive_memory_enabled",
            True,
            raising=False,
        )
        adapter = _adapter()
        adapter.resolve_message_entities = AsyncMock(
            return_value=EntityResolutionResult(
                names=[], failed=True, failure_cause="entity_resolution_failed"
            )
        )
        adapter.suggest_relevant = AsyncMock(
            return_value=ProactiveMemorySuggestions(
                candidates=[], failed=True, failure_cause="zero_embedding"
            )
        )

        result = await _assemble(adapter)

        assert result.memory_status.recall.cause == "entity_resolution_failed,zero_embedding"
        assert result.memory_status.status is MemoryStatus.UNAVAILABLE


class TestUnwiredMemory:
    """Memory that was never wired composes the weaker claim, not absence (D3)."""

    @pytest.mark.asyncio
    async def test_no_adapter_is_unavailable(self) -> None:
        result = await _assemble(None)

        assert result.memory_status.recall.outcome is RecallOutcome.NOT_REPORTED
        assert result.memory_status.status is MemoryStatus.UNAVAILABLE
        # FRE-1478: named so the turn-evidence record can tell this apart from an arm
        # that ran and failed (AC-6) — both compose UNAVAILABLE, but must not read back
        # as the same cause.
        assert result.memory_status.recall.cause == "memory_not_wired"

    @pytest.mark.asyncio
    async def test_a_disconnected_store_is_unavailable(self) -> None:
        adapter = _adapter()
        adapter.is_connected = AsyncMock(return_value=False)

        result = await _assemble(adapter)

        assert result.memory_status.recall.cause == "memory_not_connected"
        assert result.memory_status.status is MemoryStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_a_raising_adapter_is_unavailable(self) -> None:
        adapter = _adapter()
        adapter.resolve_message_entities = AsyncMock(side_effect=RuntimeError("neo4j is down"))

        result = await _assemble(adapter)

        assert result.memory_status.recall.cause == "memory_query_failed"
        assert result.memory_status.status is MemoryStatus.UNAVAILABLE


class TestBudgetStageReportsItsDrop:
    """AC-4's second case, over the real Stage 7."""

    @pytest.mark.asyncio
    async def test_dropping_an_admitted_context_is_withheld(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "personal_agent.request_gateway.context.settings.proactive_memory_enabled",
            True,
            raising=False,
        )
        adapter = _adapter()
        adapter.resolve_message_entities = AsyncMock(
            return_value=EntityResolutionResult(names=["Sailing"])
        )
        adapter.suggest_relevant = AsyncMock(
            return_value=ProactiveMemorySuggestions(
                candidates=[
                    ProactiveMemoryCandidate(
                        kind="entity",
                        payload={
                            "type": "entity",
                            "name": "Sailing",
                            "description": "a real record " * 400,
                        },
                        relevance_score=0.9,
                        score_components=ProactiveScoreComponents(
                            embedding=0.9, entity_overlap=0.0, recency=0.0, topic_coherence=0.0
                        ),
                    )
                ]
            )
        )
        assembled = await _assemble(adapter)
        assert assembled.memory_status.status is MemoryStatus.POPULATED

        budgeted = apply_budget(assembled, max_tokens=60, trace_id="t-1476")

        assert budgeted.memory_context is None
        assert budgeted.overflow_action == "dropped_memory_context"
        assert budgeted.memory_status.budget_dropped_recall_items is True
        assert budgeted.memory_status.status is MemoryStatus.WITHHELD

    def test_a_budget_that_drops_nothing_carries_the_status_through(self) -> None:
        """The status must survive Stage 7 unchanged when Phase 2 does not fire."""
        from personal_agent.request_gateway.memory_status import (
            MemoryStatusReport,
            RecallAdmission,
            RecallStageReport,
        )
        from personal_agent.request_gateway.types import AssembledContext

        context = AssembledContext(
            messages=[{"role": "user", "content": "hi"}],
            memory_context=[{"type": "entity", "name": "Sailing", "description": "a record"}],
            tool_definitions=None,
            memory_status=MemoryStatusReport(
                recall=RecallStageReport(RecallOutcome.COMPLETED),
                admission=RecallAdmission(with_content_renderable=1),
            ),
        )

        budgeted = apply_budget(context, max_tokens=100_000, trace_id="t-1476")

        assert budgeted.memory_status.status is MemoryStatus.POPULATED
        assert budgeted.memory_status.budget_dropped_recall_items is False
