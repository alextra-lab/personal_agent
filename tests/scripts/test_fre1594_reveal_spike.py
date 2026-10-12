"""The reveal spike of FRE-1594 (ADR-0156 A5, T-B): its requests, its shim, its transport and its scoring.

No test calls a model or a store. The model calls and the tool runs are gated live steps.
"""

from __future__ import annotations

import copy
import json

import httpx
import pytest
from scripts.eval.fre1594 import reveal, score, session, shim, spike, transport

from personal_agent.telemetry.trace import SystemTraceContext
from personal_agent.tools.telemetry_query import TelemetryQueryError

_SYSTEM = (
    "You are the agent.\n\n## Skills\n\nPick one.\n\n"
    "- mermaid-diagrams: Draws diagrams.\n"
    "- query-elasticsearch: Reads agent logs with query_telemetry.\n"
    "- query-tempo: Reads Tempo traces.\n"
    "- system-metrics: Takes a snapshot.\n"
)
_USER = "<turn_context>\nCurrent date: 2026-10-12\n</turn_context>\n\nCheck the logs."


def _tool(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


def _captured() -> dict[str, object]:
    return {
        "model": "unsloth/qwen3.8-flash-next",
        "max_tokens": 12288,
        "temperature": 1.0,
        "stream": True,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _USER},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_mem_1",
                        "type": "function",
                        "function": {"name": "memory_recall", "arguments": "{}"},
                        "index": 0,
                    }
                ],
            },
            {
                "tool_call_id": "call_mem_1",
                "role": "tool",
                "name": "memory_recall",
                "content": "Memory: none.",
            },
        ],
        "tools": [_tool("web_search"), _tool("query_telemetry"), _tool("read_skill")],
    }


@pytest.fixture
def scenarios() -> reveal.Scenarios:
    return reveal.load_scenarios()


# --------------------------------------------------------------------------- scenarios and tools


def test_scenarios_hold_four_prompts_two_per_skill(scenarios: reveal.Scenarios) -> None:
    assert [p.skill for p in scenarios.prompts].count("log-search") == 2
    assert [p.skill for p in scenarios.prompts].count("model-latency") == 2
    assert {s.tool for s in scenarios.skills.values()} == {"search_logs", "measure_model_latency"}


def test_the_two_tools_split_the_arguments_of_query_telemetry() -> None:
    tools = reveal.experimental_tools()
    search = tools["search_logs"]["function"]["parameters"]  # type: ignore[index]
    latency = tools["measure_model_latency"]["function"]["parameters"]  # type: ignore[index]
    assert set(search["properties"]) == set(reveal.SEARCH_ARGS)
    assert set(latency["properties"]) == set(reveal.LATENCY_ARGS)
    assert search["properties"]["action"]["enum"] == ["search", "count"]
    assert "action" not in latency["properties"]
    assert latency["required"] == ["since"]
    # Each description names the other tool in its "Not for" line, and no backend tool name.
    assert "measure_model_latency" in tools["search_logs"]["function"]["description"]  # type: ignore[index]
    assert "search_logs" in tools["measure_model_latency"]["function"]["description"]  # type: ignore[index]


# --------------------------------------------------------------------------- the base request


def test_base_body_changes_only_the_tool_the_catalog_and_the_prompt(
    scenarios: reveal.Scenarios,
) -> None:
    captured = _captured()
    prompt = scenarios.prompts[1]
    body = reveal.base_body(captured, scenarios, prompt)

    assert [reveal.tool_name(t) for t in body["tools"]] == ["web_search", "read_skill"]  # type: ignore[union-attr]
    system = body["messages"][0]["content"]  # type: ignore[index]
    assert "- query-elasticsearch:" not in system
    assert "- query-tempo:" not in system
    assert f"- log-search: {scenarios.skills['log-search'].catalog}\n" in system
    assert f"- model-latency: {scenarios.skills['model-latency'].catalog}\n" in system
    assert "- mermaid-diagrams: Draws diagrams.\n" in system
    assert body["messages"][1]["content"] == (  # type: ignore[index]
        "<turn_context>\nCurrent date: 2026-10-12\n</turn_context>\n\n" + prompt.text
    )
    # The memory exchange and every other key are the captured ones; the capture is not mutated.
    assert body["messages"][2:] == captured["messages"][2:]  # type: ignore[index]
    assert {k: v for k, v in body.items() if k not in ("messages", "tools")} == {
        k: v for k, v in captured.items() if k not in ("messages", "tools")
    }
    assert captured == _captured()


