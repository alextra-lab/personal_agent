"""Tests for tool argument schema validation."""

import pytest

from personal_agent.tools.schema_validator import validate_tool_arguments
from personal_agent.tools.types import ToolDefinition, ToolParameter


def _make_tool(
    name: str = "test_tool",
    parameters: list[ToolParameter] | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="test",
        category="mcp",
        parameters=parameters or [],
        risk_level="low",
        allowed_modes=["NORMAL"],
    )


# ── Required field checks ────────────────────────────────────────────────────


def test_missing_required_param_rejected() -> None:
    tool = _make_tool(parameters=[
        ToolParameter(name="path", type="string", description="p", required=True),
    ])
    errors = validate_tool_arguments(tool, {})
    assert any("path" in e for e in errors)


def test_present_required_param_passes() -> None:
    tool = _make_tool(parameters=[
        ToolParameter(name="path", type="string", description="p", required=True),
    ])
    errors = validate_tool_arguments(tool, {"path": "/tmp"})
    assert errors == []


def test_optional_param_missing_is_ok() -> None:
    tool = _make_tool(parameters=[
        ToolParameter(name="limit", type="number", description="l", required=False),
    ])
    errors = validate_tool_arguments(tool, {})
    assert errors == []


# ── Type checks ──────────────────────────────────────────────────────────────


def test_wrong_simple_type_rejected() -> None:
    tool = _make_tool(parameters=[
        ToolParameter(name="count", type="number", description="c", required=True),
    ])
    errors = validate_tool_arguments(tool, {"count": "not-a-number"})
    assert len(errors) >= 1
    assert any("count" in e for e in errors)


def test_correct_simple_type_passes() -> None:
    tool = _make_tool(parameters=[
        ToolParameter(name="count", type="number", description="c", required=True),
    ])
    errors = validate_tool_arguments(tool, {"count": 42})
    assert errors == []


# ── Nested schema (Perplexity messages) ──────────────────────────────────────


_MESSAGES_SCHEMA: dict = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "role": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["role", "content"],
    },
}


def test_valid_perplexity_messages_pass() -> None:
    tool = _make_tool(parameters=[
        ToolParameter(
            name="messages",
            type="array",
            description="msgs",
            required=True,
            json_schema=_MESSAGES_SCHEMA,
        ),
    ])
    errors = validate_tool_arguments(tool, {
        "messages": [{"role": "user", "content": "Hello"}],
    })
    assert errors == []


def test_perplexity_messages_wrong_type_rejected() -> None:
    tool = _make_tool(parameters=[
        ToolParameter(
            name="messages",
            type="array",
            description="msgs",
            required=True,
            json_schema=_MESSAGES_SCHEMA,
        ),
    ])
    errors = validate_tool_arguments(tool, {"messages": "not-an-array"})
    assert len(errors) >= 1


def test_perplexity_messages_missing_required_field_rejected() -> None:
    tool = _make_tool(parameters=[
        ToolParameter(
            name="messages",
            type="array",
            description="msgs",
            required=True,
            json_schema=_MESSAGES_SCHEMA,
        ),
    ])
    errors = validate_tool_arguments(tool, {
        "messages": [{"role": "user"}],
    })
    assert len(errors) >= 1
    assert any("content" in e for e in errors)


def test_perplexity_messages_item_wrong_type_rejected() -> None:
    tool = _make_tool(parameters=[
        ToolParameter(
            name="messages",
            type="array",
            description="msgs",
            required=True,
            json_schema=_MESSAGES_SCHEMA,
        ),
    ])
    errors = validate_tool_arguments(tool, {
        "messages": ["just a string"],
    })
    assert len(errors) >= 1


# ── Additional properties ────────────────────────────────────────────────────


def test_extra_properties_rejected() -> None:
    tool = _make_tool(parameters=[
        ToolParameter(name="path", type="string", description="p", required=True),
    ])
    errors = validate_tool_arguments(tool, {"path": "/tmp", "bogus": 123})
    assert any("bogus" in e for e in errors)
