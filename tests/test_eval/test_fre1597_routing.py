"""FRE-1597 (ADR-0156 A5, T-D1): Part 1 with the second step, and the AC-1 score.

No test calls a model, a gateway or a store.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from scripts.eval.fre1537.common import read_jsonl
from scripts.eval.fre1581.g2_skill_choice import Item
from scripts.eval.fre1594 import transport
from scripts.eval.fre1597 import calls, routing
from tests.test_eval.test_fre1597_calls import _first, _second


def _reply(*tool_calls: tuple[str, str], ok: bool = True, build: str = "b1") -> transport.Reply:
    return transport.Reply(
        ok=ok,
        status=200 if ok else 500,
        error=None if ok else "boom",
        content="",
        reasoning=None,
        tool_calls=tuple(
            {"id": f"m{i}", "name": n, "arguments": a} for i, (n, a) in enumerate(tool_calls)
        ),
        structured=True,
        finish_reason="tool_calls" if tool_calls else "stop",
        timings={"cache_n": 0, "prompt_n": 10, "predicted_n": 5},
        usage={},
        secs=1.0,
        system_fingerprint=build,
        cost_usd=0.0,
        request_sha256="x",
    )


def _load(skill: str) -> tuple[str, str]:
    return ("read_skill", f'{{"name": "{skill}"}}')


class _Scripted:
    def __init__(self, *replies: transport.Reply) -> None:
        self.replies = list(replies)
        self.sent: list[dict[str, object]] = []

    def send(self, body: dict[str, object]) -> transport.Reply:
        self.sent.append(copy.deepcopy(body))
        return self.replies.pop(0)


def _captured(skill: str | None) -> routing.Captured:
    first = _first()
    if skill is None:
        item = Item("ns01", "no_skill", "hello", None)
        return routing.Captured(item, calls.TwoCalls.from_capture([first], None))
    item = Item(f"{skill}#0", "scenario", "p", skill)
    return routing.Captured(item, calls.TwoCalls.from_capture([first, _second(first)], skill))


# --------------------------------------------------------------------------- items


def test_the_items_are_the_area_scenarios_and_the_no_skill_prompts() -> None:
    items = routing.load_area_items()
    scenarios = [i for i in items if i.kind == "scenario"]
    assert {i.expected_skill for i in scenarios} == set(routing.AREA_SKILLS)
    assert sum(i.kind == "no_skill" for i in items) == 20
    per_skill = {s: sum(i.expected_skill == s for i in scenarios) for s in routing.AREA_SKILLS}
    assert all(n >= 3 for n in per_skill.values())


# --------------------------------------------------------------------------- replay


def test_call_2_follows_exactly_one_read_skill_of_the_expected_skill(tmp_path: Path) -> None:
    captured = _captured("log-search")
    first_reply = _reply(_load("log-search"))
    sender = _Scripted(first_reply, _reply(("search_logs", "{}")))
    rows = tmp_path / "rows.jsonl"

    errors = routing.run_replay(sender, [captured], rows, draws=1)

    assert errors == 0
    assert sender.sent[0] == captured.calls.first
    assert sender.sent[1] == captured.calls.second_request(first_reply)
    (row,) = read_jsonl(rows)
    assert row["read_skill_names"] == ["log-search"]
    second = row["second"]
    assert isinstance(second, dict)
    assert second["revealed"] == ["search_logs"]
    assert second["tool_names"] == ["search_logs"]
    assert second["called_revealed"] is True


@pytest.mark.parametrize(
    "first_calls",
    [
        (("bash", '{"command": "curl x"}'),),
        (_load("log-search"), ("bash", '{"command": "ls"}')),
        (_load("model-latency"),),
        (),
    ],
)
def test_no_call_2_after_any_other_first_reply(
    tmp_path: Path, first_calls: tuple[tuple[str, str], ...]
) -> None:
    sender = _Scripted(_reply(*first_calls))
    routing.run_replay(sender, [_captured("log-search")], tmp_path / "rows.jsonl", draws=1)
    (row,) = read_jsonl(tmp_path / "rows.jsonl")
    assert len(sender.sent) == 1
    assert "skipped" in row["second"]  # type: ignore[operator]


def test_a_no_skill_item_gets_one_call(tmp_path: Path) -> None:
    sender = _Scripted(_reply(_load("log-search")))
    routing.run_replay(sender, [_captured(None)], tmp_path / "rows.jsonl", draws=1)
    assert len(sender.sent) == 1


def test_a_failed_call_1_writes_an_error_row_and_a_rerun_retries_it(tmp_path: Path) -> None:
    rows = tmp_path / "rows.jsonl"
    assert routing.run_replay(_Scripted(_reply(ok=False)), [_captured(None)], rows, draws=1) == 1
    sender = _Scripted(_reply())
    assert routing.run_replay(sender, [_captured(None)], rows, draws=1) == 0
    assert len(sender.sent) == 1
    assert routing.run_replay(_Scripted(), [_captured(None)], rows, draws=1) == 0


# --------------------------------------------------------------------------- score


def _row(item: Item, draw: int, *first: tuple[str, str], build: str = "b1") -> dict[str, object]:
    reply = _reply(*first, build=build)
    return routing.first_row(item, draw, reply) | {"second": {"skipped": "test"}}


def _skill_items(skill: str, n: int) -> list[Item]:
    return [Item(f"{skill}#{i}", "scenario", "p", skill) for i in range(n)]


def _no_skill_items(n: int = 1) -> list[Item]:
    return [Item(f"ns{i:02d}", "no_skill", "hello", None) for i in range(n)]


def _all_right(items: list[Item], draws: int = 3) -> list[dict[str, object]]:
    return [
        _row(i, d, _load(str(i.expected_skill))) if i.kind == "scenario" else _row(i, d)
        for d in range(draws)
        for i in items
    ]


@pytest.mark.parametrize(("scenarios", "need"), [(3, 8), (4, 10), (5, 13)])
def test_the_bound_is_the_ten_of_twelve_share_rounded_up(scenarios: int, need: int) -> None:
    items = _skill_items("log-search", scenarios)
    rows = _all_right(items)  # draw-major: row k is item k % n, draw k // n
    for k in range(scenarios * 3 - need + 1):  # one miss more than the bound allows
        rows[k] = _row(items[k % scenarios], k // scenarios)

    skill = routing.score(items, rows, draws=3)["skills"]["log-search"]  # type: ignore[index]
    assert (skill["need"], skill["first"], skill["pass"]) == (need, need - 1, False)

    rows[0] = _row(items[0], 0, _load("log-search"))
    skill = routing.score(items, rows, draws=3)["skills"]["log-search"]  # type: ignore[index]
    assert (skill["first"], skill["pass"]) == (need, True)


def test_a_load_after_another_call_is_right_but_not_first() -> None:
    items = _skill_items("log-search", 4)
    rows = _all_right(items)
    rows[0] = _row(items[0], 0, ("bash", "{}"), _load("log-search"))
    skill = routing.score(items, rows, draws=3)["skills"]["log-search"]  # type: ignore[index]
    assert skill["first"] == 11 and skill["right"] == 12


def test_an_area_confusion_fails_and_an_outside_load_is_only_reported() -> None:
    items = _skill_items("log-search", 4) + _skill_items("model-latency", 4)
    rows = _all_right(items)
    rows[0] = _row(items[0], 0, _load("system-metrics"))
    report = routing.score(items, rows, draws=3)
    assert report["area_confusion"] == []
    assert report["skills"]["log-search"]["outside_loads"] == {"system-metrics": 1}  # type: ignore[index]
    rows[1] = _row(items[1], 0, _load("model-latency"))
    report = routing.score(items, rows, draws=3)
    assert report["area_confusion"] == [
        {"id": "log-search#1", "draw": 0, "loaded": ["model-latency"]}
    ]
    assert report["verdict"] == "fail"


def test_a_no_skill_load_fails_even_a_malformed_one() -> None:
    items = _skill_items("log-search", 4) + _no_skill_items(2)
    rows = _all_right(items)
    assert routing.score(items, rows, draws=3)["no_skill_loads"] == 0
    rows[-1] = _row(items[-1], 2, ("read_skill", "{not json"))
    report = routing.score(items, rows, draws=3)
    assert report["no_skill_loads"] == 1 and report["no_skill_draws"] == 6
    assert report["verdict"] == "fail"


def test_a_missing_draw_makes_the_verdict_incomplete() -> None:
    items = _skill_items("log-search", 4)
    rows = _all_right(items)[:-1]
    assert routing.score(items, rows, draws=3)["verdict"] == "incomplete"


def test_two_llama_builds_fail_the_run() -> None:
    items = _skill_items("log-search", 4)
    rows = _all_right(items)
    rows[0] = _row(items[0], 0, _load("log-search"), build="b2")
    report = routing.score(items, rows, draws=3)
    assert report["llama_builds"] == ["b1", "b2"] and report["verdict"] == "fail"


def test_a_clean_run_passes() -> None:
    items = _skill_items("log-search", 4) + _no_skill_items(2)
    assert routing.score(items, _all_right(items), draws=3)["verdict"] == "pass"


def test_a_failed_call_2_writes_an_error_row_and_a_rerun_redoes_the_draw(tmp_path: Path) -> None:
    rows = tmp_path / "rows.jsonl"
    load = _reply(_load("log-search"))
    sender = _Scripted(load, _reply(ok=False))
    assert routing.run_replay(sender, [_captured("log-search")], rows, draws=1) == 1
    (row,) = read_jsonl(rows)
    assert "call 2" in str(row["error"])
    sender = _Scripted(load, _reply(("search_logs", "{}")))
    assert routing.run_replay(sender, [_captured("log-search")], rows, draws=1) == 0
    assert len(sender.sent) == 2
