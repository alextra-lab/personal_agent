"""Inference priority tiers (ADR-0145 D7).

Split out of ``concurrency.py`` so ``llm_client/models.py``'s ``RoleBinding`` can
declare a ``priority: InferencePriority`` field without an import cycle:
``concurrency.py`` transitively imports ``llm_client.models`` (``concurrency`` ->
``personal_agent.telemetry`` -> ``telemetry.queries`` -> ``personal_agent.config.settings``,
which runs ``config/__init__.py`` -> ``config.model_loader`` -> ``llm_client.models``),
so ``models.py`` importing straight from ``concurrency.py`` fails with a
partially-initialized-module error whenever ``concurrency`` is the first of the two
loaded (verified 2026-09-07). This module has no internal dependencies, so either
side can import it in any order.
"""

from __future__ import annotations

from enum import IntEnum


class InferencePriority(IntEnum):
    """Priority tiers for inference requests.

    Lower numeric value = higher priority. When a semaphore slot opens,
    the highest-priority (lowest value) waiting request proceeds first.
    """

    CRITICAL = 0
    USER_FACING = 1
    ELEVATED = 2
    BACKGROUND = 3
    DEFERRED = 4
