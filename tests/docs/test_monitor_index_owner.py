"""FRE-1603 (ADR-0156 A5): one skill owns the monitor indices, and the catalog lines are short.

The FRE-1597 routing run failed AC-1 because the catalog lines of ``log-search`` and
``service-health`` both claimed the monitor indices (``log-search#2`` went to ``service-health`` on
3 of 3 draws). It failed AC-3 by 0.7 points because the A4 lines cost about 300 tokens, three
times the ADR's estimate.

These tests pin the text that the routing re-run then measures. They do not replace that run.
"""

from __future__ import annotations

import pytest

from personal_agent.orchestrator.skills import SkillDoc, get_all_skills

# The seven skills that ADR-0156 A4 adds to the catalog.
_A4_SKILLS = (
    "log-search",
    "model-latency",
    "turn-explanation",
    "spend-and-tokens",
    "interaction-outcomes",
    "service-health",
    "graph-inspection",
)

# FRE-1597 measured the seven descriptions at 3,738 characters on the final build, and the catalog
# block at 4.5 characters per token (1,340 characters = 298 tokens). AC-3 needs 10 tokens less
# (about 45 characters). The budget below asks for 120 characters (about 27 tokens), so that the
# character estimate has a margin of more than two. The llama.cpp re-run gives the real count.
_A4_BASELINE_CHARS = 3_738
_A4_REQUIRED_SAVING_CHARS = 120


def _skill(name: str) -> SkillDoc:
    return get_all_skills()[name]


def _claim_and_not_for(description: str) -> tuple[str, str]:
    """Split a description at its "Not for…" line."""
    claim, _, not_for = description.partition("Not for")
    return claim, not_for


# ── AC-1: one owner ──────────────────────────────────────────────────────────────


def test_service_health_description_does_not_claim_the_monitors() -> None:
    claim, _ = _claim_and_not_for(_skill("service-health").description)
    assert "monitor" not in claim.lower()
    assert "pipeline" not in claim.lower()


def test_service_health_not_for_line_gives_the_monitors_to_log_search() -> None:
    _, not_for = _claim_and_not_for(_skill("service-health").description)
    assert "monitor" in not_for.lower()
    assert "`log-search`" in not_for


def test_service_health_body_does_not_read_a_monitor() -> None:
    body = _skill("service-health").body
    assert "agent-monitors-" not in body
    assert "search_logs(" not in body
    headings = [line for line in body.splitlines() if line.startswith("#")]
    assert not [h for h in headings if "monitor" in h.lower()]


def test_log_search_keeps_the_monitor_indices() -> None:
    skill = _skill("log-search")
    claim, _ = _claim_and_not_for(skill.description)
    assert "monitor indices" in claim
    assert "agent-monitors-slm-health" in skill.body


def test_log_search_carries_the_stale_monitor_rule_that_service_health_gave_up() -> None:
    """The old Success bullet told the model to report a stale monitor document."""
    assert "stale" in _skill("log-search").body.lower()


# ── Item 2: the trigger words of log-search come first ───────────────────────────


@pytest.mark.parametrize("word", ["log events", "counts", "indices", "es|ql"])
def test_log_search_trigger_words_are_in_its_first_sentence(word: str) -> None:
    first = _skill("log-search").description.split(". ")[0].lower()
    assert word in first


# ── Item 3 (AC-3): the A4 lines are shorter ──────────────────────────────────────


def test_the_a4_descriptions_are_shorter_than_the_fre_1597_build() -> None:
    total = sum(len(_skill(name).description) for name in _A4_SKILLS)
    assert total <= _A4_BASELINE_CHARS - _A4_REQUIRED_SAVING_CHARS, total


@pytest.mark.parametrize("name", _A4_SKILLS)
def test_each_a4_description_keeps_its_not_for_clause(name: str) -> None:
    """Shortening must not remove the neighbour that the line gives work to."""
    _, not_for = _claim_and_not_for(_skill(name).description)
    assert "`" in not_for, name
