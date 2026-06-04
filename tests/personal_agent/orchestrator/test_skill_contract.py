"""Contract tests for skill frontmatter schema and tool-reference integrity.

Phase A (FRE-skill-routing): validates that every docs/skills/*.md file with
frontmatter satisfies the schema contract defined in the plan:

  Required fields:  name, description, when_to_use
  Optional fields:  tools (list[str]), keywords (list[str]),
                    canonical_patterns (list[str], ≤3),
                    known_bad_patterns (list[dict], ≤5)

  known_bad_pattern schema:
    pattern (str, required)
    reason  (str, required)
    suggestion (str, required)
    applies_to (dict, optional):
      tool   (str)   — must resolve to a registered tool
      fields (list[str]) — must be valid parameter names of that tool

  tools[] and applies_to.tool must resolve to registered tool names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from personal_agent.orchestrator.skills import _parse_frontmatter, get_all_skills

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SKILLS_DIR = Path(__file__).resolve().parents[3] / "docs" / "skills"

# Files that intentionally have no frontmatter and are excluded from all checks
_NO_FRONTMATTER_FILES = {"EMPIRICAL_TEST_RESULTS.md", "SKILL_TEMPLATE.md"}


def _load_frontmatters() -> list[tuple[str, dict[str, Any]]]:
    """Return (filename, frontmatter_dict) for all skill files that have frontmatter."""
    results = []
    for path in sorted(_SKILLS_DIR.glob("*.md")):
        if path.name in _NO_FRONTMATTER_FILES:
            continue
        fm, _ = _parse_frontmatter(path)
        if fm:
            results.append((path.name, fm))
    return results


def _all_tool_names() -> set[str]:
    """Return all tool names across every registration path (settings-independent).

    Imports ToolDefinition objects directly from each tool module so the result
    is not gated by runtime settings (primitive_tools_enabled).  This is the
    correct set to validate frontmatter ``tools:`` entries against.

    The 8 legacy curated tools were deleted in FRE-265 (ADR-0063 PIVOT-6) and
    do not appear here.
    """
    from personal_agent.tools.context7 import get_library_docs_tool
    from personal_agent.tools.linear import (
        create_linear_issue_tool,
        create_linear_project_tool,
        find_linear_issues_tool,
        list_linear_projects_tool,
    )
    from personal_agent.tools.location import get_location_tool
    from personal_agent.tools.memory_search import search_memory_tool
    from personal_agent.tools.perplexity import perplexity_query_tool
    from personal_agent.tools.personal_history import recall_personal_history_tool
    from personal_agent.tools.primitives.bash import bash_tool
    from personal_agent.tools.primitives.read import read_tool
    from personal_agent.tools.primitives.run_python import run_python_tool
    from personal_agent.tools.primitives.write import write_tool
    from personal_agent.tools.web import web_search_tool

    return {
        search_memory_tool.name,
        web_search_tool.name,
        perplexity_query_tool.name,
        get_library_docs_tool.name,
        create_linear_issue_tool.name,
        find_linear_issues_tool.name,
        list_linear_projects_tool.name,
        create_linear_project_tool.name,
        get_location_tool.name,
        recall_personal_history_tool.name,
        bash_tool.name,
        read_tool.name,
        write_tool.name,
        run_python_tool.name,
    }


def _tool_parameter_names(tool_name: str) -> set[str]:
    """Return parameter names for a known tool by direct ToolDefinition lookup."""
    from personal_agent.tools.primitives.bash import bash_tool
    from personal_agent.tools.primitives.read import read_tool
    from personal_agent.tools.primitives.run_python import run_python_tool
    from personal_agent.tools.primitives.write import write_tool

    known = {
        t.name: t
        for t in [bash_tool, read_tool, write_tool, run_python_tool]
    }
    tool_def = known.get(tool_name)
    if tool_def is not None:
        return {p.name for p in tool_def.parameters}
    return set()


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


class TestRequiredFrontmatter:
    """Every skill file with frontmatter has the three required fields."""

    @pytest.mark.parametrize("filename,fm", _load_frontmatters())
    def test_has_name(self, filename: str, fm: dict[str, Any]) -> None:
        assert fm.get("name"), f"{filename}: 'name' is missing or empty"

    @pytest.mark.parametrize("filename,fm", _load_frontmatters())
    def test_has_description(self, filename: str, fm: dict[str, Any]) -> None:
        assert fm.get("description"), f"{filename}: 'description' is missing or empty"

    @pytest.mark.parametrize("filename,fm", _load_frontmatters())
    def test_has_when_to_use(self, filename: str, fm: dict[str, Any]) -> None:
        assert fm.get("when_to_use"), f"{filename}: 'when_to_use' is missing or empty"


class TestToolsFieldIntegrity:
    """Every entry in the optional tools: list resolves to a registered tool."""

    @pytest.mark.parametrize("filename,fm", _load_frontmatters())
    def test_tools_reference_real_tools(self, filename: str, fm: dict[str, Any]) -> None:
        tools: list[str] = fm.get("tools") or []
        if not tools:
            pytest.skip(f"{filename}: no tools field")
        registered = _all_tool_names()
        for tool_name in tools:
            assert tool_name in registered, (
                f"{filename}: tools[] entry '{tool_name}' is not a registered tool. "
                f"Registered: {sorted(registered)}"
            )


class TestBoundedPatternFields:
    """canonical_patterns ≤ 3 entries; known_bad_patterns ≤ 5 entries."""

    @pytest.mark.parametrize("filename,fm", _load_frontmatters())
    def test_canonical_patterns_bounded(self, filename: str, fm: dict[str, Any]) -> None:
        patterns: list[str] = fm.get("canonical_patterns") or []
        assert len(patterns) <= 3, (
            f"{filename}: canonical_patterns has {len(patterns)} entries (max 3)"
        )

    @pytest.mark.parametrize("filename,fm", _load_frontmatters())
    def test_known_bad_patterns_bounded(self, filename: str, fm: dict[str, Any]) -> None:
        patterns: list[Any] = fm.get("known_bad_patterns") or []
        assert len(patterns) <= 5, (
            f"{filename}: known_bad_patterns has {len(patterns)} entries (max 5)"
        )


class TestKnownBadPatternSchema:
    """Each known_bad_pattern entry has required fields and valid applies_to."""

    def _all_bad_patterns(self) -> list[tuple[str, dict[str, Any]]]:
        """Return (filename, entry) for every known_bad_pattern entry."""
        results = []
        for filename, fm in _load_frontmatters():
            for entry in fm.get("known_bad_patterns") or []:
                results.append((filename, entry))
        return results

    def test_all_bad_patterns_have_pattern(self) -> None:
        """Every known_bad_pattern entry has a non-empty 'pattern' string."""
        for filename, entry in self._all_bad_patterns():
            assert isinstance(entry.get("pattern"), str) and entry["pattern"], (
                f"{filename}: known_bad_pattern entry missing 'pattern': {entry}"
            )

    def test_all_bad_patterns_have_reason(self) -> None:
        """Every known_bad_pattern entry has a non-empty 'reason' string."""
        for filename, entry in self._all_bad_patterns():
            assert isinstance(entry.get("reason"), str) and entry["reason"], (
                f"{filename}: known_bad_pattern entry missing 'reason': {entry}"
            )

    def test_all_bad_patterns_have_suggestion(self) -> None:
        """Every known_bad_pattern entry has a non-empty 'suggestion' string."""
        for filename, entry in self._all_bad_patterns():
            assert isinstance(entry.get("suggestion"), str) and entry["suggestion"], (
                f"{filename}: known_bad_pattern entry missing 'suggestion': {entry}"
            )

    def test_applies_to_tool_is_registered(self) -> None:
        """If applies_to.tool is set, it must resolve to a registered tool."""
        registered = _all_tool_names()
        for filename, entry in self._all_bad_patterns():
            applies_to: dict[str, Any] = entry.get("applies_to") or {}
            tool_name: str | None = applies_to.get("tool")
            if tool_name is None:
                continue
            assert tool_name in registered, (
                f"{filename}: known_bad_pattern applies_to.tool '{tool_name}' "
                f"is not a registered tool. Registered: {sorted(registered)}"
            )

    def test_applies_to_fields_are_valid_parameters(self) -> None:
        """If applies_to.fields is set, each field must be a parameter of applies_to.tool."""
        for filename, entry in self._all_bad_patterns():
            applies_to: dict[str, Any] = entry.get("applies_to") or {}
            tool_name: str | None = applies_to.get("tool")
            fields: list[str] = applies_to.get("fields") or []
            if not tool_name or not fields:
                continue
            valid_params = _tool_parameter_names(tool_name)
            if not valid_params:
                # Tool is registered but parameter introspection not available — skip
                continue
            for field_name in fields:
                assert field_name in valid_params, (
                    f"{filename}: known_bad_pattern applies_to.fields['{field_name}'] "
                    f"is not a parameter of tool '{tool_name}'. "
                    f"Valid params: {sorted(valid_params)}"
                )
