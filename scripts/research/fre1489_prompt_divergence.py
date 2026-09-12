"""FRE-1489 — locate the first differing byte between consecutive tool-loop prompts.

The AC-1 instrument. It drives the real ``step_llm_call`` the way the tool loop does,
renders each call's wire messages through a chat template, and checks that each prompt is a
strict forward extension of the one before: the first difference must fall at the end of the
previous prompt, where the model's own generation continues. An earlier difference is a
mid-sequence rewrite, which the local backend cannot reuse a KV cache across (ADR-0081 D2).

Exits nonzero when a prompt diverges early, so the instrument reports a verdict rather than
only printing bytes.

Usage::

    uv run python -m scripts.research.fre1489_prompt_divergence \
        --template /path/to/qwen3.6-unsloth.jinja [--shape hybrid|plain] [--rounds 3]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

QUERY = "I will visit Mallorca Sept 19th. What events are on?"
SYNTHESIS = (
    "## Sub-agent results\n- worker 1: found the harvest festival\n"
    "Synthesize the results into a coherent response for the user's original question."
)
TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
}


def render(template: Path, wire: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> str:
    """Render one call's wire messages through a chat template.

    Args:
        template: Path to the Jinja chat template the server uses.
        wire: Wire-form message list for one call.
        tools: Tool definitions sent with that call, if any.

    Returns:
        The serialized prompt the server would build for that call.
    """
    import jinja2

    env = jinja2.Environment(extensions=["jinja2.ext.loopcontrols"], autoescape=False)
    env.filters["tojson"] = lambda value, **_: json.dumps(value, ensure_ascii=False)
    messages = json.loads(json.dumps(wire))
    for message in messages:
        # llama.cpp parses OpenAI string arguments into objects before templating.
        for call in message.get("tool_calls") or []:
            arguments = call["function"]["arguments"]
            if isinstance(arguments, str):
                call["function"]["arguments"] = json.loads(arguments)
    return env.from_string(template.read_text()).render(
        messages=messages, tools=tools, add_generation_prompt=True
    )


def first_difference(earlier: str, later: str) -> int:
    """Return the index of the first differing byte, or the shared length if none differs."""
    for index, (left, right) in enumerate(zip(earlier, later, strict=False)):
        if left != right:
            return index
    return min(len(earlier), len(later))


async def capture(shape: str, rounds: int) -> list[tuple[list[dict[str, Any]], Any]]:
    """Drive the real executor loop and capture each call's wire form.

    Args:
        shape: ``"hybrid"`` to append the expansion synthesis message, else ``"plain"``.
        rounds: Number of tool-loop iterations to drive.

    Returns:
        One ``(wire_messages, tools)`` pair per call, in call order.
    """
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.executor import build_wire_messages, step_llm_call
    from personal_agent.orchestrator.types import ExecutionContext
    from personal_agent.telemetry.trace import TraceContext

    messages: list[dict[str, Any]] = [{"role": "user", "content": QUERY}]
    if shape == "hybrid":
        messages.append({"role": "user", "content": SYNTHESIS})
    ctx = ExecutionContext(
        session_id="fre1489",
        trace_id="fre1489",
        user_message=QUERY,
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=messages,
    )
    ctx.turn_started_at = datetime(2026, 9, 10, 15, 18, tzinfo=UTC)
    ctx.salient_highlights = "<salient>the trip is in September</salient>"

    client = MagicMock()
    client.model_configs = {}
    session = MagicMock()
    session.add_message = AsyncMock()
    session.get_messages = AsyncMock(return_value=[])

    captured: list[tuple[list[dict[str, Any]], Any]] = []
    for index in range(rounds):
        client.respond = AsyncMock(
            return_value={
                "content": "",
                "tool_calls": [
                    {"id": f"c{index}", "name": "web_search", "arguments": '{"query": "x"}'}
                ],
                "reasoning_trace": f"reasoning for round {index}",
                "response_id": None,
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )
        with (
            patch("personal_agent.llm_client.factory.get_llm_client", return_value=client),
            patch(
                "personal_agent.orchestrator.executor.get_default_registry",
                return_value=MagicMock(
                    get_tool_definitions_for_llm=MagicMock(return_value=[TOOL_DEF])
                ),
            ),
        ):
            await step_llm_call(ctx, session, TraceContext.new_trace())
        kwargs = client.respond.call_args.kwargs
        captured.append(
            (
                build_wire_messages(kwargs["messages"], kwargs.get("system_prompt"), "fre1489"),
                kwargs.get("tools"),
            )
        )
        for call in ctx.messages[-1].get("tool_calls") or []:
            ctx.messages.append(
                {
                    "tool_call_id": call["id"],
                    "role": "tool",
                    "name": "web_search",
                    "content": f"tool result {index}",
                }
            )
        ctx.tool_iteration_count += 1
    return captured


def main() -> int:
    """Render the captured calls, report each divergence, and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, type=Path, help="Jinja chat template path")
    parser.add_argument("--shape", choices=("hybrid", "plain"), default="hybrid")
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()

    captured = asyncio.run(capture(args.shape, args.rounds))
    prompts = [render(args.template, wire, tools) for wire, tools in captured]

    early = 0
    for index in range(len(prompts) - 1):
        earlier, later = prompts[index], prompts[index + 1]
        diff = first_difference(earlier, later)
        forward = diff == len(earlier)
        verdict = "forward extension" if forward else "EARLY DIVERGENCE"
        print(
            f"call {index} -> {index + 1}: prompt_chars={len(earlier)} first_diff={diff} "
            f"({len(earlier) - diff} bytes before the end) — {verdict}"
        )
        if not forward:
            early += 1
            print(f"    was: {earlier[max(0, diff - 80) : diff + 160]!r}")
            print(f"    now: {later[max(0, diff - 80) : diff + 160]!r}")

    if early:
        print(f"\nFAIL: {early} of {len(prompts) - 1} transitions rewrote earlier bytes.")
        return 1
    print(f"\nOK: all {len(prompts) - 1} transitions are strict forward extensions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
