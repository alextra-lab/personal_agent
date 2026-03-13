"""Render OpenAI-format tool definitions as system-prompt text.

When a model's chat template does not support the ``tools`` API parameter
(i.e. ``tool_calling_strategy == PROMPT_INJECTED``), the orchestrator uses
this module to embed tool descriptions directly in the system prompt so the
model can still discover and invoke tools via a text-based protocol.

The rendered output pairs with ``TOOL_USE_PROMPT_INJECTED`` in
``personal_agent.orchestrator.prompts`` and is parsed by
``parse_text_tool_calls`` in ``personal_agent.llm_client.tool_call_parser``.
"""

from __future__ import annotations

from typing import Any


def render_tools_for_prompt(
    tool_definitions: list[dict[str, Any]],
    *,
    max_tools: int = 30,
) -> str:
    """Convert OpenAI-format tool definitions into a compact text block.

    Args:
        tool_definitions: List of dicts in the OpenAI ``tools`` format, e.g.::

            [{"type": "function", "function": {"name": "...", "description": "...",
              "parameters": {"type": "object", "properties": {...}, "required": [...]}}}]

        max_tools: Truncate the list after this many tools to avoid blowing up
            the context window for models with small limits.

    Returns:
        A human-readable, LLM-friendly text block listing every tool with its
        parameters.  Designed to be appended after ``TOOL_USE_PROMPT_INJECTED``.
    """
    if not tool_definitions:
        return ""

    lines: list[str] = ["## Available Tools", ""]

    for idx, tool in enumerate(tool_definitions[:max_tools], 1):
        func = tool.get("function", {})
        name = func.get("name", "unknown")
        description = func.get("description", "")
        params_schema = func.get("parameters", {})

        # Tool header
        lines.append(f"{idx}. **{name}**")
        if description:
            # Keep description short — first sentence only
            short_desc = description.split(". ")[0].rstrip(".")
            lines.append(f"   {short_desc}.")

        # Parameters
        properties = params_schema.get("properties", {})
        required = set(params_schema.get("required", []))

        if properties:
            for pname, pschema in properties.items():
                ptype = pschema.get("type", "any")
                req_marker = "required" if pname in required else "optional"
                pdesc = pschema.get("description", "")
                if pdesc:
                    pdesc = f" — {pdesc.split('. ')[0]}"
                lines.append(f"   - {pname} ({ptype}, {req_marker}){pdesc}")
        else:
            lines.append("   No parameters.")

        lines.append("")  # blank line between tools

    if len(tool_definitions) > max_tools:
        lines.append(f"... and {len(tool_definitions) - max_tools} more tools.")

    return "\n".join(lines)
