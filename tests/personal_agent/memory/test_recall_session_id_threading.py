"""FRE-1775: context-assembly recall carries the turn's session id to its model calls.

The five ``cost_record_missing_identity`` events in the logs (2026-09-08 to 2026-11-12)
all traced to one caller: Stage 6 context assembly's entity-match recall. It called
``MemoryServiceAdapter.recall`` with no session id, so the multi-query paraphrase call
inside it built ``TraceContext(session_id=None)``, and ``LiteLLMClient.respond`` logged
the error and wrote no ``api_costs`` row. ``recall_broad`` had the same gap.

AC-1: each link of that chain now carries the session id, and the model call at the
end of it writes one cost row with it. AC-2: a true system-path call still logs
``cost_record_missing_identity`` and writes no row.
"""

# ruff: noqa: D103

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

import personal_agent.memory.service as svc
from personal_agent.memory.models import MemoryQueryResult
from personal_agent.memory.protocol import EntityResolutionResult, MemoryRecallQuery
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter
from personal_agent.request_gateway.context import assemble_context
from personal_agent.request_gateway.types import Complexity, IntentResult, TaskType

_SESSION_ID = "87115f87-8f2d-4e8c-89d9-d7ac5eeb9f09"
_TRACE_ID = "c78beff3825008b947598882e650f8bf"


def _intent(task_type: TaskType) -> IntentResult:
    return IntentResult(
        task_type=task_type, complexity=Complexity.SIMPLE, confidence=0.9, signals=[]
    )


# ── Stage 6 → adapter ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assembly_entity_match_recall_passes_the_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "personal_agent.request_gateway.context.settings.proactive_memory_enabled", False
    )
    adapter = AsyncMock()
    adapter.is_connected = AsyncMock(return_value=True)
    adapter.resolve_message_entities = AsyncMock(
        return_value=EntityResolutionResult(names=["Athens"])
    )
    adapter.recall = AsyncMock(
        return_value=MagicMock(entities=[], episodes=[], relevance_scored=False, arms_failed=())
    )

    await assemble_context(
        user_message="What about Athens?",
        session_messages=[],
        intent=_intent(TaskType.CONVERSATIONAL),
        memory_adapter=adapter,
        trace_id=_TRACE_ID,
        session_id=_SESSION_ID,
    )

    adapter.recall.assert_awaited_once()
    assert adapter.recall.await_args.kwargs["session_id"] == _SESSION_ID


@pytest.mark.asyncio
async def test_assembly_broad_recall_passes_the_session_id() -> None:
    adapter = AsyncMock()
    adapter.is_connected = AsyncMock(return_value=True)
    adapter.recall_broad = AsyncMock(
        return_value=MagicMock(
            entities_by_type={},
            recent_sessions=[],
            total_entity_count=0,
            arms_failed=(),
            relevance_scored=False,
        )
    )

    await assemble_context(
        user_message="What have I asked about?",
        session_messages=[],
        intent=_intent(TaskType.MEMORY_RECALL),
        memory_adapter=adapter,
        trace_id=_TRACE_ID,
        session_id=_SESSION_ID,
    )

    adapter.recall_broad.assert_awaited_once()
    assert adapter.recall_broad.await_args.kwargs["session_id"] == _SESSION_ID


# ── adapter → MemoryService ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_recall_forwards_the_session_id_to_query_memory() -> None:
    service = MagicMock()
    service.query_memory = AsyncMock(return_value=MemoryQueryResult())
    adapter = MemoryServiceAdapter(service)

    await adapter.recall(
        MemoryRecallQuery(entity_names=["Athens"], query_text="What about Athens?"),
        trace_id=_TRACE_ID,
        session_id=_SESSION_ID,
    )

    assert service.query_memory.await_args.kwargs["session_id"] == _SESSION_ID


