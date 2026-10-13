"""FRE-1594 / ADR-0156 A5 (T-B): one draw of the reveal spike, and the five steps of AC-1.

A draw is a treatment session, then its control session.

- **Treatment.** Call 1 is the base request. If it loads the prompt's skill (step 1), the synthetic
  ``read_skill`` result goes back with the form's reveal. Call 2 must call the revealed tool with
  valid arguments (step 2), and the shim runs it (step 3). Call 3 carries the result (step 4) and
  must get a usable reply (step 5). A step that fails ends the session.
- **Control.** It runs only if step 1 held. It sends each treatment request again, with its own nonce
  and without the reveal. Its replies are discarded and its timings kept (AC-F3).

A reply that stopped on its length bound fails its step: production drops its content and its calls
(``executor.py:6411-6429``). A call to an experimental tool in the same reply as the ``read_skill``
that reveals it is refused, as A5.5 says.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from scripts.eval.fre1594.reveal import (
    Form,
    JsonDict,
    Prompt,
    Scenarios,
    Skill,
    assistant_message,
    revealed_tools,
    skill_result,
    with_nonce,
)
from scripts.eval.fre1594.shim import ToolRun, not_run
from scripts.eval.fre1594.transport import Reply, Transport, wire_messages

from personal_agent.llm_client.types import ToolCall

RunTool = Callable[[str, str, tuple[str, ...]], ToolRun]
"""Runs one call to an experimental tool: name, argument JSON, the tools revealed before the reply."""

PlaceTools = Callable[[Sequence[JsonDict], tuple[str, ...]], list[JsonDict]]
"""Returns the treatment's tools: the base tools and the revealed names (FRE-1597)."""

STEPS = 5


@dataclass(frozen=True)
class Draw:
    """One draw of one prompt in one form.

    Attributes:
        prompt: The prompt.
        form: The reveal form.
        draw: The draw number.
        base: The base request of the prompt (:func:`reveal.base_body`).
        scenarios: The scenarios.
        tools: The experimental tool definitions.
        skill_content: The ``read_skill`` result of both arms, in place of the synthetic one
            (FRE-1597: the production result of the final build). Simple form only.
        place_tools: The treatment's tools after a reveal, in place of appending the revealed
            definitions (FRE-1597: the production order). The control keeps the base tools.
    """

    prompt: Prompt
    form: Form
    draw: int
    base: JsonDict
    scenarios: Scenarios
    tools: Mapping[str, JsonDict]
    skill_content: str | None = None
    place_tools: PlaceTools | None = None

    def __post_init__(self) -> None:
        """Refuse a hook in the cache-safe form, whose reveal is in the skill result."""
        if self.form != "simple" and (self.skill_content or self.place_tools):
            raise ValueError("the final-build hooks apply to the simple form only")

    @property
    def skill(self) -> Skill:
        """The skill that the prompt needs."""
        return self.scenarios.skills[self.prompt.skill]


@dataclass(frozen=True)
class _State:
    """The transcript as it stood when one treatment request was sent."""

    messages: tuple[JsonDict, ...]
    slot: int | None
    revealed: tuple[str, ...]


class _Transcript:
    """The shared transcript of a draw. The skill result is rendered per arm."""

    def __init__(self, draw: Draw) -> None:
        self.draw = draw
        messages = draw.base["messages"]
        assert isinstance(messages, list)
        self.messages: list[JsonDict] = copy.deepcopy(messages)
        self.slot: int | None = None
        self.revealed: list[str] = []

    def state(self) -> _State:
        return _State(tuple(copy.deepcopy(self.messages)), self.slot, tuple(self.revealed))

    def render(self, state: _State, *, reveal: bool, nonce: str) -> JsonDict:
        draw = self.draw
        messages = copy.deepcopy(list(state.messages))
        if state.slot is not None:
            messages[state.slot]["content"] = draw.skill_content or skill_result(
                draw.scenarios, draw.skill, draw.form, reveal=reveal, tools=draw.tools
            )
        base_tools = draw.base["tools"]
        assert isinstance(base_tools, list)
        if draw.place_tools is not None:
            tools = (
                draw.place_tools(base_tools, state.revealed)
                if reveal and state.revealed
                else list(base_tools)
            )
        else:
            tools = revealed_tools(
                base_tools, draw.form, reveal=reveal, revealed=state.revealed, tools=draw.tools
            )
        return with_nonce({**draw.base, "messages": messages, "tools": tools}, nonce)

    def add_reply(self, reply: Reply, turn_id: int) -> list[str]:
        message = assistant_message(
            content=reply.content,
            reasoning=reply.reasoning,
            tool_calls=reply.tool_calls,
            turn_id=turn_id,
        )
        self.messages.append(message)
        calls = message.get("tool_calls") or []
        assert isinstance(calls, list)
        return [str(c["id"]) for c in calls]

    def add_result(self, call_id: str, name: str, content: str) -> None:
        self.messages.append(
            {"tool_call_id": call_id, "role": "tool", "name": name, "content": content}
        )


def _usable(reply: Reply) -> bool:
    return reply.ok and reply.finish_reason != "length"


