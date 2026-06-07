"""Route-trace ledger — per-turn stimulus → model path → result-type instrument.

Implements the FRE-452 route-trace ledger: the **direct durable write** sink of the
ADR-0088 execution-topology observability contract (D6 sink 1). A single Postgres row
per turn captures what the gateway *decided* (the deterministic-shell label) alongside
what the harness *actually did* (the orchestration event), so the gap between the two —
the core thing ADR-0088 and the result-type taxonomy exist to expose — becomes visible
and joinable to ``api_costs`` on ``trace_id``.

Layering (this module owns only the ledger, not the full ADR-0088 seam):

- :mod:`.types` — :class:`RouteTraceRow` (the seam-neutral DTO) + ``OrchestrationEvent``.
- :mod:`.classifier` — programmatic orchestration-event classification (taxonomy §3).
- :mod:`.assembler` — interim primary-turn adapter: ``ExecutionContext`` → ``RouteTraceRow``.
- :mod:`.ledger` — :class:`RouteTraceLedger` durable write/read service.

References:
    - ``docs/architecture_decisions/ADR-0088-execution-topology-observability-contract.md``
    - ``docs/specs/RESULT_TYPE_TAXONOMY_SPEC.md`` (FRE-451) → ADR-0084 §D4
    - ``docs/architecture_decisions/ADR-0074-*`` (identity / joinability)
"""

from personal_agent.observability.route_trace.assembler import assemble_route_trace
from personal_agent.observability.route_trace.classifier import (
    classify_orchestration_event,
)
from personal_agent.observability.route_trace.ledger import (
    RouteTraceLedger,
    get_route_trace_ledger,
    route_trace_ledger,
)
from personal_agent.observability.route_trace.types import (
    OrchestrationEvent,
    RouteTraceRow,
)

__all__ = [
    "OrchestrationEvent",
    "RouteTraceLedger",
    "RouteTraceRow",
    "assemble_route_trace",
    "classify_orchestration_event",
    "get_route_trace_ledger",
    "route_trace_ledger",
]
