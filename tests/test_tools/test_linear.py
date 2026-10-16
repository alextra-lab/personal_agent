"""Unit tests for the native Linear tool (FRE-224).

Tests use mocked httpx responses — no Linear API key required.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import personal_agent.tools.linear as lm
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.linear import (
    create_linear_issue_executor,
    create_linear_issue_tool,
    find_linear_issues_executor,
    find_linear_issues_tool,
)

_CTX = TraceContext.new_trace()
# ── Helpers ────────────────────────────────────────────────────────────────


def _mock_http_client(responses: list[dict]) -> AsyncMock:
    """Build an AsyncClient mock that returns successive GQL responses."""
    call_count = 0

    async def fake_post(*args, **kwargs):
        nonlocal call_count
        body = responses[min(call_count, len(responses) - 1)]
        call_count += 1
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.is_error = False
        mock_resp.json.return_value = body
        mock_resp.text = ""
        return mock_resp

    client = AsyncMock()
    client.post = fake_post
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _gql_ok(data: dict) -> dict:
    """Wrap data in a GraphQL success envelope."""
    return {"data": data}


def _teams_response() -> dict:
    """GQL response returning a single FrenchForest team."""
    return _gql_ok({"teams": {"nodes": [{"id": "team-1", "name": "FrenchForest"}]}})


def _states_response() -> dict:
    """GQL response with two workflow states."""
    return _gql_ok(
        {
            "workflowStates": {
                "nodes": [
                    {"id": "state-na", "name": "Needs Approval"},
                    {"id": "state-ip", "name": "In Progress"},
                ]
            }
        }
    )


def _labels_response(extra: list[dict] | None = None) -> dict:
    """GQL response with PersonalAgent label (and optional extras)."""
    nodes = [{"id": "lbl-pa", "name": "PersonalAgent"}]
    if extra:
        nodes.extend(extra)
    return _gql_ok({"issueLabels": {"nodes": nodes}})


def _label_create_response(name: str = "agent-filed") -> dict:
    """GQL response for a successful label creation."""
    return _gql_ok({"issueLabelCreate": {"issueLabel": {"id": "lbl-af", "name": name}}})


def _issue_create_response() -> dict:
    """GQL response for a successful issue creation."""
    return _gql_ok(
        {
            "issueCreate": {
                "issue": {
                    "id": "issue-uuid",
                    "identifier": "FRE-999",
                    "title": "Test issue",
                    "url": "https://linear.app/frenchforest/issue/FRE-999",
                }
            }
        }
    )


def _issue_search_response(issues: list[dict] | None = None) -> dict:
    """GQL response for an issue search."""
    return _gql_ok({"issues": {"nodes": issues or []}})


def _clear_caches() -> None:
    """Reset module-level caches between tests."""
    lm._team_id_cache = None
    lm._state_id_cache.clear()
    lm._label_id_cache.clear()
    lm._labels_fetched_for_teams.clear()
    lm._rate_log.clear()


# ── Tool definition tests ──────────────────────────────────────────────────


def test_create_tool_definition() -> None:
    """create_linear_issue has correct metadata."""
    assert create_linear_issue_tool.name == "create_linear_issue"
    assert create_linear_issue_tool.category == "network"
    assert create_linear_issue_tool.risk_level == "medium"
    assert create_linear_issue_tool.allowed_modes == ["NORMAL"]
    param_names = {p.name for p in create_linear_issue_tool.parameters}
    assert {"title", "description", "priority", "project", "dry_run"} <= param_names


def test_find_tool_definition() -> None:
    """find_linear_issues has correct metadata."""
    assert find_linear_issues_tool.name == "find_linear_issues"
    assert find_linear_issues_tool.category == "network"
    assert find_linear_issues_tool.risk_level == "low"
    assert "DEGRADED" in find_linear_issues_tool.allowed_modes
    param_names = {p.name for p in find_linear_issues_tool.parameters}
    assert {"query", "state"} <= param_names


# ── Validation tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_empty_title_raises() -> None:
    """Empty title raises before any HTTP call."""
    with pytest.raises(ToolExecutionError, match="'title' is required") as exc_info:
        await create_linear_issue_executor(title="", description="some body", ctx=_CTX)
    assert exc_info.value.code == "invalid_argument"


@pytest.mark.asyncio
async def test_create_empty_description_raises() -> None:
    """Empty description raises before any HTTP call."""
    with pytest.raises(ToolExecutionError, match="'description' is required") as exc_info:
        await create_linear_issue_executor(title="A title", description="", ctx=_CTX)
    assert exc_info.value.code == "invalid_argument"


@pytest.mark.asyncio
async def test_create_title_too_long_raises() -> None:
    """Title over 255 chars raises before any HTTP call."""
    with pytest.raises(ToolExecutionError, match="exceeds 255") as exc_info:
        await create_linear_issue_executor(title="x" * 256, description="body", ctx=_CTX)
    assert exc_info.value.code == "invalid_argument"


@pytest.mark.asyncio
async def test_create_bad_priority_raises() -> None:
    """Priority outside 1-4 raises before any HTTP call."""
    with pytest.raises(ToolExecutionError, match="priority") as exc_info:
        await create_linear_issue_executor(title="T", description="D", priority=5, ctx=_CTX)
    assert exc_info.value.code == "invalid_priority"


@pytest.mark.asyncio
async def test_create_missing_api_key_raises() -> None:
    """Missing API key raises ToolExecutionError before any HTTP call."""
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = None
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        with pytest.raises(ToolExecutionError, match="API key not configured") as exc_info:
            await create_linear_issue_executor(title="T", description="D", ctx=_CTX)
        assert exc_info.value.code == "not_configured"


@pytest.mark.asyncio
async def test_find_no_query_no_state_raises() -> None:
    """Empty query with no state filter raises before any HTTP call."""
    with pytest.raises(ToolExecutionError, match="Provide 'query'") as exc_info:
        await find_linear_issues_executor(query="", state=None, ctx=_CTX)
    assert exc_info.value.code == "invalid_argument"


@pytest.mark.asyncio
async def test_find_state_only_is_valid() -> None:
    """State-only listing (no query text) is valid."""
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client([_issue_search_response([])])
            result = await find_linear_issues_executor(query="", state="Needs Approval", ctx=_CTX)
    assert result["count"] == 0
    assert result["state"] == "Needs Approval"


@pytest.mark.asyncio
async def test_find_missing_api_key_raises() -> None:
    """Missing API key raises ToolExecutionError before any HTTP call."""
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = None
        with pytest.raises(ToolExecutionError, match="API key not configured") as exc_info:
            await find_linear_issues_executor(query="something", ctx=_CTX)
        assert exc_info.value.code == "not_configured"


# ── dry_run tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_dry_run_returns_payload_without_creating() -> None:
    """dry_run=True resolves IDs but skips the issueCreate mutation."""
    _clear_caches()
    responses = [
        _teams_response(),
        _states_response(),
        _labels_response(),
        _label_create_response(),
    ]
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        ms.linear_personal_agent_label_id = "25004aac-3b32-4fa4-bdc2-55ff348ea842"
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client(responses)
            result = await create_linear_issue_executor(
                title="Test dry run",
                description="body",
                dry_run=True,
                ctx=_CTX,
            )

    assert result["dry_run"] is True
    assert result["title"] == "Test dry run"
    assert "payload" in result


# ── Success path tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_issue_success() -> None:
    """Full happy path returns identifier and URL."""
    _clear_caches()
    responses = [
        _teams_response(),
        _states_response(),
        _labels_response(),
        _label_create_response(),
        _issue_search_response([]),  # dedup
        _issue_search_response([]),  # the ticket-cap count (FRE-1611)
        _issue_create_response(),
    ]
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        ms.linear_personal_agent_label_id = "25004aac-3b32-4fa4-bdc2-55ff348ea842"
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client(responses)
            result = await create_linear_issue_executor(
                title="Agent-detected memory issue",
                description="## Context\n\nMemory query latency spiked.",
                priority=2,
                ctx=_CTX,
            )

    assert result["dry_run"] is False
    assert result["identifier"] == "FRE-999"
    assert "FRE-999" in result["url"]


@pytest.mark.asyncio
async def test_create_issue_uses_cached_ids_on_second_call() -> None:
    """Second call uses cached team/state/label IDs — fewer HTTP round-trips."""
    _clear_caches()
    all_responses = [
        _teams_response(),
        _states_response(),
        _labels_response(),
        _label_create_response(),
        _issue_search_response([]),  # first call: dedup
        _issue_search_response([]),  # first call: the ticket-cap count (FRE-1611)
        _issue_create_response(),
        _issue_search_response([]),  # second call: dedup
        _issue_search_response([]),  # second call: the ticket-cap count
        _issue_create_response(),
    ]
    call_count = 0

    async def fake_post(*args, **kwargs):
        nonlocal call_count
        body = all_responses[min(call_count, len(all_responses) - 1)]
        call_count += 1
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.is_error = False
        mock_resp.json.return_value = body
        mock_resp.text = ""
        return mock_resp

    client = AsyncMock()
    client.post = fake_post
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        ms.linear_personal_agent_label_id = "25004aac-3b32-4fa4-bdc2-55ff348ea842"
        with patch("personal_agent.tools.linear.httpx.AsyncClient", return_value=client):
            await create_linear_issue_executor(title="First", description="body", ctx=_CTX)
            first_count = call_count
            await create_linear_issue_executor(title="Second", description="body", ctx=_CTX)
            second_count = call_count - first_count

    assert second_count < first_count


@pytest.mark.asyncio
async def test_find_issues_returns_matching_results() -> None:
    """find_linear_issues maps API response to simple dicts."""
    _clear_caches()
    nodes = [
        {
            "identifier": "FRE-100",
            "title": "Memory leak detected",
            "url": "https://linear.app/frenchforest/issue/FRE-100",
            "state": {"name": "Needs Approval"},
        }
    ]
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client([_issue_search_response(nodes)])
            result = await find_linear_issues_executor(query="memory leak", ctx=_CTX)

    assert result["count"] == 1
    assert result["issues"][0]["identifier"] == "FRE-100"
    assert result["issues"][0]["state"] == "Needs Approval"


@pytest.mark.asyncio
async def test_find_issues_empty_results() -> None:
    """find_linear_issues returns empty list when no matches."""
    _clear_caches()
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client([_issue_search_response([])])
            result = await find_linear_issues_executor(query="nonexistent thing xyz", ctx=_CTX)

    assert result["count"] == 0
    assert result["issues"] == []


# ── Rate limit tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_blocks_after_threshold() -> None:
    """Rate limit raises ToolExecutionError when daily cap is exceeded."""
    _clear_caches()
    lm._rate_log["__unassigned__"] = [time.time()] * 3  # already at limit

    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 3
        with pytest.raises(ToolExecutionError, match="Rate limit") as exc_info:
            await create_linear_issue_executor(title="Over limit", description="body", ctx=_CTX)
        assert exc_info.value.code == "rate_limited"


@pytest.mark.asyncio
async def test_rate_limit_does_not_apply_to_dry_run() -> None:
    """dry_run=True bypasses the rate limit check."""
    _clear_caches()
    lm._rate_log["__unassigned__"] = [time.time()] * 100  # way over any limit

    responses = [
        _teams_response(),
        _states_response(),
        _labels_response(),
        _label_create_response(),
    ]
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 5
        ms.linear_personal_agent_label_id = "25004aac-3b32-4fa4-bdc2-55ff348ea842"
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client(responses)
            result = await create_linear_issue_executor(
                title="Dry run regardless",
                description="body",
                dry_run=True,
                ctx=_CTX,
            )
    assert result["dry_run"] is True


# ── Error path tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_error_raises() -> None:
    """Non-2xx HTTP response raises ToolExecutionError."""
    _clear_caches()

    async def bad_post(*args, **kwargs):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 500
        mock_resp.is_error = True
        mock_resp.text = "Internal Server Error"
        return mock_resp

    client = AsyncMock()
    client.post = bad_post
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        with patch("personal_agent.tools.linear.httpx.AsyncClient", return_value=client):
            with pytest.raises(ToolExecutionError, match="HTTP 500") as exc_info:
                await create_linear_issue_executor(title="T", description="D", ctx=_CTX)
            assert exc_info.value.code == "upstream_rejected"


@pytest.mark.asyncio
async def test_graphql_error_raises() -> None:
    """GraphQL errors array raises ToolExecutionError."""
    _clear_caches()
    error_response = {"data": None, "errors": [{"message": "You are not authorized"}]}
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client([error_response])
            with pytest.raises(ToolExecutionError, match="not authorized") as exc_info:
                await create_linear_issue_executor(title="T", description="D", ctx=_CTX)
            assert exc_info.value.code == "graphql_error"


@pytest.mark.asyncio
async def test_connect_error_raises() -> None:
    """Network connection failure raises ToolExecutionError."""
    _clear_caches()
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        with patch("personal_agent.tools.linear.httpx.AsyncClient", return_value=client):
            with pytest.raises(ToolExecutionError, match="Cannot connect") as exc_info:
                await create_linear_issue_executor(title="T", description="D", ctx=_CTX)
            assert exc_info.value.code == "upstream_unavailable"


@pytest.mark.asyncio
async def test_unknown_team_raises() -> None:
    """Unknown team name in API response raises ToolExecutionError."""
    _clear_caches()
    data = _gql_ok({"teams": {"nodes": [{"id": "t1", "name": "OtherTeam"}]}})
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client([data])
            with pytest.raises(ToolExecutionError, match="team.*not found") as exc_info:
                await create_linear_issue_executor(title="T", description="D", ctx=_CTX)
            assert exc_info.value.code == "team_not_found"


# ── GraphQL type annotation tests ─────────────────────────────────────────────


def test_get_state_id_query_uses_id_type() -> None:
    """_get_state_id must declare $teamId as ID! not String! (Linear schema requires ID)."""
    import inspect

    source = inspect.getsource(lm._get_state_id)
    assert "$teamId: ID!" in source
    assert "$teamId: String!" not in source


def test_get_label_id_query_uses_id_type() -> None:
    """_get_label_id must declare $teamId as ID! not String!."""
    import inspect

    source = inspect.getsource(lm._get_label_id)
    assert "$teamId: ID!" in source
    assert "$teamId: String!" not in source


def test_get_project_id_query_uses_id_type() -> None:
    """_get_project_id must declare $teamId as ID! not String!."""
    import inspect

    source = inspect.getsource(lm._get_project_id)
    assert "$teamId: ID!" in source
    assert "$teamId: String!" not in source


# ── FRE-309: config-backed label ID tests ─────────────────────────────────


@pytest.mark.asyncio
async def test_create_issue_uses_configured_label_id_not_name_lookup() -> None:
    """FRE-309: PersonalAgent label ID from config is used directly; GQL label fetch skipped.

    Verifies the template-first flow: when linear_personal_agent_label_id is configured,
    the executor places that UUID in labelIds without any name-based resolution call.
    """
    _clear_caches()
    configured_id = "25004aac-3b32-4fa4-bdc2-55ff348ea842"
    # dry_run skips issueCreate; agent-filed still needs: labels fetch + create.
    responses = [
        _teams_response(),
        _states_response(),
        _labels_response(),  # fetched once for agent-filed resolution
        _label_create_response("agent-filed"),
    ]
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        ms.linear_personal_agent_label_id = configured_id
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client(responses)
            result = await create_linear_issue_executor(
                title="Template-first test",
                description="Uses config label ID, not name lookup.",
                dry_run=True,
                ctx=_CTX,
            )

    assert result["dry_run"] is True
    # Configured UUID is in the payload — not the "lbl-pa" ID from the mock GQL response.
    assert configured_id in result["payload"]["labelIds"]
    assert "lbl-pa" not in result["payload"]["labelIds"]


@pytest.mark.asyncio
async def test_create_issue_raises_when_personal_agent_label_id_not_configured() -> None:
    """FRE-309: clear error when AGENT_LINEAR_PERSONAL_AGENT_LABEL_ID is not set."""
    _clear_caches()
    responses = [
        _teams_response(),
        _states_response(),
    ]
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        ms.linear_personal_agent_label_id = None
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client(responses)
            with pytest.raises(
                ToolExecutionError, match="PersonalAgent label ID not configured"
            ) as exc_info:
                await create_linear_issue_executor(title="T", description="D", ctx=_CTX)
            assert exc_info.value.code == "label_not_configured"


# ── Eval isolation tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_eval_mode_blocks_issue_creation() -> None:
    """create_linear_issue raises in eval sessions before any HTTP call."""
    eval_ctx = TraceContext(trace_id="test-trace", eval_mode=True)
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        with pytest.raises(ToolExecutionError, match="disabled in evaluation sessions") as exc_info:
            await create_linear_issue_executor(title="T", description="D", ctx=eval_ctx)
        assert exc_info.value.code == "eval_session"


@pytest.mark.asyncio
async def test_eval_mode_false_allows_creation() -> None:
    """eval_mode=False (default) does not block issue creation."""
    _clear_caches()
    non_eval_ctx = TraceContext(trace_id="test-trace", eval_mode=False)
    responses = [
        _teams_response(),
        _states_response(),
        _labels_response([{"id": "lbl-af", "name": "agent-filed"}]),
        _issue_search_response([]),  # dedup check returns empty
        _issue_search_response([]),  # the ticket-cap count (FRE-1611)
        _issue_create_response(),
    ]
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        ms.linear_personal_agent_label_id = "lbl-pa"
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client(responses)
            result = await create_linear_issue_executor(
                title="Valid issue", description="body", ctx=non_eval_ctx
            )
    assert result["identifier"] == "FRE-999"
    assert result["dry_run"] is False


# ── Eval deployment-profile isolation (FRE-1505) ───────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("executor", "kwargs"),
    [
        (create_linear_issue_executor, {"title": "T", "description": "D"}),
        (create_linear_issue_executor, {"title": "T", "description": "D", "dry_run": True}),
        (lm.create_linear_project_executor, {"name": "P"}),
        (find_linear_issues_executor, {"query": "q"}),
        (lm.list_linear_projects_executor, {}),
    ],
)
async def test_eval_profile_sends_no_request_to_linear(executor, kwargs) -> None:
    """AC-2: with deployment_profile=eval no Linear tool builds an HTTP client.

    The API key is set, so the refusal comes from the profile guard and not from the
    missing-key check. ctx.eval_mode is False, so the per-request guard is not what stops it.
    """
    _clear_caches()
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        ms.linear_personal_agent_label_id = "lbl-pa"
        ms.deployment_profile = "eval"
        with patch("personal_agent.tools.linear.create_guarded_http_client") as mock_client:
            with pytest.raises(ToolExecutionError, match="eval deployment") as exc_info:
                await executor(**kwargs, ctx=_CTX)
            assert exc_info.value.code == "eval_deployment"
    mock_client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["cloud", "local"])
async def test_non_eval_profile_still_sends_linear_request(profile: str) -> None:
    """AC-4: the production profiles still reach the Linear API."""
    _clear_caches()
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.deployment_profile = profile
        with patch("personal_agent.tools.linear.create_guarded_http_client") as mock_client:
            mock_client.return_value = _mock_http_client([_issue_search_response([])])
            result = await find_linear_issues_executor(query="q", ctx=_CTX)
    mock_client.assert_called_once()
    assert result["count"] == 0


# ── Dedup check tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dedup_blocks_when_similar_issue_exists() -> None:
    """create_linear_issue raises ToolExecutionError when a similar issue exists."""
    _clear_caches()
    existing = [
        {
            "identifier": "FRE-123",
            "title": "Existing similar issue",
            "url": "https://linear.app/frenchforest/issue/FRE-123",
        }
    ]
    responses = [
        _teams_response(),
        _states_response(),
        _labels_response([{"id": "lbl-af", "name": "agent-filed"}]),
        _issue_search_response(existing),  # dedup returns a match
    ]
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        ms.linear_personal_agent_label_id = "lbl-pa"
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client(responses)
            with pytest.raises(ToolExecutionError, match="Duplicate.*FRE-123") as exc_info:
                await create_linear_issue_executor(
                    title="Existing similar issue", description="body", ctx=_CTX
                )
            assert exc_info.value.code == "duplicate_issue"


@pytest.mark.asyncio
async def test_dedup_passes_when_no_similar_issue() -> None:
    """create_linear_issue proceeds when dedup check finds no matches."""
    _clear_caches()
    responses = [
        _teams_response(),
        _states_response(),
        _labels_response([{"id": "lbl-af", "name": "agent-filed"}]),
        _issue_search_response([]),  # dedup returns empty
        _issue_search_response([]),  # the ticket-cap count (FRE-1611)
        _issue_create_response(),
    ]
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        ms.linear_personal_agent_label_id = "lbl-pa"
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client(responses)
            result = await create_linear_issue_executor(
                title="Brand new issue", description="body", ctx=_CTX
            )
    assert result["identifier"] == "FRE-999"


# ── FRE-1575: one tool contract — every failure carries a declared code ────


def _raising_client(exc: Exception) -> AsyncMock:
    client = AsyncMock()
    client.post = AsyncMock(side_effect=exc)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _configured(ms: MagicMock) -> None:
    ms.linear_api_key = "lin_api_test"
    ms.linear_agent_rate_limit_per_day = 10
    ms.seshat_open_ticket_cap = 10
    ms.linear_personal_agent_label_id = "lbl-pa"


def test_each_tool_declares_its_codes() -> None:
    """Each Linear tool declares its own code set; the shared API codes are in every set."""
    api = {
        "eval_deployment",
        "not_configured",
        "egress_blocked",
        "upstream_unavailable",
        "upstream_timeout",
        "upstream_rejected",
        "graphql_error",
    }
    assert lm.find_linear_issues_tool.error_codes == api | {"invalid_argument"}
    assert lm.list_linear_projects_tool.error_codes == api | {"team_not_found"}
    assert lm.create_linear_project_tool.error_codes == api | {
        "team_not_found",
        "invalid_argument",
        "bad_reply",
    }
    assert lm.create_linear_issue_tool.error_codes == api | {
        "team_not_found",
        "invalid_argument",
        "invalid_priority",
        "eval_session",
        "rate_limited",
        "state_not_found",
        "label_not_found",
        "label_not_configured",
        "label_create_failed",
        "duplicate_issue",
        "ticket_cap_reached",
        "bad_reply",
    }
    for tool in (
        lm.create_linear_issue_tool,
        lm.find_linear_issues_tool,
        lm.list_linear_projects_tool,
        lm.create_linear_project_tool,
    ):
        assert f"{tool.name}(" in tool.description
        assert all(p.description for p in tool.parameters)


@pytest.mark.asyncio
async def test_timeout_and_egress_carry_codes() -> None:
    """A timeout is retryable upstream_timeout; an egress refusal is non-retryable."""
    from personal_agent.security import EgressBlockedError

    request = httpx.Request("POST", lm._LINEAR_URL)
    errors = []
    for exc in (
        httpx.TimeoutException("slow"),
        EgressBlockedError(request, "domain not allowlisted", None),
    ):
        _clear_caches()
        with patch("personal_agent.tools.linear.settings") as ms:
            _configured(ms)
            with patch(
                "personal_agent.tools.linear.httpx.AsyncClient", return_value=_raising_client(exc)
            ):
                with pytest.raises(ToolExecutionError) as exc_info:
                    await find_linear_issues_executor(query="q", ctx=_CTX)
        errors.append(exc_info.value)
    assert [(e.code, e.retryable) for e in errors] == [
        ("upstream_timeout", True),
        ("egress_blocked", False),
    ]


@pytest.mark.asyncio
async def test_client_http_error_is_not_retryable() -> None:
    """A 4xx (other than 429) is upstream_rejected and not retryable."""
    _clear_caches()

    async def bad_post(*args: object, **kwargs: object) -> MagicMock:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 400
        resp.is_error = True
        resp.text = "Bad Request"
        return resp

    client = AsyncMock()
    client.post = bad_post
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    with patch("personal_agent.tools.linear.settings") as ms:
        _configured(ms)
        with patch("personal_agent.tools.linear.httpx.AsyncClient", return_value=client):
            with pytest.raises(ToolExecutionError, match="HTTP 400") as exc_info:
                await find_linear_issues_executor(query="q", ctx=_CTX)
    assert exc_info.value.code == "upstream_rejected"
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_missing_workflow_state_is_state_not_found() -> None:
    """A team without the Needs Approval state refuses with state_not_found."""
    _clear_caches()
    no_state = _gql_ok({"workflowStates": {"nodes": [{"id": "s1", "name": "Todo"}]}})
    with patch("personal_agent.tools.linear.settings") as ms:
        _configured(ms)
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client([_teams_response(), no_state])
            with pytest.raises(ToolExecutionError, match="Needs Approval") as exc_info:
                await create_linear_issue_executor(title="T", description="D", ctx=_CTX)
    assert exc_info.value.code == "state_not_found"


@pytest.mark.asyncio
async def test_missing_label_without_auto_create_is_label_not_found() -> None:
    """_get_label_id refuses a missing label when it may not create one."""
    _clear_caches()
    with patch("personal_agent.tools.linear.settings") as ms:
        _configured(ms)
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client([_labels_response()])
            with pytest.raises(ToolExecutionError) as exc_info:
                await lm._get_label_id("team-1", "no-such-label")
    assert exc_info.value.code == "label_not_found"


@pytest.mark.asyncio
async def test_label_creation_without_id_is_label_create_failed() -> None:
    """A label creation reply without an id refuses with label_create_failed."""
    _clear_caches()
    no_id = _gql_ok({"issueLabelCreate": {"issueLabel": {}}})
    with patch("personal_agent.tools.linear.settings") as ms:
        _configured(ms)
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client(
                [_teams_response(), _states_response(), _labels_response(), no_id]
            )
            with pytest.raises(ToolExecutionError) as exc_info:
                await create_linear_issue_executor(title="T", description="D", ctx=_CTX)
    assert exc_info.value.code == "label_create_failed"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_issue_creation_without_identifier_is_bad_reply() -> None:
    """An issueCreate reply without an identifier is bad_reply and points at find_linear_issues."""
    _clear_caches()
    responses = [
        _teams_response(),
        _states_response(),
        _labels_response([{"id": "lbl-af", "name": "agent-filed"}]),
        _issue_search_response([]),
        _issue_search_response([]),  # the ticket-cap count (FRE-1611)
        _gql_ok({"issueCreate": {"issue": {}}}),
    ]
    with patch("personal_agent.tools.linear.settings") as ms:
        _configured(ms)
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client(responses)
            with pytest.raises(ToolExecutionError) as exc_info:
                await create_linear_issue_executor(title="T", description="D", ctx=_CTX)
    assert exc_info.value.code == "bad_reply"
    assert "find_linear_issues" in exc_info.value.next_action


@pytest.mark.asyncio
async def test_create_issue_assigns_known_project() -> None:
    """A project name that resolves is set as projectId on the dry-run payload."""
    _clear_caches()
    projects = _gql_ok(
        {"teams": {"nodes": [{"projects": {"nodes": [{"id": "proj-1", "name": "Recall"}]}}]}}
    )
    responses = [
        _teams_response(),
        _states_response(),
        _labels_response([{"id": "lbl-af", "name": "agent-filed"}]),
        projects,
    ]
    with patch("personal_agent.tools.linear.settings") as ms:
        _configured(ms)
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client(responses)
            result = await create_linear_issue_executor(
                title="T", description="D", project="recall", dry_run=True, ctx=_CTX
            )
    assert result["payload"]["projectId"] == "proj-1"


@pytest.mark.asyncio
async def test_list_projects_returns_projects() -> None:
    """list_linear_projects returns the team's projects."""
    _clear_caches()
    projects = _gql_ok(
        {
            "teams": {
                "nodes": [
                    {
                        "projects": {
                            "nodes": [
                                {
                                    "id": "p1",
                                    "name": "Recall",
                                    "description": "d",
                                    "url": "https://linear.app/p1",
                                    "state": {"name": "started"},
                                }
                            ]
                        }
                    }
                ]
            }
        }
    )
    with patch("personal_agent.tools.linear.settings") as ms:
        _configured(ms)
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client([_teams_response(), projects])
            result = await lm.list_linear_projects_executor(ctx=_CTX)
    assert result["count"] == 1
    assert result["projects"][0]["state"] == "started"


