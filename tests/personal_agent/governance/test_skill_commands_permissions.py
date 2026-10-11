"""ADR-0156 D8.7 (FRE-1574): every command in a skill body is allowed, or a documented ask.

It runs over every agent skill, ``docs/skills/<name>/SKILL.md`` (FRE-1577). It
reads each fenced ``bash``/``sh``/``shell`` block of an agent skill and splits it into recipes at
blank lines, because the model runs one recipe at a time. Two notations are normalised first:

- ``bash <command>`` and ``bash command="<command>"`` mean "call the bash tool with this";
- ``<placeholder>`` is replaced by a literal word, as the model fills it in.

A recipe that does not come out allow must carry a comment line ``# ask: <reason>``. That line
is the skill documenting the ask, and the model reads it with the recipe. A deny always fails.
The external skills (``docs/external-skills/``, D5.7) are not the agent's, so they are not read.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import tree_sitter_bash
from tree_sitter import Language, Parser

from personal_agent.governance.permissions import evaluate_permission, load_permission_rules

_SKILLS = Path(__file__).resolve().parents[3] / "docs" / "skills"
_FENCE = re.compile(r"```(?:bash|sh|shell)\n(.*?)```", re.S)
_NOTATION = re.compile(r'^(\s*)bash (?:command="(.*)"\s*$)?', re.M)
_PLACEHOLDER = re.compile(r"<[A-Za-z_][\w-]*>")
_ASK_MARKER = re.compile(r"^\s*#\s*ask:\s*\S", re.M)
_RULES = load_permission_rules()


def _chunks(block: str) -> Iterator[str]:
    """Split a block at blank lines, keeping a here-doc or a quote that spans one together."""
    pending = ""
    for part in re.split(r"\n\s*\n", block):
        pending = f"{pending}\n\n{part}" if pending else part
        if not _parser().parse(pending.encode()).root_node.has_error:
            yield pending
            pending = ""
    if pending:
        yield pending


def _parser() -> Parser:
    return Parser(Language(tree_sitter_bash.language()))


def _recipes() -> Iterator[tuple[str, str, bool]]:
    """Yield (skill file, recipe, documented ask) for every recipe of every agent skill."""
    for path in sorted(_SKILLS.glob("*/SKILL.md")):
        for block in _FENCE.findall(path.read_text()):
            for chunk in _chunks(block):
                documented = bool(_ASK_MARKER.search(chunk))
                lines = [ln for ln in chunk.split("\n") if not ln.strip().startswith("#")]
                recipe = "\n".join(lines).strip()
                if not recipe:
                    continue
                recipe = _NOTATION.sub(lambda m: m.group(1) + (m.group(2) or ""), recipe)
                yield path.parent.name, _PLACEHOLDER.sub("PLACEHOLDER", recipe), documented


_ALL = list(_recipes())


def test_the_extractor_finds_recipes() -> None:
    assert len(_ALL) >= 50


@pytest.mark.parametrize(
    ("skill", "recipe", "documented"), _ALL, ids=[f"{s}:{i}" for i, (s, _, _) in enumerate(_ALL)]
)
def test_recipe_is_allowed_or_a_documented_ask(skill: str, recipe: str, documented: bool) -> None:
    decision = evaluate_permission("bash", {"command": recipe}, "NORMAL", _RULES)
    assert decision.decision != "deny", f"{skill}: a deny rule refuses {decision.refused!r}"
    if decision.decision == "ask":
        assert documented, (
            f"{skill}: {decision.refused!r} asks ({decision.reason}) but the recipe has no "
            "'# ask: <reason>' line"
        )


_TELEMETRY_SKILLS = {
    "query-elasticsearch",
    "query-tempo",
    "self-telemetry",
    "infrastructure-health",
}
_TELEMETRY_CURL = [
    (skill, recipe)
    for skill, recipe, documented in _ALL
    if skill in _TELEMETRY_SKILLS and recipe.lstrip().startswith("curl ") and not documented
]


def test_the_telemetry_skills_have_curl_recipes() -> None:
    assert {skill for skill, _ in _TELEMETRY_CURL} == _TELEMETRY_SKILLS
    assert len(_TELEMETRY_CURL) >= 40


@pytest.mark.parametrize(
    ("skill", "recipe"),
    _TELEMETRY_CURL,
    ids=[f"{s}:{i}" for i, (s, _) in enumerate(_TELEMETRY_CURL)],
)
def test_a_telemetry_skill_curl_recipe_is_allowed(skill: str, recipe: str) -> None:
    """FRE-1583 AC-2: a curl read in these skills runs without asking, unless it says it asks."""
    decision = evaluate_permission("bash", {"command": recipe}, "NORMAL", _RULES)
    assert decision.decision == "allow", f"{skill}: {decision.refused!r} ({decision.reason})"
