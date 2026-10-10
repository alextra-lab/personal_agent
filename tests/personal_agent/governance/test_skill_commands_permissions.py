"""ADR-0156 D8.7 (FRE-1574): every command in a skill body is allowed, or a documented ask.

Until T5 moves the skills into folders, this runs over the flat files in ``docs/skills/``. It
reads each fenced ``bash``/``sh``/``shell`` block of an agent skill and splits it into recipes at
blank lines, because the model runs one recipe at a time. Two notations are normalised first:

- ``bash <command>`` and ``bash command="<command>"`` mean "call the bash tool with this";
- ``<placeholder>`` is replaced by a literal word, as the model fills it in.

A recipe that does not come out allow must carry a comment line ``# ask: <reason>``. That line
is the skill documenting the ask, and the model reads it with the recipe. A deny always fails.
The ``seshat-*`` skills are for external agents (D5.7) and the template is not a skill.
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
_NOT_AGENT_SKILLS = {"SKILL_TEMPLATE.md", "EMPIRICAL_TEST_RESULTS.md"}
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
    for path in sorted(_SKILLS.glob("*.md")):
        if path.name in _NOT_AGENT_SKILLS or path.name.startswith("seshat-"):
            continue
        for block in _FENCE.findall(path.read_text()):
            for chunk in _chunks(block):
                documented = bool(_ASK_MARKER.search(chunk))
                lines = [ln for ln in chunk.split("\n") if not ln.strip().startswith("#")]
                recipe = "\n".join(lines).strip()
                if not recipe:
                    continue
                recipe = _NOTATION.sub(lambda m: m.group(1) + (m.group(2) or ""), recipe)
                yield path.name, _PLACEHOLDER.sub("PLACEHOLDER", recipe), documented


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
