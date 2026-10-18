"""FRE-1615 Phase 0: the offline token counter, the schema lint and the variant builder.

No test calls a model, a gateway or a store.
"""

from __future__ import annotations

import json

import pytest
from scripts.eval.fre1615 import lint_schemas, variants
from scripts.eval.fre1615.tokens import Counter
from tokenizers import Tokenizer, models, pre_tokenizers

# The served template's shape, cut down: tools first in the system block, then the system text.
TEMPLATE = (
    "{%- if tools %}<|im_start|>system\n"
    "{%- if reasoning_effort == 'low' %}LOW\n\n{% endif -%}"
    "# Tools\n\nYou have access to the following functions:\n\n<tools>"
    "{%- for tool in tools %}{{- '\\n' }}{{- tool | tojson }}{%- endfor %}"
    "{{- '\\n</tools>' }} PREAMBLE"
    "{{- '\\n\\n' + messages[0].content + '<|im_end|>\\n' }}"
    "{%- endif %}"
    "{%- for m in messages[1:] %}<|im_start|>{{ m.role }}\n{{ m.content }}"
    "{%- for c in m.tool_calls or [] %} CALL {{ c.function.name }}"
    "{%- for k, v in c.function.arguments | items %} {{ k }}={{ v }}{%- endfor %}{%- endfor %}"
    "<|im_end|>\n{%- endfor %}"
    "{%- if add_generation_prompt %}<|im_start|>assistant\n{%- endif %}"
)


def _tokenizer() -> Tokenizer:
    tok = Tokenizer(models.WordLevel(vocab={"[UNK]": 0}, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    return tok


def _tool(name: str, properties: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": properties or {}},
        },
    }


def _body(system: str = "be brief", tools: list[str] | None = None) -> dict[str, object]:
    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "hello there"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "memory_recall", "arguments": '{"q": "x"}'}}
                ],
            },
        ],
        "tools": [_tool(n) for n in (tools or ["web_search", "bash"])],
    }


def test_breakdown_parts_sum_to_the_total() -> None:
    """The four parts of a body add up to its total."""
    counter = Counter(TEMPLATE, _tokenizer())
    parts = counter.breakdown(_body())
    assert parts["system_prompt"] == counter.tokens("be brief")
    assert parts["tool_definitions"] > 0
    pieces = ("tool_definitions", "tool_preamble", "system_prompt", "conversation")
    # A word-level tokenizer splits at the part boundaries, so the parts add up exactly.
    assert sum(parts[p] for p in pieces) == parts["total"]


def test_json_string_arguments_render_as_an_object() -> None:
    """llama.cpp parses tool-call arguments before a template with object arguments renders them."""
    text = Counter(TEMPLATE, _tokenizer()).render(_body())
    assert "CALL memory_recall q=x" in text


def test_body_kwargs_override_the_server_kwargs() -> None:
    """A body's chat_template_kwargs win over the server's, as llama.cpp merges them."""
    counter = Counter(TEMPLATE, _tokenizer())
    body = {**_body(), "chat_template_kwargs": {"reasoning_effort": "low"}}
    assert "LOW" in counter.render(body)
    assert "LOW" not in counter.render(_body())


def test_fewer_tools_count_fewer_definition_tokens() -> None:
    """Dropping tools lowers only the definition count."""
    counter = Counter(TEMPLATE, _tokenizer())
    full = counter.breakdown(_body(tools=["web_search", "bash", "read"]))
    one = counter.breakdown(_body(tools=["web_search"]))
    assert one["tool_definitions"] < full["tool_definitions"]
    assert one["system_prompt"] == full["system_prompt"]


