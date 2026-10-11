"""The one tool contract — ADR-0156 AC-1 ("Tools" and "Dispatch"), AC-5 and AC-8 (FRE-1575).

Every registered tool, built with every registration flag on, and every dispatch failure path
gives the model ``{"ok": true, ...}`` or ``{"ok": false, "error": {code, message, retryable,
next_action}}``. A failure logs ``tool_call_completed`` with ``success: false`` and reaches the
model with its full message. The failure-line coverage half of AC-1 is
``scripts/check_tool_failure_coverage.py`` (run in CI).
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import pytest
from tests._helpers.log_capture import capture_log_records
from tests._helpers.permissions import use_rules

from personal_agent.brainstem.mode_manager import ModeManager
from personal_agent.config import settings
from personal_agent.config.governance_loader import load_governance_config
from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call
from personal_agent.telemetry import TraceContext
from personal_agent.tools import register_mvp_tools
from personal_agent.tools.contract import (
    ToolError,
    error_codes,
    render_content,
    result_tokens,
)
from personal_agent.tools.executor import ToolExecutionLayer
from personal_agent.tools.primitives import _governance
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.telemetry_query import ALLOWED_FIELDS
from personal_agent.tools.types import RESULT_TOKEN_CEILING, ToolDefinition, ToolParameter

#: The 24 tools of the ADR-0156 migration table that T3 covers: the 28 rows minus the three
#: MCP copies (D7.5) and ``edit`` (T4).
EXPECTED_TOOLS = frozenset(
    {
        "search_memory",
        "recall_personal_history",
        "web_search",
        "perplexity_query",
        "get_library_docs",
        "fetch_url",
        "query_telemetry",
        "create_linear_issue",
        "find_linear_issues",
        "list_linear_projects",
        "create_linear_project",
        "notes_write",
        "notes_search",
        "artifact_write",
        "artifact_list",
        "artifact_read",
        "artifact_draft",
        "get_location",
        "read",
        "write",
        "bash",
        "run_python",
        "read_skill",
        "expand_tool_result",
    }
)

REMOVED_MCP_COPIES = ("mcp_query-docs", "mcp_resolve-library-id", "mcp_sequentialthinking")

ERROR_KEYS = {"code", "message", "retryable", "next_action"}


@pytest.fixture
def full_registry(monkeypatch: pytest.MonkeyPatch) -> ToolRegistry:
    """Build the registry with every registration flag on, so conditional tools register."""
    monkeypatch.setattr(settings, "primitive_tools_enabled", True)
    monkeypatch.setattr(settings, "location_enabled", True)
    monkeypatch.setattr(settings, "tool_result_compression_enabled", True)
    monkeypatch.setattr(settings, "r2_endpoint_url", "https://r2.example.invalid")
    monkeypatch.setattr(settings, "r2_access_key_id", "test-key")
    monkeypatch.setattr(settings, "r2_secret_access_key", "test-secret")
    registry = ToolRegistry()
    register_mvp_tools(registry)
    return registry


def _tool_modules(registry: ToolRegistry) -> dict[str, ModuleType]:
    """Map each registered tool to the module of its executor."""
    modules: dict[str, ModuleType] = {}
    for name in registry.list_tool_names():
        entry = registry.get_tool(name)
        assert entry is not None
        module = inspect.getmodule(entry[1])
        assert module is not None, name
        modules[name] = module
    return modules


def assert_conforms(content: str) -> dict[str, Any]:
    """Assert that tool message content is in the one result shape (ADR-0156 D6.1).

    Args:
        content: The tool message content the model reads.

    Returns:
        The parsed content.
    """
    payload = json.loads(content)
    assert isinstance(payload, dict), content
    assert payload.get("ok") in (True, False), f"no boolean 'ok': {content[:200]}"
    if payload["ok"] is False:
        assert set(payload) == {"ok", "error"}, content[:200]
        error = payload["error"]
        assert set(error) == ERROR_KEYS, content[:200]
        assert isinstance(error["retryable"], bool)
        assert error["code"] != "contract_violation", f"a tool broke the contract: {content}"
        assert error["code"] != "undeclared_error_code", f"undeclared code: {content}"
    else:
        assert "error" not in payload and payload.get("success") is not False, content[:200]
    return payload


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def test_the_registry_is_the_24_rows_and_the_mcp_copies_are_gone(
    full_registry: ToolRegistry,
) -> None:
    """AC-1 registry check and AC-5: exactly the 24 rows; no MCP copy; get_library_docs kept."""
    names = set(full_registry.list_tool_names())
    assert names == EXPECTED_TOOLS, (
        f"missing: {sorted(EXPECTED_TOOLS - names)}, extra: {sorted(names - EXPECTED_TOOLS)}"
    )
    for removed in REMOVED_MCP_COPIES:
        assert removed not in names
    assert "get_library_docs" in names
    assert not any(name.startswith("mcp_") for name in names)


def test_no_mcp_server_is_enabled_by_default() -> None:
    """AC-5: the launcher's default server set is empty and the gateway is off by default."""
    script = Path(__file__).parents[3] / "docker/mcp/run-gateway.sh"
    match = re.search(r"^DEFAULT_SERVERS='(.*)'$", script.read_text(), flags=re.MULTILINE)
    assert match is not None
    assert json.loads(match.group(1)) == []
    assert type(settings).model_fields["mcp_gateway_enabled"].default is False


