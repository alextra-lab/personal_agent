"""Native Linear tool — FRE-224 (ADR-0028 Tier-1).

Creates Linear issues from agent log analysis and reflection, and finds
similar issues for deduplication.  Calls the Linear GraphQL API directly
with a Personal Access Token — no Docker Desktop MCP gateway required.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

import httpx

from personal_agent.config import settings
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

_LINEAR_URL = "https://api.linear.app/graphql"

_TEAM_NAME = "FrenchForest"
_NEEDS_APPROVAL_STATE = "Needs Approval"
_AGENT_FILED_LABEL = "agent-filed"
_PERSONAL_AGENT_LABEL = "PersonalAgent"
_AGENT_FILED_COLOR = "#6B7280"  # neutral gray for auto-filed issues

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
        "Create a Linear issue from agent log analysis, error detection, or self-reflection. "
        "Issues land in 'Needs Approval' state with 'PersonalAgent' and 'agent-filed' labels — "
        "they require human approval before implementation. "
        "Use find_linear_issues first to avoid duplicates. "
        "Set dry_run=true to preview the payload without creating the issue."
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
)

find_linear_issues_tool = ToolDefinition(
    name="find_linear_issues",
    description=(
        "Search or list Linear issues. "
        "Use before create_linear_issue to avoid filing duplicates. "
        "Pass a query string to search by title, or leave query empty and set state "
        "to list all issues in a given state (e.g. 'Needs Approval', 'In Progress'). "
        "Returns up to 25 matching issues with their status and URLs."
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
)

list_linear_projects_tool = ToolDefinition(
    name="list_linear_projects",
    description=(
        "List all Linear projects for the team. "
        "Use to discover project names before assigning an issue to a project, "
        "or to get an overview of active work."
    ),
    category="network",
    parameters=[],
    risk_level="low",
    allowed_modes=["NORMAL", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=15,
    rate_limit_per_hour=60,
)

create_linear_project_tool = ToolDefinition(
    name="create_linear_project",
    description=(
        "Create a new Linear project for the team. "
        "Use when a body of related issues needs a new container project. "
        "Projects created by the agent land without a specific state — human review recommended."
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
        ToolExecutionError: On missing API key, HTTP error, or GraphQL error.
    """
    api_key = settings.linear_api_key
    if not api_key:
        raise ToolExecutionError("Linear API key not configured. Set AGENT_LINEAR_API_KEY in .env.")
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                _LINEAR_URL,
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": api_key, "Content-Type": "application/json"},
            )
    except httpx.ConnectError as exc:
        raise ToolExecutionError("Cannot connect to Linear API (api.linear.app).") from exc
    except httpx.TimeoutException as exc:
        raise ToolExecutionError("Linear API request timed out.") from exc

    if resp.is_error:
        raise ToolExecutionError(f"Linear API HTTP {resp.status_code}: {resp.text[:400]}")
    body = resp.json()
    if errors := body.get("errors"):
        msgs = "; ".join(e.get("message", str(e)) for e in errors)
        raise ToolExecutionError(f"Linear GraphQL error: {msgs}")
    return body.get("data") or {}


# ── ID resolution (cached per process) ───────────────────────────────────


async def _get_team_id(team_name: str) -> str:
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
    raise ToolExecutionError(f"Linear team '{team_name}' not found. Available: {names}")


async def _get_state_id(team_id: str, state_name: str) -> str:
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
        raise ToolExecutionError(
            f"Linear workflow state '{state_name}' not found. Available: {names}"
        )
    return _state_id_cache[state_name]


async def _get_label_id(
    team_id: str, label_name: str, *, auto_create_color: str | None = None
) -> str:
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
            raise ToolExecutionError(f"Linear label '{label_name}' not found.")
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
            raise ToolExecutionError(f"Failed to create Linear label '{label_name}'.")
        _label_id_cache[label_name] = str(new_label["id"])
        log.info("linear_label_created", label=label_name)
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


# ── Rate limiting ─────────────────────────────────────────────────────────


