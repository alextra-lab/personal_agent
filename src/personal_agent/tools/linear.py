"""Native Linear tool — FRE-224 (ADR-0028 Tier-1).

Creates Linear issues from agent log analysis and reflection, and finds
similar issues for deduplication.  Calls the Linear GraphQL API directly
with a Personal Access Token — no Docker Desktop MCP gateway required.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Literal

import httpx

from personal_agent.config import settings
from personal_agent.linear_labels import (
    AGENT_AUTHORED_LABEL,
    AGENT_AUTHORED_LABEL_COLOR,
    open_pipeline_issues_filter,
)
from personal_agent.security import EgressBlockedError, create_guarded_http_client
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.contract import ToolError, error_codes
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

# ── Failure codes (ADR-0156 D6.1) ─────────────────────────────────────────

LinearApiCode = Literal[
    "eval_deployment",
    "not_configured",
    "egress_blocked",
    "upstream_unavailable",
    "upstream_timeout",
    "upstream_rejected",
    "graphql_error",
]
"""Failures of the shared GraphQL call, which every Linear tool makes."""

LinearSharedCode = Literal[LinearApiCode, "team_not_found"]
"""Failures raised by helpers that several Linear tools call."""

CreateLinearIssueCode = Literal[
    LinearSharedCode,
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
]
FindLinearIssuesCode = Literal[LinearApiCode, "invalid_argument"]
ListLinearProjectsCode = LinearSharedCode
CreateLinearProjectCode = Literal[LinearSharedCode, "invalid_argument", "bad_reply"]


class LinearError(ToolError[LinearSharedCode]):
    """A failure in a helper shared by the Linear tools."""


class CreateLinearIssueError(ToolError[CreateLinearIssueCode]):
    """A ``create_linear_issue`` failure."""


class FindLinearIssuesError(ToolError[FindLinearIssuesCode]):
    """A ``find_linear_issues`` failure."""


class ListLinearProjectsError(ToolError[ListLinearProjectsCode]):
    """A ``list_linear_projects`` failure (it raises only the shared codes)."""


class CreateLinearProjectError(ToolError[CreateLinearProjectCode]):
    """A ``create_linear_project`` failure."""


_RETRY_NEXT = "Call the same Linear tool once more; if it fails again, tell the user."
_NOT_CONFIGURED_NEXT = "Linear is unavailable on this deployment. Tell the user; do not retry."

_LINEAR_URL = "https://api.linear.app/graphql"

_TEAM_NAME = "FrenchForest"
_NEEDS_APPROVAL_STATE = "Needs Approval"
_PERSONAL_AGENT_LABEL = "PersonalAgent"

# Module-level ID cache — avoids repeated API round-trips within a process lifetime.
_team_id_cache: str | None = None
_state_id_cache: dict[str, str] = {}  # state_name → id
_label_id_cache: dict[str, str] = {}  # label_name → id
_labels_fetched_for_teams: set[str] = set()  # teams whose label list was already fetched

# In-memory rate-limit tracker: project_key → list of epoch timestamps
_rate_log: dict[str, list[float]] = defaultdict(list)


# ── Tool definitions ──────────────────────────────────────────────────────

create_linear_issue_tool = ToolDefinition(
    name="create_linear_issue",
    description=(
        "Create a Linear issue in the FrenchForest team, in 'Needs Approval' state with the "
        "'PersonalAgent' and 'agent-filed' labels, so a human approves it before any work. "
        "Use it to file a bug, gap or improvement found in agent logs, errors or "
        "self-reflection, about components that exist in this project (src/personal_agent/). "
        "Do not use it before calling find_linear_issues with the proposed title: if a match "
        "exists, do not file a duplicate (a title match is refused). Do not use it in eval "
        "sessions or for other projects' files. "
        "Agent filing is rate-limited per project per day and is refused when the cap of open "
        "self-filed tickets is full; dry_run=true previews the payload without creating anything. "
        'Example: create_linear_issue(title="Recall drops tags on notes_search", '
        'description="## Evidence\\n...\\n## Proposed action\\n...", priority=3).'
    ),
    category="network",
    parameters=[
        ToolParameter(
            name="title",
            type="string",
            description="Short issue title (under 120 characters).",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="description",
            type="string",
            description="Markdown body — include context, evidence, and proposed action.",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="priority",
            type="number",
            description="Linear priority: 1=Urgent, 2=High, 3=Normal (default), 4=Low.",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="project",
            type="string",
            description="Linear project name to assign. Omit to leave unassigned.",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="dry_run",
            type="boolean",
            description="If true, return the resolved payload without creating the issue.",
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="medium",
    allowed_modes=["NORMAL"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    rate_limit_per_hour=20,
    error_codes=error_codes(CreateLinearIssueError),
)

find_linear_issues_tool = ToolDefinition(
    name="find_linear_issues",
    description=(
        "Search Linear issues by title text, or list the issues in one workflow state, "
        "newest-updated first, with identifier, title, state, priority and URL. "
        "Use it before create_linear_issue to check for duplicates, or to see what is "
        "queued in a state. "
        "Do not use it for projects (use list_linear_projects). "
        "Give query, state, or both; it returns at most 'limit' issues (default 25, max 50) "
        "and matches titles only, not descriptions. "
        'Examples: find_linear_issues(query="notes_search tags"), '
        'find_linear_issues(state="Needs Approval", limit=10).'
    ),
    category="network",
    parameters=[
        ToolParameter(
            name="query",
            type="string",
            description="Text to search for in issue titles. Leave empty to list by state.",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="state",
            type="string",
            description="Filter by state name (e.g. 'Needs Approval', 'In Progress', 'Approved').",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="limit",
            type="number",
            description="Max results to return (default 25, max 50).",
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=15,
    rate_limit_per_hour=100,
    error_codes=error_codes(FindLinearIssuesError),
)

list_linear_projects_tool = ToolDefinition(
    name="list_linear_projects",
    description=(
        "List the FrenchForest team's Linear projects with id, name, state, URL and the first "
        "200 characters of each description. "
        "Use it to find the exact project name before passing project= to "
        "create_linear_issue, or for an overview of active work. "
        "Do not use it to find issues (use find_linear_issues). "
        "It returns at most 100 projects and takes no arguments. "
        "Example: list_linear_projects()."
    ),
    category="network",
    parameters=[],
    risk_level="low",
    allowed_modes=["NORMAL", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=15,
    rate_limit_per_hour=60,
    error_codes=error_codes(ListLinearProjectsError),
)

create_linear_project_tool = ToolDefinition(
    name="create_linear_project",
    description=(
        "Create a new Linear project in the FrenchForest team and return its id, name and URL. "
        "Use it only when a body of related issues needs a container that does not exist yet. "
        "Do not use it before list_linear_projects shows no fitting project, and do not use it "
        "to file a single piece of work (use create_linear_issue). "
        "The project is created with no state; a human should review it. "
        'Example: create_linear_project(name="Notes recall quality", '
        'description="Issues about notes_search ranking and tag filters.").'
    ),
    category="network",
    parameters=[
        ToolParameter(
            name="name",
            type="string",
            description="Project name.",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="description",
            type="string",
            description="Markdown project description.",
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="medium",
    allowed_modes=["NORMAL"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=15,
    rate_limit_per_hour=10,
    error_codes=error_codes(CreateLinearProjectError),
)


# ── GraphQL helper ────────────────────────────────────────────────────────


async def _gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute a Linear GraphQL operation.

    Args:
        query: GraphQL query or mutation string.
        variables: Optional variables dict.

    Returns:
        The ``data`` field from the GraphQL response.

    Raises:
        LinearError: On an eval deployment, missing API key, egress refusal, connection
            failure, timeout, HTTP error, or GraphQL error.
    """
    # FRE-1505: an eval gateway must never reach the real Linear workspace. Every Linear
    # tool routes through here, so refusing before the client is built covers all of them.
    if settings.deployment_profile == "eval":
        raise LinearError(
            "eval_deployment",
            "Linear API calls are disabled on an eval deployment "
            "(deployment_profile=eval); no request was sent.",
            retryable=False,
            next_action="No Linear call can work on this deployment. Do not retry.",
        )
    api_key = settings.linear_api_key
    if not api_key:
        raise LinearError(
            "not_configured",
            "Linear API key not configured. Set AGENT_LINEAR_API_KEY in .env.",
            retryable=False,
            next_action=_NOT_CONFIGURED_NEXT,
        )
    try:
        async with create_guarded_http_client(timeout=25) as client:
            resp = await client.post(
                _LINEAR_URL,
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": api_key, "Content-Type": "application/json"},
            )
    except EgressBlockedError as exc:
        raise LinearError(
            "egress_blocked",
            f"Linear API call refused by egress policy: {exc.reason}",
            retryable=False,
            next_action="Linear cannot be reached from here. Tell the user; do not retry.",
        ) from exc
    except httpx.ConnectError as exc:
        raise LinearError(
            "upstream_unavailable",
            "Cannot connect to Linear API (api.linear.app).",
            retryable=True,
            next_action=_RETRY_NEXT,
        ) from exc
    except httpx.TimeoutException as exc:
        raise LinearError(
            "upstream_timeout",
            "Linear API request timed out.",
            retryable=True,
            next_action=_RETRY_NEXT,
        ) from exc

    if resp.is_error:
        transient = resp.status_code == 429 or resp.status_code >= 500
        raise LinearError(
            "upstream_rejected",
            f"Linear API HTTP {resp.status_code}: {resp.text[:400]}",
            retryable=transient,
            next_action=_RETRY_NEXT if transient else "Do not repeat this call. Tell the user.",
        )
    body = resp.json()
    if errors := body.get("errors"):
        msgs = "; ".join(e.get("message", str(e)) for e in errors)
        raise LinearError(
            "graphql_error",
            f"Linear GraphQL error: {msgs}",
            retryable=False,
            next_action="Correct the arguments as the message states and call again, or tell "
            "the user.",
        )
    return body.get("data") or {}


