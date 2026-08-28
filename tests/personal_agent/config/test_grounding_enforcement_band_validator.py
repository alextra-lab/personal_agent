"""Boot-time rejection of grounding-enforcement deadlocks (ADR-0138 D5 / FRE-1285).

Both relations guarded here produce the same end state — **every model permanently
heavy, silently**. The system keeps answering, no observation is ever written, no model
can ever be measured, and the only tell is a log line nobody is watching.

They are validated at settings load rather than where they bite because the objects they
feed (``EnforcementBand``, ``ComplianceWindow``) are built lazily *inside the turn path*,
where the failure degrades to a caught exception per turn instead of a refusal to start.
A misconfiguration that stops the control loop working should stop the process, not run
forever pretending to enforce.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from personal_agent.config.settings import AppConfig


class TestBandMustBeOpen:
    """``demote_below`` strictly below the contract bar, or the band is not a band."""

    @pytest.mark.parametrize(
        ("bar", "demote_below"),
        [
            (0.95, 0.95),  # collapsed — one value, which ADR-0138 D5 forbids outright
            (0.90, 0.95),  # inverted — demote above promote
            (0.50, 0.99),  # inverted by a wide margin
        ],
    )
    def test_a_collapsed_or_inverted_band_is_refused_at_boot(
        self, bar: float, demote_below: float
    ) -> None:
        """The failure is silent at runtime, so it has to be loud at startup.

        ``EnforcementBand`` raises on construction under these values; selection catches
        that and falls back to heavy with ``probation=False``, so no observation is ever
        written and the metric dies along with the band.
        """
        with pytest.raises(ValidationError, match="demote_below"):
            AppConfig(
                grounding_compliance_bar=bar,
                grounding_enforcement_demote_below=demote_below,
            )

    def test_an_open_band_is_accepted(self) -> None:
        """The seeded positive: a legitimate band must still construct."""
        cfg = AppConfig(
            grounding_compliance_bar=0.95,
            grounding_enforcement_demote_below=0.90,
        )
        assert cfg.grounding_enforcement_demote_below < cfg.grounding_compliance_bar


class TestWindowMustBeAbleToReachItsMinimum:
    """``min_samples`` above ``window_size`` is permanent ``INSUFFICIENT_SAMPLES``."""

    def test_a_minimum_above_the_window_size_is_refused_at_boot(self) -> None:
        """No amount of traffic can satisfy a minimum larger than the window.

        The window takes the most recent ``size`` observations and only then filters for
        freshness, so the fresh count can never exceed ``size``.
        """
        with pytest.raises(ValidationError, match="min_samples"):
            AppConfig(
                grounding_compliance_window_size=10,
                grounding_compliance_min_samples=20,
            )

    def test_a_minimum_equal_to_the_window_size_is_allowed(self) -> None:
        """Reachable, if only exactly — a full fresh window meets it."""
        cfg = AppConfig(
            grounding_compliance_window_size=20,
            grounding_compliance_min_samples=20,
        )
        assert cfg.grounding_compliance_min_samples == cfg.grounding_compliance_window_size


class TestCommittedDefaultsPass:
    """The shipped configuration must satisfy its own guards."""

    def test_bare_app_config_construction_does_not_raise(self) -> None:
        """A bare AppConfig() under the test-suite environment loads cleanly."""
        cfg = AppConfig()
        assert cfg.grounding_enforcement_demote_below < cfg.grounding_compliance_bar
        assert cfg.grounding_compliance_min_samples <= cfg.grounding_compliance_window_size