def _check_rate_limit(project_key: str) -> None:
    limit = settings.linear_agent_rate_limit_per_day
    now = time.time()
    cutoff = now - 86400.0
    fresh = [t for t in _rate_log[project_key] if t > cutoff]
    _rate_log[project_key] = fresh
    if len(fresh) >= limit:
        raise ToolExecutionError(
            f"Rate limit: already filed {limit} agent issues in the last 24h "
            f"(project='{project_key}'). Wait before creating more."
        )


# ── Executors ─────────────────────────────────────────────────────────────


async def create_linear_issue_executor(
    title: str = "",
    description: str = "",
    priority: int | None = None,
    project: str | None = None,
    dry_run: bool = False,
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """Create a Linear issue from agent analysis.

    Args:
        title: Issue title.
        description: Markdown body with context and evidence.
        priority: 1=Urgent, 2=High, 3=Normal (default), 4=Low.
        project: Optional Linear project name.
        dry_run: If True, return resolved payload without creating.
        ctx: Optional trace context.

    Returns:
        Dict with ``identifier``, ``url``, ``title``, and ``dry_run`` keys.

    Raises:
        ToolExecutionError: On validation failure, rate limit, or API error.
    """
    title = (title or "").strip()
    if not title:
        raise ToolExecutionError("'title' is required and cannot be empty.")
    description = (description or "").strip()
    if not description:
        raise ToolExecutionError("'description' is required and cannot be empty.")
    if len(title) > 255:
        raise ToolExecutionError(f"'title' exceeds 255 characters ({len(title)}).")

    priority = int(priority) if priority is not None else 3
    if priority not in (1, 2, 3, 4):
        raise ToolExecutionError("'priority' must be 1 (Urgent), 2 (High), 3 (Normal), or 4 (Low).")

    if not settings.linear_api_key:
        raise ToolExecutionError("Linear API key not configured. Set AGENT_LINEAR_API_KEY in .env.")

    project_key = project or "__unassigned__"
    if not dry_run:
        _check_rate_limit(project_key)

    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"
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
    personal_agent_label_id = await _get_label_id(team_id, _PERSONAL_AGENT_LABEL)
    agent_filed_label_id = await _get_label_id(
        team_id, _AGENT_FILED_LABEL, auto_create_color=_AGENT_FILED_COLOR
    )

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
        raise ToolExecutionError("Linear issue creation returned no identifier.")

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
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """Search or list Linear issues.

    Args:
        query: Text to search in issue titles. Leave None/empty to list by state.
        state: Optional workflow state filter (e.g. 'Needs Approval', 'Approved').
        limit: Max results (default 25, max 50).
        ctx: Optional trace context.

    Returns:
        Dict with ``issues`` list (each with identifier, title, state, url).

    Raises:
        ToolExecutionError: On missing API key, empty search with no state, or API error.
    """
    query = (query or "").strip()
    state = (state or "").strip() or None

    if not query and not state:
        raise ToolExecutionError(
            "Provide 'query' to search by title or 'state' to list issues in a state."
        )

    cap = max(1, min(int(limit) if limit else 25, 50))
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"
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
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """List all Linear projects for the team.

    Args:
        ctx: Optional trace context.

    Returns:
        Dict with ``projects`` list (each with id, name, description, url).

    Raises:
        ToolExecutionError: On missing API key or API error.
    """
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"
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
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """Create a new Linear project.

    Args:
        name: Project name.
        description: Optional markdown description.
        ctx: Optional trace context.

    Returns:
        Dict with ``id``, ``name``, and ``url`` of the created project.

    Raises:
        ToolExecutionError: On validation failure or API error.
    """
    name = (name or "").strip()
    if not name:
        raise ToolExecutionError("'name' is required and cannot be empty.")
    if not settings.linear_api_key:
        raise ToolExecutionError("Linear API key not configured. Set AGENT_LINEAR_API_KEY in .env.")

    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"
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
        raise ToolExecutionError("Linear project creation returned no id.")

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