# ── ID resolution (cached per process) ───────────────────────────────────


async def _get_team_id(team_name: str) -> str:
    """Return the Linear team's id, cached for the process.

    Args:
        team_name: The team name (case-insensitive).

    Returns:
        The team id.

    Raises:
        LinearError: When no team has that name, or the GraphQL call fails.
    """
    global _team_id_cache
    if _team_id_cache:
        return _team_id_cache
    data = await _gql("{ teams { nodes { id name } } }")
    teams = (data.get("teams") or {}).get("nodes") or []
    for t in teams:
        if t.get("name", "").lower() == team_name.lower():
            _team_id_cache = str(t["id"])
            return _team_id_cache
    names = [t.get("name") for t in teams]
    raise LinearError(
        "team_not_found",
        f"Linear team '{team_name}' not found. Available: {names}",
        retryable=False,
        next_action="The API key cannot see the team. Tell the user; do not retry.",
    )


async def _get_state_id(team_id: str, state_name: str) -> str:
    """Return a workflow state's id for ``create_linear_issue``, cached for the process.

    Args:
        team_id: The team id.
        state_name: The workflow state name.

    Returns:
        The state id.

    Raises:
        CreateLinearIssueError: When the team has no state with that name.
        LinearError: When the GraphQL call fails.
    """
    if state_name in _state_id_cache:
        return _state_id_cache[state_name]
    data = await _gql(
        """
        query($teamId: ID!) {
          workflowStates(filter: { team: { id: { eq: $teamId } } }) {
            nodes { id name }
          }
        }
        """,
        {"teamId": team_id},
    )
    states = (data.get("workflowStates") or {}).get("nodes") or []
    for s in states:
        _state_id_cache[str(s["name"])] = str(s["id"])
    if state_name not in _state_id_cache:
        names = list(_state_id_cache.keys())
        raise CreateLinearIssueError(
            "state_not_found",
            f"Linear workflow state '{state_name}' not found. Available: {names}",
            retryable=False,
            next_action="The team's workflow lacks this state. Tell the user; do not retry.",
        )
    return _state_id_cache[state_name]


