"""Unit tests for LinearClient GraphQL wrapper (FRE-243)."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import personal_agent.captains_log.linear_client as lc_mod
from personal_agent.captains_log.linear_client import (
    LinearClient,
    _duration_to_cutoff,
    _extract_labels,
    _normalize_issue_node,
    extract_issue_identifier_from_description,
    extract_linear_identifier,
)

# ── Helpers & fixtures ────────────────────────────────────────────────────────


def _mock_resp(data: dict[str, Any]) -> MagicMock:
    """Build a mock httpx response for a successful GraphQL call."""
    resp = MagicMock()
    resp.is_error = False
    resp.json.return_value = {"data": data}
    return resp


def _mock_err_resp(status: int = 400, text: str = "bad") -> MagicMock:
    """Build a mock httpx error response."""
    resp = MagicMock()
    resp.is_error = True
    resp.status_code = status
    resp.text = text
    return resp


@pytest.fixture(autouse=True)
def reset_lc_caches() -> Generator[None, None, None]:
    """Pre-populate module-level caches so ID resolution skips API calls."""
    lc_mod._lc_team_id = "team-id-test"
    lc_mod._lc_state_ids = {
        "Needs Approval": "state-needs-approval",
        "Approved": "state-approved",
        "Canceled": "state-canceled",
        "Duplicate": "state-duplicate",
    }
    lc_mod._lc_label_ids = {
        "PersonalAgent": "label-personal-agent",
        "Improvement": "label-improvement",
        "AgentFeedback": "label-agent-feedback",
        "Approved": "label-approved-fb",
        "Re-evaluated": "label-re-evaluated",
    }
    lc_mod._lc_label_team_fetched = "team-id-test"
    yield
    lc_mod._lc_team_id = None
    lc_mod._lc_state_ids = {}
    lc_mod._lc_label_ids = {}
    lc_mod._lc_label_team_fetched = None


def _make_httpx_patch(responses: list[MagicMock]) -> Any:
    """Return a context-manager patch for httpx.AsyncClient returning each response in sequence."""
    call_count = 0

    class _FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            """Accept constructor kwargs forwarded from httpx.AsyncClient(timeout=...)."""

        async def __aenter__(self) -> _FakeAsyncClient:
            """Enter async context."""
            return self

        async def __aexit__(self, *args: object) -> None:
            """Exit async context."""

        async def post(self, *args: object, **kwargs: object) -> MagicMock:
            """Return the next queued mock response."""
            nonlocal call_count
            resp = responses[call_count % len(responses)]
            call_count += 1
            return resp

    return patch("personal_agent.captains_log.linear_client.httpx.AsyncClient", _FakeAsyncClient)


# ── _call tests ───────────────────────────────────────────────────────────────


class TestCall:
    """Test the internal _call() GraphQL helper."""

    @pytest.mark.asyncio
    async def test_call_returns_data_field(self) -> None:
        """Successful response extracts the data field."""
        client = LinearClient()
        resp = _mock_resp({"foo": "bar"})
        with _make_httpx_patch([resp]):
            result = await client._call("{ foo }", {})
        assert result == {"foo": "bar"}

    @pytest.mark.asyncio
    async def test_call_raises_on_http_error(self) -> None:
        """HTTP 4xx/5xx raises RuntimeError with status code."""
        client = LinearClient()
        resp = _mock_err_resp(status=500, text="server error")
        with _make_httpx_patch([resp]):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                await client._call("{ foo }", {})

    @pytest.mark.asyncio
    async def test_call_raises_on_graphql_errors(self) -> None:
        """GraphQL errors array in response raises RuntimeError."""
        client = LinearClient()
        resp = MagicMock()
        resp.is_error = False
        resp.json.return_value = {"errors": [{"message": "not found"}]}
        with _make_httpx_patch([resp]):
            with pytest.raises(RuntimeError, match="not found"):
                await client._call("{ foo }", {})

    @pytest.mark.asyncio
    async def test_call_raises_when_no_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing API key raises RuntimeError before any network call."""
        monkeypatch.setattr(
            "personal_agent.captains_log.linear_client.settings.linear_api_key", None
        )
        client = LinearClient()
        with pytest.raises(RuntimeError, match="AGENT_LINEAR_API_KEY"):
            await client._call("{ foo }", {})

    @pytest.mark.asyncio
    async def test_call_raises_on_connect_error(self) -> None:
        """httpx.ConnectError is re-raised as RuntimeError."""
        import httpx

        client = LinearClient()

        class _BadClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                """Accept constructor kwargs forwarded from httpx.AsyncClient(timeout=...)."""

            async def __aenter__(self) -> _BadClient:
                """Enter context."""
                return self

            async def __aexit__(self, *a: object) -> None:
                """Exit context."""

            async def post(self, *a: object, **kw: object) -> MagicMock:
                """Raise a connect error."""
                raise httpx.ConnectError("refused")

        with patch("personal_agent.captains_log.linear_client.httpx.AsyncClient", _BadClient):
            with pytest.raises(RuntimeError, match="Cannot connect"):
                await client._call("{ foo }", {})


