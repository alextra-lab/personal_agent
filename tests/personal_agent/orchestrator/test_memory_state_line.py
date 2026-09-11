"""The memory section is always present, and names its own state (FRE-1478, ADR-0148 D2/D5).

``test_memory_status.py`` and ``test_memory_status_assembly.py`` pin the vocabulary and its
composition. This file pins the rendering half: what actually reaches the wire when the
turn's ``memory_status`` is not POPULATED.

Acceptance criteria asserted here are FRE-1478's own AC-1 through AC-5. AC-8 (the FRE-1150
wording) is untouched by this ticket and stays covered by ``test_identity_precedence.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.request_gateway.memory_status import (
    MEMORY_STATE_LINES,
    MemoryStatus,
    MemoryStatusReport,
    RecallOutcome,
    RecallStageReport,
    classify_recall_admission,
)


def _entity(name: str, description: str) -> dict[str, Any]:
    return {"type": "entity", "name": name, "entity_type": "CONCEPT", "description": description}


def _session(session_id: str, summary: str) -> dict[str, Any]:
    return {"type": "session", "session_id": session_id, "summary": summary}


def _behavioural(target: str, affect: str) -> dict[str, Any]:
    return {"type": "behavioural_stance", "target": target, "affect": affect}


def _make_ctx(**overrides: object) -> object:
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext

    kwargs: dict[str, object] = {
        "session_id": "test-session",
        "trace_id": "test-trace",
        "user_message": "what do you know about the Kubernetes migration?",
        "mode": Mode.NORMAL,
        "channel": Channel.CHAT,
        "messages": [
            {"role": "user", "content": "what do you know about the Kubernetes migration?"}
        ],
    }
    kwargs.update(overrides)
    return ExecutionContext(**kwargs)  # type: ignore[arg-type]


def _mock_llm() -> MagicMock:
    client = MagicMock()
    client.respond = AsyncMock(
        return_value={
            "content": "I have no usable record of that.",
            "tool_calls": [],
            "response_id": None,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    client.model_configs = {}
    return client


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals():
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    saved_layer = _ex._tool_execution_layer
    yield
    _ex._tool_registry = saved_registry
    _ex._tool_execution_layer = saved_layer


async def _run(ctx: object, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Drive the real ``step_llm_call`` and return the mocked client."""
    from personal_agent.config import settings
    from personal_agent.telemetry.trace import TraceContext

    monkeypatch.setattr(settings, "prefer_primitives_enabled", False)

    client = _mock_llm()
    session = MagicMock()
    session.add_message = AsyncMock()
    session.get_messages = AsyncMock(return_value=[])

    with (
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=client),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        ),
    ):
        from personal_agent.orchestrator.executor import step_llm_call

        await step_llm_call(ctx, session, TraceContext.new_trace())  # type: ignore[arg-type]
    return client


def _dispatched_user_text(client: MagicMock) -> str:
    """The text of the last user message actually dispatched to the provider."""
    messages = client.respond.call_args.kwargs["messages"]
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            return content if isinstance(content, str) else str(content)
    return ""


# ── Fixtures for each of the four states ────────────────────────────────────────


def _populated_ctx() -> object:
    items = [_entity("Kubernetes", "the orchestrator this team runs on")]
    return _make_ctx(
        memory_context=items,
        memory_status=MemoryStatusReport(
            recall=RecallStageReport(RecallOutcome.COMPLETED),
            admission=classify_recall_admission(items),
        ),
    )


def _nothing_relevant_ctx() -> object:
    return _make_ctx(
        memory_context=None,
        memory_status=MemoryStatusReport(recall=RecallStageReport(RecallOutcome.COMPLETED)),
    )


def _withheld_ctx() -> object:
    """WITHHELD via the renderer's own unsupported-kind drop, not a budget drop.

    A session item carries real content the renderer never emits (FRE-1010's own
    non-goal), so the system holds it and the reader does not get it — D2's WITHHELD,
    reached without needing Stage 7 to have fired at all.
    """
    items = [_session("s1", "we discussed the deploy last week")]
    return _make_ctx(
        memory_context=items,
        memory_status=MemoryStatusReport(
            recall=RecallStageReport(RecallOutcome.COMPLETED),
            admission=classify_recall_admission(items),
        ),
    )


