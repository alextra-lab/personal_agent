"""The retry cap is a validated setting, not a convention (ADR-0151 D3, FRE-1509 AC-6).

ADR-0151 allows at most one cite-only retry: 1 means no retry, 2 means one. A configured
value above 2 must stop the process at settings load, not run a turn that retries twice.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from personal_agent.config.settings import AppConfig


def test_ac6_a_cap_above_two_is_refused() -> None:
    with pytest.raises(ValidationError, match="grounding_max_generation_attempts"):
        AppConfig(grounding_max_generation_attempts=3)


@pytest.mark.parametrize("attempts", [1, 2])
def test_a_cap_of_one_or_two_is_accepted(attempts: int) -> None:
    """The seeded positive: the two legal values still construct."""
    assert AppConfig(
        grounding_max_generation_attempts=attempts
    ).grounding_max_generation_attempts == (attempts)