async def _refuse_at_ticket_cap() -> None:
    """Refuse to file when the open pipeline-filed tickets reach the self-created cap.

    The tool honors the same cap as the promotion pipeline, over the same population
    (FRE-1611). The tool's own tickets do not count. The count query fails closed: a
    failed call raises ``LinearError``, so an unreadable count cannot open the cap.

    Raises:
        CreateLinearIssueError: ``ticket_cap_reached`` when the cap is full.
        LinearError: When the GraphQL call fails.
    """
    cap = settings.seshat_open_ticket_cap
    data = await _gql(
        """query($filter: IssueFilter, $first: Int) {
          issues(filter: $filter, first: $first) {
            nodes { id }
          }
        }""",
        {"filter": open_pipeline_issues_filter(_TEAM_NAME), "first": cap},
    )
    open_count = len((data.get("issues") or {}).get("nodes") or [])
    if open_count >= cap:
        raise CreateLinearIssueError(
            "ticket_cap_reached",
            f"The cap of {cap} open self-filed tickets is reached ({open_count} open).",
            retryable=False,
            next_action=(
                "Do not file it. Describe the issue in your answer. "
                "The owner must close or cancel open tickets before new ones are filed."
            ),
        )


async def _get_label_id(
    team_id: str,
    label_name: str,
    *,
    auto_create_color: str | None = None,
    trace_id: str | None = None,
) -> str:
    """Return a label's id for ``create_linear_issue``, creating it when asked.

    Args:
        team_id: The team id.
        label_name: The label name.
        auto_create_color: Create the label with this color when it is missing.
        trace_id: Trace id of the call.

    Returns:
        The label id.

    Raises:
        CreateLinearIssueError: When the label is missing and not auto-created, or its
            creation returns no id.
        LinearError: When a GraphQL call fails.
    """
    if label_name in _label_id_cache:
        return _label_id_cache[label_name]
    # Fetch the label list once per team — subsequent calls skip this if team was already fetched.
    if team_id not in _labels_fetched_for_teams:
        data = await _gql(
            """
            query($teamId: ID!) {
              issueLabels(filter: { team: { id: { eq: $teamId } } }) {
                nodes { id name }
              }
            }
            """,
            {"teamId": team_id},
        )
        labels = (data.get("issueLabels") or {}).get("nodes") or []
        for lbl in labels:
            _label_id_cache[str(lbl["name"])] = str(lbl["id"])
        _labels_fetched_for_teams.add(team_id)
    if label_name not in _label_id_cache:
        if auto_create_color is None:
            raise CreateLinearIssueError(
                "label_not_found",
                f"Linear label '{label_name}' not found.",
                retryable=False,
                next_action="The team lacks this label. Tell the user; do not retry.",
            )
        mut = await _gql(
            """
            mutation($input: IssueLabelCreateInput!) {
              issueLabelCreate(input: $input) {
                issueLabel { id name }
              }
            }
            """,
            {"input": {"name": label_name, "color": auto_create_color, "teamId": team_id}},
        )
        new_label = (mut.get("issueLabelCreate") or {}).get("issueLabel") or {}
        if not new_label.get("id"):
            raise CreateLinearIssueError(
                "label_create_failed",
                f"Failed to create Linear label '{label_name}': the reply had no label id.",
                retryable=True,
                next_action=_RETRY_NEXT,
            )
        _label_id_cache[label_name] = str(new_label["id"])
        log.info("linear_label_created", label=label_name, trace_id=trace_id)
    return _label_id_cache[label_name]