def test_every_tool_declares_its_closed_code_set(full_registry: ToolRegistry) -> None:
    """D6.1: each definition's codes are exactly one typed error class's Literal set."""
    modules = _tool_modules(full_registry)
    for name, module in modules.items():
        entry = full_registry.get_tool(name)
        assert entry is not None
        declared = entry[0].error_codes
        assert declared, f"{name} declares no error codes"
        classes = [
            obj
            for obj in vars(module).values()
            if inspect.isclass(obj)
            and issubclass(obj, ToolError)
            and obj.__module__ == module.__name__
        ]
        assert any(error_codes(cls) == declared for cls in classes), (
            f"{name}: error_codes {sorted(declared)} match no ToolError class in {module.__name__}"
        )


def _failure_shaped_return(node: ast.Return) -> bool:
    """Report whether a ``return`` returns a pre-contract error dict literal."""
    if not isinstance(node.value, ast.Dict):
        return False
    for key, value in zip(node.value.keys, node.value.values, strict=True):
        if not isinstance(key, ast.Constant):
            continue
        if key.value == "error":
            return True
        if key.value == "success" and isinstance(value, ast.Constant) and value.value is False:
            return True
        if key.value == "status" and isinstance(value, ast.Constant) and value.value == "error":
            return True
    return False


def test_tool_modules_build_failures_only_from_typed_errors(full_registry: ToolRegistry) -> None:
    """D6.1: no untyped ``ToolExecutionError(...)`` and no returned error dict in a tool module."""
    modules = {*_tool_modules(full_registry).values(), _governance}
    offenders: list[str] = []
    for module in modules:
        path = Path(inspect.getfile(module))
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ToolExecutionError"
            ):
                offenders.append(f"{path.name}:{node.lineno} untyped ToolExecutionError")
            if isinstance(node, ast.Return) and _failure_shaped_return(node):
                offenders.append(f"{path.name}:{node.lineno} returns an error dict")
    assert not offenders, "\n".join(offenders)


_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def test_every_description_follows_s5(full_registry: ToolRegistry) -> None:
    """D6.5: at least 3 sentences, at least one example call, every parameter described."""
    for name in full_registry.list_tool_names():
        entry = full_registry.get_tool(name)
        assert entry is not None
        tool_def = entry[0]
        sentences = len(_SENTENCE_END.findall(tool_def.description))
        assert sentences >= 3, f"{name}: {sentences} sentences"
        assert f"{name}(" in tool_def.description, f"{name}: no example call"
        for param in tool_def.parameters:
            assert param.description.strip(), f"{name}.{param.name} has no description"


