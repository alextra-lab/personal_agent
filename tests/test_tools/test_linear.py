"""Unit tests for the native Linear tool (FRE-224).

Tests use mocked httpx responses — no Linear API key required.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import personal_agent.tools.linear as lm
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.linear import (
    create_linear_issue_executor,
    create_linear_issue_tool,
    find_linear_issues_executor,
    find_linear_issues_tool,
)

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
    with pytest.raises(ToolExecutionError, match="'title' is required"):
        await create_linear_issue_executor(title="", description="some body")


@pytest.mark.asyncio
async def test_create_empty_description_raises() -> None:
    """Empty description raises before any HTTP call."""
    with pytest.raises(ToolExecutionError, match="'description' is required"):
        await create_linear_issue_executor(title="A title", description="")


@pytest.mark.asyncio
async def test_create_title_too_long_raises() -> None:
    """Title over 255 chars raises before any HTTP call."""
    with pytest.raises(ToolExecutionError, match="exceeds 255"):
        await create_linear_issue_executor(title="x" * 256, description="body")


@pytest.mark.asyncio
async def test_create_bad_priority_raises() -> None:
    """Priority outside 1-4 raises before any HTTP call."""
    with pytest.raises(ToolExecutionError, match="priority"):
        await create_linear_issue_executor(title="T", description="D", priority=5)


@pytest.mark.asyncio
async def test_create_missing_api_key_raises() -> None:
    """Missing API key raises ToolExecutionError before any HTTP call."""
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = None
        ms.linear_agent_rate_limit_per_day = 10
        with pytest.raises(ToolExecutionError, match="API key not configured"):
            await create_linear_issue_executor(title="T", description="D")


@pytest.mark.asyncio
async def test_find_no_query_no_state_raises() -> None:
    """Empty query with no state filter raises before any HTTP call."""
    with pytest.raises(ToolExecutionError, match="Provide 'query'"):
        await find_linear_issues_executor(query="", state=None)


@pytest.mark.asyncio
async def test_find_state_only_is_valid() -> None:
    """State-only listing (no query text) is valid."""
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client([_issue_search_response([])])
            result = await find_linear_issues_executor(query="", state="Needs Approval")
    assert result["count"] == 0
    assert result["state"] == "Needs Approval"


@pytest.mark.asyncio
async def test_find_missing_api_key_raises() -> None:
    """Missing API key raises ToolExecutionError before any HTTP call."""
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = None
        with pytest.raises(ToolExecutionError, match="API key not configured"):
            await find_linear_issues_executor(query="something")


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
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client(responses)
            result = await create_linear_issue_executor(
                title="Test dry run",
                description="body",
                dry_run=True,
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
        _issue_create_response(),
    ]
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client(responses)
            result = await create_linear_issue_executor(
                title="Agent-detected memory issue",
                description="## Context\n\nMemory query latency spiked.",
                priority=2,
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
        _issue_create_response(),
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
        with patch("personal_agent.tools.linear.httpx.AsyncClient", return_value=client):
            await create_linear_issue_executor(title="First", description="body")
            first_count = call_count
            await create_linear_issue_executor(title="Second", description="body")
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
            result = await find_linear_issues_executor(query="memory leak")

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
            result = await find_linear_issues_executor(query="nonexistent thing xyz")

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
        with pytest.raises(ToolExecutionError, match="Rate limit"):
            await create_linear_issue_executor(title="Over limit", description="body")


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
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client(responses)
            result = await create_linear_issue_executor(
                title="Dry run regardless",
                description="body",
                dry_run=True,
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
        with patch("personal_agent.tools.linear.httpx.AsyncClient", return_value=client):
            with pytest.raises(ToolExecutionError, match="HTTP 500"):
                await create_linear_issue_executor(title="T", description="D")


@pytest.mark.asyncio
async def test_graphql_error_raises() -> None:
    """GraphQL errors array raises ToolExecutionError."""
    _clear_caches()
    error_response = {"data": None, "errors": [{"message": "You are not authorized"}]}
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client([error_response])
            with pytest.raises(ToolExecutionError, match="not authorized"):
                await create_linear_issue_executor(title="T", description="D")


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
        with patch("personal_agent.tools.linear.httpx.AsyncClient", return_value=client):
            with pytest.raises(ToolExecutionError, match="Cannot connect"):
                await create_linear_issue_executor(title="T", description="D")


@pytest.mark.asyncio
async def test_unknown_team_raises() -> None:
    """Unknown team name in API response raises ToolExecutionError."""
    _clear_caches()
    data = _gql_ok({"teams": {"nodes": [{"id": "t1", "name": "OtherTeam"}]}})
    with patch("personal_agent.tools.linear.settings") as ms:
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        with patch("personal_agent.tools.linear.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _mock_http_client([data])
            with pytest.raises(ToolExecutionError, match="team.*not found"):
                await create_linear_issue_executor(title="T", description="D")
