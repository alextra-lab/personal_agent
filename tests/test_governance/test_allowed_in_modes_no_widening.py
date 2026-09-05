"""FRE-1358 AC-4: no SILENT widening when allowed_in_modes becomes authoritative.

Native (Tier 1) tools hardcode ``allowed_modes`` on their ``ToolDefinition`` in source.
The first test proves that for every native tool with a governance entry, the entry's
``allowed_in_modes`` already matches the code-defined set, so making the policy
authoritative changes enforcement only for MCP tools, where the policy previously had
no effect at all.

The second test names the one place this fix does widen access — a handful of MCP
tools whose tools.yaml entry already declared more than the old hardcoded MCP default —
so that widening is a reviewed, pinned inventory rather than a silent side effect.
"""

from personal_agent.config.governance_loader import load_governance_config
from personal_agent.tools import get_default_registry


def test_native_tool_policy_matches_code_defined_allowed_modes() -> None:
    """Every native tool's tools.yaml allowed_in_modes equals its ToolDefinition.allowed_modes."""
    governance_config = load_governance_config()
    registry = get_default_registry()

    checked = []
    for tool_def in registry.list_tools():
        tool_policy = governance_config.tools.get(tool_def.name)
        if tool_policy is None:
            continue
        assert set(tool_policy.allowed_in_modes) == set(tool_def.allowed_modes), (
            f"{tool_def.name}: tools.yaml allowed_in_modes "
            f"{tool_policy.allowed_in_modes} != code-defined allowed_modes "
            f"{tool_def.allowed_modes} — FRE-1358 would change this tool's enforced "
            f"modes, not just close the MCP gap"
        )
        checked.append(tool_def.name)

    assert checked, "no native tool had a matching governance entry — fixture drifted"


def test_mcp_widening_beyond_pre_fix_default_is_a_reviewed_allowlist() -> None:
    """A named, reviewed inventory of the one place FRE-1358 does widen access.

    Before this fix, every MCP tool was enforced against the hardcoded default
    {NORMAL, DEGRADED} (mcp/types.py), regardless of tools.yaml. A handful of tools
    already carry an allowed_in_modes wider than that default (ALERT added) — making
    the policy authoritative (required by AC-2) surfaces that already-declared grant,
    which is a real widening under a strict before/after diff (AC-4).

    None of these 14 are reachable today: 13 are Linear MCP tools and Linear is
    intentionally excluded from the VPS gateway's --servers list
    (docker/mcp/run-gateway.sh — Docker Desktop OAuth/DCR is unavailable on a plain
    VPS); the 14th (mcp_research) is not part of the sequentialthinking/context7
    servers the VPS does spawn either. All 14 are risk_level=low,
    requires_approval=false.

    This test pins the set so a new tool can't join it silently — a change here
    needs the same scrutiny given to this ticket's fix, not a drive-by tools.yaml
    edit. Revisit when Linear MCP is wired up on the VPS (tracked in
    docker/mcp/run-gateway.sh's own comment).
    """
    governance_config = load_governance_config()
    pre_fix_default = {"NORMAL", "DEGRADED"}

    widened = {
        name
        for name, policy in governance_config.tools.items()
        if name.startswith("mcp_")
        and policy.allowed_in_modes
        and not set(policy.allowed_in_modes) <= pre_fix_default
    }

    expected = {
        "mcp_get_issue",
        "mcp_get_issue_status",
        "mcp_get_milestone",
        "mcp_get_project",
        "mcp_list_comments",
        "mcp_list_documents",
        "mcp_list_issue_labels",
        "mcp_list_issue_statuses",
        "mcp_list_issues",
        "mcp_list_milestones",
        "mcp_list_project_labels",
        "mcp_list_projects",
        "mcp_research",
        "mcp_search_documentation",
    }
    assert widened == expected, (
        "the set of MCP tools that gain modes when FRE-1358 makes policy authoritative "
        f"changed — review before accepting: added={widened - expected}, "
        f"removed={expected - widened}"
    )
