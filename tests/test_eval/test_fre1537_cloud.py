"""FRE-1516 / ADR-0154 D7: the probe on a managed deployment (OVH, Anthropic).

Offline. No model call, no network, no database. The provider boundary (``litellm.acompletion``), the
cost gate and the cost tracker are replaced. The production client between them is real, so these tests
read what the gateway would send.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import openai
import pytest
from scripts.eval.fre1537 import cloud, fingerprint, llama, longhist, render, replay, score
from scripts.eval.fre1537.common import RunPaths, append_jsonl, read_jsonl
from tests._helpers.litellm_capability import pinned_litellm_capabilities

from personal_agent.cost_gate import BudgetDenied

DECLINE = '{"strategy": "SINGLE", "tasks": []}'
OVH = "qwen3.8-27b-ovh"
SONNET = "claude_sonnet"
OVH_CANDIDATE = {"temperature": 1.0, "reasoning_effort": "none"}
SONNET_CANDIDATE = {"effort": "low"}


@pytest.fixture(autouse=True)
def _pin_capabilities() -> Iterator[None]:
    """Litellm's GitHub-fetched capability map must not decide these results."""
    with pinned_litellm_capabilities():
        yield


def target(key: str = OVH, mode: str = "planner", candidate: Mapping[str, object] | None = None):  # type: ignore[no-untyped-def]
    if candidate is None and mode == "planner":
        candidate = OVH_CANDIDATE if key == OVH else SONNET_CANDIDATE
    return cloud.resolve_target(key, mode, candidate=candidate)


# ── The production stack with the provider boundary replaced ─────────────────


def provider_response(
    content: str = DECLINE,
    *,
    message_extra: Mapping[str, object] | None = None,
    reasoning_tokens: int | None = None,
    completion_tokens: int = 13,
    model: str = "served-model-1",
) -> MagicMock:
    usage = MagicMock()
    usage.prompt_tokens = 1000
    usage.completion_tokens = completion_tokens
    usage.total_tokens = 1000 + completion_tokens
    usage.cache_read_input_tokens = None
    usage.cache_creation_input_tokens = None
    usage.prompt_tokens_details = None
    usage.completion_tokens_details = (
        SimpleNamespace(reasoning_tokens=reasoning_tokens) if reasoning_tokens is not None else None
    )
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.choices[0].message.tool_calls = None
    response.choices[0].finish_reason = "stop"
    response.usage = usage
    response.id = "resp_fre1516"
    response.model_dump.return_value = {
        "model": model,
        "choices": [{"message": {"content": content, **(message_extra or {})}}],
        "usage": {
            "completion_tokens_details": (
                {"reasoning_tokens": reasoning_tokens} if reasoning_tokens is not None else None
            )
        },
    }
    return response


@pytest.fixture
def stack() -> Iterator[dict[str, Any]]:
    """Patch the provider, the cost gate and the cost tracker. Yield what litellm received."""
    seen: dict[str, Any] = {"calls": [], "response": provider_response(), "error": None}

    async def acompletion(**kwargs: Any) -> MagicMock:
        seen["calls"].append(kwargs)
        if seen["error"] is not None:
            raise seen["error"]
        return seen["response"]

    gate = MagicMock()
    gate.reserve = AsyncMock(return_value="res-fre1516")
    gate.commit = AsyncMock()
    gate.refund = AsyncMock()
    tracker = AsyncMock()
    seen["gate"] = gate
    with ExitStack() as es:
        es.enter_context(patch("litellm.acompletion", side_effect=acompletion))
        es.enter_context(patch("litellm.completion_cost", return_value=0.001))
        es.enter_context(patch("personal_agent.cost_gate.get_default_gate", return_value=gate))
        es.enter_context(
            patch("personal_agent.cost_gate.load_budget_config", return_value=MagicMock())
        )
        es.enter_context(
            patch(
                "personal_agent.llm_client.cost_estimator.estimate_reservation_for_call",
                return_value=Decimal("0.01"),
            )
        )
        es.enter_context(
            patch(
                "personal_agent.llm_client.history_sanitiser.sanitise_messages",
                side_effect=lambda msgs, trace_id: (msgs, []),
            )
        )
        es.enter_context(
            patch(
                "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
                return_value=tracker,
            )
        )
        es.enter_context(
            patch(
                "personal_agent.config.settings.get_settings",
                return_value=MagicMock(anthropic_api_key="k", managed_embedding_token="k"),
            )
        )
        yield seen


