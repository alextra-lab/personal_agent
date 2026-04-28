"""Skill-doc loader and injection module.

Loads skill documentation files from ``docs/skills/`` at import time into a
per-file cache, then exposes :func:`get_skill_block` for injection into the
system prompt.

FRE-282: switched from full-block injection (all 9 docs, ~8.7K tokens) to
intent-based injection — always inject bash.md plus one keyword-matched doc
(~2-3K tokens total).  Reduces per-request cost overhead from 3-4× to ~1.1-1.2×.
"""

from __future__ import annotations

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

# Keyword routing table: (keywords, skill_files) — first match wins.
# Keywords are lowercased substrings matched against the lowercased user message.
_KEYWORD_ROUTES: list[tuple[list[str], list[str]]] = [
    # Elasticsearch / telemetry queries
    # Note: bare "elasticsearch" removed — "are Neo4j and Elasticsearch both up?" is an
    # infra-health question, not a query question. Use specific query-intent keywords only.
    (
        [
            "agent-log",
            "trace_id",
            "kibana",
            "query_elasticsearch",
            "loop gate",
            "litellm",
            "tool_call",
            "last hour",
            "last day",
            "24 hour",
            "p95",
            "latency",
            "errors in the",
            "event_type",
            "esql",
            "agent-logs",
            "loop trace",
            "warn_consecutive",
            "block_consecutive",
            "query elasticsearch",
            "search elasticsearch",
        ],
        ["query-elasticsearch.md"],
    ),
    # URL fetching / web
    (
        [
            "fetch ",
            "https://",
            "http://",
            "readme on",
            "github.com",
            "anthropic.com",
            "current pricing",
            "what's on the page",
            "check the url",
        ],
        ["fetch-url.md"],
    ),
    # Directory / filesystem
    (
        [
            "list files",
            "files in /",
            "what's in /",
            "yaml files",
            "python files",
            "how many files",
            "under /app",
            "in /app/",
            "/app/config",
            "/app/src",
            "directory",
            "folder /",
            "how many yaml",
            "how many python",
        ],
        ["list-directory.md", "read-write.md"],
    ),
    # System metrics — CPU / memory / disk
    # Note: bare "memory usage" removed — "List top 10 processes by memory usage" is a
    # diagnostics question. Keep specific system-level metric keywords only.
    (
        [
            "cpu load",
            "cpu usage",
            "memory is the agent",
            "disk space",
            "disk usage",
            "load average",
            "how much memory",
            "current cpu",
            "is disk",
        ],
        ["system-metrics.md"],
    ),
    # System diagnostics — processes / ports / I/O
    (
        [
            "top 10 process",
            "processes by memory",
            "listening ports",
            "ports are listening",
            "which ports",
            "container ports",
            "vmstat",
            "system has been doing",
            "load swap",
            "io activity",
            "network connections",
            "iostat",
        ],
        ["system-diagnostics.md"],
    ),
    # Infrastructure health
    (
        [
            "infrastructure health",
            "services healthy",
            "postgres reachable",
            "neo4j",
            "backend services",
            "health check",
            "all services",
            "is postgres",
            "are neo4j",
            "infra health",
            "reachable",
            "check infrastructure",
        ],
        ["infrastructure-health.md"],
    ),
    # Explicit Python scripting
    (
        ["run python", "python script", "calculate using python", "write a python"],
        ["run-python.md"],
    ),
]


def _load_skill_cache() -> dict[str, str]:
    """Load each skill doc into a per-file cache at import time.

    Returns:
        Mapping of filename to stripped file content. Missing or unreadable
        files are logged and excluded.
    """
    cache: dict[str, str] = {}
    for name in _SKILL_FILES:
        p = _SKILLS_DIR / name
        if not p.exists():
            log.warning("skill_doc_missing", file=name)
            continue
        try:
            cache[name] = p.read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning("skill_doc_unreadable", file=name, error=str(exc))
    return cache


_SKILL_CACHE: dict[str, str] = _load_skill_cache()


def get_skill_block(message: str | None = None) -> str:
    """Return a skill library block for injection into the system prompt.

    Always includes ``bash.md`` (base tool reference).  When *message* is
    provided, the first matching keyword route injects one additional skill
    doc relevant to the request.  If no route matches, only ``bash.md`` is
    returned — never the full 9-doc block.

    Args:
        message: The original user message, used for keyword-based routing.
            Pass ``None`` to get bash-only (e.g. no user message context).

    Returns:
        Skill library block prefixed with a header, or an empty string when
        ``settings.prefer_primitives_enabled`` is ``False``.
    """
    if not settings.prefer_primitives_enabled:
        return ""

    chunks: list[str] = []

    bash_content = _SKILL_CACHE.get("bash.md")
    if bash_content:
        chunks.append(bash_content)

    if message:
        msg_lower = message.lower()
        matched: list[str] = []
        for keywords, skill_files in _KEYWORD_ROUTES:
            if any(kw in msg_lower for kw in keywords):
                matched = skill_files
                log.debug(
                    "skill_route_matched",
                    matched_files=skill_files,
                    message_preview=message[:80],
                )
                break

        for sf in matched:
            if sf == "bash.md":
                continue
            content = _SKILL_CACHE.get(sf)
            if content:
                chunks.append(content)

    if not chunks:
        return ""
    return SKILL_BLOCK_HEADER + _SEPARATOR.join(chunks)