def test_every_result_is_bounded_with_next_call_text(full_registry: ToolRegistry) -> None:
    """D6.4: an oversized result is cut within the tool's bound and names the next call."""
    big = {"text": "word " * 60_000}
    for name in full_registry.list_tool_names():
        entry = full_registry.get_tool(name)
        assert entry is not None
        tool_def = entry[0]
        assert tool_def.max_result_tokens <= RESULT_TOKEN_CEILING
        content = render_content(
            output=big,
            error_detail=None,
            max_result_tokens=tool_def.max_result_tokens,
            truncation_next_action=tool_def.truncation_next_action,
        )
        assert result_tokens(content) <= tool_def.max_result_tokens, name
        payload = assert_conforms(content)
        assert payload["truncated"] is True
        assert payload["next_action"] == tool_def.truncation_next_action
        assert payload["partial"]


def test_a_result_within_its_bound_is_unchanged() -> None:
    """D6.4: a result under its bound reaches the model whole."""
    content = render_content(output={"a": 1}, error_detail=None, max_result_tokens=100)
    assert json.loads(content) == {"ok": True, "a": 1}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

FakeCode = Literal["bad_input"]


class FakeError(ToolError[FakeCode]):
    """The probe tools' typed failure."""


OtherCode = Literal["other_code"]


class OtherError(ToolError[OtherCode]):
    """A failure class the probe tool does not declare."""


#: Longer than the old 150-character clip, under the 2,000-character bound.
LONG_MESSAGE = "the refusal keeps its fix: " + ", ".join(f"field_{i}" for i in range(120))


def _probe(name: str, **overrides: Any) -> ToolDefinition:
    fields: dict[str, Any] = {
        "name": name,
        "description": "Probe.",
        "category": "read_only",
        "parameters": [],
        "risk_level": "low",
        "allowed_modes": ["NORMAL"],
        "error_codes": error_codes(FakeError),
    }
    fields.update(overrides)
    return ToolDefinition(**fields)


async def _ok() -> dict[str, object]:
    return {"value": 1}


async def _typed_failure() -> dict[str, object]:
    raise FakeError("bad_input", LONG_MESSAGE, retryable=True, next_action="Fix the input.")


async def _undeclared() -> dict[str, object]:
    raise OtherError("other_code", "not declared", retryable=False, next_action="none")


async def _legacy() -> dict[str, object]:
    return {"success": False, "error": "boom"}


async def _times_out() -> dict[str, object]:
    raise TimeoutError("slow upstream")


async def _crashes() -> dict[str, object]:
    raise ValueError("kaput")


async def _needs_x(x: str) -> dict[str, object]:
    return {"x": x}


async def _huge() -> dict[str, object]:
    return {"text": "word " * 20_000}


@pytest.fixture
def probe_layer(monkeypatch: pytest.MonkeyPatch) -> ToolExecutionLayer:
    """A layer over probe tools, with a permission rule set that names each one."""
    use_rules(
        monkeypatch,
        allow=[
            "ok_tool",
            "typed_fail_tool",
            "undeclared_tool",
            "legacy_tool",
            "timeout_tool",
            "crash_tool",
            "needs_arg_tool",
            "mode_tool",
            "huge_tool",
        ],
        deny=["denied_tool"],
    )
    registry = ToolRegistry()
    registry.register(_probe("ok_tool"), _ok)
    registry.register(_probe("typed_fail_tool"), _typed_failure)
    registry.register(_probe("undeclared_tool"), _undeclared)
    registry.register(_probe("legacy_tool"), _legacy)
    registry.register(_probe("timeout_tool"), _times_out)
    registry.register(_probe("crash_tool"), _crashes)
    registry.register(
        _probe(
            "needs_arg_tool",
            parameters=[ToolParameter(name="x", type="string", description="X.", required=True)],
        ),
        _needs_x,
    )
    registry.register(_probe("mode_tool", allowed_modes=["LOCKDOWN"]), _ok)
    registry.register(_probe("denied_tool"), _ok)
    registry.register(
        _probe("huge_tool", max_result_tokens=500, truncation_next_action="Ask for less."), _huge
    )
    governance = load_governance_config()
    return ToolExecutionLayer(
        registry=registry,
        governance_config=governance,
        mode_manager=ModeManager(governance_config=governance),
    )


