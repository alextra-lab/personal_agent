"""Typed Linear MCP wrapper for promotion and feedback (ADR-0040).

Calls Docker MCP gateway tools by name via :class:`MCPGatewayAdapter.call_tool`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from personal_agent.config import settings
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.mcp.gateway import MCPGatewayAdapter

log = get_logger(__name__)

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
    """Coerce MCP tool result to a dict when possible."""
    if result is None:
        return {}
    if isinstance(result, dict):
        return result
    return {}


def _extract_issues(payload: Any) -> list[dict[str, Any]]:
    """Normalize list_issues MCP response to a list of issue dicts."""
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
    """Best-effort parse of issue identifier (e.g. FF-123) from save_issue result."""
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
    """Thin async wrapper around MCP Linear tools.

    Args:
        gateway: Initialized MCP gateway adapter with an active client session.
    """

    def __init__(self, gateway: MCPGatewayAdapter) -> None:
        """Store the initialized MCP gateway adapter."""
        self._gw = gateway

    async def call_linear(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Invoke a Linear MCP tool by name.

        Args:
            tool: Tool name (e.g. ``save_issue``).
            arguments: Arguments for the tool.

        Returns:
            Parsed MCP result.

        Raises:
            RuntimeError: If the gateway is unavailable or the tool errors.
        """
        return await self._gw.call_tool(tool, arguments)

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
        """Create a Linear issue. Returns human identifier (e.g. FF-123) or None."""
        result = await self.call_linear(
            "save_issue",
            {
                "title": title,
                "team": team,
                "description": description,
                "priority": priority,
                "labels": labels,
                "state": state,
                "project": project,
            },
        )
        ident = extract_linear_identifier(result)
        if ident:
            log.info("linear_issue_created", identifier=ident, title=title[:80])
        return ident

    async def get_issue(self, issue_id: str, include_relations: bool = False) -> dict[str, Any]:
        """Fetch issue by id or identifier."""
        raw = await self.call_linear(
            "get_issue",
            {"id": issue_id, "includeRelations": include_relations},
        )
        data = _as_dict(raw)
        issue = data.get("issue")
        if isinstance(issue, dict):
            return issue
        return data

    async def list_issues(self, **filters: Any) -> list[dict[str, Any]]:
        """List issues; passes filters through to MCP (team, label, query, etc.)."""
        raw = await self.call_linear("list_issues", dict(filters))
        return _extract_issues(raw)

    async def count_non_archived_issues(self, team: str, page_limit: int = 250) -> int:
        """Count non-archived issues for a team (paginated)."""
        total = 0
        cursor: str | None = None
        while True:
            args: dict[str, Any] = {
                "team": team,
                "includeArchived": False,
                "limit": page_limit,
                "orderBy": "updatedAt",
            }
            if cursor:
                args["cursor"] = cursor
            raw = await self.call_linear("list_issues", args)
            issues = _extract_issues(raw)
            total += len(issues)
            data = _as_dict(raw)
            cursor = None
            if isinstance(data.get("pageInfo"), dict):
                pi = data["pageInfo"]
                if pi.get("hasNextPage") and pi.get("endCursor"):
                    cursor = str(pi["endCursor"])
            elif isinstance(data.get("cursor"), str) and len(issues) >= page_limit:
                cursor = data["cursor"]
            else:
                break
            if not issues:
                break
            if total > 10000:
                log.warning("linear_issue_count_truncated", counted=total)
                break
        return total

    async def update_issue(self, issue_id: str, **fields: Any) -> None:
        """Update issue via save_issue (id + fields)."""
        payload = {"id": issue_id, **fields}
        await self.call_linear("save_issue", payload)

    async def add_comment(self, issue_id: str, body: str) -> None:
        """Post a comment on an issue."""
        await self.call_linear(
            "save_comment",
            {"issueId": issue_id, "body": body},
        )

    async def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        """List comments for an issue."""
        raw = await self.call_linear("list_comments", {"issueId": issue_id})
        data = _as_dict(raw)
        for key in ("comments", "nodes"):
            items = data.get(key)
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        return []

    async def list_issue_labels(self, team: str | None = None) -> list[dict[str, Any]]:
        """List issue labels, optionally scoped to team."""
        args: dict[str, Any] = {"limit": 250}
        if team:
            args["team"] = team
        raw = await self.call_linear("list_issue_labels", args)
        data = _as_dict(raw)
        for key in ("labels", "issueLabels", "nodes"):
            items = data.get(key)
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        return []

    async def create_label(
        self,
        name: str,
        color: str,
        *,
        parent: str | None = None,
        is_group: bool = False,
    ) -> None:
        """Create an issue label (workspace or inherited team scope via parent)."""
        payload: dict[str, Any] = {
            "name": name,
            "color": color,
            "isGroup": is_group,
        }
        if parent:
            payload["parent"] = parent
        await self.call_linear("create_issue_label", payload)

    async def ensure_feedback_labels(self, team_name: str | None = None) -> None:
        """Create AgentFeedback taxonomy labels if missing (idempotent)."""
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
            log.info("linear_creating_label", name=name, team=team)
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
    from personal_agent.mcp.gateway import MCPGatewayAdapter
    from personal_agent.tools import get_default_registry

    registry = get_default_registry()
    adapter = MCPGatewayAdapter(registry)
    await adapter.initialize()
    if not adapter.client:
        raise SystemExit("MCP gateway not available — check AGENT_MCP_GATEWAY_ENABLED and Docker")
    client = LinearClient(adapter)
    await client.ensure_feedback_labels()
    log.info("linear_feedback_labels_ensure_complete")
    await adapter.shutdown()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_cli_ensure_labels())
