"""Skill-doc loader and injection module.

Loads skill documentation files from ``docs/skills/`` at import time,
concatenates them into a single cached string, and exposes
:func:`get_skill_block` for injection into the system prompt.
"""

from pathlib import Path

import structlog

from personal_agent.config import settings

log = structlog.get_logger(__name__)

_SKILL_FILES = [
    "bash.md",
    "read-write.md",
    "run-python.md",
    "query-elasticsearch.md",
    "fetch-url.md",
    "list-directory.md",
    "system-metrics.md",
    "system-diagnostics.md",
    "infrastructure-health.md",
]
_SKILLS_DIR = Path(__file__).resolve().parents[3] / "docs" / "skills"
_SEPARATOR = "\n\n---\n\n"
SKILL_BLOCK_HEADER = (
    "## Skill Library — How to Drive Primitive Tools\n\n"
    "The following skill docs are reference material for using the `bash`, `read`, "
    "`write`, and `run_python` primitives effectively. Prefer these idioms over named "
    "curated tools when both are available.\n\n"
)


def _load_skill_block() -> str:
    chunks: list[str] = []
    for name in _SKILL_FILES:
        p = _SKILLS_DIR / name
        if not p.exists():
            log.warning("skill_doc_missing", file=name)
            continue
        try:
            content = p.read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning("skill_doc_unreadable", file=name, error=str(exc))
            continue
        chunks.append(content)
    return SKILL_BLOCK_HEADER + _SEPARATOR.join(chunks) if chunks else ""


_CACHED_BLOCK: str = _load_skill_block()


def get_skill_block() -> str:
    """Return the cached skill library block for injection into the system prompt.

    Returns:
        The concatenated skill docs prefixed with a library header, or an empty
        string if ``settings.prefer_primitives_enabled`` is False.
    """
    if not settings.prefer_primitives_enabled:
        return ""
    return _CACHED_BLOCK