def _unavailable_ctx() -> object:
    return _make_ctx(
        memory_context=None,
        memory_status=MemoryStatusReport(
            recall=RecallStageReport(RecallOutcome.FAILED, "memory_query_failed")
        ),
    )


def _nothing_relevant_with_behavioural_ctx() -> object:
    """AC-4/AC-8 of ADR-0148 D2: standing stances render, recall still admits nothing."""
    items = [_behavioural("Artifact", "prefers explicit request before creation")]
    return _make_ctx(
        memory_context=items,
        memory_status=MemoryStatusReport(
            recall=RecallStageReport(RecallOutcome.COMPLETED),
            admission=classify_recall_admission(items),
        ),
    )


class TestAc1SectionAlwaysPresent:
    """AC-1: the section is present in every one of the four states."""

    @pytest.mark.asyncio
    async def test_populated_has_no_state_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = await _run(_populated_ctx(), monkeypatch)
        text = _dispatched_user_text(client)

        assert "the orchestrator this team runs on" in text
        for line in MEMORY_STATE_LINES.values():
            assert line not in text

    @pytest.mark.asyncio
    async def test_nothing_relevant_renders_its_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = await _run(_nothing_relevant_ctx(), monkeypatch)
        text = _dispatched_user_text(client)

        assert MEMORY_STATE_LINES[MemoryStatus.NOTHING_RELEVANT] in text

    @pytest.mark.asyncio
    async def test_withheld_renders_its_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = await _run(_withheld_ctx(), monkeypatch)
        text = _dispatched_user_text(client)

        assert MEMORY_STATE_LINES[MemoryStatus.WITHHELD] in text

    @pytest.mark.asyncio
    async def test_unavailable_renders_its_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = await _run(_unavailable_ctx(), monkeypatch)
        text = _dispatched_user_text(client)

        assert MEMORY_STATE_LINES[MemoryStatus.UNAVAILABLE] in text


class TestAc2DistinctLinesPerState:
    """AC-2: each non-populated state renders its own distinct line."""

    def test_three_lines_are_pairwise_distinct(self) -> None:
        lines = list(MEMORY_STATE_LINES.values())
        assert len(lines) == len(set(lines)) == 3

    def test_nothing_relevant_says_usable_never_bare_no_record(self) -> None:
        line = MEMORY_STATE_LINES[MemoryStatus.NOTHING_RELEVANT]
        assert "usable record" in line
        assert "no record" not in line


class TestAc3NothingRelevantVsUnavailable:
    """AC-3: a NOTHING_RELEVANT turn and an unwired-recall turn render differently."""

    @pytest.mark.asyncio
    async def test_the_two_serialized_inputs_do_not_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nothing_relevant = await _run(_nothing_relevant_ctx(), monkeypatch)
        unavailable = await _run(_unavailable_ctx(), monkeypatch)

        text_a = _dispatched_user_text(nothing_relevant)
        text_b = _dispatched_user_text(unavailable)

        assert text_a != text_b
        assert "usable record" in text_a
        assert "usable record" not in text_b
        assert "could not be reached" in text_b
        assert "could not be reached" not in text_a


class TestAc4BehaviouralSectionCoexistsWithAbsence:
    """AC-4: the behavioural section and the absence line neither suppress the other."""

    @pytest.mark.asyncio
    async def test_both_render_on_the_same_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = await _run(_nothing_relevant_with_behavioural_ctx(), monkeypatch)
        text = _dispatched_user_text(client)

        assert "## Standing Behavioural Preferences" in text
        assert "prefers explicit request before creation" in text
        assert MEMORY_STATE_LINES[MemoryStatus.NOTHING_RELEVANT] in text


