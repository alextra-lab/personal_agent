"""Isolated System-graph store (ADR-0105 D2/D3).

``SysgraphRepository`` is the only code path permitted to open a connection
to the ``sysgraph`` Postgres schema. No memory/recall/tutor code path may
construct or import it — enforced by
``tests/personal_agent/sysgraph/test_isolation.py``.
"""

from __future__ import annotations

from personal_agent.sysgraph.repository import SysgraphRepository

__all__ = [
    "SysgraphRepository",
    "get_default_sysgraph_repo",
    "set_default_sysgraph_repo",
]

_default_sysgraph_repo: SysgraphRepository | None = None


def set_default_sysgraph_repo(repo: SysgraphRepository | None) -> None:
    """Set the process-level shared, connected SysgraphRepository (ADR-0105 D9/FRE-721).

    Set once at app startup (a connected repo) and cleared at shutdown (``None``),
    mirroring ``captains_log.capture.set_default_es_handler``. Avoids opening a
    fresh asyncpg pool on every per-turn Captain's Log reflection call — the
    hottest call site that reads sysgraph in this ADR.

    Args:
        repo: A connected repository, or ``None`` to clear it.
    """
    global _default_sysgraph_repo
    _default_sysgraph_repo = repo


def get_default_sysgraph_repo() -> SysgraphRepository | None:
    """Return the shared SysgraphRepository set via ``set_default_sysgraph_repo``.

    Returns:
        The connected repository, or ``None`` when not wired (e.g. sysgraph
        connect failed at startup, or in tests that never call the setter) —
        callers must treat ``None`` as "feature not available here."
    """
    return _default_sysgraph_repo
