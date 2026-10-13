"""ADR-0156 D8.7 (FRE-1574): every command in a skill body is allowed, or a documented ask.

It runs over every agent skill, ``skills/<name>/SKILL.md`` (FRE-1577), and over the
``references/*.md`` files that a body links (FRE-1578: a command in a reference is a command
that the skill names). It reads each fenced ``bash``/``sh``/``shell`` block of an agent skill and splits it into recipes at
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

_SKILLS = Path(__file__).resolve().parents[3] / "skills"
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


_SCANNED: set[str] = set()


def _recipes() -> Iterator[tuple[str, str, bool]]:
    """Yield (skill name, recipe, documented ask) for every recipe of every agent skill.

    Fills ``_SCANNED`` with the path of each file that held at least one recipe.
    """
    files = [*_SKILLS.glob("*/SKILL.md"), *_SKILLS.glob("*/references/*.md")]
    for path in sorted(files):
        skill = path.relative_to(_SKILLS).parts[0]
        for block in _FENCE.findall(path.read_text()):
            for chunk in _chunks(block):
                documented = bool(_ASK_MARKER.search(chunk))
                lines = [ln for ln in chunk.split("\n") if not ln.strip().startswith("#")]
                recipe = "\n".join(lines).strip()
                if not recipe:
                    continue
                recipe = _NOTATION.sub(lambda m: m.group(1) + (m.group(2) or ""), recipe)
                _SCANNED.add(str(path.relative_to(_SKILLS)))
                yield skill, _PLACEHOLDER.sub("PLACEHOLDER", recipe), documented


_ALL = list(_recipes())


def test_the_extractor_finds_recipes() -> None:
    # 74 before FRE-1578. The telemetry patterns became tool calls, which are not
    # commands, so the count fell. The reference files below are asserted by name.
    assert len(_ALL) >= 40


def test_the_extractor_reads_the_reference_files() -> None:
    """A command in a ``references/`` file is scanned, so a low count cannot hide a file."""
    assert {
        "log-search/references/curl-recipes.md",
        "log-search/references/esql.md",
        "model-latency/references/latency-recipe.md",
    } <= _SCANNED


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
    "interaction-outcomes",
    "log-search",
    "model-latency",
    "service-health",
    "spend-and-tokens",
    "turn-explanation",
}
_TELEMETRY_CURL = [
    (skill, recipe)
    for skill, recipe, documented in _ALL
    if skill in _TELEMETRY_SKILLS and re.search(r"(?m)^\s*curl ", recipe) and not documented
]


def test_the_telemetry_skills_have_curl_recipes() -> None:
    assert {skill for skill, _ in _TELEMETRY_CURL} == _TELEMETRY_SKILLS
    # 51 before FRE-1578, when the counts and breakdowns became tool calls.
    assert len(_TELEMETRY_CURL) >= 14


@pytest.mark.parametrize(
    ("skill", "recipe"),
    _TELEMETRY_CURL,
    ids=[f"{s}:{i}" for i, (s, _) in enumerate(_TELEMETRY_CURL)],
)
def test_a_telemetry_skill_curl_recipe_is_allowed(skill: str, recipe: str) -> None:
    """FRE-1583 AC-2: a curl read in these skills runs without asking, unless it says it asks."""
    decision = evaluate_permission("bash", {"command": recipe}, "NORMAL", _RULES)
    assert decision.decision == "allow", f"{skill}: {decision.refused!r} ({decision.reason})"
