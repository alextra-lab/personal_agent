"""SLM-health monitor — cross-tunnel inference-server observability (FRE-399 / ADR-0083).

Exposes the probe, snapshot model, cache helpers, and scheduler entry point.

The monitor ships and operates correctly today with the SLM's liveness-only
``/health`` response (rich fields → ``None``); it will automatically populate
GPU util, VRAM, queue depth, and model-loaded status once the Mac-side
enrichment child ticket lands (same pattern as FRE-411 for ``slm-requests-*``).

Typical usage (the brainstem scheduler):

.. code-block:: python

    from personal_agent.observability.slm_health import run_scheduled_slm_health_probe

    snapshot = await run_scheduled_slm_health_probe(es_client=es)

Ad-hoc / endpoint usage:

.. code-block:: python

    from personal_agent.observability.slm_health import (
        probe_slm_health,
        get_cached_snapshot,
        set_cached_snapshot,
    )
"""

from __future__ import annotations

from personal_agent.observability.slm_health.cache import (
    clear_cache,
    get_cached_snapshot,
    set_cached_snapshot,
)
from personal_agent.observability.slm_health.probe import probe_slm_health
from personal_agent.observability.slm_health.scheduler_runner import (
    run_scheduled_slm_health_probe,
)
from personal_agent.observability.slm_health.snapshot import SlmHealthSnapshot

__all__ = [
    "SlmHealthSnapshot",
    "probe_slm_health",
    "run_scheduled_slm_health_probe",
    "get_cached_snapshot",
    "set_cached_snapshot",
    "clear_cache",
]
