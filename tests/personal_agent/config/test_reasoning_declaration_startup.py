"""FRE-1007 AC-5 — a producer with no declared reasoning depth refuses to boot.

The ticket is explicit that this is not a warning and not a default: "any
scheduled or background model call with no declared role, model binding, budget
and reasoning configuration refuses to start ... Enforcement at startup, so a
misconfigured producer fails the configuration guard rather than being discovered
on a spend graph at three in the morning."

So these tests exercise ``load_app_config()`` — the real application-boot entry
point — rather than only the helper it calls. A helper that raises while nothing
invokes it at boot would satisfy the criterion on paper and fail it in fact.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from personal_agent.config.settings import enforce_reasoning_declaration, load_app_config

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The module object, taken from ``sys.modules`` rather than by dotted-string
#: target. ``personal_agent.config`` exports a ``settings`` *instance*, which
#: shadows the submodule of the same name, so
#: ``monkeypatch.setattr("personal_agent.config.settings.repo_root", ...)``
#: resolves to an attribute on the ``AppConfig`` object and fails.
_SETTINGS_MODULE = sys.modules["personal_agent.config.settings"]


class TestStartupRefusesAnUndeclaredProducer:
    """AC-5 — the boot path itself, not just the helper."""

    def test_load_app_config_raises_when_a_bound_producer_is_undeclared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            _SETTINGS_MODULE, "repo_root", lambda: _FIXTURES / "reasoning_undeclared"
        )
        with pytest.raises(ValueError, match="reasoning"):
            load_app_config()

    def test_load_app_config_does_NOT_raise_on_a_verification_finding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A network-dependent verdict must never be able to refuse a boot.

        `reasoning_declaration_rejected` is a real defect and CI blocks the deploy
        over it. But deciding it means asking litellm what the declared value
        becomes, and litellm's per-model capability map is fetched from GitHub at
        import — so on a host that reaches the provider but not GitHub, raising
        here would take the application down over a config file that had not
        changed. Not hypothetical: it is what the first cut of this gate did.
        """
        monkeypatch.setattr(_SETTINGS_MODULE, "repo_root", lambda: _FIXTURES / "reasoning_rejected")
        assert load_app_config() is not None

    def test_the_real_repo_boots(self) -> None:
        """The ratchet must not wedge the deployment it ships to."""
        assert load_app_config() is not None


class TestHelperIsSafetyClassOnly:
    """Policy findings never wedge boot — ADR-0099 D4's severity tiering."""

    def test_helper_is_silent_on_the_real_repo(self) -> None:
        from personal_agent.config.settings import AppConfig

        enforce_reasoning_declaration(AppConfig(), root=_REPO_ROOT)

    def test_helper_raises_against_a_seeded_negative(self) -> None:
        from personal_agent.config.settings import AppConfig

        with pytest.raises(ValueError, match="reasoning"):
            enforce_reasoning_declaration(AppConfig(), root=_FIXTURES / "reasoning_undeclared")
