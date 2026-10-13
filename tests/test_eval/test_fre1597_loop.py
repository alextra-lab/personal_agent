"""FRE-1597 (ADR-0156 A5, T-D1): the full tool loop (AC-2) and the cache cost (AC-4) on the final build.

No test calls a model, a gateway or a store.
"""

from __future__ import annotations

import copy

from scripts.eval.fre1594 import reveal, session, shim, transport
from scripts.eval.fre1597 import calls, loop
from tests.test_eval.test_fre1597_calls import _SKILL_CONTENT, _first, _second


def _reply(*tool_calls: tuple[str, str], content: str = "") -> transport.Reply:
    return transport.Reply(
        ok=True,
        status=200,
        error=None,
        content=content,
        reasoning=None,
        tool_calls=tuple(
            {"id": f"m{i}", "name": n, "arguments": a} for i, (n, a) in enumerate(tool_calls)
        ),
        structured=True,
        finish_reason="tool_calls" if tool_calls else "stop",
        timings={"cache_n": 0, "prompt_n": 10, "predicted_n": 5},
        usage={},
        secs=1.0,
        system_fingerprint="b1",
        cost_usd=0.0,
        request_sha256="x",
    )


class _Scripted:
    def __init__(self, *replies: transport.Reply) -> None:
        self.replies = list(replies)
        self.sent: list[dict[str, object]] = []

    def send(self, body: dict[str, object]) -> transport.Reply:
        self.sent.append(copy.deepcopy(body))
        return self.replies.pop(0) if self.replies else _reply(content="control")


def _tools(name: str, arguments: str, revealed: tuple[str, ...]) -> shim.ToolRun:
    return shim.ToolRun(content='{"ok": true, "count": 3}', valid=True, ran=True, code=None)


def test_a_draw_on_the_final_build_uses_the_captured_reveal() -> None:
    first = _first()
    second = _second(first)
    two = calls.TwoCalls.from_capture([first, second], "log-search")
    prompt = reveal.load_scenarios().prompts[0]
    assert prompt.skill == "log-search"

    draw = loop.make_draw(prompt, two, number=0)
    scripted = _Scripted(
        _reply(("read_skill", '{"name": "log-search"}')),
        _reply(("search_logs", '{"action": "count", "index": "agent-logs", "since": "1h"}')),
        _reply(content="3 errors."),
    )
    row = session.run_draw(scripted, draw, _tools, nonces=("tn", "cn"))

    assert row["passed"] is True
    treatment, control = scripted.sent[:3], scripted.sent[3:]
    for body in treatment[1:]:
        assert body["tools"] == second["tools"]
    for body in (treatment[0], *control):
        assert body["tools"] == first["tools"]
    for body in (treatment[1], control[1]):
        skill = next(m for m in body["messages"] if m.get("name") == "read_skill")  # type: ignore[union-attr]
        assert skill["content"] == _SKILL_CONTENT


def _row(
    prompt_id: str, draw: int, treatment: list[object], control: list[object]
) -> dict[str, object]:
    return {
        "prompt_id": prompt_id,
        "draw": draw,
        "passed": True,
        "failed_step": None,
        "treatment": treatment,
        "control": control,
    }


def _call(cache_n: int, prompt_n: int, predicted_n: int = 10) -> dict[str, object]:
    return {
        "ok": True,
        "timings": {"cache_n": cache_n, "prompt_n": prompt_n, "predicted_n": predicted_n},
    }


def _session(costly_k3: bool = False) -> tuple[list[object], list[object]]:
    # The treatment re-processes the whole prompt at k = 2 (the tools change), as FRE-1594 F4.
    treatment = [
        _call(0, 10000),
        _call(0, 10500),
        _call(10500, 300) if not costly_k3 else _call(0, 10800),
    ]
    control = [_call(0, 10000), _call(9996, 400), _call(10392, 300)]
    return treatment, control


def _rows(**kwargs: bool) -> list[dict[str, object]]:
    return [_row(f"p{p}", d, *_session(**kwargs)) for p in range(4) for d in range(3)]


def test_a_full_clean_run_passes_ac2_and_ac4() -> None:
    report = loop.verdicts(_rows())
    assert report["ac2"]["verdict"] == "pass"  # type: ignore[index]
    assert report["ac4"]["verdict"] == "pass"  # type: ignore[index]


def test_two_costly_calls_in_a_session_fail_ac4() -> None:
    assert loop.verdicts(_rows(costly_k3=True))["ac4"]["verdict"] == "fail"  # type: ignore[index]


def test_a_lineage_void_pair_fails_ac4_even_when_the_rest_passes() -> None:
    rows = _rows()
    rows[0]["control"][2] = _call(99999, 10)  # type: ignore[index]  # reuses more than it had
    report = loop.verdicts(rows)["ac4"]
    assert report["verdict"] == "fail"  # type: ignore[index]
    assert "lineage" in report["reasons"]  # type: ignore[index, operator]


def test_fewer_than_ten_held_draws_fail_ac2_and_a_short_run_is_incomplete() -> None:
    rows = _rows()
    for row in rows[:3]:
        row["passed"], row["failed_step"] = False, 2
    assert loop.verdicts(rows)["ac2"]["verdict"] == "fail"  # type: ignore[index]
    assert loop.verdicts(_rows()[:11])["ac2"]["verdict"] == "incomplete"  # type: ignore[index]


def test_the_preflight_projection_adds_the_parts() -> None:
    from scripts.eval.fre1597 import preflight

    minutes = preflight.project(
        first_calls=147, second_reveal=66, second_plain=21, loop_draws=12, size_tokens=34200
    )
    assert minutes["routing_call_1"] == 14.7
    assert minutes["loop"] == 28.0
    parts = sum(v for k, v in minutes.items() if k != "total")
    assert abs(minutes["total"] - parts) <= 0.2  # the total sums the unrounded parts
