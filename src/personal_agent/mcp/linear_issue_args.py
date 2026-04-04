"""Normalize Linear MCP ``save_issue`` arguments before gateway calls.

LLMs often confuse the product name (Personal Agent) with the Linear **team** name,
or use ``status`` instead of ``state``. The Linear server returns a generic
"Argument Validation Error" when the team does not exist.
"""

from __future__ import annotations

from typing import Any

# Strings commonly passed instead of the real Linear team name (e.g. FrenchForest).
_TEAM_NAME_ALIASES: frozenset[str] = frozenset(
    {
        "personalagent",
        "personal-agent",
        "personal_agent",
        "personal agent",
    }
)


def _should_replace_team(team: str) -> bool:
    key = team.strip().casefold()
    if not key:
        return True
    if key in _TEAM_NAME_ALIASES:
        return True
    compact = key.replace(" ", "").replace("-", "").replace("_", "")
    return compact == "personalagent"


def normalize_save_issue_arguments(
    arguments: dict[str, Any],
    *,
    default_team: str,
) -> dict[str, Any]:
    """Return a copy of ``arguments`` with common ``save_issue`` mistakes fixed.

    Args:
        arguments: Raw keyword arguments (tool executor or ``LinearClient``).
        default_team: Configured Linear team name (``settings.linear_team_name``).

    Returns:
        Shallow copy with ``team`` / ``state`` / ``priority`` normalized where needed.
    """
    out = dict(arguments)

    if "status" in out:
        if "state" not in out:
            out["state"] = out.pop("status")
        else:
            del out["status"]

    issue_id = out.get("id")
    creating = not (isinstance(issue_id, str) and bool(issue_id.strip()))

    team = out.get("team")
    if isinstance(team, str) and _should_replace_team(team):
        out["team"] = default_team
    elif (team is None or team == "") and creating:
        out["team"] = default_team

    pri = out.get("priority")
    if isinstance(pri, str) and pri.strip().isdigit():
        out["priority"] = int(pri.strip())

    return out
