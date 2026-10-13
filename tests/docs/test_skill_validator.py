"""ADR-0156 D1.3 (FRE-1577 AC-1, AC-4): every skill folder follows the Agent Skills format (S1).

The validator runs over ``skills/*/SKILL.md`` (the agent's skills) and
``docs/external-skills/*/SKILL.md`` (skills for external agents, D5.7). It checks the inventory
of the ADR's migration table and every S1 rule of D1.3. An agent skill also needs a success
section, a recovery section and an ``evals.yaml`` with 3 scenarios (D5.1, D5.2).

Every check must pass. T6 (FRE-1578) and T7 (FRE-1579) converted the 11 agent skills and
removed the strict xfail marks that held the checks of the skills they had not converted yet.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from personal_agent.orchestrator.skills import (
    SKILL_RULES,
    SkillKind,
    SkillRule,
    validate_skill,
)

_REPO = Path(__file__).resolve().parents[2]
_AGENT_DIR = _REPO / "skills"
_EXTERNAL_DIR = _REPO / "docs" / "external-skills"

# The ADR-0156 migration table, as amended by A4 (FRE-1596): 11 agent skills, 3 external skills.
# FRE-1590 moved artifact-design to docs/reference/artifact-builder/ (the builder's prompt source,
# no skill).
_AGENT_SKILLS = frozenset(
    {
        "graph-inspection",
        "interaction-outcomes",
        "log-search",
        "mermaid-diagrams",
        "model-latency",
        "sequential-thinking",
        "service-health",
        "spend-and-tokens",
        "system-diagnostics",
        "system-metrics",
        "turn-explanation",
    }
)
_EXTERNAL_SKILLS = frozenset({"seshat-knowledge", "seshat-observations", "seshat-sessions"})
# Folded into tool descriptions (T3) or deleted (seshat-delegate): no file of theirs is left.
_GONE = (
    "bash",
    "fetch-url",
    "get_location",
    "list-directory",
    "personal-history-recall",
    "read-write",
    "run-python",
    "web-search",
    "seshat-delegate",
)


def _params() -> list[object]:
    params: list[object] = []
    for kind, names in (("agent", _AGENT_SKILLS), ("external", _EXTERNAL_SKILLS)):
        for name in sorted(names):
            for rule in SKILL_RULES:
                if kind == "external" and rule in ("sections", "evals"):
                    continue
                params.append(pytest.param(kind, name, rule, id=f"{name}:{rule}"))
    return params


@pytest.mark.parametrize(("kind", "name", "rule"), _params())
def test_skill_follows_the_rule(kind: SkillKind, name: str, rule: SkillRule) -> None:
    root = _AGENT_DIR if kind == "agent" else _EXTERNAL_DIR
    found = [v for v in validate_skill(root / name, kind) if v.rule == rule]
    assert found == [], "; ".join(v.detail for v in found)


def test_the_agent_inventory_is_the_migration_table() -> None:
    folders = {p.name for p in _AGENT_DIR.iterdir() if p.is_dir() and p.name != "__pycache__"}
    files = {p.name for p in _AGENT_DIR.iterdir() if p.is_file()}
    assert folders == _AGENT_SKILLS
    assert files == {"SKILL_TEMPLATE.md"}


def test_the_external_inventory_is_the_migration_table() -> None:
    assert {p.name for p in _EXTERNAL_DIR.iterdir()} == _EXTERNAL_SKILLS


@pytest.mark.parametrize("name", _GONE)
def test_a_folded_or_deleted_skill_left_no_file(name: str) -> None:
    for root in (_AGENT_DIR, _EXTERNAL_DIR):
        assert not (root / name).exists()
        assert not (root / f"{name}.md").exists()


def test_the_two_documents_that_are_not_skills_moved() -> None:
    assert not (_AGENT_DIR / "EMPIRICAL_TEST_RESULTS.md").exists()
    assert (
        _REPO / "docs" / "research" / "2026-04-28-skill-docs-empirical-test-results.md"
    ).exists()
    template = (_AGENT_DIR / "SKILL_TEMPLATE.md").read_text(encoding="utf-8")
    for needle in ("## Success", "## Recovery", "evals.yaml", "requires-tools"):
        assert needle in template


# --- the validator itself (seeded negatives) ------------------------------------------------


def _skill(tmp_path: Path, frontmatter: str, body: str = "Do the task.\n") -> Path:
    folder = tmp_path / "demo-skill"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")
    return folder


def _rules(folder: Path, kind: SkillKind = "external") -> set[str]:
    return {v.rule for v in validate_skill(folder, kind)}


def test_a_valid_skill_passes(tmp_path: Path) -> None:
    folder = _skill(tmp_path, f"name: demo-skill\ndescription: {'x' * 1024}\n")
    assert _rules(folder) == set()


def test_a_1025_character_description_fails(tmp_path: Path) -> None:
    folder = _skill(tmp_path, f"name: demo-skill\ndescription: {'x' * 1025}\n")
    assert _rules(folder) == {"description"}


@pytest.mark.parametrize(
    "description",
    ["I check the logs.", "Checks your logs.", "Use when you need logs.", "Reads my notes."],
)
def test_a_first_or_second_person_description_fails(tmp_path: Path, description: str) -> None:
    folder = _skill(tmp_path, f"name: demo-skill\ndescription: {description}\n")
    assert _rules(folder) == {"description"}


@pytest.mark.parametrize(
    ("frontmatter", "rule"),
    [
        ("name: other-name\ndescription: Checks logs.\n", "name"),
        ("name: Demo-Skill\ndescription: Checks logs.\n", "name"),
        ("description: Checks logs.\n", "name"),
        ("name: demo-skill\n", "description"),
        ("name: demo-skill\ndescription: Checks logs.\nkeywords: [logs]\n", "frontmatter"),
        ("name: demo-skill\ndescription: Checks logs.\nmetadata: {tools: [bash]}\n", "metadata"),
    ],
)
def test_a_broken_frontmatter_fails(tmp_path: Path, frontmatter: str, rule: str) -> None:
    assert rule in _rules(_skill(tmp_path, frontmatter))


def test_a_body_over_the_limits_fails(tmp_path: Path) -> None:
    lines = _skill(tmp_path / "a", "name: demo-skill\ndescription: Checks logs.\n", "x\n" * 500)
    tokens = _skill(tmp_path / "b", "name: demo-skill\ndescription: Checks logs.\n", "x" * 20_001)
    assert _rules(lines) == {"body_lines"}
    assert _rules(tokens) == {"body_tokens"}


def test_a_named_file_must_exist_one_level_down(tmp_path: Path) -> None:
    body = "Run `scripts/check.py`, read [the guide](references/guide.md) and `assets/x/y.png`.\n"
    folder = _skill(tmp_path, "name: demo-skill\ndescription: Checks logs.\n", body)
    assert _rules(folder) == {"file_refs"}
    (folder / "scripts").mkdir()
    (folder / "scripts" / "check.py").write_text("")
    (folder / "references").mkdir()
    (folder / "references" / "guide.md").write_text("")
    (folder / "assets" / "x").mkdir(parents=True)
    (folder / "assets" / "x" / "y.png").write_text("")
    # assets/x/y.png is two levels below the folder: S1 keeps references one level deep.
    found = [v.detail for v in validate_skill(folder, "external") if v.rule == "file_refs"]
    assert len(found) == 1 and "assets/x/y.png" in found[0]


def test_an_agent_skill_needs_sections_and_evals(tmp_path: Path) -> None:
    folder = _skill(tmp_path, "name: demo-skill\ndescription: Checks logs.\n")
    assert _rules(folder, "agent") == {"sections", "evals", "not_for"}
    (folder / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Checks logs. Not for metrics. "
        "The `system-metrics` skill does.\n---\n\n"
        "## Success\nThe count is a number.\n\n## Recovery\nStop after 2 tries.\n",
        encoding="utf-8",
    )
    (folder / "evals.yaml").write_text(
        "scenarios:\n  - prompt: a\n  - prompt: b\n  - prompt: c\n", encoding="utf-8"
    )
    assert _rules(folder, "agent") == set()


# --- AC-4: skill scripts ship in the gateway image ------------------------------------------


def test_the_gateway_image_copies_every_skill_folder() -> None:
    """D1.4: the image copies ``skills/`` whole, so ``<name>/scripts/`` ships with it."""
    dockerfile = (_REPO / "Dockerfile.gateway").read_text(encoding="utf-8")
    assert re.search(r"^COPY skills/ skills/$", dockerfile, re.M)
    ignored = [
        line.strip()
        for line in (_REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    # No pattern may exclude a file under skills/ (a scripts/ folder or a script type).
    for pattern in ignored:
        bare = pattern.lstrip("/").rstrip("/")
        assert bare != "skills" and not bare.startswith("skills/"), pattern
        assert pattern not in ("**/skills", "**/skills/**"), pattern
        assert pattern not in ("scripts", "**/scripts", "*.py", "*.sh", "**/*.py", "**/*.sh")
