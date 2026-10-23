"""Every tool that a skill names in ``metadata.requires-tools`` is a real tool (ADR-0156 D4).

D9 filters each worker type's skill catalog by this field, so a name that is not a tool would
hide the skill from every worker. The S1 rules themselves (fields, name, description, body) are
checked by ``tests/docs/test_skill_validator.py``.
"""

from __future__ import annotations

import pytest

from personal_agent.orchestrator.skills import get_all_skills


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


_SKILLS = sorted(get_all_skills().items())


@pytest.mark.parametrize(("name", "skill"), _SKILLS, ids=[n for n, _ in _SKILLS])
def test_requires_tools_names_real_tools(name: str, skill: object) -> None:
    registered = _all_tool_names()
    for tool_name in getattr(skill, "requires_tools"):
        assert tool_name in registered, f"{name}: requires-tools names unknown tool {tool_name!r}"


def test_the_parser_reads_requires_tools_and_provides_tools() -> None:
    """A positive control: the parser reads both fields (a broken parser yields no names).

    FRE-1616 B2: the four skills that a worker without bash sees dropped ``requires-tools: bash``.
    The skills that are the bash procedure keep it.
    """
    skills = get_all_skills()
    assert skills["service-health"].requires_tools == ("bash",)
    for name in ("log-search", "model-latency", "spend-and-tokens", "turn-explanation"):
        assert skills[name].requires_tools == (), name
    assert skills["log-search"].provides_tools == ("search_logs",)
    assert skills["model-latency"].provides_tools == ("measure_model_latency",)
    assert skills["sequential-thinking"].requires_tools == ()
