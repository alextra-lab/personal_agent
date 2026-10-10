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


def test_planner_body_digest_rides_the_production_tool_result(tmp_path: Path) -> None:
    """FRE-1360: the digest follows the user message as the production memory_recall result."""
    from personal_agent.orchestrator.expansion_controller import planner_digest_exchange

    _, inputs = make_inputs(tmp_path)
    body = llama.planner_body(inputs, "boiler_expand", llama.MODES["thinking_off"], digest="DIGEST")
    messages = body["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool"]
    assert messages[0]["content"] == "SYSTEM PROMPT"
    assert "DIGEST" not in messages[1]["content"]
    assert messages[2:] == planner_digest_exchange("DIGEST", trace_id=render.PROBE_TRACE_ID)
    plain = llama.planner_body(inputs, "boiler_expand", llama.MODES["thinking_off"])
    assert plain["messages"][1] == messages[1]


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


def _with_memory_tail(inputs: llama.Inputs, label: str, *, multi_turn: bool) -> None:
    """Give a fixture the FRE-1360 primary tail: the turn's user message, then the
    harness memory exchange (assistant tool call, memory_recall tool result)."""
    history = (
        [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "answer"}]
        if multi_turn
        else []
    )
    inputs.captured[label]["body"] = {
        **inputs.captured[label]["body"],
        "messages": [
            {"role": "system", "content": "S"},
            *history,
            {"role": "user", "content": "u"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_mem_x",
                        "type": "function",
                        "function": {"name": "memory_recall", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_mem_x", "name": "memory_recall", "content": "m"},
        ],
    }


@pytest.mark.parametrize("multi_turn", [False, True])
def test_prime_cuts_back_to_the_last_user_message(tmp_path: Path, multi_turn: bool) -> None:
    """FRE-1360: the primary request no longer ends on the user turn.

    The prime is the previous turn's prefix, so it ends before this turn's user message —
    never on the harness tool call, which llama.cpp rejects with 400 (the D7 timing arm,
    2026-10-10).
    """
    _, inputs = make_inputs(tmp_path)
    _with_memory_tail(inputs, "greeting", multi_turn=multi_turn)
    prime = llama.prime_body(inputs, "greeting")
    roles = [m["role"] for m in prime["messages"]]
    assert all(not m.get("tool_calls") for m in prime["messages"])
    if multi_turn:
        assert roles == ["system", "user", "assistant"]
    else:
        assert prime["messages"][-1] == {"role": "user", "content": "."}
        assert roles == ["system", "user"]


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


def test_a_digest_run_fingerprint_names_the_tool_result_carrier(tmp_path: Path) -> None:
    """FRE-1360: a digest run from before the move is a different configuration."""
    paths, inputs = make_inputs(tmp_path)
    with client_for(props_handler()) as client:
        with_digest = fingerprint.build_fingerprint(
            client, URL, "m", llama.MODES["thinking_off"], inputs, paths, None, None, "DIGEST"
        )
        without = fingerprint.build_fingerprint(
            client, URL, "m", llama.MODES["thinking_off"], inputs, paths, None, None, None
        )
    assert with_digest["digest_carrier"] == "tool_result"
    assert "digest_carrier" not in without
    old = {k: v for k, v in with_digest.items() if k != "digest_carrier"}
    assert fingerprint._identity(old) != fingerprint._identity(with_digest)


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


def test_timing_arm_sends_the_digest_as_a_tool_result(tmp_path: Path) -> None:
    """FRE-1360: the timing arm's planner request carries the digest the production way."""
    paths, inputs = make_inputs(tmp_path)
    requests: list[list[dict[str, object]]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body.get("response_format"):
            requests.append(body["messages"])
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
    (messages,) = requests
    # The fixture's own rendered user message, unchanged: the digest is not in it.
    assert messages[1]["content"] == inputs.fixture("boiler_expand")["user"]
    assert messages[-1]["role"] == "tool" and "DIGEST" in str(messages[-1]["content"])


def sse_with_fingerprint(build: str) -> httpx.Response:
    chunk = {"system_fingerprint": build, "choices": [{"delta": {"content": DECLINE}}]}
    usage = {"choices": [], "usage": {"completion_tokens": 13}}
    body = f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(usage)}\n\ndata: [DONE]\n\n".encode()
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


def test_stream_records_the_engine_system_fingerprint() -> None:
    res = llama.stream(
        client_for(lambda req: sse_with_fingerprint("b9999-feed")), URL, "m", {"messages": []}
    )
    assert res["system_fingerprint"] == "b9999-feed"
    assert (
        llama.stream(client_for(lambda req: sse_response(content=DECLINE)), URL, "m", {})[
            "system_fingerprint"
        ]
        is None
    )


def test_an_unknown_engine_build_is_filled_from_the_first_reply_and_never_overwritten(
    tmp_path: Path,
) -> None:
    paths, inputs = make_inputs(tmp_path)
    with client_for(lambda req: httpx.Response(404)) as client:
        fp = fingerprint.build_fingerprint(
            client, URL, "m", llama.MODES["thinking_off"], inputs, paths, None, None, None
        )
    fingerprint.ensure_compatible(paths, fp)
    fingerprint.fill_engine_build(paths, None)  # a reply without a fingerprint changes nothing
    assert json.loads(paths.fingerprint.read_text())["engine"]["build"] == "unknown"
    fingerprint.fill_engine_build(paths, "b9999-feed")
    fingerprint.fill_engine_build(paths, "b0000-other")
    stored = json.loads(paths.fingerprint.read_text())["engine"]
    assert stored["build"] == "b9999-feed" and stored["build_source"] == "reply:system_fingerprint"


def test_a_resumed_run_keeps_the_build_that_an_earlier_reply_filled_in(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    with client_for(lambda req: httpx.Response(404)) as client:
        fp = fingerprint.build_fingerprint(
            client, URL, "m", llama.MODES["thinking_off"], inputs, paths, None, None, None
        )
    fingerprint.ensure_compatible(paths, fp)
    fingerprint.fill_engine_build(paths, "b9999-feed")
    fingerprint.ensure_compatible(paths, fp)  # same configuration, props still silent: allowed


def test_the_fingerprint_names_the_source_of_the_build_and_the_quant(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    with client_for(props_handler()) as client:
        from_engine = fingerprint.build_fingerprint(
            client, URL, "m", llama.MODES["thinking_off"], inputs, paths, None, None, None
        )
    with client_for(lambda req: httpx.Response(404)) as client:
        from_cli = fingerprint.build_fingerprint(
            client, URL, "m", llama.MODES["thinking_off"], inputs, paths, "UD-IQ4_XS", "b1", None
        )
    assert (
        from_engine["engine"]["build_source"] == "props"
        and from_engine["model"]["quant_source"] == "props"
    )  # type: ignore[index]
    assert (
        from_cli["engine"]["build_source"] == "cli" and from_cli["model"]["quant_source"] == "cli"
    )  # type: ignore[index]


def test_run_decide_fills_the_engine_build_from_the_first_reply(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    with client_for(lambda req: httpx.Response(404)) as client:
        fp = fingerprint.build_fingerprint(
            client, URL, "m", llama.MODES["thinking_off"], inputs, paths, None, None, None
        )
    fingerprint.ensure_compatible(paths, fp)
    with client_for(lambda req: sse_with_fingerprint("b7777-cafe")) as client:
        replay.run_decide(
            client,
            URL,
            "m",
            paths,
            inputs,
            llama.MODES["thinking_off"],
            None,
            ["greeting"],
            trials=1,
        )
    assert json.loads(paths.fingerprint.read_text())["engine"]["build"] == "b7777-cafe"


# ── FRE-1541: the probe qualifies the `planner` mode that config/models.yaml declares ──────────


def write_catalog(tmp_path: Path, planner: dict[str, object] | None) -> Path:
    """A one-deployment catalog: a default mode, and optionally a ``planner`` mode."""
    default = {
        "enable_thinking": True,
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repeat_penalty": 1.0,
    }
    modes: dict[str, object] = {"default": default}
    if planner is not None:
        modes["planner"] = {**default, **planner}
    path = tmp_path / "models.yaml"
    path.write_text(
        json.dumps(
            {"models": {llama.CATALOG_DEPLOYMENT: {"default_mode": "default", "modes": modes}}}
        )
    )  # JSON is YAML
    return path


def test_the_planner_mode_reads_its_thinking_switch_from_the_catalog(tmp_path: Path) -> None:
    off = llama.catalog_planner_mode(write_catalog(tmp_path, {"enable_thinking": False}))
    assert off.name == "planner"
    assert off.params == {"chat_template_kwargs": {"enable_thinking": False}}
    # A catalog mode that leaves thinking on sends no switch, so the probe's 0-reasoning
    # threshold fails honestly instead of the probe forcing thinking off.
    on = llama.catalog_planner_mode(write_catalog(tmp_path, {"enable_thinking": True}))
    assert on.params == {}


def test_a_catalog_without_a_planner_mode_cannot_be_probed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="declares no `planner` mode"):
        llama.catalog_planner_mode(write_catalog(tmp_path, None))


def test_a_planner_mode_that_changes_the_sampling_cannot_be_qualified(tmp_path: Path) -> None:
    # The probe replays the captured primary's sampling, so it cannot measure another sampling.
    with pytest.raises(ValueError, match="temperature"):
        llama.catalog_planner_mode(
            write_catalog(tmp_path, {"enable_thinking": False, "temperature": 0.7})
        )


def test_resolve_mode_keeps_the_fixed_modes_and_reads_planner_from_the_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llama, "CATALOG", write_catalog(tmp_path, {"enable_thinking": False}))
    assert llama.resolve_mode("thinking_off") is llama.MODES["thinking_off"]
    assert llama.resolve_mode("planner").params == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert "planner" in llama.MODE_NAMES


def test_the_catalog_parameters_reach_the_request_the_fingerprint_and_the_long_history_arm(
    tmp_path: Path,
) -> None:
    """Outcome: a changed catalog value changes what the probe sends and records."""
    paths, inputs = make_inputs(tmp_path)
    mode = llama.catalog_planner_mode(write_catalog(tmp_path, {"enable_thinking": False}))
    assert llama.planner_body(inputs, "greeting", mode)["chat_template_kwargs"] == {
        "enable_thinking": False
    }
    with client_for(props_handler()) as client:
        fp = fingerprint.build_fingerprint(client, URL, "m", mode, inputs, paths, None, None, None)
    assert fp["planner_mode"]["name"] == "planner"  # type: ignore[index]
    assert fp["planner_mode"]["params"] == mode.params  # type: ignore[index]

    bodies: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return sse_response(content=DECLINE)

    with client_for(handler) as client:
        longhist.run_longhist(client, URL, "m", paths, inputs, mode, sizes=(2000,))
    # The cold and the extended planner calls carry the catalog's switch; the primary call does not.
    assert bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert bodies[2]["chat_template_kwargs"] == {"enable_thinking": False}


def test_the_long_history_step_runs_the_mode_that_prepare_resolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: ``longhist.main`` re-resolved the mode from a fixed table, so a catalog mode
    would have been replaced by a different one.
    """
    sentinel = llama.PlannerMode("planner", {"chat_template_kwargs": {"enable_thinking": False}})
    paths, inputs = make_inputs(tmp_path)
    seen: list[llama.PlannerMode] = []
    monkeypatch.setattr(longhist, "prepare", lambda args, client: (paths, inputs, sentinel, None))
    monkeypatch.setattr(
        longhist, "run_longhist", lambda client, url, model, p, i, mode: seen.append(mode)
    )
    longhist.main(["--run-dir", str(tmp_path), "--mode", "planner"])
    assert seen == [sentinel]
