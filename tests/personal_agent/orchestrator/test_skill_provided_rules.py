"""ADR-0156 A5.3 and AC-R3 (FRE-1595 AC-2): the ``provides-tools`` rules.

A skill names the tools that it reveals in ``metadata.provides-tools``. The validator fails on
each seeded case:

- a ``provides-tools`` value that names an unknown tool, or a tool that is not skill-provided;
- a skill-provided tool that no skill provides.

A skill that a principal cannot see (filtered by ``requires-tools``, D9.1) reveals nothing to
that principal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import personal_agent.orchestrator.skills as skills_module
from personal_agent.orchestrator.skills import (
    get_all_skills,
    provider_violations,
    providers_of,
    tools_revealed_by,
)


def _write_skill(root: Path, name: str, metadata: dict[str, str]) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    lines = "".join(f"  {k}: {v}\n" for k, v in metadata.items())
    block = f"metadata:\n{lines}" if lines else ""
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Covers the {name} case.\n{block}---\n\nBody.\n",
        encoding="utf-8",
    )


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "skills"
    _write_skill(root, "finds-logs", {"provides-tools": "search_logs, measure_latency"})
    _write_skill(root, "explains-turns", {"provides-tools": "search_logs"})
    _write_skill(
        root,
        "needs-bash",
        {"requires-tools": "bash", "provides-tools": "measure_latency"},
    )
    _write_skill(root, "plain", {})
    monkeypatch.setattr(skills_module, "SKILLS_DIR", root)
    monkeypatch.setattr(skills_module, "_cache", None)
    return root


# Each registered tool and whether it is skill-provided.
_REGISTERED = {"search_logs": True, "measure_latency": True, "bash": False, "read": False}


def test_the_field_is_parsed(seeded: Path) -> None:
    skills = get_all_skills()
    assert skills["finds-logs"].provides_tools == ("search_logs", "measure_latency")
    assert skills["plain"].provides_tools == ()


def test_the_seeded_catalog_passes(seeded: Path) -> None:
    assert provider_violations(get_all_skills(), _REGISTERED) == ()


def test_an_unknown_tool_fails(seeded: Path) -> None:
    _write_skill(seeded, "names-ghost", {"provides-tools": "ghost_tool"})
    found = provider_violations(get_all_skills(), _REGISTERED)
    assert found == ("names-ghost: provides-tools names unknown tool 'ghost_tool'",)


def test_a_tool_that_is_not_skill_provided_fails(seeded: Path) -> None:
    _write_skill(seeded, "names-bash", {"provides-tools": "bash"})
    found = provider_violations(get_all_skills(), _REGISTERED)
    assert found == ("names-bash: provides-tools names 'bash', which is not skill-provided",)


def test_a_skill_provided_tool_that_no_skill_provides_fails(seeded: Path) -> None:
    found = provider_violations(get_all_skills(), {**_REGISTERED, "orphan_tool": True})
    assert found == ("orphan_tool is skill-provided, but no skill provides it",)


def test_providers_are_listed_in_name_order(seeded: Path) -> None:
    assert providers_of("search_logs") == ("explains-turns", "finds-logs")
    assert providers_of("measure_latency") == ("finds-logs", "needs-bash")
    assert providers_of("bash") == ()


def test_a_visible_skill_reveals_its_tools(seeded: Path) -> None:
    assert tools_revealed_by("finds-logs") == frozenset({"search_logs", "measure_latency"})
    assert tools_revealed_by("needs-bash", held=["bash", "read"]) == frozenset({"measure_latency"})


def test_a_hidden_skill_reveals_nothing_to_that_principal(seeded: Path) -> None:
    # A principal that does not hold bash cannot see needs-bash (D9.1), so it reveals nothing.
    assert tools_revealed_by("needs-bash", held=["read"]) == frozenset()


def test_an_unknown_skill_reveals_nothing(seeded: Path) -> None:
    assert tools_revealed_by("no-such-skill") == frozenset()


def test_the_real_skills_and_tools_follow_the_rules() -> None:
    """The repository's own skills and registry (T-C2 adds the first real providers)."""
    from personal_agent.tools import get_default_registry

    registry = get_default_registry()
    registered = {
        t.name: t.skill_provided
        for t in registry.list_tools(include_worker_only=True, revealed=None)
    }
    assert provider_violations(get_all_skills(), registered) == ()
