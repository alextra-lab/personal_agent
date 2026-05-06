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


_SKILL_INDEX_HEADER = "## Available Skills\n\nCall `read_skill(name)` to load the full guidance for any skill below.\n\n"
_CHARS_PER_TOKEN = 4  # conservative estimate for token counting


def assemble_skill_index(cap_tokens: int = 2048) -> str:
    """Emit a compact skill index for model_decided and hybrid routing modes.

    Each line is ``- <name>: <description>``.  The index is capped at *cap_tokens*
    (estimated at 4 chars/token) to stay within prompt budgets.

    Args:
        cap_tokens: Maximum token budget for the index block. Defaults to 2048.

    Returns:
        Formatted skill index string, or empty string if no skills are loaded.
    """
    cache = _get_cache()
    if not cache.docs:
        return ""

    cap_chars = cap_tokens * _CHARS_PER_TOKEN
    lines: list[str] = []
    total = len(_SKILL_INDEX_HEADER)

    for skill in cache.docs.values():
        line = f"- {skill.name}: {skill.description}\n"
        if total + len(line) > cap_chars:
            log.debug("skill_index_truncated", cap_tokens=cap_tokens, truncated_at=skill.name)
            break
        lines.append(line)
        total += len(line)

    if not lines:
        return ""
    return _SKILL_INDEX_HEADER + "".join(lines)


def get_skill_block(
    message: str | None = None,
    loaded_skills: frozenset[str] | set[str] | None = None,
) -> str:
    """Return a skill library block for keyword-based injection into the system prompt.

    Always includes the ``bash`` skill body (base tool reference). When
    *message* is provided, additionally injects any skills whose ``keywords``
    match a substring of the lowercased message.

    In ``hybrid`` routing mode, pass *loaded_skills* to suppress injecting bodies
    for skills the model has already read via ``read_skill`` this conversation.

    Args:
        message: The original user message used for keyword-based routing.
            Pass ``None`` to get the bash-only block.
        loaded_skills: Skill names already loaded this conversation. Bodies for
            these skills are suppressed to avoid duplication. Ignored when None.

    Returns:
        Skill library block prefixed with a header, or an empty string when
        ``settings.prefer_primitives_enabled`` is ``False`` or no skills load.
    """
    if not settings.prefer_primitives_enabled:
        return ""

    _already_loaded = loaded_skills or set()
    cache = _get_cache()
    chunks: list[str] = []
    seen: set[str] = set()

    bash_doc = cache.docs.get("bash")
    if bash_doc and bash_doc.body and "bash" not in _already_loaded:
        chunks.append(bash_doc.body)
        seen.add("bash")

    if message:
        msg_lower = message.lower()
        for skill in cache.docs.values():
            if skill.name in seen or skill.name in _already_loaded:
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


def find_skills_for_tool(tool_name: str) -> list[SkillDoc]:
    """Return all skills that list *tool_name* in their ``tools`` field.

    Multiple skills can declare the same tool (e.g. bash.md, query-elasticsearch.md,
    and fetch-url.md all list ``bash``).  The Phase B.5 reactive guard must check
    ``known_bad_patterns`` across ALL of them, not just the first match.

    Args:
        tool_name: Registered tool name (e.g. ``"bash"``).

    Returns:
        List of matching SkillDocs, empty if no skill claims this tool.
    """
    cache = _get_cache()
    return [skill for skill in cache.docs.values() if tool_name in skill.tools]


def get_all_skills() -> dict[str, SkillDoc]:
    """Return all loaded skill docs keyed by name.

    Primarily used by tests and the Phase B skill index assembler.

    Returns:
        Mapping of skill name to SkillDoc.
    """
    return dict(_get_cache().docs)