# ── create_issue ──────────────────────────────────────────────────────────────


class TestCreateIssue:
    """Test the create_issue operation."""

    @pytest.mark.asyncio
    async def test_create_issue_returns_identifier(self) -> None:
        """Successful creation returns the human issue identifier."""
        client = LinearClient()
        resp = _mock_resp(
            {
                "issueCreate": {
                    "issue": {
                        "id": "uuid-1",
                        "identifier": "FF-42",
                        "title": "T",
                        "url": "https://x",
                    }
                }
            }
        )
        with _make_httpx_patch([resp]):
            ident = await client.create_issue(
                title="Test",
                team="FrenchForest",
                description="body",
                priority=3,
                labels=["PersonalAgent", "Improvement"],
                state="Needs Approval",
                project="",
            )
        assert ident == "FF-42"

    @pytest.mark.asyncio
    async def test_create_issue_returns_none_on_missing_identifier(self) -> None:
        """Empty issue response returns None without raising."""
        client = LinearClient()
        resp = _mock_resp({"issueCreate": {"issue": {}}})
        with _make_httpx_patch([resp]):
            ident = await client.create_issue(
                "T", "FrenchForest", "body", 3, [], "Needs Approval", ""
            )
        assert ident is None

    @pytest.mark.asyncio
    async def test_create_issue_resolves_project_id(self) -> None:
        """Project name is resolved to ID and included in mutation input."""
        client = LinearClient()
        project_resp = _mock_resp(
            {
                "teams": {
                    "nodes": [{"projects": {"nodes": [{"id": "proj-1", "name": "My Project"}]}}]
                }
            }
        )
        issue_resp = _mock_resp(
            {"issueCreate": {"issue": {"id": "u2", "identifier": "FF-7", "title": "T", "url": ""}}}
        )
        with _make_httpx_patch([project_resp, issue_resp]):
            ident = await client.create_issue(
                "T", "FrenchForest", "", 3, [], "Needs Approval", "My Project"
            )
        assert ident == "FF-7"


# ── get_issue ─────────────────────────────────────────────────────────────────


class TestGetIssue:
    """Test the get_issue operation."""

    @pytest.mark.asyncio
    async def test_get_issue_returns_normalized_dict(self) -> None:
        """Issue dict is returned with labels.nodes flattened."""
        client = LinearClient()
        resp = _mock_resp(
            {
                "issue": {
                    "id": "abc",
                    "identifier": "FF-1",
                    "title": "Hello",
                    "description": "desc",
                    "url": "https://x",
                    "updatedAt": "2026-04-01T00:00:00Z",
                    "state": {"name": "Approved"},
                    "labels": {"nodes": [{"name": "PersonalAgent"}]},
                }
            }
        )
        with _make_httpx_patch([resp]):
            issue = await client.get_issue("FF-1")
        assert issue["identifier"] == "FF-1"
        assert issue["labels"] == [{"name": "PersonalAgent"}]

    @pytest.mark.asyncio
    async def test_get_issue_returns_empty_dict_when_not_found(self) -> None:
        """Null issue response returns empty dict."""
        client = LinearClient()
        resp = _mock_resp({"issue": None})
        with _make_httpx_patch([resp]):
            result = await client.get_issue("FF-999")
        assert result == {}


# ── list_issues ───────────────────────────────────────────────────────────────


