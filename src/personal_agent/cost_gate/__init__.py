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
- :func:`run_counter_snapshotter` — long-running task that snapshots
  ``budget_counters`` to ES on a fixed cadence (FRE-547) for the cap-utilization
  dashboard panel; spawned from the FastAPI lifespan
- :func:`run_silence_monitor` — long-running task that flags a day where a
  cloud-selected ``primary`` session booked nothing to ``main_inference``
  (FRE-1117); spawned from the FastAPI lifespan
- :func:`set_default_gate` / :func:`get_default_gate_or_none` —
  module-level singleton accessor used by ``LiteLLMClient.respond()``;
  populated by the FastAPI lifespan hook at startup.
- :func:`budget_role_for` — map factory ``role_name`` strings (e.g. ``"primary"``,
  ``"sub_agent"``, ``"entity_extraction_role"``) to the budget role keys
  used in ``budget.yaml``. **Total and fail-closed** since FRE-989: an unknown
  name raises :class:`UnknownBudgetRoleError` rather than defaulting.
- :func:`validate_role_totality` — startup assertion that the role map,
  ``ModelRole`` and ``budget.yaml`` agree; call it from the lifespan hook.

The ``DenialReason`` enum is shared with the FRE-307 telemetry layer.
"""

from __future__ import annotations

from personal_agent.cost_gate.gate import RESERVATION_TTL_SECONDS, CostGate
from personal_agent.cost_gate.policy import BudgetConfigError, load_budget_config
from personal_agent.cost_gate.reaper import DEFAULT_REAPER_INTERVAL_SECONDS, run_reaper
from personal_agent.cost_gate.role_map import (
    BUDGET_ROLE_BY_FACTORY_NAME,
    NON_GATED_ROLES,
    UnknownBudgetRoleError,
    budget_role_for,
    role_totality_findings,
    validate_role_totality,
)
from personal_agent.cost_gate.silence_monitor import (
    DEFAULT_SILENCE_MONITOR_INTERVAL_SECONDS,
    run_silence_monitor,
)
from personal_agent.cost_gate.snapshotter import (
    DEFAULT_SNAPSHOT_INTERVAL_SECONDS,
    run_counter_snapshotter,
)
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

# ---------------------------------------------------------------------------
# Module-level singleton — set by the FastAPI lifespan hook
# ---------------------------------------------------------------------------

_default_gate: CostGate | None = None


def set_default_gate(gate: CostGate | None) -> None:
    """Register (or clear) the process-wide ``CostGate`` instance.

    Called by the FastAPI lifespan hook at startup with a connected gate,
    and again with ``None`` at shutdown. Tests use this to substitute a
    mock-friendly gate.
    """
    global _default_gate
    _default_gate = gate


def get_default_gate_or_none() -> CostGate | None:
    """Return the registered gate, or ``None`` if no gate has been set."""
    return _default_gate


def get_default_gate() -> CostGate:
    """Return the registered gate or raise.

    Use this when the gate is required (e.g. inside ``LiteLLMClient.respond``);
    failing fast surfaces missing wiring rather than silently degrading to
    the old advisory-check failure mode that produced the FRE-302 incident.
    """
    if _default_gate is None:
        raise RuntimeError(
            "No CostGate registered. Call set_default_gate(gate) during "
            "application startup before any paid LLM call."
        )
    return _default_gate


__all__ = [
    "BUDGET_ROLE_BY_FACTORY_NAME",
    "DEFAULT_REAPER_INTERVAL_SECONDS",
    "DEFAULT_SILENCE_MONITOR_INTERVAL_SECONDS",
    "DEFAULT_SNAPSHOT_INTERVAL_SECONDS",
    "NON_GATED_ROLES",
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
    "UnknownBudgetRoleError",
    "budget_role_for",
    "get_default_gate",
    "get_default_gate_or_none",
    "load_budget_config",
    "role_totality_findings",
    "run_counter_snapshotter",
    "run_reaper",
    "run_silence_monitor",
    "set_default_gate",
    "validate_role_totality",
]