async def _get_project_id(team_id: str, project_name: str) -> str | None:
    data = await _gql(
        """
        query($teamId: ID!) {
          teams(filter: { id: { eq: $teamId } }) {
            nodes {
              projects { nodes { id name } }
            }
          }
        }
        """,
        {"teamId": team_id},
    )
    team_nodes = (data.get("teams") or {}).get("nodes") or []
    if not team_nodes:
        return None
    projects = (team_nodes[0].get("projects") or {}).get("nodes") or []
    for p in projects:
        if p.get("name", "").lower() == project_name.lower():
            return str(p["id"])
    return None


# ── Config-backed ID resolution (FRE-309) ─────────────────────────────────


def _require_personal_agent_label_id() -> str:
    """Return the PersonalAgent label ID from config, never from name lookup.

    Returns:
        UUID string for the PersonalAgent Linear label.

    Raises:
        CreateLinearIssueError: When AGENT_LINEAR_PERSONAL_AGENT_LABEL_ID is not set.
    """
    label_id = settings.linear_personal_agent_label_id
    if not isinstance(label_id, str) or not label_id.strip():
        raise CreateLinearIssueError(
            "label_not_configured",
            "PersonalAgent label ID not configured. "
            "Set AGENT_LINEAR_PERSONAL_AGENT_LABEL_ID in .env "
            "(expected value: 25004aac-3b32-4fa4-bdc2-55ff348ea842).",
            retryable=False,
            next_action=_NOT_CONFIGURED_NEXT,
        )
    return label_id.strip()


# ── Rate limiting ─────────────────────────────────────────────────────────


