"""FRE-1590 Part 1: the triggering harness, offline (no gateway, no model)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from scripts.eval.fre1537.common import append_jsonl, read_jsonl
from scripts.eval.fre1581.g2_skill_choice import Item, load_items
from scripts.eval.fre1590 import triggering as t

URL = "http://llama.test/v1/chat/completions"
KEY = "qwen3.8-flash-next"


def body(system: str = "SYS", tools: tuple[str, ...] = ("read", "read_skill")) -> dict:
    return {
        "model": "gateway-name",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": n}} for n in tools],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "max_tokens": 12288,
        "temperature": 1.0,
        "top_k": 64,
        "repeat_penalty": 1.0,
        "cache_prompt": True,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def record(id_: str, kind: str = "scenario", expected: str | None = "query-tempo") -> dict:
    return {"id": id_, "kind": kind, "expected_skill": expected, "prompt": "p", "body": body()}


def call(name: str, arguments: object) -> dict:
    return {"id": "c", "type": "function", "function": {"name": name, "arguments": arguments}}


def reply(*calls: dict, build: str = "b1-abc") -> dict:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": "", "tool_calls": list(calls)},
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        "system_fingerprint": build,
    }


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def fingerprint(**over: object) -> dict:
    fp = t.build_fingerprint(KEY, "unsloth/qwen3.8-flash-next", "UD-IQ4_XS", 3, [record("a#0")])
    return {**fp, **over}


# --------------------------------------------------------------------------- names


def test_safe_names_are_unique_and_safe_for_all_63_items() -> None:
    items = load_items()
    assert len(items) == 63
    names = [t.safe_name(i.id) for i in items]
    assert len(set(names)) == 63
    assert all(re.fullmatch(r"[A-Za-z0-9._-]+", n) for n in names)
    assert t.safe_name("query-tempo#3") == "query-tempo__3"


# --------------------------------------------------------------------------- parsing


def test_one_read_skill_call_gives_its_name() -> None:
    first = t.parse_tool_calls(
        reply(call("read_skill", '{"name": "query-tempo"}'))["choices"][0]["message"]
    )
    assert first == t.FirstCall(("read_skill",), ("query-tempo",), ())


def test_several_calls_keep_their_order_and_every_skill_name() -> None:
    message = reply(
        call("read", '{"path": "x"}'),
        call("read_skill", '{"name": "query-tempo", "path": "references/a.md"}'),
        call("read_skill", {"name": "turn-forensics"}),  # some servers send a dict
    )["choices"][0]["message"]
    first = t.parse_tool_calls(message)
    assert first.tool_names == ("read", "read_skill", "read_skill")
    assert first.skills == ("query-tempo", "turn-forensics")
    assert first.malformed == ()


def test_malformed_read_skill_arguments_are_recorded_not_raised() -> None:
    message = reply(
        call("read_skill", '{"name": "query-te'),
        call("read_skill", '{"path": "a.md"}'),
        call("read_skill", '"query-tempo"'),
    )["choices"][0]["message"]
    first = t.parse_tool_calls(message)
    assert first.skills == ()
    assert first.malformed == ('{"name": "query-te', '{"path": "a.md"}', '"query-tempo"')


def test_no_tool_calls_parse_as_empty() -> None:
    for message in ({"content": "hello"}, {"content": "x", "tool_calls": None}):
        assert t.parse_tool_calls(message) == t.FirstCall((), (), ())


def test_call_llama_reads_the_reply_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reply(call("read_skill", '{"name": "query-tempo"}')))

    with client_for(handler) as client:
        out = t.call_llama(client, URL, {"messages": []})
    assert out["read_skill_names"] == ["query-tempo"]
    assert out["tool_names"] == ["read_skill"]
    assert out["finish_reason"] == "tool_calls"
    assert out["system_fingerprint"] == "b1-abc"
    assert out["usage"] == {"prompt_tokens": 100, "completion_tokens": 10}


def test_call_llama_refuses_a_reply_without_a_message() -> None:
    with client_for(lambda r: httpx.Response(200, json={"choices": []})) as client:
        with pytest.raises(ValueError):
            t.call_llama(client, URL, {})


# --------------------------------------------------------------------------- request


def test_the_request_keeps_every_captured_key_and_sets_model_and_stream() -> None:
    captured = body()
    request = t.replay_request(captured, "unsloth/gemma-4-26B-A4B-it")
    assert request["model"] == "unsloth/gemma-4-26B-A4B-it"
    assert request["stream"] is False
    assert "stream_options" not in request
    for key in set(captured) - {"model", "stream", "stream_options"}:
        assert request[key] == captured[key]
    assert request["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["stream"] is True and captured["model"] == "gateway-name"  # not mutated


# --------------------------------------------------------------------------- scorer


def test_classify_each_rule_with_a_seeded_negative() -> None:
    c = t.classify
    # right: the expected skill only (a repeat is a reference read) ...
    assert c("scenario", "query-tempo", ["query-tempo", "query-tempo"], []) == "right"
    # ... and never with another skill beside it.
    assert c("scenario", "query-tempo", ["query-tempo", "neo4j-direct"], []) == "wrong"
    # wrong: another skill alone; the expected skill alone is not wrong.
    assert c("scenario", "query-tempo", ["neo4j-direct"], []) == "wrong"
    assert c("scenario", "query-tempo", ["query-tempo"], []) != "wrong"
    # none: no read_skill call; a malformed read_skill call is not none.
    assert c("scenario", "query-tempo", [], []) == "none"
    assert c("scenario", "query-tempo", [], ["{bad"]) == "malformed"
    # no-skill: any read_skill call is a load; no call is not.
    assert c("no_skill", None, ["query-tempo"], []) == "load"
    assert c("no_skill", None, [], ["{bad"]) == "load"
    assert c("no_skill", None, [], []) == "none"


def row(id_: str, draw: int, skills: list[str], **extra: object) -> dict:
    return {
        "id": id_,
        "draw": draw,
        "read_skill_names": skills,
        "read_skill_malformed": [],
        **extra,
    }


ITEMS = [
    Item("query-tempo#0", "scenario", "p", "query-tempo"),
    Item("query-tempo#1", "scenario", "p", "query-tempo"),
    Item("neo4j-direct#0", "scenario", "p", "neo4j-direct"),
    Item("ns01", "no_skill", "p", None),
    Item("ns02", "no_skill", "p", None),
    Item("ns03", "no_skill", "p", None),
    Item("ns04", "no_skill", "p", None),
]


def test_score_counts_right_wrong_none_loads_and_missing() -> None:
    rows = [
        row("query-tempo#0", 0, ["query-tempo"]),
        row("query-tempo#0", 1, ["neo4j-direct"]),
        row("query-tempo#1", 0, []),
        row("query-tempo#1", 1, ["query-tempo"]),
        row("neo4j-direct#0", 0, [], error="boom"),
        row("neo4j-direct#0", 1, [], error="boom again"),
        row("ns01", 0, ["query-tempo"]),
        row("ns01", 1, []),
        row("ns02", 0, [], error="timeout"),
        row("ns02", 0, []),  # a retry after an error: not missing
        row("ns02", 1, []),
    ]
    captured = [i.id for i in ITEMS if i.id != "ns04"]
    report = t.score_rows(ITEMS, rows, 2, captured)
    tempo = report["skills"]["query-tempo"]
    assert (tempo["scenarios"], tempo["draws"]) == (2, 4)
    assert (tempo["right"], tempo["wrong"], tempo["none"], tempo["malformed"]) == (2, 1, 1, 0)
    assert tempo["wrong_loads"] == {"neo4j-direct": 1}
    assert report["skills"]["neo4j-direct"]["draws"] == 0
    assert report["no_skill"]["ns01"] == {"draws": 2, "loads": 1, "skills": ["query-tempo"]}
    assert report["no_skill"]["ns02"]["loads"] == 0
    missing = {m["id"]: m["reason"] for m in report["missing"]}
    assert missing == {
        "neo4j-direct#0": "every replay errored: boom again",
        "ns03": "captured, not replayed",
        "ns04": "not captured",
    }
    assert all(m["reason"] for m in report["missing"])
    assert report["incomplete"] == []
    text = t.render({"model_key": KEY, **report})
    assert "ns04: not captured" in text and "query-tempo" in text


def test_score_reports_an_item_with_too_few_draws_as_incomplete() -> None:
    rows = [row("query-tempo#0", 0, ["query-tempo"]), row("query-tempo#0", 1, [], error="x")]
    report = t.score_rows(ITEMS[:1], rows, 3, ["query-tempo#0"])
    assert report["missing"] == []
    assert report["incomplete"] == [
        {"id": "query-tempo#0", "draws_done": 1, "reason": "1 of 3 draws; last error: x"}
    ]


# --------------------------------------------------------------------------- replay


def test_replay_resumes_and_retries_only_error_rows(tmp_path: Path) -> None:
    paths = t.Part1Paths(tmp_path, KEY)
    captured = [record("query-tempo#0"), record("ns01", "no_skill", None)]
    t.ensure_fingerprint(paths, fingerprint())
    append_jsonl(paths.part1, row("query-tempo#0", 0, ["query-tempo"]))
    append_jsonl(paths.part1, row("ns01", 0, [], error="timeout"))
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=reply(call("read_skill", '{"name": "query-tempo"}')))

    with client_for(handler) as client:
        errors = t.run_replay(client, URL, paths, captured, "llama-name", 2)
    assert errors == 0
    assert len(sent) == 3  # (ns01, 0) retried; (query-tempo#0, 1) and (ns01, 1) new
    assert all(s["model"] == "llama-name" and s["stream"] is False for s in sent)
    good = {(r["id"], r["draw"]) for r in read_jsonl(paths.part1) if "error" not in r}
    assert good == {("query-tempo#0", 0), ("query-tempo#0", 1), ("ns01", 0), ("ns01", 1)}
    assert json.loads(paths.fingerprint.read_text())["llama_build"] == "b1-abc"


def test_replay_writes_an_error_row_on_an_http_error(tmp_path: Path) -> None:
    paths = t.Part1Paths(tmp_path, KEY)
    t.ensure_fingerprint(paths, fingerprint())
    with client_for(lambda r: httpx.Response(500, text="down")) as client:
        errors = t.run_replay(client, URL, paths, [record("query-tempo#0")], "m", 1)
    assert errors == 1
    (only,) = read_jsonl(paths.part1)
    assert "error" in only and only["id"] == "query-tempo#0"


# --------------------------------------------------------------------------- fingerprint


def test_the_fingerprint_guard_refuses_a_different_configuration(tmp_path: Path) -> None:
    paths = t.Part1Paths(tmp_path, KEY)
    t.ensure_fingerprint(paths, fingerprint())
    t.ensure_fingerprint(paths, fingerprint())  # the same configuration resumes
    for changed in (
        {"quant": "Q8_0"},
        {"draws": 5},
        {"system_prompt_sha256": "other"},
        {"tool_names_sha256": "other"},
    ):
        with pytest.raises(SystemExit, match="different configuration"):
            t.ensure_fingerprint(paths, fingerprint(**changed))


def test_a_known_build_is_kept_and_a_changed_build_stops_the_run(tmp_path: Path) -> None:
    paths = t.Part1Paths(tmp_path, KEY)
    t.ensure_fingerprint(paths, fingerprint())
    t.record_build(paths, "b1-abc")
    # A new run does not know the build until its first reply: not a difference.
    t.ensure_fingerprint(paths, fingerprint())
    t.record_build(paths, "b1-abc")
    with pytest.raises(SystemExit, match="build changed"):
        t.record_build(paths, "b2-def")
    with pytest.raises(SystemExit, match="different configuration"):
        t.ensure_fingerprint(paths, fingerprint(llama_build="b2-def"))


def test_the_fingerprint_hashes_the_captured_prompt_and_tools() -> None:
    fp = t.build_fingerprint(KEY, "m", "Q8_0", 3, [record("a#0"), record("b#0")])
    assert fp["system_prompt_variants"] == 1
    assert fp["tool_count"] == 2
    other = t.build_fingerprint(KEY, "m", "Q8_0", 3, [{**record("a#0"), "body": body("NEW")}])
    assert other["system_prompt_sha256"] != fp["system_prompt_sha256"]
    assert other["tools_sha256"] == fp["tools_sha256"]


# --------------------------------------------------------------------------- capture


def test_capture_saves_the_primary_body_and_skips_existing_files(tmp_path: Path) -> None:
    paths = t.Part1Paths(tmp_path, KEY)
    calls = tmp_path / "stub-out" / "calls"
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        posted.append(params)
        n = len(list(calls.glob("*.json"))) + 1
        primary = {**body(), "messages": [{"role": "user", "content": params["message"]}]}
        (calls / f"{n:04d}.json").write_text(json.dumps({"body": primary}))
        (calls / f"{n + 1:04d}.json").write_text(json.dumps({"body": {"messages": []}}))
        return httpx.Response(200, json={"session_id": f"s{n}"})

    items = ITEMS[:2]
    paths.captured.mkdir(parents=True)
    paths.captured_file(items[1].id).write_text("{}")
    with client_for(handler) as client:
        failed = t.run_capture(client, "http://gw.test/chat", paths, items, settle=0)
    assert failed == 0
    assert len(posted) == 1
    assert posted[0]["model"] == KEY and "session_id" not in posted[0]
    saved = json.loads(paths.captured_file(items[0].id).read_text())
    assert saved["id"] == "query-tempo#0" and saved["expected_skill"] == "query-tempo"
    assert saved["body"]["messages"][-1]["content"] == "p"
    assert [r.get("id") for r in t.load_captured(paths, items)] == ["query-tempo#0", None]


def test_a_failed_capture_writes_no_file(tmp_path: Path) -> None:
    paths = t.Part1Paths(tmp_path, KEY)
    with client_for(lambda r: httpx.Response(502)) as client:
        failed = t.run_capture(client, "http://gw.test/chat", paths, ITEMS[:1], settle=0)
    assert failed == 1
    assert not paths.captured_file(ITEMS[0].id).exists()
