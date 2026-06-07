"""Execution-topology observability seam (ADR-0088 / FRE-513).

The seam is the mandatory boundary every execution topology passes through so status,
cost, and degradation are observable by construction (ADR-0088 D1/D2). It has two sinks
(D6): a **direct durable write** (the FRE-452 route-trace ledger row, bus-independent —
D8) and a **best-effort bus event** on ``stream:turn.observed`` for the live projector.
"""

from personal_agent.observability.topology.seam import (
    current_topology,
    observe_topology,
    report_degradation,
)

__all__ = ["current_topology", "observe_topology", "report_degradation"]
