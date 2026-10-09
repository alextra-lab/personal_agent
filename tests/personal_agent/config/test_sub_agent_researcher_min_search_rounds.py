"""FRE-1561: the researcher minimum-search-rounds setting is off by default and validated."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from personal_agent.config.settings import AppConfig

_ENV = "AGENT_SUB_AGENT_RESEARCHER_MIN_SEARCH_ROUNDS"


def test_defaults_to_zero_so_the_prompt_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # AC-6: the default does not change under FRE-1561. Guard against a local .env override.
    monkeypatch.delenv(_ENV, raising=False)
    assert AppConfig().sub_agent_researcher_min_search_rounds == 0


def test_env_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "3")
    assert AppConfig().sub_agent_researcher_min_search_rounds == 3


def test_rejects_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "-1")
    with pytest.raises(ValidationError):
        AppConfig()
