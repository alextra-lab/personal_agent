"""Isolated System-graph store (ADR-0105 D2/D3).

``SysgraphRepository`` is the only code path permitted to open a connection
to the ``sysgraph`` Postgres schema. No memory/recall/tutor code path may
construct or import it — enforced by
``tests/personal_agent/sysgraph/test_isolation.py``.
"""

from __future__ import annotations

from personal_agent.sysgraph.repository import SysgraphRepository

__all__ = ["SysgraphRepository"]