def test_base_body_refuses_a_capture_without_the_tool_or_a_catalog_line(
    scenarios: reveal.Scenarios,
) -> None:
    no_tool = _captured()
    no_tool["tools"] = [_tool("web_search")]
    with pytest.raises(ValueError, match="query_telemetry"):
        reveal.base_body(no_tool, scenarios, scenarios.prompts[0])
    no_line = copy.deepcopy(_captured())
    no_line["messages"][0]["content"] = "## Skills\n- query-tempo: x\n"  # type: ignore[index]
    with pytest.raises(ValueError, match="query-elasticsearch"):
        reveal.base_body(no_line, scenarios, scenarios.prompts[0])


def test_nonce_starts_the_system_message() -> None:
    body = reveal.with_nonce(_captured(), "abc")
    assert body["messages"][0]["content"].startswith("probe nonce abc\nYou are")  # type: ignore[index]


# --------------------------------------------------------------------------- the reveal


@pytest.mark.parametrize("form", reveal.FORMS)
def test_skill_result_differs_between_arms_only_by_the_cache_safe_schema(
    scenarios: reveal.Scenarios, form: reveal.Form
) -> None:
    tools = reveal.experimental_tools()
    skill = scenarios.skills["log-search"]
    treated = json.loads(reveal.skill_result(scenarios, skill, form, reveal=True, tools=tools))
    control = json.loads(reveal.skill_result(scenarios, skill, form, reveal=False, tools=tools))
    assert treated["ok"] is True
    assert "{reveal_note}" not in treated["body"]
    assert "`search_logs`" in treated["body"]
    if form == "cache_safe":
        assert treated.pop("provided_tools") == [tools["search_logs"]]
    assert treated == control


@pytest.mark.parametrize("form", reveal.FORMS)
def test_tools_change_only_in_the_simple_treatment(form: reveal.Form) -> None:
    tools = reveal.experimental_tools()
    base = [_tool("web_search")]
    treated = reveal.revealed_tools(base, form, reveal=True, revealed=["search_logs"], tools=tools)
    control = reveal.revealed_tools(base, form, reveal=False, revealed=["search_logs"], tools=tools)
    assert control == base
    assert treated == ([*base, tools["search_logs"]] if form == "simple" else base)


def test_assistant_message_has_the_production_wire_form() -> None:
    message = reveal.assistant_message(
        content="",
        reasoning="I will load the skill.",
        tool_calls=[{"id": "call_0", "name": "read_skill", "arguments": '{"name": "log-search"}'}],
        turn_id=0,
    )
    assert message == {
        "role": "assistant",
        "content": "",
        "reasoning_content": "I will load the skill.",
        "tool_calls": [
            {
                "id": "call_t0_0_call_0",
                "type": "function",
                "function": {"name": "read_skill", "arguments": '{"name": "log-search"}'},
                "index": 0,
            }
        ],
    }
    plain = reveal.assistant_message(content="Done.", reasoning=None, tool_calls=[], turn_id=2)
    assert plain == {"role": "assistant", "content": "Done."}


# --------------------------------------------------------------------------- the shim


_PROVIDERS = {"search_logs": ("log-search",), "measure_model_latency": ("model-latency",)}


