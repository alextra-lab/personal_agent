"""The broker configuration (ADR-0159 D5.10–D5.13; FRE-1683).

The values live in ``config/dev_agent/broker.yaml``. This module loads them into a frozen model and
refuses a file whose limits cannot hold.
"""

from __future__ import annotations

from decimal import Decimal
from ipaddress import IPv4Network
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator

from personal_agent.cost_gate.gate import RESERVATION_TTL_SECONDS

DEFAULT_BROKER_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "dev_agent" / "broker.yaml"
)

#: Time that a timed-out call needs to settle before its cost-gate reservation expires.
_SETTLE_MARGIN_SECONDS = 10


class BrokerConfig(BaseModel):
    """The broker's limits, models and listeners.

    Attributes:
        eur_to_usd: EUR to USD factor for the caps (D5.12).
        job_cap_eur: The per-job cap in EUR.
        month_cap_eur: The per-UTC-month cap in EUR.
        call_timeout_seconds: The longest time a model call can run (D5.10).
        boundary_guard_seconds: No call starts this close to a cost-gate boundary (D5.11).
        max_output_tokens: The most output tokens one call can request.
        prompt_overhead_tokens: Added to the byte length of a request to bound its prompt tokens.
        max_request_bytes: The largest request body that the model route reads.
        models: The catalog keys that a job token can name.
        default_model: The model of a job that names none.
        model_port: Port of the model route.
        control_port: Port of the control API.
        control_subnet: The ``cloud-sim`` subnet. The control API binds the broker's address in it.
        opencode_port: Port of the OpenCode server in the agent container.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    eur_to_usd: Decimal = Field(gt=0)
    job_cap_eur: Decimal = Field(gt=0)
    month_cap_eur: Decimal = Field(gt=0)
    call_timeout_seconds: float = Field(gt=0)
    boundary_guard_seconds: float = Field(ge=0)
    max_output_tokens: int = Field(gt=0)
    prompt_overhead_tokens: int = Field(ge=0)
    max_request_bytes: int = Field(gt=0)
    models: tuple[str, ...] = Field(min_length=1)
    default_model: str
    model_port: int = Field(gt=0, lt=65536)
    control_port: int = Field(gt=0, lt=65536)
    control_subnet: IPv4Network
    opencode_port: int = Field(gt=0, lt=65536)

    @model_validator(mode="after")
    def _limits_hold(self) -> BrokerConfig:
        """Refuse limits that would let a cost-gate reservation expire, or a call cross a window."""
        if self.call_timeout_seconds + _SETTLE_MARGIN_SECONDS > RESERVATION_TTL_SECONDS:
            raise ValueError(
                f"call_timeout_seconds {self.call_timeout_seconds} leaves less than "
                f"{_SETTLE_MARGIN_SECONDS} s to settle before the cost-gate TTL "
                f"({RESERVATION_TTL_SECONDS} s)"
            )
        if self.boundary_guard_seconds < self.call_timeout_seconds + _SETTLE_MARGIN_SECONDS:
            raise ValueError(
                f"boundary_guard_seconds must be at least call_timeout_seconds + "
                f"{_SETTLE_MARGIN_SECONDS}, or a call and its settle can cross a cost-gate window "
                "boundary"
            )
        if self.default_model not in self.models:
            raise ValueError(f"default_model {self.default_model!r} is not in models")
        if self.model_port == self.control_port:
            raise ValueError("model_port and control_port must differ")
        return self

    @property
    def job_cap_usd(self) -> Decimal:
        """The per-job cap in the ledger currency."""
        return self.job_cap_eur * self.eur_to_usd

    @property
    def month_cap_usd(self) -> Decimal:
        """The per-month cap in the ledger currency."""
        return self.month_cap_eur * self.eur_to_usd


def load_broker_config(path: Path = DEFAULT_BROKER_CONFIG_PATH) -> BrokerConfig:
    """Load and validate the broker configuration.

    Args:
        path: The YAML file.

    Returns:
        The validated, frozen config.

    Raises:
        pydantic.ValidationError: If a value is missing or a limit cannot hold.
    """
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return BrokerConfig.model_validate(data)