@pytest.mark.asyncio
async def test_create_project_validation_codes() -> None:
    """An empty name and a missing key each refuse with their own code before any request."""
    with pytest.raises(ToolExecutionError) as no_name:
        await lm.create_linear_project_executor(name="  ", ctx=_CTX)
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = None
        with pytest.raises(ToolExecutionError) as no_key:
            await lm.create_linear_project_executor(name="P", ctx=_CTX)
    assert (no_name.value.code, no_key.value.code) == ("invalid_argument", "not_configured")


@pytest.mark.asyncio
async def test_create_project_success_and_bad_reply() -> None:
    """A project creation returns its id; a reply without an id is bad_reply."""
    _clear_caches()
    created = _gql_ok(
        {"projectCreate": {"project": {"id": "p9", "name": "P", "url": "https://x/p9"}}}
    )
    with patch("personal_agent.tools.linear.settings") as ms:
        _configured(ms)
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client([_teams_response(), created])
            result = await lm.create_linear_project_executor(
                name="P", description="About P", ctx=_CTX
            )
            mock_cls.return_value = _mock_http_client([_gql_ok({"projectCreate": {}})])
            with pytest.raises(ToolExecutionError) as exc_info:
                await lm.create_linear_project_executor(name="P", ctx=_CTX)
    assert result == {"id": "p9", "name": "P", "url": "https://x/p9"}
    assert exc_info.value.code == "bad_reply"
    assert "list_linear_projects" in exc_info.value.next_action
