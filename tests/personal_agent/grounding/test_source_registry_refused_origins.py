"""What this turn refused, and why (ADR-0139 D8, ADR-0140 AC-3, FRE-1359).

``tool_results_offered`` and ``tool_results_admitted`` say *how many* results a turn could
not cite. They do not say **which tool** wanted citing, and an ``uncitable`` turn admitted
nothing, so its registry holds no source that could name one. Ordering the typed-wrapper
roadmap needs that name — and it needs the refusal's *reason* beside it, because two of
the four refusal shapes are reachable only by a tool that already passed the policy table
and can therefore never be wrapper demand.

These are the registry-side halves. The event that carries them lives in
``tests/personal_agent/orchestrator/test_executor_grounding.py``.
"""

from __future__ import annotations

import json

from personal_agent.grounding.source_registry import SourceRegistry

TURN = "trace-refused-origins-0001"

UNCLASSIFIED_TOOL_NAME = "some_tool_added_next_quarter"


def test_a_refused_bash_call_records_its_origin_and_its_reason() -> None:
    """The shape ADR-0139 D8 exists for: arbitrary code, refused, and now named."""
    registry = SourceRegistry(turn_id=TURN)

    registration = registry.register_tool_result(
        tool_name="bash",
        arguments={"command": "echo 'Paris has 9 million residents'"},
        content="Paris has 9 million residents",
    )

    assert registration.source is None
    assert registry.refused_tool_origins == ("bash",)
    assert registry.refused_origin_admissibility == ("bash:model_authored_invocation",)


def test_ac1_two_refused_bash_calls_and_one_unclassified_call() -> None:
    """AC-1's own seeded shape: distinct origins, in refusal order, both reasons named."""
    registry = SourceRegistry(turn_id=TURN)

    registry.register_tool_result(
        tool_name="bash",
        arguments={"command": "ls /etc"},
        content="hosts",
    )
    registry.register_tool_result(
        tool_name="bash",
        arguments={"command": "cat /etc/hosts"},
        content="127.0.0.1 localhost",
    )
    registry.register_tool_result(
        tool_name=UNCLASSIFIED_TOOL_NAME,
        arguments={"q": "anything"},
        content="Paris has 9 million residents",
    )

    assert registry.refused_tool_origins == ("bash", UNCLASSIFIED_TOOL_NAME)
    assert registry.refused_origin_admissibility == (
        "bash:model_authored_invocation",
        f"{UNCLASSIFIED_TOOL_NAME}:unclassified_tool",
    )


def test_an_admitted_call_records_no_refusal() -> None:
    registry = SourceRegistry(turn_id=TURN)

    registration = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents.",
    )

    assert registration.source is not None
    assert registry.refused_tool_origins == ()
    assert registry.refused_origin_admissibility == ()


def test_a_no_content_refusal_carries_its_own_reason() -> None:
    """A typed tool that returned nothing is refused — and is never wrapper demand.

    ``no_content`` is reachable only after the tool passed the typed-retrieval table, so
    its origin already has a typed tool. The measurement excludes it from qualification,
    which it can only do because the reason travels with the origin.
    """
    registry = SourceRegistry(turn_id=TURN)

    registration = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/empty"},
        content="",
    )

    assert registration.source is None
    assert registry.refused_tool_origins == ("fetch_url",)
    assert registry.refused_origin_admissibility == ("fetch_url:no_content",)


def test_a_derived_from_turn_write_refusal_carries_its_own_reason() -> None:
    """The laundering pair: the write is unclassified, the read of it is derived.

    Both halves are recorded, with different reasons, because they call for different
    remedies — and neither is a missing wrapper.
    """
    registry = SourceRegistry(turn_id=TURN)

    registry.register_tool_result(
        tool_name="write",
        arguments={"path": "/tmp/laundered.txt", "content": "Paris has 9 million residents"},
        content=json.dumps({"status": "written"}),
    )
    registration = registry.register_tool_result(
        tool_name="read",
        arguments={"path": "/tmp/laundered.txt"},
        content="Paris has 9 million residents",
    )

    assert registration.source is None
    assert registry.refused_tool_origins == ("write", "read")
    assert registry.refused_origin_admissibility == (
        "write:unclassified_tool",
        "read:derived_from_turn_write",
    )


def test_one_origin_can_be_both_admitted_and_refused() -> None:
    """The refused list is **not** the complement of the admitted count.

    ``tool_results_offered`` counts calls and ``tool_results_admitted`` counts unique
    sources, so the difference between them already includes a deduplicated admission.
    This turn proves the two views are independent: one tool, one admission, one refusal.
    """
    registry = SourceRegistry(turn_id=TURN)

    admitted = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris"},
        content="Paris counts 2,100,000 residents.",
    )
    refused = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/empty"},
        content="",
    )

    assert admitted.source is not None
    assert refused.source is None
    assert registry.tool_results_admitted == 1
    assert registry.refused_tool_origins == ("fetch_url",)


def test_a_repeated_refusal_does_not_repeat_the_entry() -> None:
    """Distinct by construction, so a D4 retry's resubmitted call cannot inflate it."""
    registry = SourceRegistry(turn_id=TURN)

    for _ in range(3):
        registry.register_tool_result(
            tool_name="bash",
            arguments={"command": "echo 'Paris has 9 million residents'"},
            content="Paris has 9 million residents",
        )

    assert registry.tool_results_offered == 3
    assert registry.refused_tool_origins == ("bash",)
    assert registry.refused_origin_admissibility == ("bash:model_authored_invocation",)