def _arguments(call: ToolCall) -> object:
    try:
        return json.loads(call["arguments"])
    except (json.JSONDecodeError, TypeError):
        return None


def _skill_load(calls: Sequence[ToolCall], skill: str) -> int | None:
    for i, call in enumerate(calls):
        args = _arguments(call)
        if call["name"] == "read_skill" and isinstance(args, dict) and args.get("name") == skill:
            return i
    return None


def _carried(body: Mapping[str, object], call_id: str, content: str) -> bool:
    """Step 4: the sent history holds the call and its result, after the production sanitiser."""
    messages = body["messages"]
    assert isinstance(messages, list)
    sent = wire_messages(messages)
    called = any(
        call.get("id") == call_id
        for m in sent
        if m.get("role") == "assistant"
        for call in (m.get("tool_calls") or [])  # type: ignore[attr-defined]
    )
    answered = any(
        m.get("role") == "tool" and m.get("tool_call_id") == call_id and m.get("content") == content
        for m in sent
    )
    return called and answered


def _well_formed(call: ToolCall) -> bool:
    return bool(call["name"]) and isinstance(_arguments(call), dict)


def run_draw(
    transport: Transport, draw: Draw, run_tool: RunTool, *, nonces: tuple[str, str]
) -> dict[str, object]:
    """Run one draw: the treatment session, then its control.

    Args:
        transport: Sends the requests.
        draw: The draw.
        run_tool: Runs a call to an experimental tool through the shim.
        nonces: The treatment nonce and the control nonce.

    Returns:
        The row: the five steps, the revealed call and its run, and every call of both arms.
    """
    treatment_nonce, control_nonce = nonces
    transcript = _Transcript(draw)
    states: list[_State] = []
    replies: list[Reply] = []
    steps = [False] * STEPS
    revealed_call: dict[str, str] | None = None
    tool_run: dict[str, object] | None = None
    tool = draw.skill.tool

    def send() -> Reply:
        states.append(transcript.state())
        reply = transport.send(transcript.render(states[-1], reveal=True, nonce=treatment_nonce))
        replies.append(reply)
        return reply

    def answer(
        reply: Reply,
        ids: Sequence[str],
        handled: int,
        content: str | None,
        revealed: tuple[str, ...],
    ) -> None:
        """Answer every call of a reply in call order. ``content`` None marks the skill slot."""
        for i, call in enumerate(reply.tool_calls):
            if i == handled:
                if content is None:
                    transcript.slot = len(transcript.messages)
                transcript.add_result(ids[i], call["name"], content or "")
            elif call["name"] in draw.tools:
                transcript.add_result(
                    ids[i],
                    call["name"],
                    run_tool(call["name"], call["arguments"], revealed).content,
                )
            else:
                transcript.add_result(ids[i], call["name"], not_run(call["name"]))

    # Call 1 and step 1: the model loads the prompt's skill.
    first = send()
    load = _skill_load(first.tool_calls, draw.skill.name) if _usable(first) else None
    if load is not None:
        steps[0] = True
        ids = transcript.add_reply(first, turn_id=0)
        answer(first, ids, load, None, ())  # the skill result is rendered per arm
        transcript.revealed.append(tool)

    # Call 2, steps 2 and 3: the revealed call, with valid arguments, runs.
    if steps[0]:
        second = send()
        target = next(
            (i for i, c in enumerate(second.tool_calls) if c["name"] == tool),
            None,
        )
        if _usable(second) and target is not None:
            call = second.tool_calls[target]
            ids = transcript.add_reply(second, turn_id=1)
            run = run_tool(tool, call["arguments"], tuple(transcript.revealed))
            revealed_call = {"name": tool, "arguments": call["arguments"]}
            tool_run = {"valid": run.valid, "ran": run.ran, "code": run.code}
            steps[1], steps[2] = run.valid, run.ran
            answer(second, ids, target, run.content, tuple(transcript.revealed))

            # Call 3, steps 4 and 5: the result goes back, and the provider accepts it.
            if steps[2]:
                body = transcript.render(transcript.state(), reveal=True, nonce=treatment_nonce)
                steps[3] = _carried(body, ids[target], run.content)
                third = send()
                steps[4] = (
                    steps[3]
                    and _usable(third)
                    and (
                        bool(third.content.strip())
                        or (
                            bool(third.tool_calls)
                            and all(_well_formed(c) for c in third.tool_calls)
                        )
                    )
                )

    control: list[Reply] = []
    if steps[0]:
        for state in states:
            control.append(
                transport.send(transcript.render(state, reveal=False, nonce=control_nonce))
            )

    failed = next((i + 1 for i, held in enumerate(steps) if not held), None)
    return {
        "prompt_id": draw.prompt.id,
        "skill": draw.skill.name,
        "form": draw.form,
        "draw": draw.draw,
        "nonce": treatment_nonce,
        "control_nonce": control_nonce,
        "steps": steps,
        "passed": failed is None,
        "failed_step": failed,
        "first_call_tools": [c["name"] for c in first.tool_calls],
        "revealed_call": revealed_call,
        "tool_run": tool_run,
        "treatment": [r.record() for r in replies],
        "control": [r.record() for r in control],
    }
