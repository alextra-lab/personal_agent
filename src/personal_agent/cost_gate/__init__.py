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
- :func:`set_default_gate` / :func:`get_default_gate_or_none` —
  module-level singleton accessor used by ``LiteLLMClient.respond()``;
  populated by the FastAPI lifespan hook at startup.
- :func:`budget_role_for` — map factory ``role_name`` strings (e.g. ``"primary"``,
  ``"sub_agent"``, ``"entity_extraction_role"``) to the budget role keys
  used in ``budget.yaml``.

The ``DenialReason`` enum is shared with the FRE-307 telemetry layer.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Factory role_name → budget role mapping
# ---------------------------------------------------------------------------

# Maps the factory's ``role_name`` argument (used by callers of
# ``get_llm_client``) to the budget role keys declared in budget.yaml. New
# call sites should prefer to use a budget-role name directly; this mapping
# exists to give existing call sites a sensible default without a sweeping
# rename.
_BUDGET_ROLE_BY_FACTORY_NAME: dict[str, str] = {
    # Executor / orchestrator roles → main_inference (user-facing flow)
    "primary": "main_inference",
    "sub_agent": "main_inference",
    "compressor": "main_inference",
    "router": "main_inference",
    "reasoning": "main_inference",
    "standard": "main_inference",
    "main_inference": "main_inference",
    # Background consumers
    "captains_log_role": "captains_log",
    "captains_log": "captains_log",
    "insights_role": "insights",
    "insights": "insights",
    "entity_extraction_role": "entity_extraction",
    "entity_extraction": "entity_extraction",
    "promotion_role": "promotion",
    "promotion": "promotion",
    "freshness_role": "freshness",
    "freshness": "freshness",
}


def budget_role_for(factory_role_name: str) -> str:
    """Resolve a factory ``role_name`` to its budget role key.

    Unknown names default to ``"main_inference"`` so a new role flag doesn't
    silently bypass the gate. The default still triggers the user-facing cap
    rather than a more permissive background cap.

    Args:
        factory_role_name: The ``role_name`` argument to ``get_llm_client``.

    Returns:
        Budget role key (declared in ``budget.yaml``).
    """
    return _BUDGET_ROLE_BY_FACTORY_NAME.get(factory_role_name, "main_inference")


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
    "budget_role_for",
    "get_default_gate",
    "get_default_gate_or_none",
    "load_budget_config",
    "run_reaper",
    "set_default_gate",
]
