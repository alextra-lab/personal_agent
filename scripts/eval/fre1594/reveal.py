"""FRE-1594 / ADR-0156 A5 (T-B): the requests of the reveal spike.

Pure functions. Nothing here sends a request or runs a tool.

- :func:`experimental_tools` builds ``search_logs`` and ``measure_model_latency`` (A5.1) from the
  parameters of ``query_telemetry``, in the wire format that the gateway sends.
- :func:`base_body` turns a captured primary request into the spike's first request: no
  ``query_telemetry``, the two A4 catalog lines, and the prompt's text.
- :func:`skill_result` builds the synthetic ``read_skill`` result of a form, with or without the reveal.
- :func:`render_request` builds the request of one call of one arm from the shared transcript. The
  treatment and the control of a form differ only in the reveal and the nonce (AC-F3).
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml
from scripts.eval.fre1537.replay import nonce_line

from personal_agent.llm_client.types import ToolCall
from personal_agent.orchestrator.executor import _build_assistant_tool_calls
from personal_agent.tools.contract import render_content
from personal_agent.tools.telemetry_query import (
    ALLOWED_FIELDS,
    FAMILY_TIME_FIELD,
    TEMPO_GROUP_KEYS,
    TEMPO_SPANS,
    query_telemetry_tool,
)
from personal_agent.tools.types import ToolParameter

SCENARIOS = Path(__file__).resolve().parent / "scenarios.yaml"
Form = Literal["cache_safe", "simple"]
FORMS: tuple[Form, ...] = ("cache_safe", "simple")
REMOVED_TOOL = "query_telemetry"
TURN_CONTEXT_END = "</turn_context>\n\n"
SEARCH_ARGS = ("action", "index", "query", "aggs", "fields", "size", "since")
LATENCY_ARGS = ("since", "span", "group_by", "role", "model", "limit")

JsonDict = dict[str, object]


@dataclass(frozen=True)
class Skill:
    """A synthetic skill of the spike.

    Attributes:
        name: The skill name (A4).
        replaces: The catalog name whose line this skill's line replaces.
        tool: The experimental tool that the skill reveals.
        catalog: The catalog description.
        body: The skill body. ``{reveal_note}`` marks where the form's note goes.
    """

    name: str
    replaces: str
    tool: str
    catalog: str
    body: str


@dataclass(frozen=True)
class Prompt:
    """One prompt of the population.

    Attributes:
        id: ``<skill>#<n>``.
        skill: The skill that the prompt needs.
        text: The user text.
    """

    id: str
    skill: str
    text: str


@dataclass(frozen=True)
class Scenarios:
    """The content of ``scenarios.yaml``.

    Attributes:
        skills: The skills, by name.
        prompts: The prompts, in file order.
        reveal_notes: The note of each form. ``{tool}`` marks the tool name.
    """

    skills: Mapping[str, Skill]
    prompts: Sequence[Prompt]
    reveal_notes: Mapping[str, str]


def load_scenarios(path: Path = SCENARIOS) -> Scenarios:
    """Load the skills and the prompts.

    Args:
        path: The scenarios file.

    Returns:
        The scenarios.

    Raises:
        ValueError: A prompt names an unknown skill, or a form has no note.
    """
    raw = yaml.safe_load(path.read_text())
    skills = {name: Skill(name=name, **entry) for name, entry in raw["skills"].items()}
    prompts = [Prompt(**entry) for entry in raw["prompts"]]
    unknown = sorted({p.skill for p in prompts} - set(skills))
    if unknown:
        raise ValueError(f"prompts name unknown skills: {unknown}")
    notes = dict(raw["reveal_notes"])
    if set(notes) != set(FORMS):
        raise ValueError(f"reveal_notes must name exactly the forms {FORMS}")
    return Scenarios(skills=skills, prompts=prompts, reveal_notes=notes)


# --------------------------------------------------------------------------- the two tools


def _param(name: str) -> ToolParameter:
    return next(p for p in query_telemetry_tool.parameters if p.name == name)


def _property(param: ToolParameter) -> JsonDict:
    # The same rule as ToolRegistry.get_tool_definitions_for_llm.
    if param.json_schema:
        return dict(param.json_schema)
    return {"type": param.type, "description": param.description}


def _definition(
    name: str, description: str, properties: Mapping[str, object], required: list[str]
) -> JsonDict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": dict(properties), "required": required},
        },
    }


def _search_description() -> str:
    families = [
        f"- {family} fields: {', '.join(sorted(fields))}."
        for family, fields in ALLOWED_FIELDS.items()
    ]
    odd = [f"{f} uses {t}" for f, t in FAMILY_TIME_FIELD.items() if t != "@timestamp"]
    return "\n".join(
        [
            "Count, break down or find log events in the system's own Elasticsearch indices. "
            "Read-only. Never use it for the user's conversation text: that is not available.",
            "Actions: 'search' (hits and aggregations) and 'count' (a document count).",
            "Index families and the only fields each accepts:",
            *families,
            f"'since' filters on @timestamp{', except ' + ', '.join(odd) if odd else ''}. "
            "Default 24h, at most 30d.",
            "Query operators: match_all, bool, term, terms, match, match_phrase, prefix, range, "
            "exists. Aggregations: terms, date_histogram, avg, sum, min, max, value_count, "
            "cardinality, stats, percentiles.",
            "A name outside these lists is refused. Not for model-call latency: "
            "measure_model_latency gives that.",
            'Example: search_logs(action="count", index="agent-logs", '
            'query={"term": {"level": "ERROR"}}, since="24h").',
        ]
    )


def _latency_description() -> str:
    return "\n".join(
        [
            "Measure model-call latency from Tempo trace spans. Read-only.",
            f"Spans: {', '.join(TEMPO_SPANS)}. model_call is one span per model call; slm_chat is "
            "the local model server's side and adds prefill and decode time.",
            f"group_by keys: {', '.join(TEMPO_GROUP_KEYS)}. Filter with 'role' and 'model' "
            "(exact values). 'since' is required, 1m to 7d.",
            "It returns, per group, count, errors, p50_ms, p90_ms, max_ms and token sums over the "
            "spans it read. complete=false means a cap was hit and the numbers cover a sample.",
            "Not for counts or searches of log events: search_logs gives that.",
            'Example: measure_model_latency(since="24h", group_by=["model"]).',
        ]
    )


def experimental_tools() -> dict[str, JsonDict]:
    """Build the two experimental tool definitions of A5.1.

    The parameters are those of ``query_telemetry``, split by its own argument sets. ``search_logs``
    keeps ``action``, limited to ``search`` and ``count``. ``measure_model_latency`` has no
    ``action`` and requires ``since``.

    Returns:
        The OpenAI-format definitions, by tool name.
    """
    search = {name: _property(_param(name)) for name in SEARCH_ARGS}
    search["action"] = {
        "type": "string",
        "enum": ["search", "count"],
        "description": "'search' (hits and aggregations) or 'count' (a document count).",
    }
    latency = {name: _property(_param(name)) for name in LATENCY_ARGS}
    return {
        "search_logs": _definition(
            "search_logs", _search_description(), search, ["action", "index"]
        ),
        "measure_model_latency": _definition(
            "measure_model_latency", _latency_description(), latency, ["since"]
        ),
    }


# --------------------------------------------------------------------------- the base request


def tool_name(definition: Mapping[str, object]) -> str:
    """Return the name of an OpenAI-format tool definition."""
    function = definition.get("function")
    assert isinstance(function, Mapping)
    return str(function["name"])


def _messages(body: Mapping[str, object]) -> list[JsonDict]:
    messages = body["messages"]
    assert isinstance(messages, list)
    return cast(list[JsonDict], messages)


def base_body(captured: Mapping[str, object], scenarios: Scenarios, prompt: Prompt) -> JsonDict:
    """Build the spike's first request of a prompt from a captured primary request.

    Args:
        captured: A captured primary request body (the fre1537 probe gateway's capture).
        scenarios: The scenarios.
        prompt: The prompt.

    Returns:
        A new body: ``query_telemetry`` removed from ``tools``, each replaced catalog line swapped
        for its skill's line, and the text after ``</turn_context>`` of the user message replaced
        with the prompt's text. Every other byte is the captured one.

    Raises:
        ValueError: The capture has no ``query_telemetry``, a catalog line is not found exactly
            once, or the user message has no ``<turn_context>`` block.
    """
    body = copy.deepcopy(dict(captured))
    tools = cast(list[JsonDict], body["tools"])
    kept = [t for t in tools if tool_name(t) != REMOVED_TOOL]
    if len(kept) != len(tools) - 1:
        raise ValueError(f"the capture must hold {REMOVED_TOOL} exactly once")
    body["tools"] = kept

    messages = _messages(body)
    system = messages[0]
    if system.get("role") != "system" or not isinstance(system.get("content"), str):
        raise ValueError("the capture must start with a system message of plain text")
    text = str(system["content"])
    for skill in scenarios.skills.values():
        pattern = re.compile(rf"^- {re.escape(skill.replaces)}: .*$", re.M)
        if len(pattern.findall(text)) != 1:
            raise ValueError(f"the catalog line of {skill.replaces!r} is not found exactly once")
        line = f"- {skill.name}: {skill.catalog}"
        text = pattern.sub(line.replace("\\", "\\\\"), text)
    system["content"] = text

    users = [m for m in messages if m.get("role") == "user"]
    if len(users) != 1:
        raise ValueError("the capture must hold exactly one user message")
    content = users[0].get("content")
    if not isinstance(content, str) or TURN_CONTEXT_END not in content:
        raise ValueError("the user message has no <turn_context> block")
    head = content[: content.index(TURN_CONTEXT_END) + len(TURN_CONTEXT_END)]
    users[0]["content"] = head + prompt.text
    return body


def with_nonce(body: Mapping[str, object], nonce: str) -> JsonDict:
    """Return a copy of a body whose system message starts with the arm's nonce line.

    Args:
        body: A request body.
        nonce: The arm's nonce.

    Returns:
        The new body.
    """
    new = copy.deepcopy(dict(body))
    system = _messages(new)[0]
    system["content"] = f"{nonce_line(nonce)}\n{system['content']}"
    return new


# --------------------------------------------------------------------------- the reveal


def skill_result(
    scenarios: Scenarios, skill: Skill, form: Form, *, reveal: bool, tools: Mapping[str, JsonDict]
) -> str:
    """Build the synthetic ``read_skill`` result, in the shape of ``read_skill_executor``.

    Args:
        scenarios: The scenarios.
        skill: The loaded skill.
        form: The reveal form.
        reveal: True for the treatment. In the cache-safe form it adds ``provided_tools``. In the
            simple form the result is the same either way: the reveal is in ``tools``.
        tools: The experimental tool definitions.

    Returns:
        The tool message content in the D6 contract shape.
    """
    note = scenarios.reveal_notes[form].replace("{tool}", skill.tool)
    output: JsonDict = {
        "skill_name": skill.name,
        "base_dir": f"/app/skills/{skill.name}",
        "body": skill.body.replace("{reveal_note}", note),
        "files": [],
    }
    if form == "cache_safe" and reveal:
        output["provided_tools"] = [tools[skill.tool]]
    return render_content(output=output, error_detail=None)


def revealed_tools(
    base_tools: Sequence[JsonDict],
    form: Form,
    *,
    reveal: bool,
    revealed: Sequence[str],
    tools: Mapping[str, JsonDict],
) -> list[JsonDict]:
    """Return the ``tools`` array of a call.

    Args:
        base_tools: The base request's tools.
        form: The reveal form.
        reveal: True for the treatment.
        revealed: The tools revealed before this call, in reveal order.
        tools: The experimental tool definitions.

    Returns:
        The base tools, plus each revealed tool in the treatment of the simple form.
    """
    if form == "simple" and reveal:
        return [*base_tools, *(tools[name] for name in revealed)]
    return list(base_tools)


def request_bytes(body: Mapping[str, object]) -> bytes:
    """Return the canonical bytes of a request, for its hash."""
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def assistant_message(
    *, content: str, reasoning: str | None, tool_calls: Sequence[ToolCall], turn_id: int
) -> JsonDict:
    """Build the assistant message of a reply, as the executor does (``executor.py:6436-6451``).

    Args:
        content: The reply text.
        reasoning: The reasoning trace, kept as ``reasoning_content`` when present.
        tool_calls: The reply's tool calls.
        turn_id: The round of the call, 0 for the first call.

    Returns:
        The message, with the round-prefixed call ids of ``_build_assistant_tool_calls``.
    """
    message: JsonDict = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = _build_assistant_tool_calls(list(tool_calls), turn_id=turn_id)
    return message
