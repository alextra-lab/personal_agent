"""FRE-1515 AC-5 precondition (ADR-0154 Amendment 1, 1.1 and 1.3) — a decline costs only the planner.

The D7 probe measures the planner's added delay by sending the ``SINGLE`` primary request in
both arms of a pair. That is valid only if a declined ``planner_asked`` turn sends the same
primary request as a ``SINGLE`` turn with the same session state.

"The same" is the final request: the keyword arguments that ``LiteLLMClient`` passes to
``litellm.acompletion``, after the client adds the catalog's mode, sampling, dialect
parameters and token limit. So this test runs the real executor and the real client on the
real catalog, and replaces only ``litellm.acompletion``. Only the HTTP ``client`` object and
the trace and span header values inside ``extra_headers`` may differ.

The calls are told apart by their order. ``SINGLE``: the first call is the primary call.
Planner route: the first call is the planner call, and the second is the primary call.
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import personal_agent.orchestrator.executor as ex
from personal_agent.config.model_loader import load_model_config
from personal_agent.config.selection import _current_selection, set_current_selection
from personal_agent.governance.models import Mode
from personal_agent.llm_client import litellm_client as litellm_client_module
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.expansion_controller import _PLANNER_DECLINE_RULE
from personal_agent.orchestrator.types import ExecutionContext, TaskState
from personal_agent.request_gateway.types import (
    AssembledContext,
    Complexity,
    DecompositionResult,
    DecompositionStrategy,
    GatewayOutput,
    GovernanceContext,
    IntentResult,
    TaskType,
)
from personal_agent.telemetry.trace import TraceContext
from tests._helpers.litellm_capability import pinned_litellm_capabilities

_CATALOG_PATH = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
_SESSION_ID = "8d0f3a52-6b1e-4c1e-9a52-1515aaaa0001"
_TRACE_ID = "fre1515-parity-trace"
_STARTED_AT = datetime(2026, 10, 11, 9, 30, tzinfo=UTC)
_QUERY = "What is a good name for a grey cat?"
_HISTORY = [
    {"role": "user", "content": "We adopted a cat last week."},
    {"role": "assistant", "content": "Congratulations! What is the cat like?"},
]
_DECLINE = json.dumps({"strategy": "SINGLE", "tasks": []})
_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web.",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
}

#: Header names whose values carry the trace and the span, the only header values that may
#: differ between the two primary calls (Amendment 1, 1.1).
_TRACE_HEADER_HINTS = ("trace", "span")


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    from personal_agent.llm_client.concurrency import set_inference_concurrency_controller

    set_inference_concurrency_controller(None)
    saved_registry, saved_layer = ex._tool_registry, ex._tool_execution_layer
    token = _current_selection.set({})
    with pinned_litellm_capabilities():
        yield
    _current_selection.reset(token)
    set_inference_concurrency_controller(None)
    ex._tool_registry, ex._tool_execution_layer = saved_registry, saved_layer
    litellm_client_module._guarded_httpx_clients.clear()
    litellm_client_module._guarded_async_http_handlers.clear()


def _stream(content: str) -> AsyncIterator[Any]:
    class _Chunk:
        def model_dump(self) -> dict[str, Any]:
            return {
                "id": "chunk-1",
                "choices": [
                    {"delta": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            }

    async def _gen() -> AsyncIterator[Any]:
        yield _Chunk()

    return _gen()


def _gateway(strategy: DecompositionStrategy, reason: str) -> GatewayOutput:
    return GatewayOutput(
        intent=IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        ),
        governance=GovernanceContext(
            mode=Mode.NORMAL, expansion_permitted=True, expansion_budget=3
        ),
        decomposition=DecompositionResult(strategy=strategy, reason=reason),
        context=AssembledContext(
            messages=[{"role": "user", "content": _QUERY}],
            memory_context=None,
            tool_definitions=None,
        ),
        session_id=_SESSION_ID,
        trace_id=_TRACE_ID,
    )


def _session_manager() -> MagicMock:
    """A fresh session manager over one immutable snapshot, so both routes start equal."""
    session = MagicMock()
    session.messages = copy.deepcopy(_HISTORY)
    manager = MagicMock()
    manager.get_session = MagicMock(return_value=session)
    return manager


async def _drive(gw: GatewayOutput, replies: list[str]) -> list[dict[str, Any]]:
    """Run ``step_init`` then ``step_llm_call`` and return every ``litellm.acompletion`` call."""
    calls: list[dict[str, Any]] = []

    def _fake_acompletion(**kwargs: Any) -> AsyncIterator[Any]:
        calls.append(kwargs)
        return _stream(replies[len(calls) - 1])

    catalog = load_model_config(_CATALOG_PATH)
    gate = MagicMock()
    gate.reserve = AsyncMock(return_value="res-fre1515")
    gate.commit = AsyncMock()
    gate.refund = AsyncMock()
    tracker = AsyncMock()
    registry = MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[_TOOL]))

    with ExitStack() as stack:
        for target, kwargs in (
            ("personal_agent.llm_client.factory.load_model_config", {"return_value": catalog}),
            ("personal_agent.config.load_model_config", {"return_value": catalog}),
            ("litellm.acompletion", {"side_effect": _fake_acompletion}),
            ("litellm.completion_cost", {"return_value": 0.0}),
            ("personal_agent.cost_gate.get_default_gate", {"return_value": gate}),
            ("personal_agent.cost_gate.load_budget_config", {"return_value": MagicMock()}),
            (
                "personal_agent.llm_client.cost_estimator.estimate_reservation_for_call",
                {"return_value": Decimal("0.01")},
            ),
            (
                "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
                {"return_value": tracker},
            ),
            ("personal_agent.orchestrator.skills.assemble_skill_catalog", {"return_value": ""}),
        ):
            stack.enter_context(patch(target, **kwargs))
        stack.enter_context(patch.object(ex, "get_default_registry", return_value=registry))
        stack.enter_context(patch.object(ex, "_report_turn_progress", AsyncMock()))
        stack.enter_context(patch.object(ex, "_populate_operator_identity", AsyncMock()))

        primary_key = catalog.roles["primary"].deployment
        set_current_selection({"primary": primary_key})
        ctx = ExecutionContext(
            session_id=_SESSION_ID,
            trace_id=_TRACE_ID,
            user_message=_QUERY,
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            gateway_output=gw,
        )
        ctx.turn_started_at = _STARTED_AT
        manager = _session_manager()
        trace = TraceContext(trace_id=_TRACE_ID, session_id=_SESSION_ID)

        state = await ex.step_init(ctx, manager, trace)
        assert state == TaskState.LLM_CALL
        await ex.step_llm_call(ctx, manager, trace)
    return calls


def _comparable(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop the values Amendment 1, 1.1 allows to differ: ``client``, trace and span headers."""
    out = {k: v for k, v in kwargs.items() if k != "client"}
    headers = dict(out.get("extra_headers") or {})
    for name in list(headers):
        if any(hint in name.lower() for hint in _TRACE_HEADER_HINTS):
            headers[name] = "<trace>"
    if "extra_headers" in out:
        out["extra_headers"] = headers
    return out


