"""FRE-1358 AC-4: switching mode-check authority to allowed_in_modes must not widen
any native tool's permitted modes.

Native (Tier 1) tools hardcode ``allowed_modes`` on their ``ToolDefinition`` in source.
This test proves that for every native tool with a governance entry, the entry's
``allowed_in_modes`` already matches the code-defined set, so making the policy
authoritative (FRE-1358) changes enforcement only for tools where it did not already
agree — i.e. the MCP tools this ticket targets.
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
