"""Parser for text-based tool calls from reasoning models.

Reasoning models like DeepSeek-R1 that don't support native function calling
often generate tool requests in text format. This module provides parsing logic
to extract and normalize these text-based tool calls into the standard ToolCall format.

Supported formats:
1. [TOOL_REQUEST]{"name":"tool_name","arguments":{...}}[END_TOOL_REQUEST]
2. <tool_call>{"name":"tool_name","arguments":{...}}</tool_call>
2b. <tool_call><function=name><parameter=key>value</parameter></function></tool_call>
    (Qwen XML-parameter variant)
3. Tool: tool_name(arg1=value1, arg2=value2)
4. [tool_name, {"arg":"value"}]  (common malformed fallback from some models)
5. <tool_code>print(tool_name(arg1=value1, ...))</tool_code>  (Gemini-style;
    also matches the bare form without the print wrapper — observed when a
    prior assistant turn's pseudo-code poisoned the session history)
"""

import ast
import json
import re
from ast import literal_eval as _literal  # safe: evaluates only Python literals
from typing import Any

from personal_agent.llm_client.types import ToolCall
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


def _parse_relaxed_json_object(raw: str) -> dict[str, Any] | None:
    """Best-effort parse of a JSON object with trailing noise.

    Some models emit bracket fallback calls with an extra closing brace/bracket
    suffix, e.g. `{"messages":[...}]}}]`.
    """
    candidate = raw.strip()
    while candidate:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
            return None
        except json.JSONDecodeError:
            candidate = candidate[:-1].rstrip()
    return None


def _extract_bracket_fallback_calls(content: str) -> list[tuple[str, str]]:
    """Extract `[tool_name, {json}]` chunks from free-form model output."""
    extracted: list[tuple[str, str]] = []
    idx = 0
    n = len(content)

    while idx < n:
        start = content.find("[", idx)
        if start == -1:
            break

        j = start + 1
        while j < n and content[j].isspace():
            j += 1

        name_start = j
        while j < n and (content[j].isalnum() or content[j] == "_"):
            j += 1
        tool_name = content[name_start:j]
        if not tool_name:
            idx = start + 1
            continue

        while j < n and content[j].isspace():
            j += 1
        if j >= n or content[j] != ",":
            idx = start + 1
            continue
        j += 1

        while j < n and content[j].isspace():
            j += 1
        if j >= n or content[j] != "{":
            idx = start + 1
            continue

        brace_start = j
        depth = 0
        in_string = False
        escape = False
        while j < n:
            ch = content[j]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1

        if j >= n or depth != 0:
            idx = start + 1
            continue

        args_json = content[brace_start : j + 1]
        extracted.append((tool_name, args_json))
        idx = j + 1

    return extracted


