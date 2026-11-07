# ruff: noqa: D102
"""The broker configuration (ADR-0159 D5.10, D5.12, D5.13; FRE-1683)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from personal_agent.dev_broker.config import BrokerConfig, load_broker_config
from pydantic import ValidationError

from personal_agent.config import load_model_config
from personal_agent.cost_gate.gate import RESERVATION_TTL_SECONDS


@pytest.fixture(scope="module")
def config() -> BrokerConfig:
    """The committed config."""
    return load_broker_config()


class TestCommittedConfig:
    def test_caps_are_the_owners_numbers_in_usd(self, config: BrokerConfig) -> None:
        assert config.job_cap_eur == Decimal("2.00")
        assert config.month_cap_eur == Decimal("30.00")
        assert config.job_cap_usd == Decimal("2.00") * config.eur_to_usd
        assert config.month_cap_usd == Decimal("30.00") * config.eur_to_usd

    def test_conversion_matches_the_rate_of_the_catalog_prices(self, config: BrokerConfig) -> None:
        # config/models.yaml prices the Scaleway entries at EUR x 1.18.
        assert config.eur_to_usd == Decimal("1.18")

    def test_call_limit_ends_before_the_cost_gate_ttl(self, config: BrokerConfig) -> None:
        assert config.call_timeout_seconds == 150
        assert config.call_timeout_seconds < RESERVATION_TTL_SECONDS

    def test_boundary_guard_covers_a_whole_call(self, config: BrokerConfig) -> None:
        assert config.boundary_guard_seconds >= config.call_timeout_seconds

    def test_default_model_is_deepseek(self, config: BrokerConfig) -> None:
        assert config.default_model == "deepseek-v4.1-flash-scaleway"
        assert config.default_model in config.models

    def test_every_model_is_a_priced_scaleway_catalog_entry(self, config: BrokerConfig) -> None:
        catalog = load_model_config().models
        for key in config.models:
            entry = catalog[key]
            assert entry.provider == "scaleway", key
            assert entry.input_cost_per_token and entry.input_cost_per_token > 0, key
            assert entry.output_cost_per_token and entry.output_cost_per_token > 0, key

    def test_listeners_use_two_ports(self, config: BrokerConfig) -> None:
        assert config.model_port != config.control_port


class TestValidation:
    def _base(self) -> dict[str, object]:
        return load_broker_config().model_dump()

    def test_a_call_limit_too_close_to_the_ttl_is_refused(self) -> None:
        data = self._base()
        data["call_timeout_seconds"] = RESERVATION_TTL_SECONDS - 5
        with pytest.raises(ValidationError, match="TTL"):
            BrokerConfig.model_validate(data)

    def test_a_guard_shorter_than_a_call_is_refused(self) -> None:
        data = self._base()
        data["boundary_guard_seconds"] = 60
        with pytest.raises(ValidationError, match="boundary_guard_seconds"):
            BrokerConfig.model_validate(data)

    def test_a_default_model_outside_the_list_is_refused(self) -> None:
        data = self._base()
        data["default_model"] = "claude_sonnet"
        with pytest.raises(ValidationError, match="default_model"):
            BrokerConfig.model_validate(data)

    def test_the_config_is_frozen(self) -> None:
        config = load_broker_config()
        with pytest.raises(ValidationError):
            config.job_cap_eur = Decimal("100")  # type: ignore[misc]