class _Executor:
    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.result = {"count": 3} if result is None else result
        self.error = error

    async def __call__(self, **kwargs: object) -> object:
        kwargs.pop("ctx")
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


async def _run(
    name: str, arguments: str, revealed: tuple[str, ...], executor: _Executor
) -> shim.ToolRun:
    return await shim.run_tool(
        name,
        arguments,
        revealed=revealed,
        providers=_PROVIDERS,
        ctx=SystemTraceContext.new("test"),
        executor=executor,
    )


@pytest.mark.asyncio
async def test_a_tool_that_is_not_revealed_is_refused_and_not_run() -> None:
    executor = _Executor()
    run = await _run("search_logs", '{"action": "count", "index": "agent-logs"}', (), executor)
    assert (run.valid, run.ran, run.code) == (False, False, "not_revealed")
    assert executor.calls == []
    error = json.loads(run.content)["error"]
    assert "log-search" in error["message"]


@pytest.mark.asyncio
async def test_the_revealed_tools_map_to_query_telemetry_actions() -> None:
    executor = _Executor()
    run = await _run(
        "search_logs",
        '{"action": "count", "index": "agent-logs", "since": "1h"}',
        ("search_logs",),
        executor,
    )
    assert (run.valid, run.ran, run.code) == (True, True, None)
    assert json.loads(run.content) == {"ok": True, "count": 3}
    latency = await _run(
        "measure_model_latency", '{"since": "24h"}', ("measure_model_latency",), executor
    )
    assert latency.ran
    assert executor.calls == [
        {"action": "count", "index": "agent-logs", "since": "1h"},
        {"action": "latency", "since": "24h"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        "{not json",
        "[1]",
        '{"action": "latency", "since": "1h"}',  # search_logs takes search or count only
        '{"action": "count", "index": "agent-logs", "span": "model_call"}',  # an argument of the other tool
    ],
)
async def test_malformed_or_foreign_arguments_are_not_valid(arguments: str) -> None:
    executor = _Executor()
    run = await _run("search_logs", arguments, ("search_logs",), executor)
    assert (run.valid, run.ran) == (False, False)
    assert executor.calls == []


@pytest.mark.asyncio
async def test_a_refusal_fails_step_2_and_a_store_error_fails_step_3() -> None:
    refused = _Executor(
        error=TelemetryQueryError("refused", "no field x", retryable=True, next_action="fix")
    )
    run = await _run(
        "search_logs", '{"action": "count", "index": "agent-logs"}', ("search_logs",), refused
    )
    assert (run.valid, run.ran, run.code) == (False, False, "refused")
    down = _Executor(
        error=TelemetryQueryError("store_unavailable", "down", retryable=True, next_action="retry")
    )
    run = await _run(
        "search_logs", '{"action": "count", "index": "agent-logs"}', ("search_logs",), down
    )
    assert (run.valid, run.ran, run.code) == (True, False, "store_unavailable")


def test_the_shim_refuses_an_elasticsearch_that_is_not_the_eval_one() -> None:
    shim.assert_eval_elasticsearch("http://127.0.0.1:9202")
    with pytest.raises(SystemExit):
        shim.assert_eval_elasticsearch("http://localhost:9200")  # fre-375-allow: seeded refusal


# --------------------------------------------------------------------------- the local transport