@pytest.mark.asyncio
async def test_adapter_recall_broad_forwards_the_session_id_to_query_memory_broad() -> None:
    service = MagicMock()
    service.query_memory_broad = AsyncMock(return_value={"entities": [], "sessions": []})
    adapter = MemoryServiceAdapter(service)

    await adapter.recall_broad(
        entity_types=None,
        recency_days=90,
        limit=20,
        trace_id=_TRACE_ID,
        session_id=_SESSION_ID,
        query_text="What have I asked about?",
    )

    assert service.query_memory_broad.await_args.kwargs["session_id"] == _SESSION_ID


# ── the model call at the end of the chain ────────────────────────────────────


@contextmanager
def _mocked_respond_io(cost_rows: list[dict[str, object]], errors: list[str]) -> Iterator[None]:
    """Mock every external I/O of ``LiteLLMClient.respond``; capture cost rows and errors."""
    usage = MagicMock(
        prompt_tokens=79,
        completion_tokens=43,
        total_tokens=122,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
        prompt_tokens_details=None,
    )
    response = MagicMock(usage=usage, id="resp-1", system_fingerprint=None, model=None)
    response.choices = [MagicMock()]
    response.choices[0].message.content = "perception\neyesight"
    response.choices[0].message.tool_calls = None

    gate = MagicMock()
    gate.reserve = AsyncMock(return_value="res-001")
    gate.commit = AsyncMock()
    tracker = AsyncMock()
    tracker.record_api_call = AsyncMock(side_effect=lambda **kw: cost_rows.append(kw))
    log = MagicMock()
    log.error = MagicMock(side_effect=lambda event, **_kw: errors.append(event))

    with (
        patch("personal_agent.llm_client.litellm_client.log", log),
        patch("litellm.acompletion", AsyncMock(return_value=response)),
        patch("litellm.completion_cost", return_value=0.000447),
        patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
        patch("personal_agent.cost_gate.load_budget_config", return_value=MagicMock()),
        patch(
            "personal_agent.llm_client.cost_estimator.estimate_reservation_for_call",
            return_value=Decimal("0.01"),
        ),
        patch(
            "personal_agent.llm_client.history_sanitiser.sanitise_messages",
            side_effect=lambda msgs, trace_id: (msgs, []),
        ),
        patch(
            "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
            return_value=tracker,
        ),
        patch(
            "personal_agent.config.settings.get_settings",
            return_value=MagicMock(anthropic_api_key="test-key", openai_api_key=None),
        ),
    ):
        yield


def _paid_client() -> object:
    from personal_agent.llm_client.litellm_client import LiteLLMClient

    return LiteLLMClient(
        model_id="claude-sonnet-4-6",
        provider="anthropic",
        max_tokens=200,
        budget_role="main_inference",
    )


@pytest.mark.asyncio
async def test_paraphrase_call_in_a_user_turn_writes_one_cost_row_with_the_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(svc, "get_llm_client", lambda role_name: _paid_client())
    cost_rows: list[dict[str, object]] = []
    errors: list[str] = []

    with _mocked_respond_io(cost_rows, errors):
        paraphrases = await svc.generate_query_paraphrases(
            "What about Athens?", 2, trace_id=_TRACE_ID, session_id=_SESSION_ID
        )

    assert paraphrases == ["perception", "eyesight"]
    assert "cost_record_missing_identity" not in errors
    [row] = cost_rows
    assert row["session_id"] == UUID(_SESSION_ID)
    assert row["trace_id"] == UUID(_TRACE_ID)


@pytest.mark.asyncio
async def test_a_system_path_call_still_logs_missing_identity_and_writes_no_row() -> None:
    from personal_agent.llm_client.types import ModelRole
    from personal_agent.telemetry.trace import SystemTraceContext

    cost_rows: list[dict[str, object]] = []
    errors: list[str] = []

    with _mocked_respond_io(cost_rows, errors):
        await _paid_client().respond(  # type: ignore[attr-defined]
            role=ModelRole.SUB_AGENT,
            messages=[{"role": "user", "content": "probe"}],
            trace_ctx=SystemTraceContext.new("probe"),
        )

    assert "cost_record_missing_identity" in errors
    assert cost_rows == []
