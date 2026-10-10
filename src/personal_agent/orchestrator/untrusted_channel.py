"""The tool-result channel for untrusted input the harness itself delivers (FRE-1360).

ADR-0140 T2 declares tool results, retrieved pages, MCP responses and knowledge-graph
content untrusted, and untrusted content belongs in tool-result blocks — never in a
system prompt or a plain user text block — because the model is trained to read
instructions inside a tool result with scepticism. Real tool calls already arrive that
way. Two inputs did not, because the harness produces them without a model tool call:

* recalled memory (``memory_section``), which rode the ``<turn_context>`` fence inlined
  into the user's own message; and
* HYBRID worker reports (``synthesis_context``), appended as a ``user`` message.

A *harness tool exchange* gives each of them the same shape a real tool call has: one
assistant message carrying one ``tool_calls`` entry, then the ``role: "tool"`` message that
answers it. The model did not make the call. Neither tool name is registered, so a model
that tries to call one gets the ordinary unknown-tool result.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

MEMORY_RECALL_TOOL = "memory_recall"
"""Tool name on the exchange that carries the turn's recalled memory."""

WORKER_REPORTS_TOOL = "worker_reports"
"""Tool name on the exchange that carries a HYBRID turn's worker reports."""

HarnessCallKind = Literal["mem", "wrk"]

_CALL_ID_PREFIXES: tuple[str, ...] = ("call_mem_", "call_wrk_")


def harness_call_id(kind: HarnessCallKind, trace_id: str) -> str:
    """Return the tool-call id for one harness exchange of this turn.

    Unique per turn (31 characters of the trace id, 124 bits of a hex trace id) and per
    kind, deterministic within the turn, and at most 40 characters of ``[A-Za-z0-9_-]`` —
    OpenAI's id cap and Anthropic's id alphabet. Prior turns' exchanges carry their own
    trace ids, so this id also anchors the exchange to *this* turn in the evidence record.

    Args:
        kind: ``"mem"`` for recalled memory, ``"wrk"`` for worker reports.
        trace_id: The turn's trace id.

    Returns:
        The call id.
    """
    safe = "".join(c for c in trace_id if c.isalnum() or c in "_-")[:31]
    return f"call_{kind}_{safe}"


def harness_tool_exchange(
    *, call_id: str, tool_name: str, content: str
) -> tuple[dict[str, object], dict[str, object]]:
    """Build the assistant tool call and the tool result that answers it.

    Args:
        call_id: From :func:`harness_call_id`.
        tool_name: :data:`MEMORY_RECALL_TOOL` or :data:`WORKER_REPORTS_TOOL`.
        content: The untrusted text, carried byte for byte.

    Returns:
        ``(assistant_message, tool_message)``, in the order they are appended.
    """
    call: dict[str, object] = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": "{}"},
            }
        ],
    }
    result: dict[str, object] = {
        "tool_call_id": call_id,
        "role": "tool",
        "name": tool_name,
        "content": content,
    }
    return call, result


def is_harness_tool_result(message: Mapping[str, object]) -> bool:
    """Whether *message* is the tool result of a harness exchange.

    Args:
        message: Any message dict.

    Returns:
        True for the ``role: "tool"`` half of a harness exchange.
    """
    if message.get("role") != "tool":
        return False
    return str(message.get("tool_call_id") or "").startswith(_CALL_ID_PREFIXES)