def _parse_python_call_expr(expr: str) -> dict[str, Any] | None:
    """Parse a Python call expression like ``fn("x", k=v)`` into name + arguments.

    Uses :mod:`ast` with ``literal_eval`` so only literal values are accepted
    (strings, numbers, lists, dicts, booleans, ``None``). Arbitrary code is
    never executed. Positional args are mapped to keys ``arg0``, ``arg1``, …
    and kwargs are preserved by name.

    Returns ``None`` if the expression isn't a simple call with literal args.
    """
    try:
        node = ast.parse(expr.strip(), mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None

    arguments: dict[str, Any] = {}
    for idx, arg in enumerate(node.args):
        try:
            arguments[f"arg{idx}"] = _literal(arg)
        except (ValueError, SyntaxError):
            return None
    for kw in node.keywords:
        if kw.arg is None:  # **kwargs unpacking — reject
            return None
        try:
            arguments[kw.arg] = _literal(kw.value)
        except (ValueError, SyntaxError):
            return None

    return {"name": node.func.id, "arguments": arguments}


_TOOL_CODE_BLOCK_RE: re.Pattern[str] = re.compile(
    r"<tool_code>(.*?)</tool_code>", re.DOTALL | re.IGNORECASE
)


def _parse_tool_code_block(block: str) -> dict[str, Any] | None:
    """Extract a function call from a ``<tool_code>`` block body.

    Accepts both ``print(fn(...))`` (the common Gemini-style output) and the
    bare ``fn(...)`` form. Strips the ``print(...)`` wrapper when present,
    then delegates to :func:`_parse_python_call_expr`.
    """
    body = block.strip()
    m = re.fullmatch(r"print\s*\((.*)\)\s*", body, re.DOTALL)
    if m:
        body = m.group(1).strip()
    return _parse_python_call_expr(body)


def _parse_qwen_xml_tool_call(block: str) -> dict[str, Any] | None:
    """Try to parse a Qwen XML-parameter variant from a ``<tool_call>`` block.

    Qwen3.x models sometimes emit tool calls as::

        <function=tool_name>
        <parameter=key>value</parameter>
        </function>

    This helper extracts the function name and each parameter, attempting to
    JSON-parse parameter values (they may be JSON arrays/objects or plain strings).

    Args:
        block: The inner text of a ``<tool_call>...</tool_call>`` match.

    Returns:
        Dict with ``name`` (str) and ``arguments`` (dict) if parsing succeeds,
        None if the block doesn't match the Qwen XML format.
    """
    # Match <function=name> ... </function>
    func_match = re.search(r"<function=(\w+)>(.*?)</function>", block, re.DOTALL)
    if not func_match:
        return None

    func_name = func_match.group(1)
    func_body = func_match.group(2)

    # Extract <parameter=key>value</parameter> pairs
    param_pattern = re.compile(r"<parameter=(\w+)>(.*?)</parameter>", re.DOTALL)
    arguments: dict[str, Any] = {}
    for pmatch in param_pattern.finditer(func_body):
        param_name = pmatch.group(1)
        raw_value = pmatch.group(2).strip()
        # Try JSON first (handles arrays, objects, numbers, booleans)
        try:
            arguments[param_name] = json.loads(raw_value)
        except (json.JSONDecodeError, ValueError):
            arguments[param_name] = raw_value

    return {"name": func_name, "arguments": arguments}


def parse_text_tool_calls(content: str, trace_id: str | None = None) -> list[ToolCall]:
    """Parse tool calls from text content generated by reasoning models.

    This function attempts multiple parsing strategies to extract tool calls
    from free-form text output. It handles various formats that reasoning models
    might use to indicate tool invocations.

    Args:
        content: Text content from model response.
        trace_id: Optional trace ID for logging.

    Returns:
        List of ToolCall objects extracted from text.
    """
    tool_calls: list[ToolCall] = []

    # Strategy 1: [TOOL_REQUEST]{...}[END_TOOL_REQUEST]
    # Note: Some models incorrectly close with [END_TOOL_RESULT] — accept both to be robust.
    pattern_1 = r"\[TOOL_REQUEST\](.*?)\[(?:END_TOOL_REQUEST|END_TOOL_RESULT)\]"
    matches = re.findall(pattern_1, content, re.DOTALL)
    for match in matches:
        try:
            data = json.loads(match.strip())
            tool_calls.append(
                ToolCall(
                    id=f"text_tool_{len(tool_calls)}",
                    name=data.get("name", ""),
                    arguments=json.dumps(data.get("arguments", {})),
                )
            )
            log.debug(
                "parsed_text_tool_call",
                format="TOOL_REQUEST",
                tool_name=data.get("name"),
                trace_id=trace_id,
            )
        except json.JSONDecodeError as e:
            log.warning(
                "failed_to_parse_tool_request",
                format="TOOL_REQUEST",
                match=match[:100],
                error=str(e),
                trace_id=trace_id,
            )

    # Strategy 2: <tool_call>{...}</tool_call>  (JSON body)
    # Strategy 2b: <tool_call><function=name><parameter=...>...</tool_call>  (Qwen XML variant)
    pattern_2 = r"<tool_call>(.*?)</tool_call>"
    matches = re.findall(pattern_2, content, re.DOTALL)
    for match in matches:
        # Try JSON body first (standard format)
        try:
            data = json.loads(match.strip())
            tool_calls.append(
                ToolCall(
                    id=f"text_tool_{len(tool_calls)}",
                    name=data.get("name", ""),
                    arguments=json.dumps(data.get("arguments", {})),
                )
            )
            log.debug(
                "parsed_text_tool_call",
                format="tool_call_tag",
                tool_name=data.get("name"),
                trace_id=trace_id,
            )
            continue
        except json.JSONDecodeError:
            pass

        # Fallback: Qwen XML-parameter variant (<function=name><parameter=...>)
        xml_data = _parse_qwen_xml_tool_call(match)
        if xml_data:
            tool_calls.append(
                ToolCall(
                    id=f"text_tool_{len(tool_calls)}",
                    name=xml_data["name"],
                    arguments=json.dumps(xml_data["arguments"]),
                )
            )
            log.debug(
                "parsed_text_tool_call",
                format="tool_call_tag_qwen_xml",
                tool_name=xml_data["name"],
                trace_id=trace_id,
            )
            continue

        log.warning(
            "failed_to_parse_tool_request",
            format="tool_call_tag",
            match=match[:100],
            trace_id=trace_id,
        )

    # Strategy 3: Tool: tool_name(arg1=value1, arg2=value2)
    # This is a simplified parser for function-style tool calls
    pattern_3 = r"Tool:\s*(\w+)\((.*?)\)"
    matches = re.findall(pattern_3, content, re.DOTALL)
    for tool_name, args_str in matches:
        try:
            # Parse key=value arguments
            arguments: dict[str, Any] = {}
            if args_str.strip():
                # Split by commas not inside quotes
                arg_pairs = re.findall(
                    r'(\w+)=([^,]+(?:,(?![^"]*"[^"]*(?:"[^"]*"[^"]*)*$))?)', args_str
                )
                for key, value in arg_pairs:
                    # Remove quotes if present
                    value = value.strip().strip('"').strip("'")
                    arguments[key.strip()] = value

            tool_calls.append(
                ToolCall(
                    id=f"text_tool_{len(tool_calls)}",
                    name=tool_name,
                    arguments=json.dumps(arguments),
                )
            )
            log.debug(
                "parsed_text_tool_call",
                format="function_style",
                tool_name=tool_name,
                trace_id=trace_id,
            )
        except Exception as e:
            log.warning(
                "failed_to_parse_tool_request",
                format="function_style",
                tool_name=tool_name,
                args_str=args_str[:100],
                error=str(e),
                trace_id=trace_id,
            )

    # Strategy 5: <tool_code>print(fn(...))</tool_code>  (Gemini-style)
    # Also covers bare fn(...) inside tool_code when sessions are poisoned by
    # prior mimicked assistant output.
    for match in _TOOL_CODE_BLOCK_RE.findall(content):
        parsed = _parse_tool_code_block(match)
        if parsed is None:
            log.warning(
                "failed_to_parse_tool_request",
                format="tool_code",
                match=match[:120],
                trace_id=trace_id,
            )
            continue
        tool_calls.append(
            ToolCall(
                id=f"text_tool_{len(tool_calls)}",
                name=parsed["name"],
                arguments=json.dumps(parsed["arguments"]),
            )
        )
        log.debug(
            "parsed_text_tool_call",
            format="tool_code",
            tool_name=parsed["name"],
            trace_id=trace_id,
        )

    # Strategy 4: [tool_name, {"arg":"value"}]
    # Some models emit this instead of required [TOOL_REQUEST] JSON envelope.
    for tool_name, args_json in _extract_bracket_fallback_calls(content):
        parsed_args = _parse_relaxed_json_object(args_json)
        if parsed_args is None:
            log.warning(
                "malformed_text_tool_call_detected",
                format="bracket_fallback",
                tool_name=tool_name,
                args_preview=args_json[:160],
                trace_id=trace_id,
            )
            continue

        tool_calls.append(
            ToolCall(
                id=f"text_tool_{len(tool_calls)}",
                name=tool_name,
                arguments=json.dumps(parsed_args),
            )
        )
        log.debug(
            "parsed_text_tool_call",
            format="bracket_fallback",
            tool_name=tool_name,
            trace_id=trace_id,
        )

    if tool_calls:
        log.info(
            "extracted_text_tool_calls",
            count=len(tool_calls),
            tools=[tc["name"] for tc in tool_calls],
            trace_id=trace_id,
        )

    return tool_calls