def _check_rate_limit(project_key: str) -> None:
    """Refuse an issue over the per-project daily filing limit.

    Args:
        project_key: The project name, or ``__unassigned__``.

    Raises:
        CreateLinearIssueError: When the limit is reached.
    """
    limit = settings.linear_agent_rate_limit_per_day
    now = time.time()
    cutoff = now - 86400.0
    fresh = [t for t in _rate_log[project_key] if t > cutoff]
    _rate_log[project_key] = fresh
    if len(fresh) >= limit:
        raise CreateLinearIssueError(
            "rate_limited",
            f"Rate limit: already filed {limit} agent issues in the last 24h "
            f"(project='{project_key}'). Wait before creating more.",
            retryable=False,
            next_action="Do not file more issues today. Tell the user what you would have filed.",
        )


# ── Executors ─────────────────────────────────────────────────────────────


async def create_linear_issue_executor(
    title: str = "",
    description: str = "",
    priority: int | None = None,
    project: str | None = None,
    dry_run: bool = False,
    *,
    ctx: TraceContext,
) -> dict[str, Any]:
    """Create a Linear issue from agent analysis.

    Args:
        title: Issue title.
        description: Markdown body with context and evidence.
        priority: 1=Urgent, 2=High, 3=Normal (default), 4=Low.
        project: Optional Linear project name.
        dry_run: If True, return resolved payload without creating.
        ctx: Trace context.

    Returns:
        Dict with ``identifier``, ``url``, ``title``, and ``dry_run`` keys.

    Raises:
        CreateLinearIssueError: On validation failure, an eval session, the rate limit, a
            missing state or label, a duplicate title, or an empty creation reply.
        LinearError: On a missing API key, team, or a failed GraphQL call.
    """
    title = (title or "").strip()
    if not title:
        raise CreateLinearIssueError(
            "invalid_argument",
            "'title' is required and cannot be empty.",
            retryable=False,
            next_action="Call create_linear_issue again with a short title.",
        )
    description = (description or "").strip()
    if not description:
        raise CreateLinearIssueError(
            "invalid_argument",
            "'description' is required and cannot be empty.",
            retryable=False,
            next_action="Call create_linear_issue again with a markdown description.",
        )
    if len(title) > 255:
        raise CreateLinearIssueError(
            "invalid_argument",
            f"'title' exceeds 255 characters ({len(title)}).",
            retryable=False,
            next_action="Call create_linear_issue again with a title under 120 characters.",
        )

    priority = int(priority) if priority is not None else 3
    if priority not in (1, 2, 3, 4):
        raise CreateLinearIssueError(
            "invalid_priority",
            "'priority' must be 1 (Urgent), 2 (High), 3 (Normal), or 4 (Low).",
            retryable=False,
            next_action="Call create_linear_issue again with priority 1, 2, 3 or 4.",
        )

    if not settings.linear_api_key:
        raise CreateLinearIssueError(
            "not_configured",
            "Linear API key not configured. Set AGENT_LINEAR_API_KEY in .env.",
            retryable=False,
            next_action=_NOT_CONFIGURED_NEXT,
        )

    if ctx.eval_mode:
        raise CreateLinearIssueError(
            "eval_session",
            "Issue creation is disabled in evaluation sessions. "
            "This avoids filing tickets about synthetic eval prompts.",
            retryable=False,
            next_action="Do not file issues in this session. Describe the issue in your answer.",
        )

    project_key = project or "__unassigned__"
    if not dry_run:
        _check_rate_limit(project_key)

    trace_id = ctx.trace_id
    log.info(
        "linear_create_issue_start",
        trace_id=trace_id,
        title=title[:80],
        priority=priority,
        project=project,
        dry_run=dry_run,
    )

    team_id = await _get_team_id(_TEAM_NAME)
    state_id = await _get_state_id(team_id, _NEEDS_APPROVAL_STATE)
    personal_agent_label_id = _require_personal_agent_label_id()
    agent_filed_label_id = await _get_label_id(
        team_id,
        AGENT_AUTHORED_LABEL,
        auto_create_color=AGENT_AUTHORED_LABEL_COLOR,
        trace_id=trace_id,
    )

    # Prepend machine-readable attribution so every agent-filed issue is traceable
    user_id_str = str(ctx.user_id) if ctx.user_id else "unknown"
    session_str = ctx.session_id or "unknown"
    attribution_comment = (
        f"<!-- filed-by: user_id={user_id_str} session={session_str} -->\n"
        f"**Filed by session:** `{session_str[:8]}` (user `{user_id_str[:8]}`)\n\n"
    )
    description = attribution_comment + description

    issue_input: dict[str, Any] = {
        "teamId": team_id,
        "title": title,
        "description": description,
        "priority": priority,
        "stateId": state_id,
        "labelIds": [personal_agent_label_id, agent_filed_label_id],
    }

    if project:
        project_id = await _get_project_id(team_id, project)
        if project_id:
            issue_input["projectId"] = project_id
        else:
            log.warning("linear_project_not_found", project=project, trace_id=trace_id)

    if dry_run:
        log.info("linear_create_issue_dry_run", trace_id=trace_id, title=title[:80])
        return {"dry_run": True, "payload": issue_input, "title": title}

    # Dedup check: refuse if a similar title already exists. A canceled issue counts:
    # the owner canceled it, so filing it again repeats a decided case. Only an issue
    # closed as a duplicate is skipped, because the original it points to still matches.
    # The state types here must be real Linear types: ``cancelled`` matched none (FRE-1611).
    dedup_data = await _gql(
        """query($filter: IssueFilter) {
          issues(filter: $filter, first: 5) {
            nodes { identifier title url }
          }
        }""",
        {
            "filter": {
                "title": {"containsIgnoreCase": title},
                "team": {"name": {"eq": _TEAM_NAME}},
                "state": {"type": {"nin": ["duplicate"]}},
            }
        },
    )
    existing = (dedup_data.get("issues") or {}).get("nodes") or []
    if existing:
        ids = ", ".join(n.get("identifier", "?") for n in existing)
        raise CreateLinearIssueError(
            "duplicate_issue",
            f"Duplicate: similar issue(s) already exist: {ids}.",
            retryable=False,
            next_action=(
                "Do not file it. Call find_linear_issues to review the existing issue(s), and "
                "name them to the user."
            ),
        )

    await _refuse_at_ticket_cap()

    data = await _gql(
        """
        mutation($input: IssueCreateInput!) {
          issueCreate(input: $input) {
            issue { id identifier title url }
          }
        }
        """,
        {"input": issue_input},
    )
    issue = (data.get("issueCreate") or {}).get("issue") or {}
    if not issue.get("identifier"):
        raise CreateLinearIssueError(
            "bad_reply",
            "Linear issue creation returned no identifier; the issue may or may not exist.",
            retryable=False,
            next_action=(
                "Call find_linear_issues with the title to check whether it was created before "
                "filing again."
            ),
        )

    _rate_log[project_key].append(time.time())
    log.info(
        "linear_create_issue_done",
        trace_id=trace_id,
        identifier=issue["identifier"],
        url=issue.get("url", ""),
    )
    return {
        "identifier": issue["identifier"],
        "url": issue.get("url", ""),
        "title": issue.get("title", title),
        "dry_run": False,
    }


