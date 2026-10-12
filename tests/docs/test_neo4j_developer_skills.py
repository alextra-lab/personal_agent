"""FRE-1586: three Neo4j skills are developer references pinned to Neo4j 5.26 Community.

The skills are third-party files (MIT) under ``.claude/skills/``. They are for the seats that
write Cypher. They are not in the agent catalog of ``skills/``. These checks hold what the
ticket's acceptance criteria say about the files themselves:

- AC-1: each skill exists, starts with the pin line, and carries the MIT notice.
- AC-2: no file holds the version prefix of the 2025 dialect, the vector-search clause of
  2026.01, or the ``LET`` clause. The server refuses all three.
- AC-4: each folder records the upstream commit.

AC-3 (every read example runs on the server) needs a live Neo4j and is not a unit test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_SKILLS_DIR = _REPO / ".claude" / "skills"
_SKILLS = ("neo4j-cypher-skill", "neo4j-vector-index-skill", "neo4j-modeling-skill")

#: Each fragment must appear in the first line of the body.
_PIN_FRAGMENTS = (
    "Neo4j 5.26 Community",
    "Cypher 5",
    "APOC only",
    "no GDS",
    "no GenAI procedures",
)

#: The three forms that a 5.26 Community server refuses (study N3). The token ``SEARCH`` is
#: matched in upper case only, because the word "search" is the subject of the vector skill.
_FORBIDDEN = re.compile(r"CYPHER 25|\bSEARCH\b|\bLET\b")

_SHA = re.compile(r"\b[0-9a-f]{40}\b")


def split_skill(text: str) -> tuple[dict[str, object], str]:
    """Split a ``SKILL.md`` into its frontmatter map and its body.

    Args:
        text: The whole file.

    Returns:
        The parsed frontmatter and the text after the closing ``---`` line.

    Raises:
        ValueError: When the file does not open with a frontmatter block.
    """
    parts = text.split("---\n", 2)
    if len(parts) != 3 or parts[0] != "":
        raise ValueError("SKILL.md must open with a frontmatter block")
    front = yaml.safe_load(parts[1])
    if not isinstance(front, dict):
        raise ValueError("frontmatter must be a mapping")
    return front, parts[2]


def first_body_line(body: str) -> str:
    """Return the first non-blank line of a skill body.

    Args:
        body: The text after the frontmatter block.

    Returns:
        The first line that holds a non-space character, or an empty string.
    """
    return next((ln for ln in body.splitlines() if ln.strip()), "")


def missing_pin_fragments(line: str) -> list[str]:
    """Return the pin fragments that a line does not hold.

    Args:
        line: The first body line of a ``SKILL.md``.

    Returns:
        The required fragments that are absent, in the order of ``_PIN_FRAGMENTS``.
    """
    return [frag for frag in _PIN_FRAGMENTS if frag not in line]


def forbidden_tokens(text: str) -> list[str]:
    """Return every forbidden token in a text, in order.

    Args:
        text: The file content to scan.

    Returns:
        Each match of the version prefix, ``SEARCH`` or ``LET``, in order of appearance.
    """
    return _FORBIDDEN.findall(text)


def _files(skill: str) -> list[Path]:
    return sorted(p for p in (_SKILLS_DIR / skill).rglob("*") if p.is_file())


@pytest.mark.parametrize("skill", _SKILLS)
def test_the_skill_exists_with_a_skill_file(skill: str) -> None:
    """AC-1: the folder exists under ``.claude/skills/`` and holds a ``SKILL.md``."""
    assert (_SKILLS_DIR / skill / "SKILL.md").is_file()


def test_the_vector_skill_carries_the_hybrid_search_reference() -> None:
    """The ticket names ``references/hybrid-search.md`` for the vector skill."""
    assert (_SKILLS_DIR / "neo4j-vector-index-skill/references/hybrid-search.md").is_file()


@pytest.mark.parametrize("skill", _SKILLS)
def test_each_skill_has_the_pin_line_and_the_mit_notice(skill: str) -> None:
    """AC-1: the first body line pins the target, and the folder holds the MIT text."""
    front, body = split_skill((_SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8"))
    assert front["name"] == skill
    line = first_body_line(body)
    assert missing_pin_fragments(line) == [], f"pin line is: {line!r}"
    notice = (_SKILLS_DIR / skill / "LICENSE").read_text(encoding="utf-8")
    assert notice.startswith("MIT License")
    assert "Copyright (c) 2026 Neo4j Contrib" in notice
    assert "Permission is hereby granted, free of charge" in notice
    assert 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND' in notice


@pytest.mark.parametrize("skill", _SKILLS)
def test_the_frontmatter_pre_approves_no_tool(skill: str) -> None:
    """A copied file must not grant ``Bash`` or ``WebFetch`` to every seat that loads it."""
    front, _ = split_skill((_SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8"))
    assert "allowed-tools" not in front


@pytest.mark.parametrize("skill", _SKILLS)
def test_no_forbidden_token_in_any_skill_file(skill: str) -> None:
    """AC-2: no file in the folder holds the version prefix, ``SEARCH`` or ``LET``."""
    files = _files(skill)
    assert files, f"{skill} holds no file"
    hits = {
        str(p.relative_to(_SKILLS_DIR)): forbidden_tokens(p.read_text(encoding="utf-8"))
        for p in files
    }
    assert {rel: tokens for rel, tokens in hits.items() if tokens} == {}


def test_each_skill_records_the_same_upstream_commit() -> None:
    """AC-4: each folder holds ``UPSTREAM.md`` with a full commit SHA, and the three agree."""
    found: set[str] = set()
    for skill in _SKILLS:
        shas = _SHA.findall((_SKILLS_DIR / skill / "UPSTREAM.md").read_text(encoding="utf-8"))
        assert shas, f"{skill}/UPSTREAM.md records no commit SHA"
        found.update(shas)
    assert len(found) == 1, f"the three skills record different commits: {sorted(found)}"


@pytest.mark.parametrize(
    "text",
    [
        "CYPHER 25\nMATCH (n) RETURN n",
        "MATCH (c:Chunk)\n  SEARCH c IN (VECTOR INDEX i FOR $v LIMIT 5)",
        "LET x = 1",
        "see `CYPHER 25 RETURN 1`",
    ],
)
def test_the_forbidden_pattern_finds_each_form(text: str) -> None:
    """A seeded file with each refused form is found, so the AC-2 check can fail."""
    assert forbidden_tokens(text) != []


@pytest.mark.parametrize(
    "text",
    [
        "Run a vector search over the index.",
        "CYPHER 5 MATCH (n) RETURN n",
        "The set of letters, let me see.",
        "SEARCHING and OUTLET are different words",
    ],
)
def test_the_forbidden_pattern_ignores_ordinary_words(text: str) -> None:
    """Lower-case prose and longer upper-case words are not hits."""
    assert forbidden_tokens(text) == []


def test_a_pin_line_that_misses_a_fragment_is_reported() -> None:
    """A seeded line without the Community pin fails the pin check."""
    line = "Target: Neo4j 5.26, Cypher 5, APOC only, no GDS, no GenAI procedures."
    assert missing_pin_fragments(line) == ["Neo4j 5.26 Community"]


def test_a_file_without_frontmatter_is_refused() -> None:
    """The parser does not guess when the file has no frontmatter block."""
    with pytest.raises(ValueError):
        split_skill("# no frontmatter\n")