def test_the_ovh_candidate_reaches_the_provider_as_thinking_off(stack: dict[str, Any]) -> None:
    with cloud.CloudSession(target(OVH), gate=stack["gate"]) as session:
        row = session.call("SYS", "USER")
    (kwargs,) = stack["calls"]
    assert kwargs["model"] == "ovhcloud/Qwen3.8-27B"
    assert kwargs["reasoning_effort"] == "none" and kwargs["temperature"] == 1.0
    assert kwargs["allowed_openai_params"] == ["reasoning_effort"]
    assert kwargs["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in kwargs["messages"]] == ["system", "user"]
    assert kwargs["messages"][1]["content"] == "USER"
    for banned in (
        "top_k",
        "min_p",
        "cache_prompt",
        "chat_template_kwargs",
        "extra_body",
        "stream",
    ):
        assert banned not in kwargs
    assert row["content"] == DECLINE and row["served_model"] == "served-model-1"
    assert row["cost_usd"] == pytest.approx(0.001)
    assert row["usage"]["completion_tokens"] == 13  # type: ignore[index]


def test_the_sonnet_candidate_reaches_the_provider_as_low_effort(stack: dict[str, Any]) -> None:
    with cloud.CloudSession(target(SONNET), gate=stack["gate"]) as session:
        session.call("SYS", "USER")
    (kwargs,) = stack["calls"]
    assert kwargs["model"] == "anthropic/claude-sonnet-5"
    assert kwargs["reasoning_effort"] == "low"
    assert "temperature" not in kwargs
    assert kwargs["extra_headers"] == {"anthropic-beta": "prompt-caching-2024-07-31"}
    system = kwargs["messages"][0]
    assert system["content"][0]["text"] == "SYS"
    assert system["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_the_default_mode_is_what_the_catalog_declares(stack: dict[str, Any]) -> None:
    with cloud.CloudSession(target(OVH, "default"), gate=stack["gate"]) as session:
        session.call("SYS", "USER")
    assert stack["calls"][0]["reasoning_effort"] == "medium"


def test_a_provider_error_becomes_cloud_call_error(stack: dict[str, Any]) -> None:
    stack["error"] = openai.APIConnectionError(
        request=SimpleNamespace(method="POST", url="https://x")  # type: ignore[arg-type]
    )
    with cloud.CloudSession(target(OVH), gate=stack["gate"]) as session:
        with pytest.raises(cloud.CloudCallError):
            session.call("SYS", "USER")


def test_a_cost_gate_denial_stops_the_run(stack: dict[str, Any]) -> None:
    stack["gate"].reserve = AsyncMock(
        side_effect=BudgetDenied(
            role="main_inference",
            time_window="daily",
            current_spend=Decimal("1"),
            cap=Decimal("1"),
            window_resets_at=datetime.now(UTC),
        )
    )
    with cloud.CloudSession(target(OVH), gate=stack["gate"]) as session:
        with pytest.raises(SystemExit, match="cost gate denied"):
            session.call("SYS", "USER")
    assert stack["calls"] == []


# ── The database guard (FRE-375) ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@127.0.0.1:5432/seshat",  # production
        "postgresql+asyncpg://u:p@127.0.0.1:5433/seshat",  # the unit-test stack
        "postgresql+asyncpg://u:p@db.example.com:5434/seshat",  # a remote host
        "postgresql+asyncpg://u:p@postgres/seshat",  # no port
    ],
)
def test_a_database_that_is_not_the_eval_postgres_is_refused(url: str) -> None:
    with pytest.raises(SystemExit, match="eval Postgres"):
        cloud.assert_eval_database(url)


def test_the_eval_postgres_is_accepted() -> None:
    cloud.assert_eval_database("postgresql+asyncpg://agent:p@127.0.0.1:5434/seshat")


def test_a_session_on_the_production_database_does_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(database_url="postgresql+asyncpg://u:p@127.0.0.1:5432/seshat")
    monkeypatch.setattr(cloud, "get_settings", lambda: settings)
    with pytest.raises(SystemExit, match="eval Postgres"):
        cloud.CloudSession(target(OVH))


# ── Target resolution ────────────────────────────────────────────────────────


def test_candidates_are_declared_as_given() -> None:
    assert target(OVH).declared == OVH_CANDIDATE
    assert target(SONNET).declared == SONNET_CANDIDATE
    assert target(OVH).mode_name == "planner"


def test_the_default_mode_declares_the_catalog_default() -> None:
    t = target(OVH, "default")
    assert t.mode_name == "default" and t.declared["reasoning_effort"] == "medium"


