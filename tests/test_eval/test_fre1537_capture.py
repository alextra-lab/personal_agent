"""FRE-1511 / ADR-0154 D7: the recording stub and the capture driver."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from scripts.eval.fre1537 import capture, stub
from scripts.eval.fre1537.common import RunPaths
from scripts.eval.fre1537.fixtures import Fixture

TOOLS = [{"type": "function", "function": {"name": "web_search"}}]


def test_stub_records_every_body_and_answers_a_tool_call_with_the_scripted_reply(
    tmp_path: Path,
) -> None:
    (tmp_path / "next_reply.txt").write_text("SCRIPTED")
    client = TestClient(stub.create_app(tmp_path))
    primary = client.post(
        "/v1/chat/completions", json={"messages": [], "tools": TOOLS, "stream": False}
    )
    assert primary.json()["choices"][0]["message"]["content"] == "SCRIPTED"
    other = client.post("/v1/chat/completions", json={"messages": [], "stream": False})
    assert other.json()["choices"][0]["message"]["content"] == "OK"
    json_mode = client.post(
        "/v1/chat/completions", json={"messages": [], "response_format": {"type": "json_object"}}
    )
    assert json_mode.json()["choices"][0]["message"]["content"] == "{}"
    files = sorted((tmp_path / "calls").glob("*.json"))
    assert [f.name for f in files] == ["0001.json", "0002.json", "0003.json"]
    assert json.loads(files[0].read_text())["body"]["tools"] == TOOLS


def test_stub_streams_sse_with_usage_and_done(tmp_path: Path) -> None:
    client = TestClient(stub.create_app(tmp_path))
    response = client.post("/v1/chat/completions", json={"messages": [], "stream": True})
    lines = [x for x in response.text.splitlines() if x.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    assert json.loads(lines[0][6:])["choices"][0]["delta"]["content"] == "OK"
    assert json.loads(lines[-2][6:])["usage"]["total_tokens"] == 2


def test_stub_numbering_continues_after_a_restart(tmp_path: Path) -> None:
    TestClient(stub.create_app(tmp_path)).post("/v1/chat/completions", json={"messages": []})
    TestClient(stub.create_app(tmp_path)).post("/v1/chat/completions", json={"messages": []})
    assert sorted(p.name for p in (tmp_path / "calls").glob("*.json")) == ["0001.json", "0002.json"]


def write_call(directory: Path, n: int, body: dict[str, object]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{n:04d}.json"
    path.write_text(json.dumps({"seq": n, "body": body}))
    return path


def test_primary_body_needs_exactly_one_hit(tmp_path: Path) -> None:
    msg = "Please run the tool"
    primary = write_call(
        tmp_path, 1, {"tools": TOOLS, "messages": [{"role": "user", "content": f"x {msg}"}]}
    )
    helper = write_call(tmp_path, 2, {"messages": [{"role": "user", "content": msg}]})  # no tools
    other_turn = write_call(
        tmp_path, 3, {"tools": TOOLS, "messages": [{"role": "user", "content": "else"}]}
    )
    assert capture.primary_body([primary, helper, other_turn], msg)["tools"] == TOOLS
    with pytest.raises(ValueError, match="found 0"):
        capture.primary_body([helper, other_turn], msg)
    with pytest.raises(ValueError, match="found 2"):
        capture.primary_body(
            [
                primary,
                write_call(
                    tmp_path, 4, {"tools": TOOLS, "messages": [{"role": "user", "content": msg}]}
                ),
            ],
            msg,
        )


def test_capture_runs_turn_one_with_the_scripted_reply_then_turn_two_in_the_same_session(
    tmp_path: Path,
) -> None:
    paths = RunPaths(tmp_path)
    calls = paths.stub_out / "calls"
    seen: list[dict[str, str]] = []
    counter = iter(range(1, 100))

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        reply = (paths.stub_out / "next_reply.txt").read_text()
        write_call(
            calls,
            next(counter),
            {
                "tools": TOOLS,
                "messages": [{"role": "user", "content": params["message"]}],
                "reply": reply,
            },
        )
        return httpx.Response(200, json={"session_id": "sid-1"})

    fx = Fixture(
        label="boiler_decline",
        message="Which is cheapest?",
        expected="decline",
        history="boiler",
        history_user="Options?",
        history_assistant="Three options.",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        record = capture.capture_fixture(client, "http://gw.test/chat", paths, fx, settle=0)
    assert [p["message"] for p in seen] == ["Options?", "Which is cheapest?"]
    assert "session_id" not in seen[0] and seen[1]["session_id"] == "sid-1"
    assert all(p["channel"] == "EVAL" and p["profile"] == "local" for p in seen)
    assert record["body"]["reply"] == "OK"  # type: ignore[index]  # turn 2 asks the stub for a plain reply
    assert record["expected"] == "decline" and record["history"] == "boiler"


def test_capture_of_a_single_turn_fixture_makes_one_request(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["message"])
        write_call(
            paths.stub_out / "calls",
            len(seen),
            {
                "tools": TOOLS,
                "messages": [{"role": "user", "content": request.url.params["message"]}],
            },
        )
        return httpx.Response(200, json={"session_id": "s"})

    fx = Fixture(label="greeting", message="How is your day going?", expected="decline")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        capture.capture_fixture(client, "http://gw.test/chat", paths, fx, settle=0)
    assert seen == ["How is your day going?"]


def test_capture_sends_the_model_on_both_turns_only_when_set(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path)
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        write_call(
            paths.stub_out / "calls",
            len(seen),
            {"tools": TOOLS, "messages": [{"role": "user", "content": params["message"]}]},
        )
        return httpx.Response(200, json={"session_id": "sid-1"})

    fx = Fixture(
        label="boiler_decline",
        message="Which is cheapest?",
        expected="decline",
        history="boiler",
        history_user="Options?",
        history_assistant="Three options.",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        capture.capture_fixture(client, "http://gw.test/chat", paths, fx, settle=0, model="m-key")
        capture.capture_fixture(client, "http://gw.test/chat", paths, fx, settle=0)
    assert [p.get("model") for p in seen] == ["m-key", "m-key", None, None]
