"""Cost Check Gate — atomic Postgres-backed reservation primitive (ADR-0065).

Public surface:

- :class:`CostGate` — instantiated once at app startup; exposes ``reserve``,
  ``commit``, ``refund``, and ``reap_stale``
- :class:`BudgetDenied` — raised by ``reserve`` when any matching cap would
  be exceeded; carries the structured payload FastAPI/PWA need to render an
  explicit failure
- :class:`BudgetConfig`, :class:`RoleConfig`, :class:`CapEntry` — frozen
  Pydantic models loaded from ``config/governance/budget.yaml``
- :func:`load_budget_config` — load the YAML into a ``BudgetConfig``
- :func:`run_reaper` — long-running task to spawn from the FastAPI lifespan

The ``DenialReason`` enum is shared with the FRE-307 telemetry layer.
"""

from personal_agent.cost_gate.gate import RESERVATION_TTL_SECONDS, CostGate
from personal_agent.cost_gate.policy import BudgetConfigError, load_budget_config
from personal_agent.cost_gate.reaper import DEFAULT_REAPER_INTERVAL_SECONDS, run_reaper
from personal_agent.cost_gate.types import (
    BudgetConfig,
    BudgetDenied,
    CapEntry,
    DenialReason,
    OnDenialBehaviour,
    ReservationId,
    ReservationStatus,
    RoleConfig,
    TimeWindow,
)

__all__ = [
    "DEFAULT_REAPER_INTERVAL_SECONDS",
    "RESERVATION_TTL_SECONDS",
    "BudgetConfig",
    "BudgetConfigError",
    "BudgetDenied",
    "CapEntry",
    "CostGate",
    "DenialReason",
    "OnDenialBehaviour",
    "ReservationId",
    "ReservationStatus",
    "RoleConfig",
    "TimeWindow",
    "load_budget_config",
    "run_reaper",
]