@pytest.mark.parametrize(
    ("key", "candidate"),
    [
        (OVH, {"enable_thinking": False}),  # a llama.cpp field
        (SONNET, {"temperature": 1.0}),  # anthropic_adaptive rejects temperature
        (OVH, {"temperature": 1.0, "reasoning_effort": "high"}),  # outside the OVH domain
    ],
)
def test_a_candidate_outside_the_dialect_is_refused(key: str, candidate: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        cloud.resolve_target(key, "planner", candidate=candidate)


def test_a_local_deployment_is_refused() -> None:
    with pytest.raises(ValueError, match="local"):
        cloud.resolve_target("qwen3.8-flash-next", "default")


def test_a_planner_mode_that_the_catalog_lacks_is_refused() -> None:
    with pytest.raises(ValueError, match="declares no `planner` mode"):
        cloud.resolve_target(SONNET, "planner")


# ── What a response records ──────────────────────────────────────────────────


def llm_response(
    content: str = DECLINE,
    message: Mapping[str, object] | None = None,
    usage: Mapping[str, object] | None = None,
    raw_usage: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "content": content,
        "finish_reason": "stop",
        "cost_usd": 0.0009719,
        "usage": {"prompt_tokens": 2000, "completion_tokens": 10, **(usage or {})},
        "raw": {
            "model": "served-model-1",
            "choices": [{"message": dict(message or {})}],
            "usage": dict(raw_usage or {}),
        },
    }


def _reasoning_check(row: Mapping[str, object]) -> score.Check:
    rows = [
        {
            "arm": "decide",
            "label": "x",
            "trial": 0,
            "expected": "decline",
            "kind": "single",
            "secs": 1.0,
            "plan": llama.parse_plan(DECLINE),
            "declined": True,
            **row,
        }
    ]
    return next(c for c in score.score(rows, [], [], {}).checks if c.name.startswith("Reasoning"))


def test_a_clean_response_records_no_reasoning() -> None:
    row = cloud.row_from_response(llm_response(), 1.234)
    assert row["secs"] == 1.23 and row["content"] == DECLINE
    assert row["reasoning_chars"] == 0 and row["thinking_blocks"] == 0
    assert row["served_model"] == "served-model-1" and row["timings"] == {}
    assert row["cost_usd"] == pytest.approx(0.0009719)
    assert row["usage"] == {"prompt_tokens": 2000, "completion_tokens": 10}
    assert _reasoning_check(row).passed


@pytest.mark.parametrize(
    ("response", "chars", "blocks", "tokens"),
    [
        (llm_response(message={"reasoning_content": "abc"}), 3, 0, 0),
        (
            llm_response(message={"thinking_blocks": [{"type": "thinking", "thinking": "hello"}]}),
            5,
            1,
            0,
        ),
        (
            llm_response(
                message={"thinking_blocks": [{"type": "redacted_thinking", "data": "xyz"}]}
            ),
            0,
            1,
            0,
        ),
        (llm_response(content="<think>why</think>" + DECLINE), 3, 0, 0),
        (llm_response(usage={"reasoning_tokens": 12}), 0, 0, 12),
        (llm_response(raw_usage={"completion_tokens_details": {"reasoning_tokens": 7}}), 0, 0, 7),
    ],
)
def test_reasoning_evidence_is_seen_and_the_scorer_fails_it(
    response: dict[str, object], chars: int, blocks: int, tokens: int
) -> None:
    row = cloud.row_from_response(response, 1.0)
    assert (row["reasoning_chars"], row["thinking_blocks"]) == (chars, blocks)
    assert row["usage"].get("reasoning_tokens", 0) == tokens  # type: ignore[union-attr]
    assert not _reasoning_check(row).passed


def test_a_reasoning_token_count_of_zero_passes() -> None:
    row = cloud.row_from_response(llm_response(usage={"reasoning_tokens": 0}), 1.0)
    assert row["usage"]["reasoning_tokens"] == 0  # type: ignore[index]
    assert _reasoning_check(row).passed


def test_a_thinking_block_in_a_production_response_fails_the_threshold(
    stack: dict[str, Any],
) -> None:
    stack["response"] = provider_response(
        message_extra={"thinking_blocks": [{"type": "redacted_thinking", "data": "xyz"}]}
    )
    with cloud.CloudSession(target(SONNET), gate=stack["gate"]) as session:
        row = session.call("SYS", "USER")
    assert row["thinking_blocks"] == 1 and not _reasoning_check(row).passed


# ── The arms ─────────────────────────────────────────────────────────────────


class FakeSession:
    """Stands in for a CloudSession: records calls, returns rows, can fail."""

    def __init__(self, t: cloud.CloudTarget, fail: bool = False) -> None:
        self.target = t
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def call(self, system: str, user: str) -> dict[str, object]:
        self.calls.append((system, user))
        if self.fail:
            raise cloud.CloudCallError("boom")
        return cloud.row_from_response(llm_response(), 0.5)


def make_inputs(tmp_path: Path, tag: str = "ovh-planner") -> tuple[RunPaths, llama.Inputs]:
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
        },
    }
    body = {"messages": [{"role": "system", "content": "S"}], "tools": [], "temperature": 1.0}
    captured = {
        "greeting": {
            "label": "greeting",
            "expected": "decline",
            "history": None,
            "message": "m",
            "body": body,
        }
    }
    return RunPaths(tmp_path, tag), llama.Inputs(prompts=prompts, captured=captured)