@pytest.mark.asyncio
async def test_a_declined_turn_sends_the_single_turns_primary_request() -> None:
    single = await _drive(
        _gateway(DecompositionStrategy.SINGLE, "conversational_always_single"), ["ok"]
    )
    asked = await _drive(_gateway(DecompositionStrategy.HYBRID, "planner_asked"), [_DECLINE, "ok"])

    assert len(single) == 1
    assert len(asked) == 2
    # The planner call comes first on the planner route, and it carries the decline rule.
    assert _PLANNER_DECLINE_RULE in asked[0]["messages"][0]["content"]
    assert _PLANNER_DECLINE_RULE not in str(single[0]["messages"])

    primary_single, primary_asked = _comparable(single[0]), _comparable(asked[1])
    assert set(primary_single) == set(primary_asked)
    differing = sorted(k for k in primary_single if primary_single[k] != primary_asked[k])
    assert differing == [], differing


def test_the_comparison_masks_only_the_allowed_values() -> None:
    """A seeded negative: any other difference is still seen."""
    base = {
        "model": "m",
        "client": object(),
        "extra_headers": {"x-trace-id": "a", "x-span-id": "b", "x-seshat-role": "primary"},
    }
    other = {
        "model": "m",
        "client": object(),
        "extra_headers": {"x-trace-id": "c", "x-span-id": "d", "x-seshat-role": "primary"},
    }
    assert _comparable(base) == _comparable(other)
    other["extra_headers"]["x-seshat-role"] = "planner"
    assert _comparable(base) != _comparable(other)
    assert _comparable(base) != _comparable({**base, "temperature": 0.7})
