"""FRE-1511 / ADR-0154 D7: fixtures and the design A planner request (AC-4)."""

from __future__ import annotations

import hashlib

import pytest
from scripts.eval.fre1537 import render
from scripts.eval.fre1537.fixtures import load_fixtures

from personal_agent.orchestrator.expansion_controller import _render_planner_history

SURFACE = ["web_search", "read_file"]
HISTORY = [
    {"role": "user", "content": "Replace my boiler?"},
    {"role": "assistant", "content": "Three options."},
]
QUERY = "Which is cheapest to install?"
DIGEST = "Memory digest:\n- lives in Brittany"


def test_fixture_counts_and_expected_decisions() -> None:
    fixtures = load_fixtures()
    assert len(fixtures) == 27
    single = [f for f in fixtures if f.kind == "single"]
    follow = [f for f in fixtures if f.kind == "followup"]
    assert len(single) == 19 and len(follow) == 8
    scored = [f for f in fixtures if f.expected != "excluded"]
    assert sum(f.expected == "decline" for f in scored) == 10
    assert sum(f.expected == "expand" for f in scored) == 16
    assert [f.label for f in fixtures if f.expected == "excluded"] == ["c5_noverb"]
    assert sum(f.expected == "decline" for f in follow) == 4
    assert sum(f.expected == "expand" for f in follow) == 4
    assert len({f.label for f in fixtures}) == 27


def test_followup_fixture_carries_its_scripted_history() -> None:
    fx = next(f for f in load_fixtures() if f.label == "boiler_decline")
    assert [m["role"] for m in fx.history_messages] == ["user", "assistant"]
    assert "gas boiler" in fx.history_messages[0]["content"]


def test_system_prompt_admits_single_and_leads_with_the_decline_rule() -> None:
    system = render.render_system_prompt(SURFACE)
    assert '"strategy": "SINGLE|HYBRID|DECOMPOSE"' in system
    assert system.index("Rules:\n- First decide whether") > 0
    assert 'output {"strategy": "SINGLE", "tasks": []} and nothing else' in system


def test_system_prompt_hash_is_stable() -> None:
    a = render.render_system_prompt(SURFACE)
    b = render.render_system_prompt(SURFACE)
    assert render.prompt_hash(a) == render.prompt_hash(b) == hashlib.sha256(a.encode()).hexdigest()


def test_digest_follows_the_query_as_a_tool_result() -> None:
    """AC-4: history, then query, then the digest; the system prompt does not change.

    FRE-1360: the probe renders the production request, where the digest is a
    ``memory_recall`` tool result after the user message, never user text.
    """
    system = render.render_system_prompt(SURFACE)
    without = render.build_planner_request(
        system, HISTORY, QUERY, digest=None, history_max_chars=60000
    )
    with_digest = render.build_planner_request(
        system, HISTORY, QUERY, digest=DIGEST, history_max_chars=60000
    )

    user = with_digest["messages"][1]["content"]
    history_at = user.index("assistant: Three options.")
    query_at = user.index(f"Strategy: HYBRID\nQuery: {QUERY}")
    assert history_at < query_at
    assert DIGEST not in user
    assert [m["role"] for m in with_digest["messages"]] == ["system", "user", "assistant", "tool"]
    assert DIGEST in with_digest["messages"][3]["content"]

    assert with_digest["messages"][0]["role"] == "system"
    assert (
        with_digest["messages"][0]["content"].encode() == without["messages"][0]["content"].encode()
    )
    assert without["messages"][1]["content"] == (
        f"Conversation so far:\n{_render_planner_history(HISTORY, 60000)}\n\n"
        f"Strategy: HYBRID\nQuery: {QUERY}\n\nProduce the JSON plan."
    )


def test_digest_without_history_still_rides_a_tool_result() -> None:
    system = render.render_system_prompt(SURFACE)
    req = render.build_planner_request(system, [], QUERY, digest=DIGEST, history_max_chars=60000)
    user = req["messages"][1]["content"]
    assert "Conversation so far" not in user
    assert DIGEST not in user
    assert DIGEST in req["messages"][-1]["content"]


def test_history_is_trimmed_from_the_oldest_end_by_the_production_function() -> None:
    system = render.render_system_prompt(SURFACE)
    long_history = [{"role": "user", "content": f"turn {i} " + "x" * 50} for i in range(20)]
    req = render.build_planner_request(
        system, long_history, QUERY, digest=None, history_max_chars=300
    )
    user = req["messages"][1]["content"]
    assert "turn 19" in user and "turn 0 " not in user
    assert req["history_chars"] <= 300


def test_missing_rule_anchor_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(render, "_production_system_prompt", lambda surface: "no schema here")
    with pytest.raises(RuntimeError, match="anchor"):
        render.render_system_prompt(SURFACE)


def test_build_user_message_refuses_a_digest() -> None:
    """FRE-1360: a digest in the user text is the retired shape; the probe must not send it."""
    assert "Query: q" in render.build_user_message("", None, "q")
    with pytest.raises(ValueError, match="tool result"):
        render.build_user_message("", "DIGEST", "q")