def run_decide(
    paths: RunPaths,
    inputs: llama.Inputs,
    session: FakeSession,
    budget: cloud.Budget | None = None,
) -> None:
    replay.run_decide(
        None,  # type: ignore[arg-type]
        "",
        "",
        paths,
        inputs,
        llama.PlannerMode(session.target.mode_name, session.target.declared),
        None,
        ["greeting"],
        trials=1,
        cloud=session,  # type: ignore[arg-type]
        budget=budget,
    )


def start(tmp_path: Path, session: FakeSession) -> tuple[RunPaths, llama.Inputs]:
    paths, inputs = make_inputs(tmp_path)
    fingerprint.ensure_compatible(
        paths, fingerprint.build_cloud_fingerprint(session.target, inputs, paths, None)
    )
    return paths, inputs


def test_run_decide_writes_a_managed_row(tmp_path: Path) -> None:
    session = FakeSession(target(OVH))
    paths, inputs = start(tmp_path, session)
    run_decide(paths, inputs, session)
    (row,) = read_jsonl(paths.decide)
    assert row["mode"] == "planner" and row["declined"] is True and "error" not in row
    assert session.calls == [("SYSTEM PROMPT", "Q-greeting")]
    assert json.loads(paths.fingerprint.read_text())["engine"]["served_model"] == "served-model-1"


def test_run_decide_records_an_error_row_and_its_input_estimate(tmp_path: Path) -> None:
    session = FakeSession(target(OVH), fail=True)
    paths, inputs = start(tmp_path, session)
    run_decide(paths, inputs, session, cloud.Budget(tmp_path, max_usd=5.0))
    (row,) = read_jsonl(paths.decide)
    assert "error" in row and row["cost_usd"] > 0  # type: ignore[operator]


def test_the_cost_cap_counts_every_tag_of_the_run(tmp_path: Path) -> None:
    session = FakeSession(target(OVH))
    paths, inputs = make_inputs(tmp_path)
    other = RunPaths(tmp_path, "sonnet-planner")
    other.rows.mkdir(parents=True)
    append_jsonl(other.decide, {"label": "x", "trial": 0, "cost_usd": 0.5})
    append_jsonl(other.longhist, {"size_chars": 8000, "cold": {"cost_usd": 0.2}, "extended": {}})
    budget = cloud.Budget(tmp_path, max_usd=0.6)
    assert budget.spent() == pytest.approx(0.7)
    with pytest.raises(SystemExit, match="cap"):
        run_decide(paths, inputs, session, budget)
    assert session.calls == []


def test_the_cost_cap_refuses_a_call_whose_worst_case_input_reaches_it(tmp_path: Path) -> None:
    budget = cloud.Budget(tmp_path, max_usd=0.01)
    with pytest.raises(SystemExit, match="cap"):
        budget.check(target(OVH), "S", "x" * 300_000)  # 100k tokens * 0.00000047 = 0.047


def test_the_cost_cap_lets_a_small_call_through(tmp_path: Path) -> None:
    estimate = cloud.Budget(tmp_path, max_usd=5.0).check(target(OVH), "S", "x" * 3000)
    assert 0 < estimate < 0.01


def test_a_managed_long_history_row_has_no_primary_call(tmp_path: Path) -> None:
    session = FakeSession(target(OVH))
    paths, inputs = start(tmp_path, session)
    longhist.run_longhist(
        None,  # type: ignore[arg-type]
        "",
        "",
        paths,
        inputs,
        llama.PlannerMode("planner", session.target.declared),
        sizes=(2000,),
        cloud=session,  # type: ignore[arg-type]
    )
    (row,) = read_jsonl(paths.longhist)
    assert {"cold", "extended"} <= set(row) and "primary_between" not in row
    assert len(session.calls) == 2  # a planner call, then the extended planner call


