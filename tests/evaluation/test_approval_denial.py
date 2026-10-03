"""FRE-1539 — the shared approval-denial check for eval harness turns.

A production harness turn has no PWA WebSocket, so FRE-1535 denies every approval-capable tool
call. These tests cover the check's verdict: ``invalid`` on a denial, ``valid`` on a proven clean
turn, ``unverified`` when the evidence cannot prove a clean turn (FRE-1051: an absent event is not
a pass). Nothing here reaches a gateway or Elasticsearch.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import httpx
from scripts.eval.approval_denial import (
    CAPTURES_INDEX,
    ApprovalVerdict,
    approval_capable_tools,
    assess,
    check_turn,
    merge,
)

DENIED_CONNECTION_LOST = "Permission denied: approval_connection_lost"
DENIED_NO_TRANSPORT = "Permission denied: approval_no_transport"
CAPABLE = frozenset({"bash", "write"})


def _capture(*results: Mapping[str, object]) -> dict[str, object]:
    return {"trace_id": "t-1", "tool_results": list(results)}


def _denied(tool: str, error: str) -> dict[str, object]:
    return {"tool_name": tool, "success": False, "error": error}


def _ran(tool: str) -> dict[str, object]:
    return {"tool_name": tool, "success": True, "error": None}


# --- AC-1: the check sees a denial --------------------------------------------------------


def test_capture_denial_connection_lost_is_reported_with_tool_and_reason() -> None:
    verdict = assess([_capture(_denied("bash", DENIED_CONNECTION_LOST))], [])
    assert verdict.validity == "invalid"
    assert [(d.tool_name, d.reason, d.source) for d in verdict.denials] == [
        ("bash", "approval_connection_lost", "capture")
    ]


def test_capture_denial_no_transport_is_reported_with_tool_and_reason() -> None:
    verdict = assess([_capture(_denied("write", DENIED_NO_TRANSPORT))], [])
    assert verdict.validity == "invalid"
    assert [(d.tool_name, d.reason) for d in verdict.denials] == [
        ("write", "approval_no_transport")
    ]


def test_log_event_alone_marks_the_turn_invalid_even_without_a_capture() -> None:
    event = {"event_type": "approval_denied_no_session_id", "tool_name": "bash"}
    verdict = assess([], [event])
    assert verdict.validity == "invalid"
    assert [(d.tool_name, d.reason, d.source) for d in verdict.denials] == [
        ("bash", "approval_no_session_id", "log")
    ]


def test_approval_denied_event_takes_its_reason_from_the_decision() -> None:
    event = {"event_type": "approval_denied", "tool_name": "bash", "decision": "connection_lost"}
    verdict = assess([_capture()], [event])
    assert [d.reason for d in verdict.denials] == ["approval_connection_lost"]


def test_the_same_denial_in_capture_and_log_is_reported_once() -> None:
    capture = _capture(_denied("bash", DENIED_CONNECTION_LOST))
    event = {"event_type": "approval_denied", "tool_name": "bash", "decision": "connection_lost"}
    verdict = assess([capture], [event])
    assert len(verdict.denials) == 1
    assert verdict.denials[0].source == "capture"


def test_sub_agent_denial_event_is_reported_with_a_sub_agent_reason() -> None:
    event = {
        "event_type": "sub_agent_tool_approval_denied",
        "tool_name": "bash",
        "reason": "no_approver_in_scope",
    }
    verdict = assess([_capture()], [event])
    assert verdict.validity == "invalid"
    assert [(d.tool_name, d.reason) for d in verdict.denials] == [
        ("bash", "sub_agent_no_approver_in_scope")
    ]


# --- AC-2: a clean turn stays valid -------------------------------------------------------


def test_turn_with_no_approval_tool_is_valid() -> None:
    verdict = assess([_capture(_ran("web_search"))], [])
    assert verdict == ApprovalVerdict(validity="valid", capture_found=True)


def test_turn_with_no_tool_results_is_valid() -> None:
    assert assess([_capture()], []).validity == "valid"


def test_approval_tool_that_ran_under_the_eval_opt_out_is_valid() -> None:
    event = {"event_type": "approval_ui_disabled_proceeding", "tool_name": "bash"}
    verdict = assess([_capture(_ran("bash"))], [event])
    assert verdict.validity == "valid"
    assert verdict.denials == ()


def test_a_permission_error_that_is_not_an_approval_denial_is_valid() -> None:
    other = _denied("bash", "Permission denied: Tool not allowed in LOCKDOWN mode")
    assert assess([_capture(other)], []).validity == "valid"


# --- the evidence cannot prove a clean turn -----------------------------------------------


def test_no_capture_and_no_event_is_unverified_not_valid() -> None:
    verdict = assess([], [])
    assert verdict.validity == "unverified"
    assert verdict.capture_found is False
    assert verdict.unverified_reason == "capture_not_found"


def test_sub_agent_that_used_an_approval_capable_tool_is_unverified() -> None:
    sub = {"task_id": "a", "rounds": [{"round": 1, "tool": "bash", "result_chars": 90}]}
    verdict = assess([_capture()], [], [sub], CAPABLE)
    assert verdict.validity == "unverified"
    assert verdict.unverified_reason == "sub_agent_approval_capable_tool:bash"


def test_sub_agent_that_used_an_approval_capable_tool_is_invalid_when_the_denial_is_logged() -> (
    None
):
    sub = {"task_id": "a", "rounds": [{"round": 1, "tool": "bash", "result_chars": 90}]}
    event = {
        "event_type": "sub_agent_tool_approval_denied",
        "tool_name": "bash",
        "reason": "no_approver_in_scope",
    }
    assert assess([_capture()], [event], [sub], CAPABLE).validity == "invalid"


def test_sub_agent_that_used_only_other_tools_is_valid() -> None:
    sub = {"task_id": "a", "rounds": [{"round": 1, "tool": "web_search", "result_chars": 90}]}
    start = {"event_type": "sub_agent_start"}
    assert assess([_capture()], [start], [sub], CAPABLE).validity == "valid"


def test_a_missing_sub_agent_capture_is_unverified() -> None:
    sub = {"task_id": "a", "rounds": []}
    events = [{"event_type": "sub_agent_start"}, {"event_type": "sub_agent_start"}]
    verdict = assess([_capture()], events, [sub], CAPABLE)
    assert verdict.validity == "unverified"
    assert verdict.unverified_reason == "sub_agent_capture_missing"


def test_merge_takes_the_worst_verdict() -> None:
    valid = ApprovalVerdict(validity="valid", capture_found=True)
    unverified = ApprovalVerdict(validity="unverified", unverified_reason="capture_not_found")
    invalid = assess([_capture(_denied("bash", DENIED_CONNECTION_LOST))], [])
    assert merge([valid, valid]).validity == "valid"
    assert merge([valid, unverified]).validity == "unverified"
    merged = merge([valid, unverified, invalid])
    assert merged.validity == "invalid"
    assert [d.tool_name for d in merged.denials] == ["bash"]


def test_approval_capable_tools_come_from_the_governance_config() -> None:
    capable = approval_capable_tools()
    assert {"bash", "write", "run_python"} <= capable
    assert "web_search" not in capable


# --- the Elasticsearch read ---------------------------------------------------------------


def _hits(*sources: Mapping[str, object]) -> dict[str, object]:
    return {"hits": {"hits": [{"_source": s} for s in sources]}}


def _es(
    capture: list[Mapping[str, object]],
    events: list[Mapping[str, object]] | None = None,
    subs: list[Mapping[str, object]] | None = None,
    seen: list[httpx.Request] | None = None,
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if "subagents" in path and not path.split("/")[1].startswith("agent-captains-captures-*"):
            return httpx.Response(200, json=_hits(*(subs or [])))
        if "captains-captures" in path:
            return httpx.Response(200, json=_hits(*capture))
        return httpx.Response(200, json=_hits(*(events or [])))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_check_turn_reports_a_denied_turn_from_the_capture() -> None:
    client = _es([_capture(_denied("bash", DENIED_CONNECTION_LOST))])
    verdict = check_turn("t-1", es_url="http://es", client=client, wait_s=0)
    assert verdict.validity == "invalid"
    assert [d.reason for d in verdict.denials] == ["approval_connection_lost"]


def test_check_turn_reports_a_clean_turn_valid() -> None:
    verdict = check_turn("t-1", es_url="http://es", client=_es([_capture(_ran("bash"))]), wait_s=0)
    assert verdict.validity == "valid"


def test_check_turn_capture_query_excludes_the_sub_agent_index() -> None:
    seen: list[httpx.Request] = []
    check_turn("t-1", es_url="http://es", client=_es([_capture()], seen=seen), wait_s=0)
    capture_paths = [
        r.url.path for r in seen if r.url.path.startswith("/agent-captains-captures-*")
    ]
    assert capture_paths, "no capture query was sent"
    assert "-agent-captains-captures-subagents-*" in capture_paths[0]
    assert CAPTURES_INDEX in capture_paths[0]


def test_check_turn_polls_until_the_capture_lands() -> None:
    calls = {"capture": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/agent-captains-captures-*"):
            calls["capture"] += 1
            return httpx.Response(200, json=_hits(*([_capture()] if calls["capture"] >= 2 else [])))
        return httpx.Response(200, json=_hits())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    verdict = check_turn("t-1", es_url="http://es", client=client, wait_s=5, poll_s=0)
    assert verdict.validity == "valid"
    assert calls["capture"] == 2


def test_check_turn_without_a_capture_is_unverified() -> None:
    verdict = check_turn("t-1", es_url="http://es", client=_es([]), wait_s=0)
    assert verdict.validity == "unverified"
    assert verdict.unverified_reason == "capture_not_found"


def test_check_turn_elasticsearch_failure_is_unverified_and_does_not_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    verdict = check_turn("t-1", es_url="http://es", client=client, wait_s=0)
    assert verdict.validity == "unverified"
    assert (verdict.unverified_reason or "").startswith("es_error")


def test_verdict_serialises_for_a_jsonl_row() -> None:
    verdict = assess([_capture(_denied("bash", DENIED_CONNECTION_LOST))], [])
    row = json.loads(json.dumps(verdict.as_dict()))
    assert row["validity"] == "invalid"
    assert row["denials"] == [
        {"tool_name": "bash", "reason": "approval_connection_lost", "source": "capture"}
    ]