async def _dispatch(
    layer: ToolExecutionLayer, tool_name: str, arguments: Mapping[str, object] | None = None
) -> tuple[dict[str, Any], list[MutableMapping[str, Any]]]:
    trace_ctx = TraceContext.new_trace()
    with capture_log_records() as records:
        result = await dispatch_tool_call(
            tool_call_id="call-1",
            tool_name=tool_name,
            arguments=dict(arguments or {}),
            tool_layer=layer,
            trace_ctx=trace_ctx,
            trace_id=trace_ctx.trace_id,
            session_id=None,
            loaded_skills=set(),
        )
    return result, records


def _completions(records: list[MutableMapping[str, Any]]) -> list[MutableMapping[str, Any]]:
    return [r for r in records if r.get("event") == "tool_call_completed"]


@pytest.mark.asyncio
async def test_a_success_is_ok_true(probe_layer: ToolExecutionLayer) -> None:
    """D6.1: a success is ``{"ok": true, ...payload}`` and logs ``success: true``."""
    result, records = await _dispatch(probe_layer, "ok_tool")
    assert assert_conforms(result["content"]) == {"ok": True, "value": 1}
    assert [r["success"] for r in _completions(records)] == [True]


@pytest.mark.parametrize(
    ("tool_name", "arguments", "code", "executed"),
    [
        ("no_such_tool", {}, "unknown_tool", False),
        ("needs_arg_tool", {}, "missing_parameter", False),
        ("mode_tool", {}, "mode_refused", False),
        ("denied_tool", {}, "permission_denied", False),
        ("timeout_tool", {}, "timeout", True),
        ("crash_tool", {}, "tool_exception", True),
        ("typed_fail_tool", {}, "bad_input", True),
    ],
)
@pytest.mark.asyncio
async def test_every_dispatch_failure_has_the_one_shape_and_logs_a_failure(
    probe_layer: ToolExecutionLayer,
    tool_name: str,
    arguments: dict[str, object],
    code: str,
    executed: bool,
) -> None:
    """AC-1 Dispatch and AC-8: each failure path gives D6.1 and logs ``success: false``."""
    result, records = await _dispatch(probe_layer, tool_name, arguments)
    payload = assert_conforms(result["content"])
    assert payload["ok"] is False
    assert payload["error"]["code"] == code
    assert result["success"] is False
    completions = _completions(records)
    assert len(completions) == 1, completions
    assert completions[0]["success"] is False
    assert completions[0]["error_code"] == code
    assert completions[0]["executed"] is executed


@pytest.mark.asyncio
async def test_a_failure_message_reaches_the_model_unclipped(
    probe_layer: ToolExecutionLayer,
) -> None:
    """AC-8: the full message (over the old 150-character clip) reaches the model."""
    result, _ = await _dispatch(probe_layer, "typed_fail_tool")
    error = json.loads(result["content"])["error"]
    assert len(LONG_MESSAGE) > 150
    assert error["message"] == LONG_MESSAGE
    assert error["retryable"] is True
    assert error["next_action"] == "Fix the input."


@pytest.mark.asyncio
async def test_an_undeclared_code_is_a_contract_failure(probe_layer: ToolExecutionLayer) -> None:
    """D6.1: a code outside the tool's declared set never reaches the model as the tool's."""
    result, _ = await _dispatch(probe_layer, "undeclared_tool")
    assert json.loads(result["content"])["error"]["code"] == "undeclared_error_code"


