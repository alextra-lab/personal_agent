"""Types and enums for the Cost Check Gate (ADR-0065).

The gate's public surface is intentionally small: a few enums, the
``BudgetDenied`` exception that callers handle, and the typed config models
loaded from ``config/governance/budget.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Reservation identifiers are UUIDs. The alias keeps call sites readable
# without forcing every consumer to import ``uuid.UUID`` directly.
ReservationId = UUID


class TimeWindow(str, Enum):
    """Time-window keys recognised by ``budget_policies`` and ``budget_counters``."""

    DAILY = "daily"
    WEEKLY = "weekly"


class ReservationStatus(str, Enum):
    """Lifecycle states for a ``budget_reservations`` row."""

    ACTIVE = "active"
    COMMITTED = "committed"
    REFUNDED = "refunded"
    EXPIRED = "expired"


class DenialReason(str, Enum):
    """Structured reason a reservation was denied.

    Used both by ``BudgetDenied`` and as the ``denial_reason`` enum on the
    ``consolidation_attempts`` and budget/governance log events (FRE-307).
    """

    CAP_EXCEEDED = "cap_exceeded"
    POLICY_VIOLATION = "policy_violation"
    RESERVATION_FAILED = "reservation_failed"
    PROVIDER_ERROR = "provider_error"


class OnDenialBehaviour(str, Enum):
    """How a caller of the gate handles a ``BudgetDenied`` exception.

    Loaded from each role's ``on_denial`` field in ``budget.yaml``. The gate
    itself always raises; the caller's behaviour is what matters at the
    integration site (raise to FastAPI vs let the message redeliver).
    """

    RAISE = "raise"
    NACK = "nack"


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


@dataclass
class BudgetDenied(Exception):
    """Raised by the gate when a reservation cannot be approved.

    The structured payload is everything the FastAPI 503 mapping (FRE-306) and
    the Kibana panels (FRE-307) need to render an actionable failure surface.

    Attributes:
        role: The role that tripped the cap (the most-restrictive one when
            multiple matching policies exist — e.g. ``main_inference`` vs
            ``_total``).
        time_window: ``daily`` or ``weekly``.
        current_spend: ``running_total`` as observed inside the locked
            transaction.
        cap: The cap on the policy that denied.
        window_resets_at: UTC timestamp of the next window boundary; PWA
            renders this as the "you can try again at" line.
        denial_reason: Machine-readable reason; see ``DenialReason``.
        provider: Provider scope of the denied policy (None in v1).
    """

    role: str
    time_window: str
    current_spend: Decimal
    cap: Decimal
    window_resets_at: datetime
    denial_reason: str = DenialReason.CAP_EXCEEDED.value
    provider: str | None = None

    def __str__(self) -> str:
        """Human-readable rendering used by structured loggers and tests."""
        return (
            f"BudgetDenied(role={self.role}, window={self.time_window}, "
            f"spend=${self.current_spend:.4f}, cap=${self.cap:.4f}, "
            f"reason={self.denial_reason})"
        )


# ---------------------------------------------------------------------------
# Configuration models (loaded from config/governance/budget.yaml)
# ---------------------------------------------------------------------------


class RoleConfig(BaseModel):
    """Per-role estimation and denial behaviour.

    These are *not* caps — caps live in the ``caps`` list. This carries the
    pre-call estimator inputs and how the caller should react to denial.
    """

    model_config = ConfigDict(frozen=True)

    default_output_tokens: int = Field(
        ge=1,
        description=(
            "Used when the call doesn't pin a max_tokens, or as the upper "
            "bound when max_tokens is larger. The estimator sizes the "
            "reservation against min(max_tokens, default_output_tokens)."
        ),
    )
    safety_factor: float = Field(
        default=1.2,
        ge=1.0,
        description=(
            "Multiplier applied to the estimated output cost. 1.2 catches "
            "95%+ of overshoots without starving normal-shaped calls."
        ),
    )
    on_denial: OnDenialBehaviour = Field(
        description=(
            "How the caller for this role handles BudgetDenied. The gate "
            "always raises; this controls integration-site behaviour."
        ),
    )


class CapEntry(BaseModel):
    """A single cap row from the YAML, mirroring ``budget_policies``.

    v1 only populates ``time_window`` + ``role`` + ``cap_usd``. ``user_id``
    and ``provider`` are reserved for v2 and not exposed in YAML yet.
    """

    model_config = ConfigDict(frozen=True)

    time_window: Literal["daily", "weekly"]
    role: str
    cap_usd: Decimal


class BudgetConfig(BaseModel):
    """Top-level shape of ``config/governance/budget.yaml``."""

    model_config = ConfigDict(frozen=True)

    version: int = Field(default=1, ge=1)
    roles: dict[str, RoleConfig]
    caps: list[CapEntry]

    def role(self, name: str) -> RoleConfig:
        """Look up role config; raises ``KeyError`` if undeclared.

        Args:
            name: Role identifier (e.g. ``main_inference``).

        Returns:
            The frozen ``RoleConfig`` entry.

        Raises:
            KeyError: If the role is not declared in YAML. Callers should
                surface this as a configuration error, not silently default.
        """
        return self.roles[name]

    def caps_for(
        self,
        role: str,
        *,
        time_windows: tuple[str, ...] = ("daily", "weekly"),
    ) -> list[CapEntry]:
        """Return every cap that applies to a reservation for ``role``.

        Includes both same-role caps and the synthetic ``_total`` caps for
        each window — the gate locks every returned row inside a single
        transaction so one role can't starve another.

        Args:
            role: Role being reserved against.
            time_windows: Windows to consider; defaults to all known windows.

        Returns:
            List of caps to enforce. Empty when no caps apply (no caps
            configured = unlimited; treat as approval).
        """
        out: list[CapEntry] = []
        for cap in self.caps:
            if cap.time_window not in time_windows:
                continue
            if cap.role == role or cap.role == "_total":
                out.append(cap)
        return out