@pytest.mark.parametrize(
    ("schema", "rule"),
    [
        (
            {"type": "number", "description": "Max results (1..50, default 10)."},
            "integer-as-number",
        ),
        ({"type": "number", "description": "Max results (1..50, default 10)."}, "prose-range"),
        ({"type": "array", "description": "Names."}, "array-without-items"),
        ({"type": "string", "description": "'overwrite' (default) or 'append'"}, "prose-enum"),
        ({"type": "string", "description": "Options: 'day', 'week'."}, "prose-enum"),
    ],
)
def test_lint_rule_fires(schema: dict[str, object], rule: str) -> None:
    """Each lint rule fires on its defect."""
    findings = lint_schemas.lint_tool(_tool("t", {"limit": schema}))
    assert rule in {f.rule for f in findings}


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "integer", "minimum": 1, "maximum": 50, "description": "Max results (1..50)."},
        {"type": "array", "items": {"type": "string"}, "description": "Names."},
        {"type": "string", "enum": ["a", "b"], "description": "'a' or 'b'"},
        {"type": "string", "description": "A library name (e.g. 'fastapi', 'react')."},
    ],
)
def test_lint_is_silent_on_a_clean_schema(schema: dict[str, object]) -> None:
    """A typed, bounded or example-only schema gives no finding."""
    assert lint_schemas.lint_tool(_tool("t", {"x": schema})) == []


def test_with_tools_keeps_the_registry_order() -> None:
    """A profile keeps the tools in their registry order and leaves the input unchanged."""
    body = _body(tools=["web_search", "bash", "read"])
    kept = variants.with_tools(body, {"read", "web_search"})
    assert [t["function"]["name"] for t in kept["tools"]] == ["web_search", "read"]
    assert len(body["tools"]) == 3  # type: ignore[arg-type]


def test_with_system_replaces_only_the_system_text() -> None:
    """Variant B changes the system message and nothing else."""
    body = _body()
    out = variants.with_system(body, "new")
    assert out["messages"][0]["content"] == "new"  # type: ignore[index]
    assert out["messages"][1:] == body["messages"][1:]  # type: ignore[index]
    assert body["messages"][0]["content"] == "be brief"  # type: ignore[index]


def test_every_task_type_has_a_profile_and_every_tool_is_grouped() -> None:
    """Every TaskType has a profile, and each of the 23 tools is in exactly one group."""
    import yaml

    from personal_agent.request_gateway.types import TaskType

    config = yaml.safe_load(variants.PROFILES.read_text())
    assert {t.value for t in TaskType} == set(config["profiles"])
    grouped = [name for names in config["groups"].values() for name in names]
    assert len(grouped) == len(set(grouped)) == 23
    groups, names = variants.profile_tools("conversational", config)
    assert groups == ["core"] and "web_search" in names


def test_suite_has_three_tasks_per_category_and_known_groups() -> None:
    """The suite has 8 categories of 3 tasks and holds the owner's chimichurri pair."""
    import yaml
    from scripts.eval.fre1615.capture import load_suite

    groups = set(yaml.safe_load(variants.PROFILES.read_text())["groups"])
    tasks = load_suite()
    by_category: dict[str, int] = {}
    for task in tasks:
        by_category[task["category"]] = by_category.get(task["category"], 0) + 1
        assert set(task["expects"]) <= groups, task["id"]
    assert len(by_category) == 8 and set(by_category.values()) == {3}
    assert len({t["id"] for t in tasks}) == 24
    chimichurri = next(t for t in tasks if t["id"] == "fu1")
    assert chimichurri["history_user"] == "How do i make chimichurri?"
    assert chimichurri["message"] == "Is there a version with coriander?"
    json.dumps(tasks)


def test_with_tools_refuses_a_name_that_is_not_a_tool() -> None:
    """A profile typo raises instead of shrinking the profile."""
    with pytest.raises(ValueError, match="web_serch"):
        variants.with_tools(_body(), {"web_serch"})


def test_breakdown_refuses_a_body_without_a_system_message() -> None:
    """The split needs the system block, so a body without one is refused."""
    body = _body()
    body["messages"] = body["messages"][1:]  # type: ignore[index]
    with pytest.raises(ValueError, match="system message"):
        Counter(TEMPLATE, _tokenizer()).breakdown(body)