class TestListIssues:
    """Test the list_issues operation."""

    @pytest.mark.asyncio
    async def test_list_issues_returns_normalized_list(self) -> None:
        """Returned nodes have labels.nodes flattened to a list."""
        client = LinearClient()
        resp = _mock_resp(
            {
                "issues": {
                    "nodes": [
                        {
                            "id": "i1",
                            "identifier": "FF-10",
                            "title": "Issue 10",
                            "description": "d",
                            "url": "",
                            "updatedAt": "2026-04-01T00:00:00Z",
                            "state": {"name": "Approved"},
                            "labels": {"nodes": [{"name": "PersonalAgent"}]},
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        )
        with _make_httpx_patch([resp]):
            issues = await client.list_issues(team="FrenchForest", label="PersonalAgent", limit=10)
        assert len(issues) == 1
        assert issues[0]["identifier"] == "FF-10"
        assert issues[0]["labels"] == [{"name": "PersonalAgent"}]

    @pytest.mark.asyncio
    async def test_list_issues_handles_updatedAt_duration(self) -> None:
        """updatedAt='-P3D' is converted to a cutoff datetime filter without raising."""
        client = LinearClient()
        resp = _mock_resp({"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}})
        with _make_httpx_patch([resp]):
            issues = await client.list_issues(
                team="FrenchForest",
                updatedAt="-P3D",
                includeArchived=False,
            )
        assert issues == []

    @pytest.mark.asyncio
    async def test_list_issues_empty_response(self) -> None:
        """Empty nodes list returns empty Python list."""
        client = LinearClient()
        resp = _mock_resp({"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}})
        with _make_httpx_patch([resp]):
            result = await client.list_issues()
        assert result == []


# ── count_non_archived_issues ─────────────────────────────────────────────────


class TestCountNonArchivedIssues:
    """Test the count_non_archived_issues operation."""

    @pytest.mark.asyncio
    async def test_count_single_page(self) -> None:
        """Single page of results returns correct count."""
        client = LinearClient()
        resp = _mock_resp(
            {
                "issues": {
                    "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        )
        with _make_httpx_patch([resp]):
            count = await client.count_non_archived_issues("FrenchForest")
        assert count == 3

    @pytest.mark.asyncio
    async def test_count_two_pages(self) -> None:
        """Paginated results are summed correctly across pages."""
        client = LinearClient()
        page1 = _mock_resp(
            {
                "issues": {
                    "nodes": [{"id": "a"}, {"id": "b"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor1"},
                }
            }
        )
        page2 = _mock_resp(
            {
                "issues": {
                    "nodes": [{"id": "c"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        )
        with _make_httpx_patch([page1, page2]):
            count = await client.count_non_archived_issues("FrenchForest", page_limit=2)
        assert count == 3


# ── update_issue ──────────────────────────────────────────────────────────────


class TestUpdateIssue:
    """Test the update_issue operation."""

    @pytest.mark.asyncio
    async def test_update_issue_resolves_state(self) -> None:
        """State name is resolved to ID before calling issueUpdate."""
        client = LinearClient()
        resp = _mock_resp({"issueUpdate": {"success": True}})
        with _make_httpx_patch([resp]):
            await client.update_issue("issue-id", state="Approved")

    @pytest.mark.asyncio
    async def test_update_issue_resolves_labels(self) -> None:
        """Label names are resolved to IDs before calling issueUpdate."""
        client = LinearClient()
        resp = _mock_resp({"issueUpdate": {"success": True}})
        with _make_httpx_patch([resp]):
            await client.update_issue("issue-id", labels=["PersonalAgent"])

    @pytest.mark.asyncio
    async def test_update_issue_passes_other_fields_through(self) -> None:
        """Unknown kwargs are forwarded to the mutation input unchanged."""
        client = LinearClient()
        resp = _mock_resp({"issueUpdate": {"success": True}})
        with _make_httpx_patch([resp]):
            await client.update_issue("issue-id", duplicateOf="other-issue-id")


# ── add_comment ───────────────────────────────────────────────────────────────


class TestAddComment:
    """Test the add_comment operation."""

    @pytest.mark.asyncio
    async def test_add_comment_success(self) -> None:
        """Comment is posted without raising on success."""
        client = LinearClient()
        resp = _mock_resp({"commentCreate": {"comment": {"id": "cmt-1"}}})
        with _make_httpx_patch([resp]):
            await client.add_comment("issue-id", "Great work!")


# ── list_comments ─────────────────────────────────────────────────────────────


class TestListComments:
    """Test the list_comments operation."""

    @pytest.mark.asyncio
    async def test_list_comments_returns_nodes(self) -> None:
        """Comment nodes are returned as a flat list of dicts."""
        client = LinearClient()
        resp = _mock_resp(
            {
                "issue": {
                    "comments": {
                        "nodes": [
                            {"id": "c1", "body": "Hello", "createdAt": "2026-04-01T00:00:00Z"},
                            {"id": "c2", "body": "World", "createdAt": "2026-04-02T00:00:00Z"},
                        ]
                    }
                }
            }
        )
        with _make_httpx_patch([resp]):
            comments = await client.list_comments("issue-id")
        assert len(comments) == 2
        assert comments[0]["body"] == "Hello"

    @pytest.mark.asyncio
    async def test_list_comments_empty(self) -> None:
        """No comments returns empty list."""
        client = LinearClient()
        resp = _mock_resp({"issue": {"comments": {"nodes": []}}})
        with _make_httpx_patch([resp]):
            comments = await client.list_comments("issue-id")
        assert comments == []


# ── list_issue_labels ─────────────────────────────────────────────────────────


class TestListIssueLabels:
    """Test the list_issue_labels operation."""

    @pytest.mark.asyncio
    async def test_list_issue_labels_with_team(self) -> None:
        """Team-scoped label list uses a teamId filter."""
        client = LinearClient()
        resp = _mock_resp(
            {
                "issueLabels": {
                    "nodes": [
                        {
                            "id": "l1",
                            "name": "PersonalAgent",
                            "color": "#000",
                            "isGroup": False,
                            "parent": None,
                        }
                    ]
                }
            }
        )
        with _make_httpx_patch([resp]):
            labels = await client.list_issue_labels(team="FrenchForest")
        assert labels[0]["name"] == "PersonalAgent"

    @pytest.mark.asyncio
    async def test_list_issue_labels_without_team(self) -> None:
        """No team argument returns all workspace labels."""
        client = LinearClient()
        resp = _mock_resp(
            {
                "issueLabels": {
                    "nodes": [
                        {
                            "id": "l1",
                            "name": "Global",
                            "color": "#fff",
                            "isGroup": False,
                            "parent": None,
                        }
                    ]
                }
            }
        )
        with _make_httpx_patch([resp]):
            labels = await client.list_issue_labels()
        assert labels[0]["name"] == "Global"


# ── create_label ──────────────────────────────────────────────────────────────


class TestCreateLabel:
    """Test the create_label operation."""

    @pytest.mark.asyncio
    async def test_create_label_no_parent(self) -> None:
        """Top-level label is created and its ID is cached."""
        client = LinearClient()
        resp = _mock_resp(
            {"issueLabelCreate": {"issueLabel": {"id": "new-l-1", "name": "NewLabel"}}}
        )
        with _make_httpx_patch([resp]):
            await client.create_label("NewLabel", "#123456")
        assert lc_mod._lc_label_ids["NewLabel"] == "new-l-1"

    @pytest.mark.asyncio
    async def test_create_label_with_parent(self) -> None:
        """Child label resolves parent from cache and sends parentId in mutation."""
        client = LinearClient()
        resp = _mock_resp(
            {"issueLabelCreate": {"issueLabel": {"id": "new-l-2", "name": "ChildLabel"}}}
        )
        with _make_httpx_patch([resp]):
            await client.create_label("ChildLabel", "#abcdef", parent="AgentFeedback")
        assert lc_mod._lc_label_ids["ChildLabel"] == "new-l-2"

    @pytest.mark.asyncio
    async def test_create_label_is_group(self) -> None:
        """Group label includes isGroup=True in the mutation input."""
        client = LinearClient()
        resp = _mock_resp(
            {"issueLabelCreate": {"issueLabel": {"id": "grp-1", "name": "GroupLabel"}}}
        )
        with _make_httpx_patch([resp]):
            await client.create_label("GroupLabel", "#aabbcc", is_group=True)


# ── ensure_feedback_labels ────────────────────────────────────────────────────


class TestEnsureFeedbackLabels:
    """Test the ensure_feedback_labels idempotent setup operation."""

    @pytest.mark.asyncio
    async def test_ensure_feedback_labels_idempotent_when_all_exist(self) -> None:
        """No create_label calls when all labels already present in the list response."""
        client = LinearClient()
        all_names = list(lc_mod.FEEDBACK_LABELS_SPEC) + list(lc_mod.RESPONSE_LABELS_SPEC)
        existing = [{"name": n, "id": f"id-{n}", "color": "#000"} for n in all_names]

        list_resp = _mock_resp({"issueLabels": {"nodes": existing}})
        with _make_httpx_patch([list_resp]):
            await client.ensure_feedback_labels(team_name="FrenchForest")

    @pytest.mark.asyncio
    async def test_ensure_feedback_labels_creates_missing(self) -> None:
        """Labels absent from the list response are created via create_label."""
        client = LinearClient()
        existing = [{"name": "AgentFeedback", "id": "af-id", "color": "#95A2B3"}]

        list_resp = _mock_resp({"issueLabels": {"nodes": existing}})
        create_resp = _mock_resp(
            {"issueLabelCreate": {"issueLabel": {"id": "new-id", "name": "Approved"}}}
        )
        missing_count = len(lc_mod.FEEDBACK_LABELS_SPEC) - 1 + len(lc_mod.RESPONSE_LABELS_SPEC)
        with _make_httpx_patch([list_resp] + [create_resp] * missing_count):
            await client.ensure_feedback_labels(team_name="FrenchForest")


# ── Module-level helpers ──────────────────────────────────────────────────────


class TestModuleHelpers:
    """Test module-level helper functions."""

    def test_extract_labels_from_list_of_dicts(self) -> None:
        """Labels as list of dicts are extracted by name key."""
        issue = {"labels": [{"name": "A"}, {"name": "B"}]}
        assert _extract_labels(issue) == ["A", "B"]

    def test_extract_labels_from_list_of_strings(self) -> None:
        """Labels as list of strings are returned as-is."""
        issue = {"labels": ["X", "Y"]}
        assert _extract_labels(issue) == ["X", "Y"]

    def test_extract_labels_empty(self) -> None:
        """Issue with no labels key returns empty list."""
        assert _extract_labels({}) == []

    def test_extract_labels_falls_back_to_label_ids(self) -> None:
        """LabelIds key is used when labels key is absent."""
        issue = {"labelIds": [{"name": "Z"}]}
        assert _extract_labels(issue) == ["Z"]

    def test_normalize_issue_node_flattens_labels(self) -> None:
        """labels.nodes dict structure is flattened to a plain list."""
        node = {
            "id": "x",
            "identifier": "FF-1",
            "labels": {"nodes": [{"name": "Foo"}]},
        }
        result = _normalize_issue_node(node)
        assert result["labels"] == [{"name": "Foo"}]

    def test_normalize_issue_node_passthrough_when_already_list(self) -> None:
        """Already-flat labels list passes through unchanged."""
        node = {"id": "x", "labels": [{"name": "Bar"}]}
        result = _normalize_issue_node(node)
        assert result["labels"] == [{"name": "Bar"}]

    def test_extract_issue_identifier_html_comment(self) -> None:
        """Fingerprint in HTML comment is parsed correctly."""
        assert (
            extract_issue_identifier_from_description("<!-- fingerprint: abcd1234 -->")
            == "abcd1234"
        )

    def test_extract_issue_identifier_markdown(self) -> None:
        """Fingerprint in bold Markdown line is parsed correctly."""
        assert (
            extract_issue_identifier_from_description("**Fingerprint**: `DEADBEEF`") == "deadbeef"
        )

    def test_extract_issue_identifier_none(self) -> None:
        """Description without fingerprint returns None."""
        assert extract_issue_identifier_from_description("no fingerprint here") is None

    def test_extract_linear_identifier_from_issue_dict(self) -> None:
        """Identifier is extracted from issueCreate response dict."""
        result = {"issue": {"identifier": "FF-99"}}
        assert extract_linear_identifier(result) == "FF-99"

    def test_extract_linear_identifier_fallback_string(self) -> None:
        """Identifier is parsed from a plain string fallback."""
        assert extract_linear_identifier("FF-12 some title") == "FF-12"

    def test_duration_to_cutoff_negative_3d(self) -> None:
        """-P3D resolves to a datetime ~3 days in the past."""
        cutoff = _duration_to_cutoff("-P3D")
        assert cutoff is not None
        assert "2026" in cutoff or "2025" in cutoff

    def test_duration_to_cutoff_unparseable(self) -> None:
        """Unrecognised duration string returns None."""
        assert _duration_to_cutoff("notaduration") is None

    def test_linear_client_no_args_constructor(self) -> None:
        """LinearClient() constructs without arguments."""
        client = LinearClient()
        assert client is not None
