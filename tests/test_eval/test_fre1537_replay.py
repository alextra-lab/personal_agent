"""FRE-1511 / ADR-0154 D7: the replay arms, the long-history arm and the fingerprint."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from scripts.eval.fre1537 import fingerprint, llama, longhist, render, replay
from scripts.eval.fre1537.common import RunPaths, read_jsonl

URL = "http://llama.test/v1/chat/completions"
SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0, "cache_prompt": True}


def sse(
    content: str = "",
    reasoning: str = "",
    completion_tokens: int = 13,
    cache_n: int = 574,
) -> bytes:
    chunks = []
    if reasoning:
        chunks.append({"choices": [{"delta": {"reasoning_content": reasoning}}]})
    if content:
        chunks.append({"choices": [{"delta": {"content": content}}]})
    chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    chunks.append(
        {
            "choices": [],
            "usage": {"prompt_tokens": 800, "completion_tokens": completion_tokens},
            "timings": {"cache_n": cache_n, "prompt_n": 218, "predicted_n": completion_tokens},
        }
    )
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


DECLINE = '{"strategy": "SINGLE", "tasks": []}'
EXPAND = '{"strategy": "HYBRID", "tasks": [{"name": "a", "goal": "g"}]}'


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def sse_response(**kwargs: object) -> httpx.Response:
    return httpx.Response(200, content=sse(**kwargs), headers={"content-type": "text/event-stream"})  # type: ignore[arg-type]


def make_inputs(tmp_path: Path) -> tuple[RunPaths, llama.Inputs]:
    paths = RunPaths(tmp_path, "thinking_off")
    system = "SYSTEM PROMPT"
    prompts = {
        "system": system,
        "prompt_hash": render.prompt_hash(system),
        "fixtures": {
            "greeting": {
                "user": "Q-greeting",
                "history": "",
                "query": "How is your day going?",
                "history_chars": 0,
            },
            "boiler_expand": {
                "user": "H\n\nQ",
                "history": "user: hi\nassistant: hello",
                "query": "research it",
                "history_chars": 24,
            },
        },
    }
    body = {
        "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "u"}],
        "tools": [{"function": {"name": "t1"}}, {"function": {"name": "t2"}}],
        **SAMPLING,
    }
    captured = {
        "greeting": {
            "label": "greeting",
            "expected": "decline",
            "history": None,
            "message": "m",
            "body": body,
        },
        "boiler_expand": {
            "label": "boiler_expand",
            "expected": "expand",
            "history": "boiler",
            "message": "m",
            "body": body,
        },
    }
    return paths, llama.Inputs(prompts=prompts, captured=captured)


def test_parse_plan_variants() -> None:
    assert llama.parse_plan(DECLINE)["declined"] is True
    assert llama.parse_plan("<think>x</think>" + EXPAND)["task_count"] == 1
    assert llama.parse_plan("```json\n" + DECLINE + "\n```")["declined"] is True
    assert llama.parse_plan("no json")["parse_error"] is True
    assert llama.parse_plan("[1, 2]")["parse_error"] is True
    single_with_tasks = llama.parse_plan('{"strategy": "SINGLE", "tasks": [{"goal": "g"}]}')
    assert single_with_tasks["declined"] is False and single_with_tasks["task_count"] == 1


def test_stream_records_reasoning_content_usage_and_timings() -> None:
    client = client_for(
        lambda req: sse_response(content=DECLINE, reasoning="thinking...", completion_tokens=9)
    )
    res = llama.stream(client, URL, "m", {"messages": []})
    assert res["content"] == DECLINE
    assert res["reasoning_chars"] == len("thinking...")
    assert res["usage"]["completion_tokens"] == 9  # type: ignore[index]
    assert res["timings"]["cache_n"] == 574  # type: ignore[index]
    assert res["ttft_any"] is not None and res["ttft_content"] is not None


def test_stream_request_asks_for_usage_and_streaming() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return sse_response(content=DECLINE)

    llama.stream(client_for(handler), URL, "the-model", {"messages": []})
    assert seen["model"] == "the-model" and seen["stream"] is True
    assert seen["stream_options"] == {"include_usage": True}


def test_planner_body_thinking_off_and_sampling(tmp_path: Path) -> None:
    _, inputs = make_inputs(tmp_path)
    off = llama.planner_body(inputs, "greeting", llama.MODES["thinking_off"])
    on = llama.planner_body(inputs, "greeting", llama.MODES["server_default"])
    assert off["chat_template_kwargs"] == {"enable_thinking": False}
    assert "chat_template_kwargs" not in on
    for key, value in SAMPLING.items():
        assert off[key] == value
    assert off["response_format"] == {"type": "json_object"}
    assert off["messages"][0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert off["messages"][1]["content"] == "Q-greeting"


def test_planner_body_digest_goes_between_history_and_query(tmp_path: Path) -> None:
    _, inputs = make_inputs(tmp_path)
    body = llama.planner_body(inputs, "boiler_expand", llama.MODES["thinking_off"], digest="DIGEST")
    user = body["messages"][1]["content"]
    assert user.index("assistant: hello") < user.index("DIGEST") < user.index("Query: research it")
    assert body["messages"][0]["content"] == "SYSTEM PROMPT"


def test_primary_and_prime_bodies(tmp_path: Path) -> None:
    _, inputs = make_inputs(tmp_path)
    primary = llama.primary_body(inputs, "greeting", max_tokens=1)
    assert primary["max_tokens"] == 1 and len(primary["tools"]) == 2
    prime = llama.prime_body(inputs, "greeting")
    assert prime["messages"][-1] == {
        "role": "user",
        "content": ".",
    }  # single-turn: system-only prefix
    assert prime["max_tokens"] == 1
    assert prime["chat_template_kwargs"] == {"enable_thinking": False}


def test_run_decide_writes_rows_and_resumes(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return sse_response(content=DECLINE)

    with client_for(handler) as client:
        replay.run_decide(
            client,
            URL,
            "m",
            paths,
            inputs,
            llama.MODES["thinking_off"],
            None,
            ["greeting", "boiler_expand"],
            trials=2,
        )
        first = len(calls)
        replay.run_decide(
            client,
            URL,
            "m",
            paths,
            inputs,
            llama.MODES["thinking_off"],
            None,
            ["greeting", "boiler_expand"],
            trials=2,
        )
    rows = read_jsonl(paths.decide)
    assert first == 4 and len(calls) == 4 and len(rows) == 4
    row = next(r for r in rows if r["label"] == "boiler_expand")
    assert row["expected"] == "expand" and row["kind"] == "followup" and row["declined"] is True
    assert next(r for r in rows if r["label"] == "greeting")["kind"] == "single"
    assert row["plan"]["strategy"] == "SINGLE"  # type: ignore[index]


def test_run_decide_records_an_error_row_and_retries_it(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    state = {"fail": True}

    def handler(req: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(500)
        return sse_response(content=DECLINE)

    with client_for(handler) as client:
        args = (client, URL, "m", paths, inputs, llama.MODES["thinking_off"], None, ["greeting"], 1)
        replay.run_decide(*args)
        assert "error" in read_jsonl(paths.decide)[0]
        state["fail"] = False
        replay.run_decide(*args)
    rows = read_jsonl(paths.decide)
    assert len(rows) == 2 and "error" not in rows[1]


def test_run_timing_row_shape(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    with client_for(lambda req: sse_response(content=DECLINE)) as client:
        replay.run_timing(
            client, URL, "m", paths, inputs, llama.MODES["thinking_off"], ["greeting"]
        )
    (row,) = read_jsonl(paths.timing)
    assert {"T1_primary", "T3_planner", "T3_primary"} <= set(row)
    assert row["T3_planner"]["plan"]["declined"] is True  # type: ignore[index]
    assert "content" not in row["T3_planner"]  # type: ignore[operator]


def test_long_history_arm_builds_each_size_and_extends_the_history(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    bodies: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return sse_response(content=DECLINE)

    with client_for(handler) as client:
        longhist.run_longhist(
            client, URL, "m", paths, inputs, llama.MODES["thinking_off"], sizes=(2000, 4000)
        )
    rows = read_jsonl(paths.longhist)
    assert [r["size_chars"] for r in rows] == [2000, 4000]
    assert all({"cold", "primary_between", "extended"} <= set(r) for r in rows)
    cold, _primary, extended = bodies[0], bodies[1], bodies[2]
    cold_user = cold["messages"][1]["content"]  # type: ignore[index]
    ext_user = extended["messages"][1]["content"]  # type: ignore[index]
    assert len(cold_user) >= 2000
    history = cold_user.split("Conversation so far:\n", 1)[1].split("\n\nStrategy:", 1)[0]
    assert history in ext_user  # the extended call's history extends the cold call's
    assert "Air-source is the cheapest to install." in ext_user
    assert cold["messages"][0]["content"] == "SYSTEM PROMPT"


def test_long_history_arm_resumes(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    with client_for(lambda req: sse_response(content=DECLINE)) as client:
        longhist.run_longhist(
            client, URL, "m", paths, inputs, llama.MODES["thinking_off"], sizes=(2000,)
        )
        longhist.run_longhist(
            client, URL, "m", paths, inputs, llama.MODES["thinking_off"], sizes=(2000, 3000)
        )
    assert [r["size_chars"] for r in read_jsonl(paths.longhist)] == [2000, 3000]


def props_handler(
    build: str = "b6789-abcdef", model_path: str = "/models/qwen3.8-flash-next-UD-IQ4_XS.gguf"
):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/props"
        return httpx.Response(200, json={"build_info": build, "model_path": model_path})

    return handler


def test_fingerprint_records_engine_model_mode_and_prompt_hash(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    with client_for(props_handler()) as client:
        fp = fingerprint.build_fingerprint(
            client,
            URL,
            "unsloth/qwen3.8-flash-next",
            llama.MODES["thinking_off"],
            inputs,
            paths,
            None,
            None,
            None,
        )
    assert fp["engine"]["build"] == "b6789-abcdef"  # type: ignore[index]
    assert fp["model"]["quant"] == "UD-IQ4_XS"  # type: ignore[index]
    assert fp["planner_mode"]["params"] == {"chat_template_kwargs": {"enable_thinking": False}}  # type: ignore[index]
    assert fp["system_prompt_sha256"] == inputs.prompts["prompt_hash"]
    assert fp["captured_primary"]["tool_count"] == 2  # type: ignore[index]


def test_fingerprint_overrides_fill_what_the_engine_does_not_report(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    with client_for(lambda req: httpx.Response(404)) as client:
        fp = fingerprint.build_fingerprint(
            client, URL, "m", llama.MODES["thinking_off"], inputs, paths, "Q4_K_M", "b1234", None
        )
    assert fp["engine"]["build"] == "b1234" and fp["model"]["quant"] == "Q4_K_M"  # type: ignore[index]


def test_fingerprint_is_unknown_when_nothing_reports_it(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    with client_for(lambda req: httpx.Response(404)) as client:
        fp = fingerprint.build_fingerprint(
            client, URL, "m", llama.MODES["thinking_off"], inputs, paths, None, None, None
        )
    assert fp["engine"]["build"] == "unknown" and fp["model"]["quant"] == "unknown"  # type: ignore[index]


def test_a_tag_refuses_rows_from_a_different_configuration(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    with client_for(props_handler("b1")) as client:
        first = fingerprint.build_fingerprint(
            client, URL, "m", llama.MODES["thinking_off"], inputs, paths, None, None, None
        )
    with client_for(props_handler("b2")) as client:
        second = fingerprint.build_fingerprint(
            client, URL, "m", llama.MODES["thinking_off"], inputs, paths, None, None, None
        )
    fingerprint.ensure_compatible(paths, first)
    fingerprint.ensure_compatible(paths, first)  # same configuration: fine
    with pytest.raises(SystemExit, match="different configuration"):
        fingerprint.ensure_compatible(paths, second)


def test_reasoning_written_inline_in_the_content_counts_as_reasoning() -> None:
    client = client_for(lambda req: sse_response(content="<think>abcde</think>" + DECLINE))
    res = llama.stream(client, URL, "m", {"messages": []})
    assert res["reasoning_chars"] == 5


def test_timing_arm_sends_the_digest_between_history_and_query(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    users: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body.get("response_format"):
            users.append(body["messages"][1]["content"])
        return sse_response(content=DECLINE)

    with client_for(handler) as client:
        replay.run_timing(
            client,
            URL,
            "m",
            paths,
            inputs,
            llama.MODES["thinking_off"],
            ["boiler_expand"],
            digest="DIGEST",
        )
    (user,) = users
    assert user.index("assistant: hello") < user.index("DIGEST") < user.index("Query: research it")
