"""FRE-1541 AC-1 / ADR-0154 D1: the planner's user message is bounded, and the query is never cut."""

from __future__ import annotations

import re

import pytest

from personal_agent.exceptions import PlannerInputTooLargeError
from personal_agent.orchestrator.expansion_controller import build_planner_user_message

_TOTAL = 64_000
_HISTORY = 60_000
_TAIL_OVERHEAD = len("Strategy: HYBRID\nQuery: \n\nProduce the JSON plan.")


def _turns(count: int, size: int) -> list[dict[str, str]]:
    """Alternating user/assistant messages, each ``size`` chars, labelled by index."""
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"{i:04d}" + "x" * (size - 4)}
        for i in range(count)
    ]


def _build(
    query: str,
    history: list[dict[str, str]],
    *,
    digest: str = "",
    total: int = _TOTAL,
    history_max: int = _HISTORY,
):
    return build_planner_user_message(
        query,
        "HYBRID",
        [*history, {"role": "user", "content": query}],
        digest_text=digest,
        history_max_chars=history_max,
        input_max_chars=total,
    )


def test_oversized_history_is_trimmed_from_the_oldest_end_and_total_holds() -> None:
    history = _turns(200, 1_000)  # 200,000 chars: far over both bounds
    built = _build("What next?", history)

    assert len(built.content) <= _TOTAL
    assert built.history_chars <= _HISTORY
    assert "What next?" in built.content
    # Newest message kept, oldest dropped, no half message.
    assert "0199" in built.content
    assert "0000" not in built.content
    kept = re.findall(r"^(?:user|assistant): (\d{4})(x+)$", built.content, flags=re.M)
    indices = [int(index) for index, _ in kept]
    assert indices == list(range(indices[0], 200)), "kept history must be a contiguous newest run"
    assert all(len(index) + len(filler) == 1_000 for index, filler in kept), "whole messages only"


def test_history_receives_only_what_the_query_and_digest_leave() -> None:
    history = _turns(100, 500)
    query = "q" * 10_000
    digest = "d" * 5_000
    built = _build(query, history, digest=digest, total=20_000)

    assert len(built.content) <= 20_000
    assert query in built.content
    assert digest in built.content
    assert built.history_chars < 20_000 - 10_000 - 5_000
    assert built.history_chars > 0


def test_the_history_cap_binds_when_it_is_below_the_remainder() -> None:
    built = _build("hi", _turns(100, 500), history_max=1_200)
    assert built.history_chars <= 1_200
    assert len(built.content) <= _TOTAL


def test_oversized_message_fails_instead_of_being_cut() -> None:
    query = "q" * (_TOTAL + 1)
    with pytest.raises(PlannerInputTooLargeError) as caught:
        _build(query, _turns(4, 100))
    assert caught.value.message_chars >= len(query)
    assert caught.value.max_chars == _TOTAL


def test_message_plus_digest_over_the_bound_fails() -> None:
    query = "q" * 40_000
    digest = "d" * 25_000
    with pytest.raises(PlannerInputTooLargeError) as caught:
        _build(query, _turns(4, 100), digest=digest)
    assert caught.value.digest_chars == len(digest)


def test_the_bound_is_inclusive_and_one_more_char_fails() -> None:
    query_fits = "q" * (_TOTAL - _TAIL_OVERHEAD)
    built = _build(query_fits, [])
    assert len(built.content) == _TOTAL
    assert query_fits in built.content

    with pytest.raises(PlannerInputTooLargeError):
        _build(query_fits + "q", [])


def test_no_room_for_history_omits_the_history_block() -> None:
    query = "q" * (_TOTAL - _TAIL_OVERHEAD - 5)  # five chars left: less than the history header
    built = _build(query, _turns(10, 100))
    assert built.history_chars == 0
    assert "Conversation so far" not in built.content
    assert len(built.content) <= _TOTAL


def test_order_is_history_then_digest_then_query() -> None:
    built = _build("the question", _turns(2, 50), digest="DIGEST-LINE")
    history_at = built.content.index("Conversation so far:")
    digest_at = built.content.index("DIGEST-LINE")
    query_at = built.content.index("Query: the question")
    assert history_at < digest_at < query_at


def test_three_turn_history_precedes_the_query() -> None:
    history = [
        {"role": "user", "content": "rule: never use sources from before 2020"},
        {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": "find me heat pump prices"},
    ]
    built = _build("now compare two options", history)
    assert built.content.index("rule: never use sources") < built.content.index("Query: ")
    assert built.content.startswith("Conversation so far:\n")


def test_counts_describe_each_input() -> None:
    built = _build("abc", _turns(2, 40), digest="DIG")
    assert built.digest_chars == 3
    assert built.message_chars == _TAIL_OVERHEAD + 3
    assert built.history_chars == len(
        built.content.split("Conversation so far:\n", 1)[1].split("\n\nDIG", 1)[0]
    )


def test_query_that_is_not_the_last_message_is_still_not_cut() -> None:
    built = build_planner_user_message(
        "the question",
        "DECOMPOSE",
        _turns(3, 20),
        digest_text="",
        history_max_chars=_HISTORY,
        input_max_chars=_TOTAL,
    )
    assert "Strategy: DECOMPOSE\nQuery: the question" in built.content


@pytest.mark.parametrize(
    ("history_turns", "digest", "total"),
    [
        (0, "", _TOTAL),  # query only
        (6, "", _TOTAL),  # history only
        (0, "DIGEST", _TOTAL),  # digest only
        (6, "DIGEST", _TOTAL),  # all three
        (50, "DIGEST", 400),  # history trimmed hard by a small bound
    ],
)
def test_total_chars_is_the_length_of_the_whole_message(
    history_turns: int, digest: str, total: int
) -> None:
    built = _build("abc", _turns(history_turns, 100), digest=digest, total=total)
    assert built.total_chars == len(built.content)
    assert built.total_chars <= total


def test_zero_and_negative_remainders_still_account_exactly() -> None:
    # Remainder after the query is exactly the header plus separator: zero history budget.
    tail = _TAIL_OVERHEAD + 3
    exact = tail + len("Conversation so far:\n") + 2
    for total in (tail, exact, exact + 1):
        built = _build("abc", _turns(10, 100), total=total)
        assert built.total_chars == len(built.content) <= total
        assert built.history_chars <= max(0, total - exact)
