"""The pure parts of the AC-4 probe (FRE-1564): the request it builds and how it scores.

The model call itself is not tested here. It is a gated live step.
"""

from __future__ import annotations

import json

from scripts.eval.fre1564 import telemetry_routing_probe as probe

_SURFACE = (
    "run_python",
    "web_search",
    "search_memory",
    "recall_personal_history",
    "fetch_url",
    "get_library_docs",
    "read_skill",
    "notes_search",
    "query_telemetry",
)


def _row(types: list[str], *, parsed: bool = True, strategy: str = "HYBRID") -> dict[str, object]:
    return {
        "label": "q",
        "parsed": parsed,
        "strategy": strategy,
        "declined": strategy == "SINGLE" and not types,
        "types": types,
    }


def test_three_telemetry_questions_times_the_trials() -> None:
    records = probe.build_requests(trials=2, surface=_SURFACE)
    assert len(records) == 6
    assert {r["label"] for r in records} == {"logs_last_hour", "latency_24h", "backend_health"}


def test_the_request_is_the_planner_mode_with_the_new_general_description() -> None:
    body = probe.build_requests(trials=1, surface=_SURFACE)[0]["body"]
    system = body["messages"][0]["content"]
    assert "the system's own logs, metrics, errors and health" in system
    assert "query_telemetry" in system
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 1.0


def test_every_call_of_a_question_sends_the_same_body() -> None:
    records = probe.build_requests(trials=3, surface=_SURFACE)
    bodies = {json.dumps(r["body"], sort_keys=True) for r in records if r["label"] == "latency_24h"}
    assert len(bodies) == 1


def test_classify_reads_a_plan_a_decline_and_garbage() -> None:
    plan = json.dumps({"strategy": "HYBRID", "tasks": [{"type": "general"}, {"type": "general"}]})
    assert probe.classify(plan)["types"] == ["general", "general"]
    decline = probe.classify(json.dumps({"strategy": "SINGLE", "tasks": []}))
    assert decline["declined"] is True
    assert probe.classify("not json")["parsed"] is False
    assert probe.classify("[1, 2]")["parsed"] is False


def test_general_and_declined_plans_pass() -> None:
    result = probe.verdict([_row(["general"]), _row([], strategy="SINGLE"), _row(["general"] * 2)])
    assert result["passed"] is True
    assert (result["general"], result["declined"], result["researcher"]) == (2, 1, 0)


def test_seeded_negative_one_researcher_task_fails_the_run() -> None:
    """*Fails if* the verdict cannot see a telemetry task routed to `researcher`."""
    result = probe.verdict([_row(["general"]), _row(["general", "researcher"])])
    assert result["passed"] is False
    assert result["researcher"] == 1


def test_a_reply_that_does_not_parse_fails_the_run() -> None:
    assert probe.verdict([_row(["general"]), _row([], parsed=False)])["passed"] is False


def test_an_empty_run_does_not_pass() -> None:
    assert probe.verdict([])["passed"] is False