def _sse(*chunks: dict[str, object]) -> str:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def _llama(
    reply: str, status: int = 200
) -> tuple[transport.LlamaTransport, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(status, text=reply, headers={"content-type": "text/event-stream"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return transport.LlamaTransport(client, "http://llama/v1/chat/completions", "flash"), sent


_TIMINGS = {
    "cache_n": 100,
    "prompt_n": 20,
    "predicted_n": 7,
    "prompt_ms": 50.0,
    "predicted_ms": 300.0,
}


def test_llama_transport_reads_structured_calls_reasoning_and_timings() -> None:
    reply = _sse(
        {"choices": [{"delta": {"reasoning_content": "think"}}], "system_fingerprint": "b123"},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c0",
                                "function": {"name": "read_skill", "arguments": '{"name"'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": ': "log-search"}'}}]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {
            "choices": [],
            "usage": {"prompt_tokens": 120, "completion_tokens": 7},
            "timings": _TIMINGS,
        },
    )
    llama, sent = _llama(reply)
    out = llama.send(_captured())
    assert out.ok and out.structured
    assert out.tool_calls == (
        {"id": "c0", "name": "read_skill", "arguments": '{"name": "log-search"}'},
    )
    assert out.reasoning == "think"
    assert out.finish_reason == "tool_calls"
    assert out.timings["cache_n"] == 100 and out.timings["prompt_n"] == 20
    assert out.system_fingerprint == "b123"
    assert sent[0]["model"] == "flash" and sent[0]["stream"] is True
    assert out.request_sha256 == transport.sha256(reveal.request_bytes(sent[0]))


def test_llama_transport_recovers_a_text_tool_call_as_production_does() -> None:
    text = '<tool_call>{"name": "search_logs", "arguments": {"action": "count", "index": "agent-logs"}}</tool_call>'
    reply = _sse(
        {"choices": [{"delta": {"content": text}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "timings": _TIMINGS},
    )
    llama, _ = _llama(reply)
    out = llama.send(_captured())
    assert out.ok and not out.structured
    assert [c["name"] for c in out.tool_calls] == ["search_logs"]
    assert json.loads(out.tool_calls[0]["arguments"]) == {"action": "count", "index": "agent-logs"}


def test_llama_transport_reports_an_http_error_as_not_ok() -> None:
    llama, _ = _llama('{"error": "tool not found"}', status=400)
    out = llama.send(_captured())
    assert not out.ok and out.status == 400
    assert "tool not found" in (out.error or "")
    assert out.timings == {}


# --------------------------------------------------------------------------- one draw


def _reply(
    content: str = "",
    calls: tuple[tuple[str, str], ...] = (),
    *,
    finish: str = "tool_calls",
    ok: bool = True,
) -> transport.Reply:
    return transport.Reply(
        ok=ok,
        status=200 if ok else 500,
        error=None if ok else "boom",
        content=content,
        reasoning="r",
        tool_calls=tuple(
            {"id": f"c{i}", "name": name, "arguments": args} for i, (name, args) in enumerate(calls)
        ),
        structured=True,
        finish_reason=finish,
        timings=dict(_TIMINGS),
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
        return self.replies.pop(0) if self.replies else _reply("control")


_LOAD = ("read_skill", '{"name": "log-search"}')
_SEARCH = ("search_logs", '{"action": "count", "index": "agent-logs", "since": "1h"}')


class _Tools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def __call__(self, name: str, arguments: str, revealed: tuple[str, ...]) -> shim.ToolRun:
        self.calls.append((name, arguments, revealed))
        if name not in revealed:
            return shim.ToolRun(
                content='{"ok": false}', valid=False, ran=False, code="not_revealed"
            )
        return shim.ToolRun(content='{"ok": true, "count": 3}', valid=True, ran=True, code=None)


def _draw(scenarios: reveal.Scenarios, form: reveal.Form) -> session.Draw:
    prompt = scenarios.prompts[0]
    return session.Draw(
        prompt=prompt,
        form=form,
        draw=0,
        base=reveal.base_body(_captured(), scenarios, prompt),
        scenarios=scenarios,
        tools=reveal.experimental_tools(),
    )


def _strip_reveal(
    body: dict[str, object], form: reveal.Form, base_tools: list[object]
) -> dict[str, object]:
    body = copy.deepcopy(body)
    for message in body["messages"]:  # type: ignore[union-attr]
        if message.get("name") == "read_skill":
            result = json.loads(message["content"])
            result.pop("provided_tools", None)
            message["content"] = json.dumps(result)
        if message["role"] == "system":
            message["content"] = message["content"].split("\n", 1)[1]  # drop the nonce line
    if form == "simple":
        body["tools"] = base_tools
    return body


@pytest.mark.parametrize("form", reveal.FORMS)
def test_a_full_draw_holds_five_steps_and_the_control_differs_only_by_the_reveal(
    scenarios: reveal.Scenarios, form: reveal.Form
) -> None:
    draw = _draw(scenarios, form)
    scripted = _Scripted(
        _reply(calls=(_LOAD,)), _reply(calls=(_SEARCH,)), _reply("3 errors.", finish="stop")
    )
    tools = _Tools()
    row = session.run_draw(scripted, draw, tools, nonces=("tn", "cn"))

    assert row["steps"] == [True, True, True, True, True]
    assert row["passed"] is True and row["failed_step"] is None
    assert len(row["treatment"]) == 3 and len(row["control"]) == 3
    assert tools.calls == [("search_logs", _SEARCH[1], ("search_logs",))]

    treatment, control = scripted.sent[:3], scripted.sent[3:]
    assert treatment[0]["messages"][0]["content"].startswith("probe nonce tn\n")
    assert control[0]["messages"][0]["content"].startswith("probe nonce cn\n")
    base_tools = draw.base["tools"]
    for t_body, c_body in zip(treatment, control, strict=True):
        assert _strip_reveal(t_body, form, base_tools) == _strip_reveal(c_body, form, base_tools)  # type: ignore[arg-type]
    skill_message = next(m for m in treatment[1]["messages"] if m.get("name") == "read_skill")
    has_schema = "provided_tools" in json.loads(skill_message["content"])
    assert has_schema is (form == "cache_safe")
    assert (len(treatment[1]["tools"]) == len(base_tools) + 1) is (form == "simple")
    # Step 4: the third request carries the result under the rewritten call id.
    last_tool = treatment[2]["messages"][-1]
    assert last_tool == {
        "tool_call_id": "call_t1_0_c0",
        "role": "tool",
        "name": "search_logs",
        "content": '{"ok": true, "count": 3}',
    }


def test_no_skill_load_ends_the_draw_without_a_control(scenarios: reveal.Scenarios) -> None:
    scripted = _Scripted(_reply(calls=(("bash", '{"command": "curl x"}'),)))
    row = session.run_draw(scripted, _draw(scenarios, "simple"), _Tools(), nonces=("t", "c"))
    assert row["steps"] == [False] * 5 and row["failed_step"] == 1
    assert len(scripted.sent) == 1 and row["control"] == []


def test_a_length_stop_fails_its_step(scenarios: reveal.Scenarios) -> None:
    scripted = _Scripted(_reply(calls=(_LOAD,)), _reply(calls=(_SEARCH,), finish="length"))
    tools = _Tools()
    row = session.run_draw(scripted, _draw(scenarios, "cache_safe"), tools, nonces=("t", "c"))
    assert row["steps"] == [True, False, False, False, False] and row["failed_step"] == 2
    assert tools.calls == []
    assert len(row["control"]) == 2


def test_a_provider_error_after_the_tool_result_fails_step_5(scenarios: reveal.Scenarios) -> None:
    scripted = _Scripted(_reply(calls=(_LOAD,)), _reply(calls=(_SEARCH,)), _reply(ok=False))
    row = session.run_draw(scripted, _draw(scenarios, "cache_safe"), _Tools(), nonces=("t", "c"))
    assert row["steps"] == [True, True, True, True, False] and row["failed_step"] == 5


def test_a_call_in_the_same_reply_as_its_read_skill_is_refused(scenarios: reveal.Scenarios) -> None:
    scripted = _Scripted(
        _reply(calls=(_LOAD, _SEARCH)), _reply(calls=(_SEARCH,)), _reply("ok", finish="stop")
    )
    tools = _Tools()
    row = session.run_draw(scripted, _draw(scenarios, "cache_safe"), tools, nonces=("t", "c"))
    assert tools.calls[0] == ("search_logs", _SEARCH[1], ())
    assert tools.calls[1] == ("search_logs", _SEARCH[1], ("search_logs",))
    assert row["passed"] is True


# --------------------------------------------------------------------------- scoring


def _call(cache_n: int, prompt_n: int, predicted_n: int = 0, **usage: int) -> dict[str, object]:
    timings = {"cache_n": cache_n, "prompt_n": prompt_n, "predicted_n": predicted_n}
    return {"timings": timings, "usage": usage}


def _row(
    treatment: list[dict[str, object]], control: list[dict[str, object]], passed: bool = True
) -> dict[str, object]:
    return {
        "prompt_id": "log-search#0",
        "draw": 0,
        "passed": passed,
        "failed_step": None if passed else 2,
        "treatment": treatment,
        "control": control,
    }


_SAME = [_call(0, 1000), _call(990, 40), _call(1020, 30)]


def test_shortfall_is_the_previous_context_minus_this_cache() -> None:
    previous = _call(990, 50, predicted_n=20)["timings"]
    assert score.shortfall(previous, _call(1050, 30)["timings"]) == 10  # type: ignore[arg-type]


def test_reuse_of_generated_tokens_is_scored_not_voided() -> None:
    # A hybrid model continues from the live end state: cache_n passes the previous prompt.
    control = [_call(0, 1000, 20), _call(1015, 40, 20), _call(1070, 30, 20)]
    treated = [_call(0, 1000, 20), _call(1010, 45, 20), _call(1070, 30, 20)]
    result = score.cache_verdict([_row(treated, control)], "cache_safe")
    first = result["pairs"][0]
    assert "void" not in first and first["treatment"] == 10 and first["control"] == 5
    assert first["generated_reuse"] == {"treatment": 10, "control": 15}
    assert result["verdict"] == "fail"


def test_no_reuse_in_both_arms_is_uninformative_not_a_pass() -> None:
    # Both arms re-process the whole prompt: the treatment equals the control, which proves nothing.
    nothing = [_call(0, 1000, 20), _call(0, 1060, 20), _call(0, 1110, 20)]
    for form in reveal.FORMS:
        result = score.cache_verdict([_row(nothing, nothing)], form)
        assert result["verdict"] == "uninformative"
        assert all(p["no_reuse"] for p in result["pairs"])


def test_cache_safe_passes_when_the_reveal_costs_no_more_than_the_control() -> None:
    treated = [_call(0, 1000), _call(990, 50), _call(1030, 30)]
    assert score.cache_verdict([_row(treated, _SAME)], "cache_safe")["verdict"] == "pass"
    worse = [_call(0, 1000), _call(500, 540), _call(1030, 30)]
    assert score.cache_verdict([_row(worse, _SAME)], "cache_safe")["verdict"] == "fail"


def test_simple_allows_one_costly_call_per_session() -> None:
    once = [_call(0, 1000), _call(400, 640), _call(1030, 30)]
    assert score.cache_verdict([_row(once, _SAME)], "simple")["verdict"] == "pass"
    twice = [_call(0, 1000), _call(400, 640), _call(500, 560)]
    assert score.cache_verdict([_row(twice, _SAME)], "simple")["verdict"] == "fail"


def test_a_broken_lineage_voids_the_pair_and_no_valid_pair_is_undecided() -> None:
    control = [_call(0, 1000), _call(1005, 40), _call(1040, 30)]
    treated = [_call(0, 1000), _call(990, 50)]
    result = score.cache_verdict([_row(treated, control)], "cache_safe")
    assert result["verdict"] == "undecided"
    assert result["pairs"][0]["void"] == "lineage" and result["pairs"][0]["excess"] == 5


def test_a_call_without_timings_fails_the_form() -> None:
    treated = [_call(0, 1000), {"timings": {}, "usage": {}}]
    result = score.cache_verdict([_row(treated, _SAME)], "cache_safe")
    assert result["verdict"] == "fail" and "timings_missing" in result["reasons"]


def test_loop_count_and_failed_steps() -> None:
    rows = [_row(_SAME, _SAME), _row(_SAME[:2], _SAME[:2], passed=False)]
    assert score.loop_count(rows) == {"held": 1, "draws": 2, "failed_steps": {"2": 1}}


def test_cloud_cache_table_reads_the_cache_fields() -> None:
    treated = [_call(0, 0, cache_read_input_tokens=0, cache_creation_input_tokens=9000)]
    table = score.cloud_cache([_row(treated, [])])
    assert table[0]["treatment"] == [
        {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 9000}
    ]


# --------------------------------------------------------------------------- the command line


def test_the_cloud_reservation_counts_the_whole_request_and_the_output_budget() -> None:
    target = type("T", (), {"input_cost": 3e-06, "output_cost": 1.5e-05})()
    body = {"messages": [{"role": "user", "content": "x" * 2998}], "tools": []}
    size = len(reveal.request_bytes(body))
    assert transport.reserve_usd(body, target, 1000) == pytest.approx(
        size / 3 * 3e-06 + 1000 * 1.5e-05
    )  # type: ignore[arg-type]


def test_a_changed_configuration_is_refused(tmp_path: object) -> None:
    path = tmp_path / "fingerprint-local.json"  # type: ignore[operator]
    spike.ensure_fingerprint(path, {"quant": "UD-IQ4_XS", "draws": 3})
    spike.ensure_fingerprint(path, {"quant": "UD-IQ4_XS", "draws": 3})
    with pytest.raises(SystemExit, match="draws"):
        spike.ensure_fingerprint(path, {"quant": "UD-IQ4_XS", "draws": 1})


def test_props_record_the_template_hash_and_the_cache_settings() -> None:
    props = {
        "chat_template": "{{ messages }}",
        "build_info": "b6000",
        "default_generation_settings": {"params": {"n_cache_reuse": 256, "temperature": 0.6}},
        "n_ctx_checkpoints": 32,
        "cache_ram_mib": 8192,
        "kv_unified": True,
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=props)))
    out = spike.read_props(client, "http://llama:8600/v1/chat/completions")
    assert out["build_info"] == "b6000"
    assert out["cache_settings"] == {
        "default_generation_settings.params.n_cache_reuse": 256,
        "n_ctx_checkpoints": 32,
        "cache_ram_mib": 8192,
        "kv_unified": True,
    }
    assert out["chat_template_sha256"] == transport.sha256(b"{{ messages }}")


def test_props_can_come_from_a_saved_file_without_a_request(tmp_path: object) -> None:
    saved = tmp_path / "props.json"  # type: ignore[operator]
    saved.write_text(json.dumps({"chat_template": "t", "build_info": "b7000"}))

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    client = httpx.Client(transport=httpx.MockTransport(refuse))
    out = spike.read_props(client, "http://llama:8600/v1/chat/completions", saved)
    assert out["build_info"] == "b7000" and out["source"] == str(saved)


def test_report_names_w3_only_when_both_local_forms_miss_the_floor(tmp_path: object) -> None:
    run_dir = tmp_path  # type: ignore[assignment]
    for form, held in (("cache_safe", 9), ("simple", 4)):
        path = spike.rows_path(run_dir, "local", form)  # type: ignore[arg-type]
        for i in range(12):
            row = _row(_SAME, _SAME, passed=i < held)
            spike.append_jsonl(path, {**row, "draw": i})
    assert spike.build_report(run_dir)["w3"] is True  # type: ignore[arg-type]
    for i in range(12):
        spike.append_jsonl(
            spike.rows_path(run_dir, "local", "simple"), {**_row(_SAME, _SAME), "draw": 12 + i}
        )  # type: ignore[arg-type]
    assert spike.build_report(run_dir)["w3"] is False  # type: ignore[arg-type]


# --------------------------------------------------------------------------- review fixes


def test_step_4_reads_the_history_after_the_production_sanitiser() -> None:
    call = {
        "id": "call_t1_0_c0",
        "type": "function",
        "function": {"name": "search_logs", "arguments": "{}"},
    }
    result = {"tool_call_id": "call_t1_0_c0", "role": "tool", "name": "search_logs", "content": "R"}
    kept = {
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [call]},
            result,
        ]
    }
    assert session._carried(kept, "call_t1_0_c0", "R") is True
    # The assistant call is missing, so the sanitiser drops the orphaned result.
    orphan = {"messages": [{"role": "user", "content": "q"}, result]}
    assert session._carried(orphan, "call_t1_0_c0", "R") is False


def test_a_corrupt_stream_line_is_a_failed_reply_not_a_crash() -> None:
    llama, _ = _llama("data: {not json\n\n")
    out = llama.send(_captured())
    assert not out.ok and "JSONDecodeError" in (out.error or "")


def _cloud(tmp_path: object, max_usd: float) -> transport.CloudTransport:
    cloud = transport.CloudTransport.__new__(transport.CloudTransport)
    cloud._ledger = tmp_path / "ledger.jsonl"  # type: ignore[operator]
    cloud._max_usd = max_usd
    cloud.target = type("T", (), {"input_cost": 3e-06, "output_cost": 1.5e-05})()  # type: ignore[assignment]
    cloud._client = type("C", (), {"max_tokens": 1000})()  # type: ignore[assignment]
    return cloud


def test_the_cloud_cap_counts_every_booked_call_at_once(tmp_path: object) -> None:
    cloud = _cloud(tmp_path, max_usd=1.0)
    body = {"messages": [{"role": "user", "content": "q"}], "tools": []}
    cloud.reserve(body)
    cloud._book("a", 0.6, ok=True)
    cloud._book("b", 0.39, ok=False)  # a failed call is booked at its reservation
    assert cloud.spent() == pytest.approx(0.99)
    with pytest.raises(SystemExit, match="cost cap"):
        cloud.reserve(body)


def test_a_rejected_call_voids_its_pair_instead_of_failing_the_form() -> None:
    treated = [_call(0, 1000), _call(990, 50), {"ok": False, "timings": {}, "usage": {}}]
    result = score.cache_verdict([_row(treated, _SAME)], "cache_safe")
    assert result["verdict"] == "pass"
    assert result["pairs"][1]["void"] == "provider_error"


def test_w3_needs_the_full_population(tmp_path: object) -> None:
    for form in reveal.FORMS:
        path = spike.rows_path(tmp_path, "local", form)  # type: ignore[arg-type]
        spike.append_jsonl(path, {**_row(_SAME, _SAME, passed=False), "draw": 0})
    assert spike.build_report(tmp_path)["w3"] is False  # type: ignore[arg-type]


def test_a_local_run_needs_the_llama_server_arguments(tmp_path: object) -> None:
    argv = ["run", "--run-dir", str(tmp_path), "--base", str(tmp_path / "none.json")]  # type: ignore[operator]
    argv += ["--provider", "local", "--form", "simple", "--llama-model", "m", "--quant", "q"]
    with pytest.raises(SystemExit, match="--server-args"):
        spike.main(argv)


def test_cache_safe_ignores_no_reuse_at_a_step_it_does_not_score() -> None:
    # Call 2 is void (lineage), call 3 reused nothing: cache-safe scores call 2 only, so undecided.
    control = [_call(0, 1000), _call(1005, 40), _call(0, 1100)]
    treated = [_call(0, 1000), _call(990, 50), _call(0, 1100)]
    assert score.cache_verdict([_row(treated, control)], "cache_safe")["verdict"] == "undecided"
    assert score.cache_verdict([_row(treated, control)], "simple")["verdict"] == "uninformative"
