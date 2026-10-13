"""FRE-1603 (ADR-0156 A5): each monitor index has one owner skill, and the catalog lines are short.

The FRE-1597 routing run failed AC-1 because the catalog lines of ``log-search`` and
``service-health`` both claimed the monitor indices (``log-search#2`` went to ``service-health`` on
3 of 3 draws). It failed AC-3 by 0.7 points because the A4 lines cost about 300 tokens, three
times the ADR's estimate.

The owner is chosen by objective (ticket text of 2026-10-13):

- ``service-health`` owns ``agent-monitors-slm-health``, the health monitor of the local model
  server (scenario ``service-health#4``).
- ``log-search`` owns every other monitor family (scenarios ``log-search#2`` and ``#3``).

These tests pin the text that the routing re-run then measures. They do not replace that run.
"""

from __future__ import annotations

import re

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

_HEALTH_FAMILY = "agent-monitors-slm-health"
_OTHER_FAMILIES = (
    "agent-monitors-cache-erosion",
    "agent-monitors-cache-reset-cadence",
    "agent-monitors-projector-health",
    "agent-monitors-delivery-ratio",
    "agent-monitors-joinability",
)
_FAMILY = re.compile(r"agent-monitors-[a-z-]+[a-z]")

# FRE-1597 measured the seven descriptions at 3,738 characters on the final build, and the catalog
# block at 4.5 characters per token (1,340 characters = 298 tokens). AC-3 needs 10 tokens less
# (about 45 characters). The budget below asks for 80 characters (about 18 tokens), so that the
# character estimate has a margin of more than two. The llama.cpp re-run gives the real count.
_A4_BASELINE_CHARS = 3_738
_A4_REQUIRED_SAVING_CHARS = 80


def _skill(name: str) -> SkillDoc:
    return get_all_skills()[name]


def _claim_and_not_for(description: str) -> tuple[str, str]:
    """Split a description at its "Not for…" line."""
    claim, _, not_for = description.partition("Not for")
    return claim, not_for


# ── AC-1: service-health owns the model-server monitor and nothing else ─────────


def test_service_health_description_claims_only_the_model_server_monitor() -> None:
    claim, _ = _claim_and_not_for(_skill("service-health").description)
    assert "monitor" in claim.lower()
    assert "local model server" in claim
    assert "pipeline" not in claim.lower()
    for family in _OTHER_FAMILIES:
        assert family not in claim


def test_service_health_not_for_line_gives_the_other_monitors_to_log_search() -> None:
    _, not_for = _claim_and_not_for(_skill("service-health").description)
    assert "monitor" in not_for.lower()
    assert "`log-search`" in not_for


def test_service_health_body_names_no_monitor_family_but_its_own() -> None:
    body = _skill("service-health").body
    assert set(_FAMILY.findall(body)) == {_HEALTH_FAMILY}
    assert f'search_logs(action="search", index="{_HEALTH_FAMILY}"' in body
    assert "probed_at" in body


def test_service_health_keeps_the_stale_rule_for_its_monitor() -> None:
    lines = [line.lower() for line in _skill("service-health").body.splitlines()]
    assert [line for line in lines if "stale" in line]


# ── AC-1: log-search owns every other monitor family ────────────────────────────


def test_log_search_claims_the_monitor_indices_and_gives_the_model_server_to_service_health() -> (
    None
):
    claim, not_for = _claim_and_not_for(_skill("log-search").description)
    assert "monitor indices" in claim
    assert "`service-health`" in not_for
    assert "model server" in not_for


@pytest.mark.parametrize("family", _OTHER_FAMILIES)
def test_each_other_monitor_family_is_in_log_search_and_not_in_service_health(family: str) -> None:
    assert family in _skill("log-search").body
    service_health = _skill("service-health")
    assert family not in service_health.body
    assert family not in service_health.description


def test_log_search_rules_and_examples_leave_the_model_server_monitor_to_service_health() -> None:
    lines = _skill("log-search").body.splitlines()
    assert not [line for line in lines if _HEALTH_FAMILY in line and "search_logs(" in line]
    assert not [line for line in lines if _HEALTH_FAMILY in line and "interval" in line.lower()]


def test_log_search_carries_the_stale_rule_for_the_monitors_that_it_owns() -> None:
    """Two of its monitors write on events (settings hold no interval), so the rule names them."""
    lines = [line.lower() for line in _skill("log-search").body.splitlines()]
    assert [line for line in lines if "stale" in line and "interval" in line]
    assert [line for line in lines if "cache-reset-cadence" in line and "no interval" in line]


# ── Item 2: the trigger words of log-search come first ───────────────────────────


@pytest.mark.parametrize("word", ["log events", "counts", "indices", "es|ql"])
def test_log_search_trigger_words_are_in_its_first_sentence(word: str) -> None:
    first = _skill("log-search").description.split(". ")[0].lower()
    assert word in first


# ── Item 3 (AC-3): the A4 lines are shorter ──────────────────────────────────────


def test_the_a4_descriptions_are_shorter_than_the_fre_1597_build() -> None:
    total = sum(len(_skill(name).description) for name in _A4_SKILLS)
    assert total <= _A4_BASELINE_CHARS - _A4_REQUIRED_SAVING_CHARS, total