async def find_linear_issues_executor(
    query: str | None = None,
    state: str | None = None,
    limit: int | None = None,
    *,
    ctx: TraceContext,
) -> dict[str, Any]:
    """Search or list Linear issues.

    Args:
        query: Text to search in issue titles. Leave None/empty to list by state.
        state: Optional workflow state filter (e.g. 'Needs Approval', 'Approved').
        limit: Max results (default 25, max 50).
        ctx: Trace context.

    Returns:
        Dict with ``issues`` list (each with identifier, title, state, url).

    Raises:
        FindLinearIssuesError: On an empty search with no state.
        LinearError: On a missing API key or a failed GraphQL call.
    """
    query = (query or "").strip()
    state = (state or "").strip() or None

    if not query and not state:
        raise FindLinearIssuesError(
            "invalid_argument",
            "Provide 'query' to search by title or 'state' to list issues in a state.",
            retryable=False,
            next_action='Call find_linear_issues with query="..." or state="Needs Approval".',
        )

    cap = max(1, min(int(limit) if limit else 25, 50))
    trace_id = ctx.trace_id
    log.info("linear_find_issues_start", trace_id=trace_id, query=query[:80], state=state)

    gql_filter: dict[str, Any] = {}
    if query:
        gql_filter["title"] = {"containsIgnoreCase": query}
    if state:
        gql_filter["state"] = {"name": {"eq": state}}

    data = await _gql(
        """
        query($filter: IssueFilter, $first: Int) {
          issues(filter: $filter, first: $first, orderBy: updatedAt) {
            nodes { identifier title url state { name } priority }
          }
        }
        """,
        {"filter": gql_filter, "first": cap},
    )
    nodes = (data.get("issues") or {}).get("nodes") or []
    issues = [
        {
            "identifier": n.get("identifier", ""),
            "title": n.get("title", ""),
            "state": (n.get("state") or {}).get("name", ""),
            "priority": n.get("priority"),
            "url": n.get("url", ""),
        }
        for n in nodes
    ]
    log.info("linear_find_issues_done", trace_id=trace_id, count=len(issues))
    return {"issues": issues, "count": len(issues), "query": query or None, "state": state}


