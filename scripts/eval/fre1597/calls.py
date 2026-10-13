"""FRE-1597 / ADR-0156 A5 (T-D1): a capture of two primary calls, and the call 2 of a replay.

The capture scripts the stub to answer the first primary request with one ``read_skill`` call
(``stub.py``, ``next_replies.json``). The gateway then runs the real ``read_skill``, applies the real
reveal and sends call 2. So call 2 is the production body: the ``read_skill`` result with its
``_citation`` field, and the ``tools`` array with the revealed tool where the registry puts it.

:meth:`TwoCalls.from_capture` refuses a capture that is not exactly "call 1, then call 1's messages
plus the assistant message and its tool result". :meth:`TwoCalls.second_request` swaps the stub's
assistant message for the model's real reply. A reply with any other call is not continued
(:func:`skip_reason`): production would run that call, and the harness cannot.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scripts.eval.fre1537.capture import last_user_text
from scripts.eval.fre1594.reveal import assistant_message, tool_name
from scripts.eval.fre1594.transport import Reply, wire_messages

from personal_agent.orchestrator.skills import tools_revealed_by

JsonDict = dict[str, object]

_CALL_ID = re.compile(r"^call_t(\d+)_0_(.+)$")


class FidelityError(Exception):
    """A capture is not the production request sequence that the harness expects."""


def primary_calls(call_files: Sequence[Path], prompt: str) -> list[JsonDict]:
    """Return the primary requests of one turn, in the order the stub received them.

    Args:
        call_files: The stub call files of the turn, oldest first.
        prompt: The turn's user prompt.

    Returns:
        Each body that carries ``tools`` and whose last user message holds the prompt.
    """
    out: list[JsonDict] = []
    for path in call_files:
        body = json.loads(path.read_text())["body"]
        if body.get("tools") and prompt in last_user_text(body):
            out.append(body)
    return out


def _messages(body: Mapping[str, object]) -> list[JsonDict]:
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise FidelityError("a request has no messages list")
    return cast(list[JsonDict], messages)


def _tools(body: Mapping[str, object]) -> list[JsonDict]:
    tools = body.get("tools")
    if not isinstance(tools, list):
        raise FidelityError("a request has no tools list")
    return cast(list[JsonDict], tools)


def _skill_of(arguments: object) -> str | None:
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        return None
    name = args.get("name") if isinstance(args, Mapping) else None
    return name if isinstance(name, str) else None


@dataclass(frozen=True)
class TwoCalls:
    """The captured primary calls of one item.

    Attributes:
        first: The first primary request.
        second: The request after the scripted ``read_skill``, or None for an item with no skill.
        skill: The scripted skill, or None.
        skill_message: The tool message of the ``read_skill`` result, as the gateway sent it.
        turn_id: The round number of the call ids in the assistant message (``executor.py:6665``).
        added_tools: The tools that call 2 holds and call 1 does not, in call 2's order.
    """

    first: JsonDict
    second: JsonDict | None = None
    skill: str | None = None
    skill_message: JsonDict | None = None
    turn_id: int | None = None
    added_tools: tuple[str, ...] = ()

    @classmethod
    def from_capture(cls, bodies: Sequence[JsonDict], skill: str | None) -> TwoCalls:
        """Check a capture and split it.

        Args:
            bodies: The primary requests of the turn (:func:`primary_calls`).
            skill: The skill that the stub's ``read_skill`` call named, or None.

        Returns:
            The two calls.

        Raises:
            FidelityError: The capture is not the expected production sequence.
        """
        expected = 1 if skill is None else 2
        if len(bodies) != expected:
            raise FidelityError(f"{len(bodies)} primary calls, expected {expected}")
        first = bodies[0]
        if skill is None:
            return cls(first=first)
        second = bodies[1]

        changed = sorted(
            k
            for k in (set(first) | set(second)) - {"messages", "tools"}
            if first.get(k) != second.get(k)
        )
        if changed:
            raise FidelityError(f"call 2 changes keys other than messages and tools: {changed}")

        head, tail = _messages(first), _messages(second)
        if len(tail) != len(head) + 2 or tail[: len(head)] != head:
            raise FidelityError("call 2's messages are not call 1's messages plus two")
        assistant, result = tail[-2], tail[-1]
        calls = assistant.get("tool_calls")
        if assistant.get("role") != "assistant" or not isinstance(calls, list) or len(calls) != 1:
            raise FidelityError("call 2's assistant message must hold exactly one tool call")
        call = cast(JsonDict, calls[0])
        function = cast(Mapping[str, object], call.get("function") or {})
        loaded = _skill_of(function.get("arguments"))
        if function.get("name") != "read_skill" or loaded != skill:
            raise FidelityError(f"the scripted call loaded {loaded!r}, expected {skill!r}")
        match = _CALL_ID.match(str(call.get("id")))
        if match is None:
            raise FidelityError(f"the call id {call.get('id')!r} is not call_t<n>_0_<id>")
        turn_id, stub_id = int(match.group(1)), match.group(2)
        if (
            result.get("role") != "tool"
            or result.get("tool_call_id") != call.get("id")
            or result.get("name") != "read_skill"
        ):
            raise FidelityError("call 2's last message is not the read_skill result")
        rebuilt = assistant_message(
            content=str(assistant.get("content") or ""),
            reasoning=cast(str | None, assistant.get("reasoning_content")),
            tool_calls=[
                {
                    "id": stub_id,
                    "name": "read_skill",
                    "arguments": str(function.get("arguments")),
                }
            ],
            turn_id=turn_id,
        )
        if wire_messages([rebuilt, result]) != [assistant, result]:
            raise FidelityError("the captured assistant message is not the production rebuild")

        names1 = [tool_name(t) for t in _tools(first)]
        names2 = [tool_name(t) for t in _tools(second)]
        added = tuple(n for n in names2 if n not in names1)
        kept = [t for t in _tools(second) if tool_name(t) not in added]
        if kept != _tools(first) or set(added) != tools_revealed_by(skill):
            raise FidelityError(
                f"call 2's tools add {list(added)}; {skill} reveals "
                f"{sorted(tools_revealed_by(skill))}"
            )
        return cls(
            first=first,
            second=second,
            skill=skill,
            skill_message=result,
            turn_id=turn_id,
            added_tools=added,
        )

    def second_request(self, reply: Reply) -> JsonDict:
        """Build call 2 of a replay: call 1, the model's reply, the captured ``read_skill`` result.

        Args:
            reply: The model's reply to call 1.

        Returns:
            Call 2's body, with every key except ``messages`` taken from the captured call 2.

        Raises:
            ValueError: If :func:`skip_reason` gives a reason.
        """
        reason = skip_reason(self, reply)
        if reason is not None:
            raise ValueError(reason)
        assert self.second is not None and self.skill_message is not None
        assert self.turn_id is not None
        assistant = assistant_message(
            content=reply.content,
            reasoning=reply.reasoning,
            tool_calls=reply.tool_calls,
            turn_id=self.turn_id,
        )
        ids = cast(list[JsonDict], assistant["tool_calls"])
        result = {**self.skill_message, "tool_call_id": ids[0]["id"]}
        return {**self.second, "messages": [*_messages(self.first), assistant, result]}


def skip_reason(two: TwoCalls, reply: Reply) -> str | None:
    """Say why call 2 is not sent after a reply, or return None.

    Call 2 follows only a usable reply whose one tool call is ``read_skill`` of the scripted skill.

    Args:
        two: The captured calls.
        reply: The model's reply to call 1.

    Returns:
        The reason, or None.
    """
    if two.second is None:
        return "no skill was scripted for this item"
    if not reply.ok:
        return "call 1 failed"
    if reply.finish_reason == "length":
        return "call 1 stopped on its length bound"
    if len(reply.tool_calls) != 1:
        return f"call 1 made {len(reply.tool_calls)} tool calls"
    call = reply.tool_calls[0]
    if call["name"] != "read_skill":
        return f"call 1 called {call['name']}"
    loaded = _skill_of(call["arguments"])
    if loaded != two.skill:
        return f"call 1 loaded {loaded!r}, not {two.skill!r}"
    return None
