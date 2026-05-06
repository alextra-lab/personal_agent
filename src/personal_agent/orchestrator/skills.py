"""Skill-doc loader and injection module.

Loads skill documentation files from ``docs/skills/`` via glob discovery.
Each skill file must have YAML frontmatter with at minimum ``name``,
``description``, and ``when_to_use``.  Optional fields: ``tools``,
``keywords``, ``canonical_patterns``, ``known_bad_patterns``.

Routing: ``get_skill_block`` always injects the ``bash`` skill plus any
skills whose ``keywords`` match the user message (substring, case-insensitive).
Files without frontmatter (e.g. EMPIRICAL_TEST_RESULTS.md, SKILL_TEMPLATE.md)
are silently skipped.

Cache is invalidated whenever any ``docs/skills/*.md`` file is added, removed,
or modified (mtime-based).

FRE-282: original intent-based routing preserved.
Phase A (FRE-skill-routing): migrated from hardcoded _SKILL_FILES /
_KEYWORD_ROUTES to frontmatter-driven auto-discovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
import structlog

from personal_agent.config import settings

log = structlog.get_logger(__name__)

_SKILLS_DIR = Path(__file__).resolve().parents[3] / "docs" / "skills"
_SEPARATOR = "\n\n---\n\n"
SKILL_BLOCK_HEADER = (
    "## Skill Library — How to Drive Primitive Tools\n\n"
    "The following skill docs are reference material for using the `bash`, `read`, "
    "`write`, and `run_python` primitives effectively. Prefer these idioms over named "
    "curated tools when both are available.\n\n"
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillDoc:
    """Parsed representation of a single skill doc with frontmatter."""

    name: str
    description: str
    when_to_use: str
    tools: tuple[str, ...]
    keywords: tuple[str, ...]
    canonical_patterns: tuple[str, ...]
    known_bad_patterns: tuple[dict[str, Any], ...]
    body: str


@dataclass
class _SkillCache:
    """Loaded skill docs indexed by name, with file mtimes for invalidation."""

    docs: dict[str, SkillDoc]
    mtimes: dict[Path, float]


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def _parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter and body from a markdown file.

    Args:
        path: Path to the markdown file.

    Returns:
        Tuple of (frontmatter_dict, body_text). Returns ({}, full_text) when
        the file has no frontmatter block.
    """
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        return {}, content.strip()
    end = content.find("\n---\n", 4)
    if end == -1:
        return {}, content.strip()
    fm: dict[str, Any] = yaml.safe_load(content[4:end]) or {}
    body = content[end + 5:].strip()
    return fm, body


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def _load_all_skills(skills_dir: Path) -> _SkillCache:
    """Scan *skills_dir* for markdown files and parse frontmatter.

    Files without a ``name`` key in frontmatter are skipped with a debug log.
    Parse errors are logged as warnings and skipped.

    Args:
        skills_dir: Directory containing skill markdown files.

    Returns:
        Populated _SkillCache with all valid skill docs.
    """
    docs: dict[str, SkillDoc] = {}
    mtimes: dict[Path, float] = {}

    for path in sorted(skills_dir.glob("*.md")):
        mtimes[path] = path.stat().st_mtime
        try:
            fm, body = _parse_frontmatter(path)
        except OSError as exc:
            log.warning("skill_doc_unreadable", file=path.name, error=str(exc))
            continue
        except yaml.YAMLError as exc:
            log.warning("skill_doc_parse_error", file=path.name, error=str(exc))
            continue

        name = fm.get("name")
        if not name:
            log.debug("skill_doc_no_frontmatter", file=path.name)
            continue

        docs[name] = SkillDoc(
            name=str(name),
            description=str(fm.get("description", "")),
            when_to_use=str(fm.get("when_to_use", "")),
            tools=tuple(fm.get("tools") or []),
            keywords=tuple(fm.get("keywords") or []),
            canonical_patterns=tuple(fm.get("canonical_patterns") or []),
            known_bad_patterns=tuple(fm.get("known_bad_patterns") or []),
            body=body,
        )

    return _SkillCache(docs=docs, mtimes=mtimes)


_cache: _SkillCache | None = None


def _needs_reload(skills_dir: Path) -> bool:
    """Return True when the cache is absent or any skill file has changed."""
    if _cache is None:
        return True
    current_files = set(skills_dir.glob("*.md"))
    cached_files = set(_cache.mtimes.keys())
    if current_files != cached_files:
        return True
    return any(
        not path.exists() or path.stat().st_mtime != mtime
        for path, mtime in _cache.mtimes.items()
    )


def _get_cache() -> _SkillCache:
    """Return the skill cache, reloading from disk when stale."""
    global _cache
    if _needs_reload(_SKILLS_DIR):
        _cache = _load_all_skills(_SKILLS_DIR)
    assert _cache is not None  # _needs_reload returns True when None; just assigned above
    return _cache


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_skill_block(message: str | None = None) -> str:
    """Return a skill library block for injection into the system prompt.

    Always includes the ``bash`` skill body (base tool reference). When
    *message* is provided, additionally injects any skills whose ``keywords``
    match a substring of the lowercased message.

    Args:
        message: The original user message used for keyword-based routing.
            Pass ``None`` to get the bash-only block.

    Returns:
        Skill library block prefixed with a header, or an empty string when
        ``settings.prefer_primitives_enabled`` is ``False`` or no skills load.
    """
    if not settings.prefer_primitives_enabled:
        return ""

    cache = _get_cache()
    chunks: list[str] = []
    seen: set[str] = set()

    bash_doc = cache.docs.get("bash")
    if bash_doc and bash_doc.body:
        chunks.append(bash_doc.body)
        seen.add("bash")

    if message:
        msg_lower = message.lower()
        for skill in cache.docs.values():
            if skill.name in seen:
                continue
            if skill.keywords and any(kw.lower() in msg_lower for kw in skill.keywords):
                chunks.append(skill.body)
                seen.add(skill.name)
                log.debug(
                    "skill_route_matched",
                    skill=skill.name,
                    message_preview=message[:80],
                )

    if not chunks:
        return ""
    return SKILL_BLOCK_HEADER + _SEPARATOR.join(chunks)


def find_skill_for_tool(tool_name: str) -> SkillDoc | None:
    """Return the first skill that lists *tool_name* in its ``tools`` field.

    Used by Phase B.5 reactive guards to look up linked skill metadata
    (known_bad_patterns, canonical_patterns) before a tool call executes.

    Args:
        tool_name: Registered tool name (e.g. ``"bash"``).

    Returns:
        The matching SkillDoc, or None if no skill claims this tool.
    """
    cache = _get_cache()
    for skill in cache.docs.values():
        if tool_name in skill.tools:
            return skill
    return None


def get_all_skills() -> dict[str, SkillDoc]:
    """Return all loaded skill docs keyed by name.

    Primarily used by tests and the Phase B skill index assembler.

    Returns:
        Mapping of skill name to SkillDoc.
    """
    return dict(_get_cache().docs)
