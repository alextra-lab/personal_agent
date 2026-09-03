"""ADR-0141 D2.2 / FRE-1364 — boot refuses to start with the unsafe litellm flag set.

EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER reroutes litellm's OpenAI-SDK-route
dispatch through a handler that silently drops the egress-guarded AsyncOpenAI
client LiteLLMClient injects for that route (ADR-0141 D2.1) — no error, no
signal, just an unguarded call. Exercises ``load_app_config()`` — the real
application-boot entry point — not only the helper it calls, matching the
FRE-1007 precedent in ``test_reasoning_declaration_startup.py``: a helper that
raises while nothing invokes it at boot satisfies the criterion on paper and
fails it in fact.
"""

from __future__ import annotations

import pytest

from personal_agent.config.settings import (
    AppConfig,
    enforce_experimental_litellm_handler_disabled,
    load_app_config,
)


class TestStartupRefusesTheExperimentalHandler:
    def test_load_app_config_raises_when_the_flag_is_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER", "true")
        with pytest.raises(ValueError, match="EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER"):
            load_app_config()

    def test_the_real_repo_boots(self) -> None:
        """The guard must not wedge the deployment it ships to."""
        assert load_app_config() is not None


class TestHelperSeededNegative:
    def test_helper_is_silent_when_unset(self) -> None:
        enforce_experimental_litellm_handler_disabled(AppConfig())

    def test_helper_raises_against_a_seeded_negative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER", "true")
        with pytest.raises(ValueError, match="EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER"):
            enforce_experimental_litellm_handler_disabled(AppConfig())
