"""Typed Linear GraphQL wrapper for promotion and feedback (ADR-0040).

Calls the Linear GraphQL API directly with httpx + AGENT_LINEAR_API_KEY.
No MCP gateway dependency — works on VPS without Docker Desktop (FRE-243).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from personal_agent.config import settings
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

_LINEAR_URL = "https://api.linear.app/graphql"

# Module-level caches — avoids repeated API round-trips within a process lifetime.
_lc_team_id: str | None = None
_lc_state_ids: dict[str, str] = {}
_lc_label_ids: dict[str, str] = {}
_lc_label_team_fetched: str | None = None

# Human feedback labels (AgentFeedback group children)
FEEDBACK_LABEL_NAMES: frozenset[str] = frozenset(
    {"Approved", "Rejected", "Deepen", "Too Vague", "Duplicate", "Defer"}
)
# Agent-applied response labels
RESPONSE_LABEL_NAMES: frozenset[str] = frozenset({"Re-evaluated", "Refined"})

FEEDBACK_LABELS_SPEC: dict[str, dict[str, Any]] = {
    "AgentFeedback": {"color": "#95A2B3", "is_group": True},
    "Approved": {"color": "#0E7D1C", "parent": "AgentFeedback"},
    "Rejected": {"color": "#EB5757", "parent": "AgentFeedback"},
    "Deepen": {"color": "#F2C94C", "parent": "AgentFeedback"},
    "Too Vague": {"color": "#F2994A", "parent": "AgentFeedback"},
    "Duplicate": {"color": "#95A2B3", "parent": "AgentFeedback"},
    "Defer": {"color": "#6B7280", "parent": "AgentFeedback"},
}

RESPONSE_LABELS_SPEC: dict[str, dict[str, Any]] = {
    "Re-evaluated": {"color": "#2F80ED"},
    "Refined": {"color": "#9B51E0"},
}


def _as_dict(result: Any) -> dict[str, Any]:
    """Coerce a result to a dict when possible."""
    if result is None:
        return {}
    if isinstance(result, dict):
        return result
    return {}


def _extract_issues(payload: Any) -> list[dict[str, Any]]:
    """Normalize issues response to a list of issue dicts."""
    data = _as_dict(payload)
    for key in ("issues", "nodes", "data"):
        raw = data.get(key)
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def _extract_labels(issue: dict[str, Any]) -> list[str]:
    """Extract label names from a get_issue-style dict."""
    labels = issue.get("labels") or issue.get("labelIds")
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for item in labels:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            n = item.get("name")
            if isinstance(n, str):
                names.append(n)
    return names


def _duration_to_cutoff(s: str) -> str | None:
    """Parse ISO 8601 duration like ``-P3D`` and return an absolute ISO datetime string.

    Args:
        s: Duration string, e.g. ``-P3D`` (3 days ago) or ``P1DT2H`` (1 day 2 hours ahead).

    Returns:
        ISO 8601 datetime string or None if unparseable.
    """
    negative = s.startswith("-")
    body = s.lstrip("-")
    m = re.match(
        r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$",
        body,
    )
    if not m:
        return None
    delta = timedelta(
        days=int(m.group(1) or 0),
        hours=int(m.group(2) or 0),
        minutes=int(m.group(3) or 0),
        seconds=int(m.group(4) or 0),
    )
    cutoff = (
        datetime.now(tz=timezone.utc) - delta if negative else datetime.now(tz=timezone.utc) + delta
    )
    return cutoff.isoformat()


def _normalize_issue_node(node: dict[str, Any]) -> dict[str, Any]:
    """Flatten ``labels.nodes`` in a GraphQL issue node to a plain list."""
    result = dict(node)
    labels_raw = result.get("labels")
    if isinstance(labels_raw, dict):
        result["labels"] = labels_raw.get("nodes") or []
    return result


def extract_issue_identifier_from_description(description: str) -> str | None:
    """Parse fingerprint from promotion issue description (ADR-0040).

    Args:
        description: Markdown body from Linear.

    Returns:
        Fingerprint hex substring or None.
    """
    m = re.search(r"<!--\s*fingerprint:\s*([a-fA-F0-9]+)\s*-->", description)
    if m:
        return m.group(1).lower()
    m2 = re.search(r"\*\*Fingerprint\*\*:\s*`([a-fA-F0-9]+)`", description)
    if m2:
        return m2.group(1).lower()
    return None


def extract_linear_identifier(result: Any) -> str | None:
    """Best-effort parse of issue identifier (e.g. FF-123) from issueCreate result."""
    data = _as_dict(result)
    issue = data.get("issue")
    if isinstance(issue, dict):
        ident = issue.get("identifier") or issue.get("id")
        if isinstance(ident, str):
            return ident
    ident2 = data.get("identifier") or data.get("id")
    if isinstance(ident2, str) and "-" in ident2:
        return ident2
    if isinstance(result, str) and "-" in result.strip():
        return result.strip().split()[0]
    return None


class LinearClient:
    """Thin async wrapper around the Linear GraphQL API (ADR-0040).

    Reads ``settings.linear_api_key`` at call time — no external dependencies.
    """

    def __init__(self) -> None:
        """No external dependencies — reads settings.linear_api_key at call time."""

    # ── Internal GraphQL helper ───────────────────────────────────────────

    async def _call(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """POST to Linear GraphQL API with PAT auth.

        Args:
            query: GraphQL query or mutation string.
            variables: Variables dict.

        Returns:
            The ``data`` field from the GraphQL response.

        Raises:
            RuntimeError: On missing API key, HTTP error, or GraphQL error.
        """
        api_key = settings.linear_api_key
        if not api_key:
            raise RuntimeError("AGENT_LINEAR_API_KEY not configured")
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                resp = await client.post(
                    _LINEAR_URL,
                    json={"query": query, "variables": variables},
                    headers={"Authorization": api_key, "Content-Type": "application/json"},
                )
        except httpx.ConnectError as exc:
            raise RuntimeError("Cannot connect to Linear API (api.linear.app).") from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError("Linear API request timed out.") from exc

        if resp.is_error:
            raise RuntimeError(f"Linear API HTTP {resp.status_code}: {resp.text[:400]}")
        body = resp.json()
        if errors := body.get("errors"):
            msgs = "; ".join(e.get("message", str(e)) for e in errors)
            raise RuntimeError(f"Linear GraphQL error: {msgs}")
        return body.get("data") or {}

    # ── ID resolution helpers ─────────────────────────────────────────────

    async def _team_id(self, team_name: str) -> str:
        global _lc_team_id
        if _lc_team_id:
            return _lc_team_id
        data = await self._call("{ teams { nodes { id name } } }", {})
        nodes = (data.get("teams") or {}).get("nodes") or []
        for t in nodes:
            if t.get("name", "").lower() == team_name.lower():
                _lc_team_id = str(t["id"])
                return _lc_team_id
        names = [t.get("name") for t in nodes]
        raise RuntimeError(f"Linear team '{team_name}' not found. Available: {names}")

    async def _state_id(self, team_id: str, state_name: str) -> str:
        if state_name in _lc_state_ids:
            return _lc_state_ids[state_name]
        data = await self._call(
            """
            query($teamId: String!) {
              workflowStates(filter: { team: { id: { eq: $teamId } } }) {
                nodes { id name }
              }
            }
            """,
            {"teamId": team_id},
        )
        states = (data.get("workflowStates") or {}).get("nodes") or []
        for s in states:
            _lc_state_ids[str(s["name"])] = str(s["id"])
        if state_name not in _lc_state_ids:
            available = list(_lc_state_ids.keys())
            raise RuntimeError(
                f"Linear workflow state '{state_name}' not found. Available: {available}"
            )
        return _lc_state_ids[state_name]

    async def _label_id(
        self, team_id: str, label_name: str, *, auto_create_color: str | None = None
    ) -> str:
        global _lc_label_team_fetched
        if label_name in _lc_label_ids:
            return _lc_label_ids[label_name]
        if _lc_label_team_fetched != team_id:
            data = await self._call(
                """
                query($teamId: String!) {
                  issueLabels(filter: { team: { id: { eq: $teamId } } }) {
                    nodes { id name }
                  }
                }
                """,
                {"teamId": team_id},
            )
            labels = (data.get("issueLabels") or {}).get("nodes") or []
            for lbl in labels:
                _lc_label_ids[str(lbl["name"])] = str(lbl["id"])
            _lc_label_team_fetched = team_id
        if label_name not in _lc_label_ids:
            if auto_create_color is None:
                raise RuntimeError(f"Linear label '{label_name}' not found.")
            mut = await self._call(
                """
                mutation($input: IssueLabelCreateInput!) {
                  issueLabelCreate(input: $input) {
                    issueLabel { id name }
                  }
                }
                """,
                {"input": {"name": label_name, "color": auto_create_color, "teamId": team_id}},
            )
            new_lbl = (mut.get("issueLabelCreate") or {}).get("issueLabel") or {}
            if not new_lbl.get("id"):
                raise RuntimeError(f"Failed to create Linear label '{label_name}'.")
            _lc_label_ids[label_name] = str(new_lbl["id"])
        return _lc_label_ids[label_name]

    async def _project_id(self, team_id: str, project_name: str) -> str | None:
        data = await self._call(
            """
            query($teamId: String!) {
              teams(filter: { id: { eq: $teamId } }) {
                nodes { projects { nodes { id name } } }
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

    # ── Public operations ─────────────────────────────────────────────────

    async def create_issue(
        self,
        title: str,
        team: str,
        description: str,
        priority: int,
        labels: list[str],
        state: str,
        project: str,
    ) -> str | None:
        """Create a Linear issue. Returns human identifier (e.g. FF-123) or None.

        Args:
            title: Issue title.
            team: Team name (e.g. ``FrenchForest``).
            description: Markdown body.
            priority: 1=Urgent, 2=High, 3=Normal, 4=Low.
            labels: Label display names to apply.
            state: Workflow state name (e.g. ``Needs Approval``).
            project: Project name; skipped silently if not found.

        Returns:
            Human issue identifier (e.g. ``FF-123``) or None on failure.
        """
        team_id = await self._team_id(team)
        state_id = await self._state_id(team_id, state)
        label_ids = [await self._label_id(team_id, name) for name in labels]

        issue_input: dict[str, Any] = {
            "teamId": team_id,
            "title": title,
            "description": description,
            "priority": priority,
            "stateId": state_id,
            "labelIds": label_ids,
        }

        if project:
            project_id = await self._project_id(team_id, project)
            if project_id:
                issue_input["projectId"] = project_id
            else:
                log.warning("linear_project_not_found", project=project)

        data = await self._call(
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
        ident = issue.get("identifier") or issue.get("id")
        if ident:
            log.info("linear_issue_created", identifier=ident, title=title[:80])
        return str(ident) if ident else None

    async def get_issue(self, issue_id: str, include_relations: bool = False) -> dict[str, Any]:
        """Fetch issue by id or identifier.

        Args:
            issue_id: Linear issue ID (UUID) or identifier (e.g. ``FF-123``).
            include_relations: Unused — retained for API compatibility.

        Returns:
            Issue dict with id, identifier, title, description, labels, updatedAt.
        """
        data = await self._call(
            """
            query($id: String!) {
              issue(id: $id) {
                id identifier title description url updatedAt
                state { name }
                labels { nodes { name } }
              }
            }
            """,
            {"id": issue_id},
        )
        issue = data.get("issue")
        if not isinstance(issue, dict):
            return {}
        return _normalize_issue_node(issue)

    async def list_issues(self, **filters: Any) -> list[dict[str, Any]]:
        """List issues; translates filter kwargs to a GraphQL IssueFilter.

        Args:
            **filters: Supported keys: ``team`` (name), ``label`` (name),
                ``query`` (title search), ``state`` (name), ``updatedAt``
                (ISO 8601 duration like ``-P3D``), ``includeArchived``,
                ``limit``, ``orderBy``, ``cursor``.

        Returns:
            List of normalized issue dicts.
        """
        gql_filter: dict[str, Any] = {}

        team_name = filters.pop("team", None)
        if team_name:
            gql_filter["team"] = {"name": {"eq": team_name}}

        label = filters.pop("label", None)
        if label:
            gql_filter["labels"] = {"some": {"name": {"in": [label]}}}

        query = filters.pop("query", None)
        if query:
            gql_filter["title"] = {"containsIgnoreCase": query}

        state_name = filters.pop("state", None)
        if state_name:
            gql_filter["state"] = {"name": {"eq": state_name}}

        updated_at = filters.pop("updatedAt", None)
        if updated_at and isinstance(updated_at, str):
            cutoff = _duration_to_cutoff(updated_at)
            if cutoff:
                gql_filter["updatedAt"] = {"gt": cutoff}

        include_archived: bool | None = filters.pop("includeArchived", None)
        limit = int(filters.pop("limit", 50))
        order_by = filters.pop("orderBy", "updatedAt")
        cursor = filters.pop("cursor", None)

        variables: dict[str, Any] = {
            "filter": gql_filter,
            "first": limit,
            "orderBy": order_by,
        }
        if include_archived is not None:
            variables["includeArchived"] = include_archived
        if cursor:
            variables["after"] = cursor

        data = await self._call(
            """
            query(
              $filter: IssueFilter,
              $first: Int,
              $after: String,
              $orderBy: PaginationOrderBy,
              $includeArchived: Boolean
            ) {
              issues(
                filter: $filter,
                first: $first,
                after: $after,
                orderBy: $orderBy,
                includeArchived: $includeArchived
              ) {
                nodes {
                  id identifier title description url updatedAt
                  state { name }
                  labels { nodes { name } }
                }
                pageInfo { hasNextPage endCursor }
              }
            }
            """,
            variables,
        )
        issues_data = data.get("issues") or {}
        nodes = issues_data.get("nodes") or []
        return [_normalize_issue_node(n) for n in nodes if isinstance(n, dict)]

    async def count_non_archived_issues(self, team: str, page_limit: int = 250) -> int:
        """Count non-archived issues for a team (paginated).

        Args:
            team: Team name.
            page_limit: Issues to fetch per page (max 250).

        Returns:
            Total issue count (capped at 10 000).
        """
        total = 0
        cursor: str | None = None
        while True:
            variables: dict[str, Any] = {
                "filter": {"team": {"name": {"eq": team}}},
                "first": page_limit,
                "orderBy": "updatedAt",
                "includeArchived": False,
            }
            if cursor:
                variables["after"] = cursor

            data = await self._call(
                """
                query(
                  $filter: IssueFilter,
                  $first: Int,
                  $after: String,
                  $orderBy: PaginationOrderBy,
                  $includeArchived: Boolean
                ) {
                  issues(
                    filter: $filter,
                    first: $first,
                    after: $after,
                    orderBy: $orderBy,
                    includeArchived: $includeArchived
                  ) {
                    nodes { id }
                    pageInfo { hasNextPage endCursor }
                  }
                }
                """,
                variables,
            )
            issues_data = data.get("issues") or {}
            nodes = issues_data.get("nodes") or []
            page_info = issues_data.get("pageInfo") or {}
            total += len(nodes)

            if page_info.get("hasNextPage") and page_info.get("endCursor"):
                cursor = str(page_info["endCursor"])
            else:
                break
            if not nodes:
                break
            if total > 10000:
                log.warning("linear_issue_count_truncated", counted=total)
                break

        return total

    async def update_issue(self, issue_id: str, **fields: Any) -> None:
        """Update issue fields via issueUpdate mutation.

        Args:
            issue_id: Linear issue ID or identifier.
            **fields: Fields to update. ``state`` (name) and ``labels``
                (list of names) are resolved to IDs automatically.
                All other fields pass through as-is.
        """
        team_id = await self._team_id(settings.linear_team_name)
        update_input: dict[str, Any] = {}

        for key, val in fields.items():
            if key == "state":
                update_input["stateId"] = await self._state_id(team_id, str(val))
            elif key == "labels":
                names = val if isinstance(val, list) else [val]
                update_input["labelIds"] = [await self._label_id(team_id, n) for n in names]
            else:
                update_input[key] = val

        await self._call(
            """
            mutation($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) {
                success
              }
            }
            """,
            {"id": issue_id, "input": update_input},
        )

    async def add_comment(self, issue_id: str, body: str) -> None:
        """Post a comment on an issue.

        Args:
            issue_id: Linear issue ID or identifier.
            body: Comment body (Markdown).
        """
        await self._call(
            """
            mutation($input: CommentCreateInput!) {
              commentCreate(input: $input) {
                comment { id }
              }
            }
            """,
            {"input": {"issueId": issue_id, "body": body}},
        )

    async def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        """List comments for an issue.

        Args:
            issue_id: Linear issue ID or identifier.

        Returns:
            List of comment dicts with id, body, createdAt.
        """
        data = await self._call(
            """
            query($id: String!) {
              issue(id: $id) {
                comments {
                  nodes { id body createdAt }
                }
              }
            }
            """,
            {"id": issue_id},
        )
        issue = data.get("issue") or {}
        nodes = (issue.get("comments") or {}).get("nodes") or []
        return [c for c in nodes if isinstance(c, dict)]

    async def list_issue_labels(self, team: str | None = None) -> list[dict[str, Any]]:
        """List issue labels, optionally scoped to team.

        Args:
            team: Team name to filter by; returns all workspace labels if None.

        Returns:
            List of label dicts with id, name, color, isGroup, parent.
        """
        if team:
            team_id = await self._team_id(team)
            data = await self._call(
                """
                query($teamId: String!) {
                  issueLabels(filter: { team: { id: { eq: $teamId } } }, first: 250) {
                    nodes { id name color isGroup parent { id name } }
                  }
                }
                """,
                {"teamId": team_id},
            )
        else:
            data = await self._call(
                """
                {
                  issueLabels(first: 250) {
                    nodes { id name color isGroup parent { id name } }
                  }
                }
                """,
                {},
            )
        labels = (data.get("issueLabels") or {}).get("nodes") or []
        return [lbl for lbl in labels if isinstance(lbl, dict)]

    async def create_label(
        self,
        name: str,
        color: str,
        *,
        parent: str | None = None,
        is_group: bool = False,
    ) -> None:
        """Create an issue label (workspace or team-scoped via parent).

        Args:
            name: Label display name.
            color: Hex color string (e.g. ``#95A2B3``).
            parent: Parent label name to make this a child label.
            is_group: Whether this label is a group container.
        """
        team_id = await self._team_id(settings.linear_team_name)
        label_input: dict[str, Any] = {
            "name": name,
            "color": color,
            "teamId": team_id,
        }
        if parent:
            label_input["parentId"] = await self._label_id(team_id, parent)
        if is_group:
            label_input["isGroup"] = True

        data = await self._call(
            """
            mutation($input: IssueLabelCreateInput!) {
              issueLabelCreate(input: $input) {
                issueLabel { id name }
              }
            }
            """,
            {"input": label_input},
        )
        new_lbl = (data.get("issueLabelCreate") or {}).get("issueLabel") or {}
        if new_lbl.get("id"):
            _lc_label_ids[name] = str(new_lbl["id"])
        log.info("linear_label_created", name=name)

    async def ensure_feedback_labels(self, team_name: str | None = None) -> None:
        """Create AgentFeedback taxonomy labels if missing (idempotent).

        Args:
            team_name: Override team name; defaults to ``settings.linear_team_name``.
        """
        team = team_name or settings.linear_team_name
        existing = await self.list_issue_labels(team=team)
        by_name = {
            str(x.get("name", "")).strip(): x for x in existing if isinstance(x.get("name"), str)
        }

        async def ensure_one(
            name: str,
            spec: dict[str, Any],
            *,
            parent_name: str | None = None,
        ) -> None:
            if name in by_name:
                return
            await self.create_label(
                name,
                spec["color"],
                parent=parent_name,
                is_group=bool(spec.get("is_group", False)),
            )
            by_name[name] = {}

        for name, spec in FEEDBACK_LABELS_SPEC.items():
            parent = spec.get("parent")
            await ensure_one(
                name,
                spec,
                parent_name=str(parent) if parent else None,
            )
        for name, spec in RESPONSE_LABELS_SPEC.items():
            await ensure_one(name, spec)

    @staticmethod
    def labels_from_issue(issue: dict[str, Any]) -> list[str]:
        """Return label display names for an issue dict."""
        return _extract_labels(issue)


async def _cli_ensure_labels() -> None:
    """Entry point for ``python -m personal_agent.captains_log.linear_client``."""
    client = LinearClient()
    await client.ensure_feedback_labels()
    log.info("linear_feedback_labels_ensure_complete")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_cli_ensure_labels())
