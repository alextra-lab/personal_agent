"""FRE-1600 AC-3 (ADR-0140 T2) — a stored session replays untrusted content only as tool results.

FRE-1360 probes one turn. This probe runs two real orchestrator turns on one session. Turn 1
recalls a memory marker and calls a tool that returns a tool marker. Between the turns the
session is stored and loaded as the service does it, so turn 2's requests are built from the
stored session. In every turn-2 request, each marker may appear only in a tool result that
answers an earlier tool call: never in a system, user or assistant message.

The service's own write path is inline code in ``service/app.py`` that this test cannot call.
``_store_turn`` and ``_load_session`` mirror it, with the lines cited.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.orchestrator.untrusted_channel import MEMORY_RECALL_TOOL
from tests.personal_agent.orchestrator.test_fre1360_untrusted_input_channel import (
    _message_text,
    _registry_with,
)
from tests.personal_agent.orchestrator.test_fre1489_volatile_duplication import _episode

_SESSION = "fre-1600-stored-session"
_TOOL = "fre1600_probe"
_T1_MEMORY = "FRE1600-T1-MEMORY-MARKER-J5"
_T1_TOOL = "FRE1600-T1-TOOL-MARKER-W3"
_T2_MEMORY = "FRE1600-T2-MEMORY-MARKER-P8"
_MARKERS = (_T1_MEMORY, _T1_TOOL, _T2_MEMORY)


def _gateway(user_message: str, memory: list[dict[str, Any]], trace_id: str) -> Any:
    from personal_agent.captains_log.turn_evidence import build_recall_candidates
    from personal_agent.governance.models import Mode
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

    return GatewayOutput(
        intent=IntentResult(
            task_type=TaskType.CONVERSATIONAL,
            complexity=Complexity.SIMPLE,
            confidence=0.9,
            signals=[],
        ),
        governance=GovernanceContext(mode=Mode.NORMAL, expansion_permitted=False),
        decomposition=DecompositionResult(strategy=DecompositionStrategy.SINGLE, reason="test"),
        context=AssembledContext(
            messages=[{"role": "user", "content": user_message}],
            memory_context=memory,
            tool_definitions=None,
            recall_candidates=build_recall_candidates(memory, {}),
        ),
        session_id=_SESSION,
        trace_id=trace_id,
    )


async def _turn(
    orchestrator: Any,
    user_message: str,
    memory: list[dict[str, Any]],
    trace_id: str,
    *,
    call_tool: bool,
) -> tuple[str, list[list[dict[str, Any]]]]:
    """One real orchestrator turn. With ``call_tool``, the model calls the probe tool once.

    Returns:
        The reply, and the wire form of every model call of the turn, in order.
    """
    from personal_agent.governance.models import Mode
    from personal_agent.governance.permissions import PermissionRules, parse_rule
    from personal_agent.orchestrator import Channel
    from personal_agent.orchestrator.executor import build_wire_messages
    from personal_agent.tools.executor import ToolExecutionLayer
    from tests.test_orchestrator.conftest import configure_mock_llm_client_model_configs

    async def _probe(**_kwargs: object) -> dict[str, str]:
        return {"text": f"probe result {_T1_TOOL}"}

    wires: list[list[dict[str, Any]]] = []

    async def _respond(**kwargs: Any) -> dict[str, Any]:
        wires.append(
            build_wire_messages(list(kwargs["messages"]), kwargs.get("system_prompt"), "t")
        )
        calls = []
        if call_tool and len(wires) == 1:
            calls = [{"id": "probe_1", "name": _TOOL, "arguments": json.dumps({})}]
        return {
            "role": "assistant",
            "content": "" if calls else "Done.",
            "tool_calls": calls,
            "reasoning_trace": None,
            "usage": {"total_tokens": 10},
            "raw": {},
        }

    client = AsyncMock()
    client.respond = AsyncMock(side_effect=_respond)
    configure_mock_llm_client_model_configs(client)
    with (
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=client),
        patch(
            "personal_agent.orchestrator.executor._get_tool_execution_layer",
            return_value=ToolExecutionLayer(_registry_with(_TOOL, _probe)),
        ),
        patch(
            "personal_agent.governance.permissions.load_permission_rules",
            return_value=PermissionRules(allow=(parse_rule(_TOOL),)),
        ),
    ):
        result = await orchestrator.handle_user_request(
            session_id=_SESSION,
            user_message=user_message,
            mode=Mode.NORMAL,
            channel=Channel.CHAT,
            trace_id=trace_id,
            gateway_output=_gateway(user_message, memory, trace_id),
        )
    return str(result.get("reply", "")), wires


def _store_turn(stored: list[dict[str, Any]], user_message: str, reply: str, trace_id: str) -> None:
    """Append the turn as the service stores it.

    ``service/app.py:421-428``: the raw user text. ``service/app.py:562-576``: the reply.
    """
    now = datetime.now(UTC).isoformat()
    stored.append(
        {
            "role": "user",
            "content": user_message,
            "trace_id": trace_id,
            "timestamp": now,
            "metadata": {"source": "service.app"},
        }
    )
    stored.append(
        {
            "role": "assistant",
            "content": reply,
            "trace_id": trace_id,
            "timestamp": now,
            "metadata": {"source": "service.app", "model_role": "primary"},
        }
    )


def _load_session(orchestrator: Any, stored: Sequence[dict[str, Any]]) -> None:
    """Load the stored session as the service does it before a turn (``service/app.py:495-500``)."""
    from personal_agent.config import settings
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator import Channel

    max_history = settings.conversation_max_history_messages
    prior = list(stored)[-max_history:] if max_history > 0 else list(stored)
    manager = orchestrator.session_manager
    if manager.get_session(_SESSION) is None:
        manager.create_session(Mode.NORMAL, Channel.CHAT, session_id=_SESSION)
    manager.update_session(_SESSION, messages=prior)


def _assert_only_in_tool_results(wire: Sequence[Mapping[str, Any]], marker: str) -> int:
    """Every message that holds ``marker`` is a tool result answering an earlier call.

    Returns:
        The number of tool results that hold the marker.
    """
    holders = [m for m in wire if marker in _message_text(m)]
    for message in holders:
        assert message.get("role") == "tool", (
            f"marker {marker!r} reached a later request in a {message.get('role')!r} message: "
            f"{_message_text(message)[:300]}"
        )
        index = list(wire).index(message)
        issued = [
            tc.get("id")
            for m in list(wire)[:index]
            if m.get("role") == "assistant"
            for tc in m.get("tool_calls") or []
        ]
        assert message.get("tool_call_id") in issued, "the tool result answers no earlier call"
    return len(holders)


async def _replay_two_turns() -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """Turn 1 (memory + tool), store and load the session, then turn 2 (memory only).

    Returns:
        The stored session after turn 1, and the wire form of every turn-2 model call.
    """
    from personal_agent.orchestrator import Orchestrator

    orchestrator = Orchestrator()
    stored: list[dict[str, Any]] = []

    _load_session(orchestrator, stored)
    first = "Check the switch firmware."
    reply, wires_1 = await _turn(
        orchestrator,
        first,
        [_episode("conv-1", f"the owner said {_T1_MEMORY}")],
        "fre1600-t1",
        call_tool=True,
    )
    # The probe is live: turn 1 sent both markers, and only as tool results.
    assert _assert_only_in_tool_results(wires_1[0], _T1_MEMORY) == 1
    assert _assert_only_in_tool_results(wires_1[-1], _T1_TOOL) == 1
    _store_turn(stored, first, reply, "fre1600-t1")

    _load_session(orchestrator, stored)
    _, wires_2 = await _turn(
        orchestrator,
        "And the fan?",
        [_episode("conv-2", f"the owner said {_T2_MEMORY}")],
        "fre1600-t2",
        call_tool=False,
    )
    return stored, wires_2


class TestAc3StoredSessionReplay:
    """Turn 2 is built from the stored session of turn 1."""

    @pytest.mark.asyncio
    async def test_later_request_carries_memory_and_tool_output_only_as_tool_results(
        self,
    ) -> None:
        """AC-3: after a stored turn, untrusted content reaches turn 2 only as tool results."""
        _, wires_2 = await _replay_two_turns()
        assert wires_2, "turn 2 made no model call"
        for wire in wires_2:
            for marker in _MARKERS:
                _assert_only_in_tool_results(wire, marker)
        # Turn 2's own recall reaches the model, as a memory_recall result.
        holders = [m for m in wires_2[0] if _T2_MEMORY in _message_text(m)]
        assert [m.get("name") for m in holders] == [MEMORY_RECALL_TOOL]

    @pytest.mark.asyncio
    async def test_the_stored_session_is_replayed_into_turn_two(self) -> None:
        """The probe reads a request built from the stored session, not a fresh one."""
        stored, wires_2 = await _replay_two_turns()
        texts = [_message_text(m) for m in wires_2[0]]
        for row in stored:
            assert any(str(row["content"]) in t for t in texts), (
                f"stored {row['role']} message never reached turn 2: {row['content']!r}"
            )

    def test_check_fails_when_a_stored_message_carries_untrusted_content(self) -> None:
        """Seeded negative: a turn-1 memory line stored in the reply text fails the check."""
        wire = [
            {"role": "user", "content": "Check the switch firmware."},
            {"role": "assistant", "content": f"Noted: {_T1_MEMORY}"},
            {"role": "user", "content": "And the fan?"},
        ]
        with pytest.raises(AssertionError, match="'assistant' message"):
            _assert_only_in_tool_results(wire, _T1_MEMORY)