# ── The fingerprint ──────────────────────────────────────────────────────────


def test_the_managed_fingerprint_does_not_call_the_served_model_a_build(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    fp = fingerprint.build_cloud_fingerprint(target(OVH), inputs, paths, None)
    assert fp["engine"]["build"] == "managed (provider reports no build)"  # type: ignore[index]
    assert fp["engine"]["served_model"] == "unknown"  # type: ignore[index]
    assert fp["model"]["quant"] == "managed (not reported)"  # type: ignore[index]
    assert fp["planner_mode"] == {"name": "planner", "params": OVH_CANDIDATE}
    fingerprint.ensure_compatible(paths, fp)
    assert not score._fingerprint_check(json.loads(paths.fingerprint.read_text())).passed
    fingerprint.fill_served_model(paths, "served-model-1")
    stored = json.loads(paths.fingerprint.read_text())
    assert stored["engine"]["served_model"] == "served-model-1"
    assert score._fingerprint_check(stored).passed


def test_a_different_served_model_is_a_different_configuration(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    fp = fingerprint.build_cloud_fingerprint(target(OVH), inputs, paths, None)
    fingerprint.ensure_compatible(paths, fp)
    fingerprint.fill_served_model(paths, "served-model-1")
    fingerprint.fill_served_model(paths, "served-model-2")  # a known value is never overwritten
    assert json.loads(paths.fingerprint.read_text())["engine"]["served_model"] == "served-model-1"
    other = fingerprint.build_cloud_fingerprint(target(OVH), inputs, paths, None)
    other["engine"]["served_model"] = "served-model-2"  # type: ignore[index]
    with pytest.raises(SystemExit, match="different configuration"):
        fingerprint.ensure_compatible(paths, other)


def _catalog_with_planner(spec: Mapping[str, object]):  # type: ignore[no-untyped-def]
    from personal_agent.config import load_model_config
    from personal_agent.llm_client.models import ModeSpec

    config = load_model_config()
    definition = config.models[OVH]
    modes = {**definition.modes, "planner": ModeSpec.model_validate(dict(spec))}
    return config.model_copy(
        update={"models": {**config.models, OVH: definition.model_copy(update={"modes": modes})}}
    )


def test_a_candidate_run_and_the_landed_run_have_the_same_fingerprint(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    candidate = fingerprint.build_cloud_fingerprint(target(OVH), inputs, paths, None)
    landed_target = cloud.resolve_target(
        OVH, "planner", config=_catalog_with_planner(OVH_CANDIDATE)
    )
    landed = fingerprint.build_cloud_fingerprint(landed_target, inputs, paths, None)
    assert fingerprint._same_configuration(candidate, landed)


def test_a_landed_mode_that_differs_is_a_different_configuration(tmp_path: Path) -> None:
    paths, inputs = make_inputs(tmp_path)
    candidate = fingerprint.build_cloud_fingerprint(target(OVH), inputs, paths, None)
    differs = {"temperature": 1.0, "reasoning_effort": "low"}
    landed_target = cloud.resolve_target(OVH, "planner", config=_catalog_with_planner(differs))
    landed = fingerprint.build_cloud_fingerprint(landed_target, inputs, paths, None)
    assert not fingerprint._same_configuration(candidate, landed)


# ── Arguments ────────────────────────────────────────────────────────────────


def test_the_timing_arm_is_refused_for_a_managed_deployment(tmp_path: Path) -> None:
    argv = [
        "--run-dir",
        str(tmp_path),
        "--deployment",
        OVH,
        "--arms",
        "timing",
        "--mode",
        "default",
    ]
    with pytest.raises(SystemExit, match="timing"):
        replay.main(argv)


def test_a_managed_run_needs_a_cap(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="max-usd"):
        replay.main(["--run-dir", str(tmp_path), "--deployment", OVH, "--mode", "default"])


def test_a_managed_run_refuses_a_llama_mode(tmp_path: Path) -> None:
    argv = ["--run-dir", str(tmp_path), "--deployment", OVH, "--mode", "thinking_off"]
    with pytest.raises(SystemExit, match="planner or default"):
        replay.main([*argv, "--max-usd", "1"])


def test_the_llama_path_refuses_the_default_mode(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="--deployment"):
        replay.main(["--run-dir", str(tmp_path), "--mode", "default"])
