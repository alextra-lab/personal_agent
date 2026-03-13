"""Validate tool arguments against ToolDefinition parameter schemas.

Uses the ``jsonschema`` library (already a transitive dependency of ``mcp``)
for spec-complete JSON Schema validation.  Error messages are compressed to
concise retry-safe hints per ADR-0032 error depoisoning; full details are
available in structured logs.

Usage::

    from personal_agent.tools.schema_validator import validate_tool_arguments

    errors = validate_tool_arguments(tool_def, {"messages": [...]})
    if errors:
        # reject with retry hint
"""

from __future__ import annotations

from typing import Any

import jsonschema

from personal_agent.tools.types import ToolDefinition


def _build_json_schema(tool_def: ToolDefinition) -> dict[str, Any]:
    """Build a JSON Schema object from a ToolDefinition.

    Mirrors the schema construction in ``ToolRegistry.get_tool_definitions_for_llm``
    so validation applies the same rules the LLM sees.

    Args:
        tool_def: Registered tool definition.

    Returns:
        A JSON Schema ``object`` dict suitable for ``jsonschema.validate``.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param in tool_def.parameters:
        if param.json_schema:
            properties[param.name] = param.json_schema
        else:
            properties[param.name] = {
                "type": param.type,
            }
        if param.required:
            required.append(param.name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required

    return schema


def validate_tool_arguments(
    tool_def: ToolDefinition,
    arguments: dict[str, Any],
) -> list[str]:
    """Validate *arguments* against *tool_def* parameter schemas.

    Args:
        tool_def: The tool's registered definition.
        arguments: Parsed argument dict from the LLM.

    Returns:
        List of concise human-readable error strings.  Empty list means valid.
    """
    schema = _build_json_schema(tool_def)

    validator = jsonschema.Draft7Validator(schema)
    raw_errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.path))

    errors: list[str] = []
    for err in raw_errors:
        path = ".".join(str(p) for p in err.absolute_path) if err.absolute_path else "(root)"
        errors.append(f"{path}: {err.message}")

    return errors