async def list_linear_projects_executor(
    *,
    ctx: TraceContext,
) -> dict[str, Any]:
    """List all Linear projects for the team.

    Args:
        ctx: Trace context.

    Returns:
        Dict with ``projects`` list (each with id, name, description, url).

    Raises:
        LinearError: On a missing API key or team, or a failed GraphQL call.
    """
    trace_id = ctx.trace_id
    log.info("linear_list_projects_start", trace_id=trace_id)

    team_id = await _get_team_id(_TEAM_NAME)
    data = await _gql(
        """
        query($teamId: ID!) {
          teams(filter: { id: { eq: $teamId } }) {
            nodes {
              projects(first: 100) {
                nodes { id name description url state { name } }
              }
            }
          }
        }
        """,
        {"teamId": team_id},
    )
    team_nodes = (data.get("teams") or {}).get("nodes") or []
    raw_projects = (team_nodes[0].get("projects") or {}).get("nodes") or [] if team_nodes else []
    projects = [
        {
            "id": p.get("id", ""),
            "name": p.get("name", ""),
            "description": (p.get("description") or "")[:200],
            "state": (p.get("state") or {}).get("name", ""),
            "url": p.get("url", ""),
        }
        for p in raw_projects
    ]
    log.info("linear_list_projects_done", trace_id=trace_id, count=len(projects))
    return {"projects": projects, "count": len(projects)}


async def create_linear_project_executor(
    name: str = "",
    description: str | None = None,
    *,
    ctx: TraceContext,
) -> dict[str, Any]:
    """Create a new Linear project.

    Args:
        name: Project name.
        description: Optional markdown description.
        ctx: Trace context.

    Returns:
        Dict with ``id``, ``name``, and ``url`` of the created project.

    Raises:
        CreateLinearProjectError: On an empty name, a missing API key, or an empty creation
            reply.
        LinearError: On a missing team or a failed GraphQL call.
    """
    name = (name or "").strip()
    if not name:
        raise CreateLinearProjectError(
            "invalid_argument",
            "'name' is required and cannot be empty.",
            retryable=False,
            next_action="Call create_linear_project again with a project name.",
        )
    if not settings.linear_api_key:
        raise CreateLinearProjectError(
            "not_configured",
            "Linear API key not configured. Set AGENT_LINEAR_API_KEY in .env.",
            retryable=False,
            next_action=_NOT_CONFIGURED_NEXT,
        )

    trace_id = ctx.trace_id
    log.info("linear_create_project_start", trace_id=trace_id, name=name[:80])

    team_id = await _get_team_id(_TEAM_NAME)
    project_input: dict[str, Any] = {"name": name, "teamIds": [team_id]}
    if description:
        project_input["description"] = description.strip()

    data = await _gql(
        """
        mutation($input: ProjectCreateInput!) {
          projectCreate(input: $input) {
            project { id name url }
          }
        }
        """,
        {"input": project_input},
    )
    project = (data.get("projectCreate") or {}).get("project") or {}
    if not project.get("id"):
        raise CreateLinearProjectError(
            "bad_reply",
            "Linear project creation returned no id; the project may or may not exist.",
            retryable=False,
            next_action=(
                "Call list_linear_projects to check whether it was created before creating "
                "it again."
            ),
        )

    log.info(
        "linear_create_project_done",
        trace_id=trace_id,
        project_id=project["id"],
        name=project.get("name", name),
    )
    return {
        "id": project["id"],
        "name": project.get("name", name),
        "url": project.get("url", ""),
    }