@pytest.mark.asyncio
async def test_seeded_negative_a_plain_error_dict_fails_the_contract(
    probe_layer: ToolExecutionLayer,
) -> None:
    """AC-1 seeded negative: a tool that returns a plain error dict fails the contract test."""
    result, records = await _dispatch(probe_layer, "legacy_tool")
    payload = json.loads(result["content"])
    assert payload["ok"] is False
    assert payload["error"]["code"] == "contract_violation"
    assert [r["success"] for r in _completions(records)] == [False]
    with pytest.raises(AssertionError, match="broke the contract"):
        assert_conforms(result["content"])


@pytest.mark.asyncio
async def test_an_oversized_result_is_truncated_at_dispatch(
    probe_layer: ToolExecutionLayer,
) -> None:
    """D6.4: dispatch bounds the result with the tool's own bound and next-call text."""
    result, _ = await _dispatch(probe_layer, "huge_tool")
    payload = assert_conforms(result["content"])
    assert payload["truncated"] is True
    assert payload["next_action"] == "Ask for less."
    assert result_tokens(result["content"]) <= 500


# ---------------------------------------------------------------------------
# AC-2 / AC-8 named instance: a query_telemetry refusal keeps its allowed fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_query_telemetry_refusal_reaches_the_model_with_the_allowed_fields(
    full_registry: ToolRegistry,
) -> None:
    """AC-2: the refusal lists every allowed field, unclipped, and logs ``success: false``."""
    governance = load_governance_config()
    layer = ToolExecutionLayer(
        registry=full_registry,
        governance_config=governance,
        mode_manager=ModeManager(governance_config=governance),
    )
    result, records = await _dispatch(
        layer,
        "query_telemetry",
        {"action": "search", "index": "agent-logs", "fields": ["user_message"]},
    )
    payload = assert_conforms(result["content"])
    error = payload["error"]
    allowed = ", ".join(sorted(ALLOWED_FIELDS["agent-logs"]))
    assert error["code"] == "refused"
    assert f"Allowed fields: {allowed}" in error["message"]
    assert len(error["message"]) > 150
    assert "[truncated]" not in error["message"]
    completions = _completions(records)
    assert [(c["success"], c["error_code"]) for c in completions] == [(False, "refused")]


# ---------------------------------------------------------------------------
# AC-3: the folded skills' "use this, not that" rules live in the descriptions (D5.6)
# ---------------------------------------------------------------------------

#: For each folded skill: the tool description, and phrases that carry the skill's rule.
FOLDED_RULES: dict[str, tuple[str, tuple[str, ...]]] = {
    "bash (bash.md)": (
        "bash",
        ("absolute paths", "plain command", "always ask", "refused", "curl runs without asking"),
    ),
    "bash (list-directory.md)": ("bash", ("wc -l", "not ls -R")),
    "fetch-url.md": ("fetch_url", ("citable", "bash curl", "web_search", "asks the user")),
    "get_location.md": ("get_location", ("never ask", "consent_not_given", "session_notes")),
    "personal-history-recall.md (recall)": (
        "recall_personal_history",
        ("search_memory", "we talked about", "days_ago"),
    ),
    "personal-history-recall.md (search)": (
        "search_memory",
        ("recall_personal_history", "what do we know about"),
    ),
    "read-write.md (read)": ("read", ("grep -n", "offset", "tail_lines")),
    "read-write.md (write)": ("write", ("/app/agent_workspace", "append", "/tmp/")),
    "run-python.md": ("run_python", ("fresh container", "not importable", "network=true")),
    "web-search.md": ("web_search", ("not to search_memory", "is a guess", "categories")),
}


@pytest.mark.parametrize("skill", sorted(FOLDED_RULES))
def test_each_folded_skill_rule_is_in_its_tool_description(
    full_registry: ToolRegistry, skill: str
) -> None:
    """AC-3: a folded skill's "use this, not that" rule is in the tool's description."""
    tool_name, phrases = FOLDED_RULES[skill]
    entry = full_registry.get_tool(tool_name)
    assert entry is not None
    description = entry[0].description
    missing = [phrase for phrase in phrases if phrase not in description]
    assert not missing, f"{skill}: {tool_name} description lacks {missing}"