class TestAc5SourceRegistryHoldsNoRecallSource:
    """AC-5: in each non-populated state, the registry holds no recall-derived source."""

    @pytest.mark.asyncio
    async def test_nothing_relevant_registers_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.grounding.source_registry import SourceRegistry

        ctx = _nothing_relevant_ctx()
        ctx.source_registry = SourceRegistry(turn_id=ctx.trace_id)  # type: ignore[attr-defined]
        await _run(ctx, monkeypatch)

        assert ctx.source_registry.sources() == ()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_withheld_registers_no_recall_derived_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The session item carries content but the renderer never emits it, so it must
        never resolve as a citable source (D5: the registry is the truth AC-5 checks).
        """
        from personal_agent.grounding.source_registry import SourceRegistry

        ctx = _withheld_ctx()
        ctx.source_registry = SourceRegistry(turn_id=ctx.trace_id)  # type: ignore[attr-defined]
        await _run(ctx, monkeypatch)

        assert ctx.source_registry.sources() == ()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_unavailable_registers_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.grounding.source_registry import SourceRegistry

        ctx = _unavailable_ctx()
        ctx.source_registry = SourceRegistry(turn_id=ctx.trace_id)  # type: ignore[attr-defined]
        await _run(ctx, monkeypatch)

        assert ctx.source_registry.sources() == ()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_nothing_relevant_with_behavioural_still_registers_nothing_recall_derived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A standing behavioural stance may register (it is not recall-derived, D2) —
        this only asserts no recall-layer item does.
        """
        from personal_agent.grounding.source_registry import SourceRegistry

        ctx = _nothing_relevant_with_behavioural_ctx()
        ctx.source_registry = SourceRegistry(turn_id=ctx.trace_id)  # type: ignore[attr-defined]
        await _run(ctx, monkeypatch)

        sources = ctx.source_registry.sources()  # type: ignore[attr-defined]
        # The only item this turn carries at all is the standing behavioural stance —
        # so at most it, and nothing else, may register.
        assert len(sources) <= 1
        for source in sources:
            assert "prefers explicit request before creation" in source.content


class TestRenderStageReportWiredFromTheRealRender:
    """The renderer's own report replaces the mirror, per D1."""

    @pytest.mark.asyncio
    async def test_populated_turn_carries_a_render_report_with_recall_emitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _populated_ctx()
        await _run(ctx, monkeypatch)

        assert ctx.memory_status.render.ran is True  # type: ignore[attr-defined]
        assert ctx.memory_status.render.recall_emitted == 1  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_withheld_turn_carries_a_render_report_with_nothing_emitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _withheld_ctx()
        await _run(ctx, monkeypatch)

        assert ctx.memory_status.render.ran is True  # type: ignore[attr-defined]
        assert ctx.memory_status.render.recall_emitted == 0  # type: ignore[attr-defined]
        assert ctx.memory_status.status is MemoryStatus.WITHHELD  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_withheld_render_report_carries_a_real_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-6 (master bounce, PR #1131): the session item genuinely had content the
        renderer dropped, so the cause must be present and true.
        """
        ctx = _withheld_ctx()
        await _run(ctx, monkeypatch)

        assert ctx.memory_status.render.cause == "render_dropped_all_recall_items"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_nothing_relevant_with_behavioural_carries_no_false_drop_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-6 (master bounce, PR #1131): nothing from the recall layer was ever
        admitted here — only a standing behavioural stance — so there was nothing to
        drop. Before the fix this persisted ``render_dropped_all_recall_items`` for a
        turn where nothing was dropped at all.
        """
        ctx = _nothing_relevant_with_behavioural_ctx()
        await _run(ctx, monkeypatch)

        assert ctx.memory_status.status is MemoryStatus.NOTHING_RELEVANT  # type: ignore[attr-defined]
        assert ctx.memory_status.render.cause is None  # type: ignore[attr-defined]
        # The persisted evidence record is what master's finding was about directly —
        # asserted here too, not only on the intermediate RenderStageReport.
        assert ctx.turn_evidence is not None  # type: ignore[attr-defined]
        assert ctx.turn_evidence.recall.memory_state == "nothing_relevant"  # type: ignore[attr-defined]
        assert ctx.turn_evidence.recall.memory_state_cause is None  # type: ignore[attr-defined]
