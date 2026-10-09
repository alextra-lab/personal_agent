"""FRE-1548 — the planner request on ``claude_sonnet`` carries a JSON schema.

Every wire test drives the planner through the **real** client for the deployment, entering at
``ExpansionController.execute`` the way the executor does. Only ``litellm.acompletion`` (or the local
transport) is replaced, so the request that is read is the one the client builds.

- AC-1: the schema reaches ``litellm.acompletion`` and the Anthropic HTTP body.
- AC-2: the local binding's planner request is the request that main sends.
- AC-3: a thinking block on a managed deployment shows in ``planner_reasoning_chars``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import jsonschema
import litellm
import pytest
from scripts.eval.fre1537.cloud import row_from_response

from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.types import ModelRole
from personal_agent.orchestrator import expansion_controller as ec
from personal_agent.orchestrator.expansion_controller import (
    ExpansionController,
    _validate_plan_json,
    planner_plan_schema,
    planner_response_format,
)
from personal_agent.orchestrator.expansion_types import ExpansionPlan
from personal_agent.orchestrator.sub_agent_types import SubAgentResult
from personal_agent.orchestrator.worker_types import THOROUGHNESS_LEVELS, WORKER_TYPES
from tests._helpers.litellm_capability import pinned_litellm_capabilities

BARE_JSON_OBJECT = {"type": "json_object"}

FULL_PLAN = {
    "strategy": "DECOMPOSE",
    "tasks": [
        {
            "name": "find_sources",
            "goal": "Find two sources on the topic",
            "constraints": ["Use euros", "Return two items"],
            "type": "general",
            "thoroughness": "quick",
        },
        {"name": "compare", "goal": "Compare the sources", "type": "general"},
    ],
    "memory_relevance": "used",
}


@pytest.fixture(autouse=True)
def _pin_capabilities() -> Iterator[None]:
    """Litellm's capability map must not decide these results (see the FRE-1007 tests)."""
    with pinned_litellm_capabilities():
        yield


def _walk_objects(node: object) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from _walk_objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_objects(value)


class TestSchemaMatchesTheValidator:
    """The schema is the plan shape the validator already checks, not a second contract."""

    def test_the_enums_come_from_the_registries_the_prompt_uses(self) -> None:
        item = planner_plan_schema()["properties"]["tasks"]["items"]["properties"]
        assert item["type"]["enum"] == [t.value for t in WORKER_TYPES]
        assert item["thoroughness"]["enum"] == list(THOROUGHNESS_LEVELS)

    def test_a_plan_the_validator_accepts_satisfies_the_schema(self) -> None:
        assert _validate_plan_json(json.dumps(FULL_PLAN), "DECOMPOSE") is not None
        jsonschema.validate(FULL_PLAN, planner_plan_schema())

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p["tasks"][0].update(type="oracle"),
            lambda p: p["tasks"][0].update(thoroughness="exhaustive"),
            lambda p: p["tasks"][0].pop("goal"),
            lambda p: p["tasks"][0].pop("name"),
            lambda p: p.pop("tasks"),
        ],
    )
    def test_a_plan_the_validator_rejects_violates_the_schema(self, mutate: Any) -> None:
        plan = json.loads(json.dumps(FULL_PLAN))
        mutate(plan)
        assert _validate_plan_json(json.dumps(plan), "DECOMPOSE") is None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(plan, planner_plan_schema())

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p.update(strategy="PARALLEL"),
            lambda p: p.update(memory_relevance="maybe"),
        ],
    )
    def test_the_schema_is_stricter_than_the_validator_on_two_enums(self, mutate: Any) -> None:
        """The validator tolerates both values (it falls back). The schema never emits them."""
        plan = json.loads(json.dumps(FULL_PLAN))
        mutate(plan)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(plan, planner_plan_schema())

    def test_single_is_admitted_only_when_asked(self) -> None:
        decline = {"strategy": "SINGLE", "tasks": []}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(decline, planner_plan_schema())
        jsonschema.validate(decline, planner_plan_schema(admit_single=True))

    def test_every_object_forbids_extra_keys(self) -> None:
        """Anthropic's structured output refuses an object that allows them."""
        objects = list(_walk_objects(planner_plan_schema()))
        assert len(objects) == 2
        assert all(o.get("additionalProperties") is False for o in objects)


class TestRequestFormat:
    def test_anthropic_gets_a_json_schema_request(self) -> None:
        fmt = planner_response_format("anthropic")
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["schema"] == planner_plan_schema()

    @pytest.mark.parametrize("provider", ["slm_local", "ovhcloud", "openai", None])
    def test_every_other_provider_keeps_the_bare_request(self, provider: str | None) -> None:
        assert planner_response_format(provider) == BARE_JSON_OBJECT
        assert planner_response_format(provider, admit_single=True) == BARE_JSON_OBJECT


# ── The planner through the real client ───────────────────────────────────


def _model_response(
    content: str,
    *,
    thinking_blocks: list[dict[str, Any]] | None = None,
    reasoning_tokens: int | None = None,
) -> litellm.ModelResponse:
    message = litellm.Message(role="assistant", content=content, thinking_blocks=thinking_blocks)
    usage = litellm.Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    if reasoning_tokens is not None:
        usage.completion_tokens_details = litellm.types.utils.CompletionTokensDetailsWrapper(
            reasoning_tokens=reasoning_tokens
        )
    return litellm.ModelResponse(
        id="resp_fre1548",
        choices=[litellm.Choices(finish_reason="stop", index=0, message=message)],
        usage=usage,
    )


def _sub_agent_result() -> SubAgentResult:
    return SubAgentResult(
        task_id=MagicMock(),
        spec_task="task_0",
        summary="ok",
        full_output="ok",
        tools_used=[],
        token_count=1,
        duration_ms=1,
        success=True,
        error=None,
        cost_usd=0.0,
    )


async def _run_planner_through(
    client: LiteLLMClient, *, caplog: pytest.LogCaptureFixture | None = None
) -> ExpansionPlan:
    """Run the planner phase the way the executor does, with the sub-agent runner stubbed."""
    if caplog is not None:
        caplog.set_level("INFO", logger="personal_agent.orchestrator.expansion_controller")
    with patch.object(ec, "run_sub_agent", side_effect=[_sub_agent_result()] * 3):
        result = await ExpansionController().execute(
            query="Compare two sources",
            strategy="HYBRID",
            llm_client=client,
            planner_llm_client=client,
            trace_id=str(uuid4()),
            messages=[{"role": "user", "content": "Compare two sources"}],
        )
    assert result.plan is not None
    return result.plan


def _cloud_patches(fake_acompletion: Any | None = None) -> list[Any]:
    gate = MagicMock()
    gate.reserve = AsyncMock(return_value="res-fre1548")
    gate.commit = AsyncMock()
    tracker = AsyncMock()
    tracker.connect = AsyncMock()
    tracker.record_api_call = AsyncMock()
    acompletion = (
        [patch("litellm.acompletion", side_effect=fake_acompletion)] if fake_acompletion else []
    )
    return [
        *acompletion,
        patch("litellm.completion_cost", return_value=0.001),
        patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
        patch("personal_agent.cost_gate.load_budget_config", return_value=MagicMock()),
        patch(
            "personal_agent.llm_client.cost_estimator.estimate_reservation_for_call",
            return_value=Decimal("0.01"),
        ),
        patch(
            "personal_agent.llm_client.history_sanitiser.sanitise_messages",
            side_effect=lambda msgs, trace_id: (msgs, []),
        ),
        patch(
            "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
            return_value=tracker,
        ),
        patch(
            "personal_agent.config.settings.get_settings",
            return_value=MagicMock(anthropic_api_key="k", openai_api_key="k"),
        ),
    ]


async def _planner_on_claude_sonnet(
    response: litellm.ModelResponse, *, caplog: pytest.LogCaptureFixture | None = None
) -> dict[str, Any]:
    """Run the planner on the real ``claude_sonnet`` client. Return litellm's kwargs."""
    from contextlib import ExitStack

    from personal_agent.llm_client.factory import get_llm_client_for_key

    captured: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs: Any) -> litellm.ModelResponse:
        captured.update(kwargs)
        return response

    client = get_llm_client_for_key("claude_sonnet", budget_role="main_inference")
    with ExitStack() as stack:
        for p in _cloud_patches(_fake_acompletion):
            stack.enter_context(p)
        await _run_planner_through(client, caplog=caplog)
    return captured


async def _planner_http_request_on_claude_sonnet() -> tuple[dict[str, Any], dict[str, str]]:
    """Run the planner on the real client with only the HTTP transport replaced.

    Returns:
        The JSON body and the headers that the Anthropic handler posts.
    """
    from contextlib import ExitStack

    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    from personal_agent.llm_client.factory import get_llm_client_for_key

    seen: dict[str, Any] = {}

    async def _post(self: Any, url: str, headers: Any = None, **kwargs: Any) -> Any:
        # litellm posts the body as ``json=`` on this route and as ``data=`` on others.
        body = kwargs.get("json")
        seen["body"] = body if body is not None else json.loads(kwargs["data"])
        seen["headers"] = dict(headers or {})
        reply = {
            "id": "msg_fre1548",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": json.dumps(FULL_PLAN)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        return httpx.Response(200, json=reply, request=httpx.Request("POST", url))

    client = get_llm_client_for_key("claude_sonnet", budget_role="main_inference")
    with ExitStack() as stack:
        for p in _cloud_patches():
            stack.enter_context(p)
        stack.enter_context(patch.object(AsyncHTTPHandler, "post", _post))
        await _run_planner_through(client)
    return seen["body"], seen["headers"]


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    return [r.msg for r in caplog.records if isinstance(r.msg, dict) and r.msg.get("event") == name]


class TestTheSchemaReachesTheWire:
    """AC-1 — fails if the request is still a bare json_object."""

    @pytest.mark.asyncio
    async def test_the_claude_sonnet_planner_call_carries_the_plan_schema(self) -> None:
        kwargs = await _planner_on_claude_sonnet(_model_response(json.dumps(FULL_PLAN)))

        sent = kwargs["response_format"]
        assert sent != BARE_JSON_OBJECT
        assert sent["type"] == "json_schema"
        assert sent["json_schema"]["schema"] == planner_plan_schema()

    @pytest.mark.asyncio
    async def test_the_http_body_carries_the_providers_native_output_format(self) -> None:
        """Reaching ``acompletion`` is not enough: litellm sends nothing for a bare json_object."""
        body, headers = await _planner_http_request_on_claude_sonnet()

        assert body["output_format"]["type"] == "json_schema"
        assert body["output_format"]["schema"]["required"] == ["strategy", "tasks"]
        assert body["output_format"]["schema"]["additionalProperties"] is False
        assert "structured-outputs" in headers["anthropic-beta"]


class TestTheLocalRequestIsUnchanged:
    """AC-2 — qwen3.8-flash-next qualified on the request as it ships on main."""

    @pytest.mark.asyncio
    async def test_the_local_planner_call_sends_the_bare_json_object(self) -> None:
        from personal_agent.llm_client.factory import get_llm_client_for_key

        client = get_llm_client_for_key("qwen3.8-flash-next", budget_role="main_inference")
        local_response = {
            "role": "assistant",
            "content": json.dumps(FULL_PLAN),
            "tool_calls": [],
            "reasoning_trace": None,
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "response_id": None,
            "cost_usd": 0.0,
            "finish_reason": "stop",
            "refusal": None,
            "raw": {},
        }
        with patch.object(
            LiteLLMClient, "_respond_local", new=AsyncMock(return_value=local_response)
        ) as local:
            await _run_planner_through(client)

        planner_call = local.await_args_list[0].kwargs
        assert planner_call["response_format"] == BARE_JSON_OBJECT
        assert json.dumps(planner_call["response_format"]) == '{"type": "json_object"}'
        assert planner_call["role"] is ModelRole.PRIMARY


class TestReasoningIsVisibleOnAManagedDeployment:
    """AC-3 — fails if a thinking block still reads 0."""

    @pytest.mark.asyncio
    async def test_a_thinking_block_shows_in_the_planner_events(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        block = {"type": "thinking", "thinking": "t" * 40, "signature": "sig"}
        response = _model_response(
            json.dumps(FULL_PLAN), thinking_blocks=[block], reasoning_tokens=7
        )

        await _planner_on_claude_sonnet(response, caplog=caplog)

        (completed,) = _events(caplog, "planner_completed")
        (outcome,) = _events(caplog, "planner_outcome")
        assert completed["planner_reasoning_chars"] == 40
        assert completed["planner_thinking_blocks"] == 1
        assert completed["planner_reasoning_tokens"] == 7
        assert outcome["planner_reasoning_chars"] == 40

    @pytest.mark.asyncio
    async def test_no_thinking_block_still_reads_zero(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        await _planner_on_claude_sonnet(_model_response(json.dumps(FULL_PLAN)), caplog=caplog)

        (completed,) = _events(caplog, "planner_completed")
        assert completed["planner_reasoning_chars"] == 0
        assert completed["planner_thinking_blocks"] == 0

    def test_a_redacted_block_counts_its_payload(self) -> None:
        raw = {
            "choices": [
                {"message": {"thinking_blocks": [{"type": "redacted_thinking", "data": "x" * 25}]}}
            ]
        }
        reasoning = ec._planner_reasoning({"content": "{}", "reasoning_trace": None, "raw": raw})
        assert (reasoning.chars, reasoning.thinking_blocks) == (25, 1)

    def test_the_provider_reasoning_content_counts(self) -> None:
        raw = {"choices": [{"message": {"reasoning_content": "r" * 12}}]}
        assert ec._planner_reasoning({"raw": raw}).chars == 12

    def test_the_larger_of_the_trace_and_the_provider_evidence_wins(self) -> None:
        raw = {"choices": [{"message": {"reasoning_content": "r" * 12}}]}
        assert ec._planner_reasoning({"reasoning_trace": "q" * 120, "raw": raw}).chars == 120

    @pytest.mark.parametrize("raw", [None, {}, {"choices": []}, {"choices": [None]}, "text", 5])
    def test_a_response_without_a_readable_provider_message_reads_zero(self, raw: object) -> None:
        reasoning = ec._planner_reasoning({"content": "{}", "reasoning_trace": None, "raw": raw})
        assert (reasoning.chars, reasoning.thinking_blocks, reasoning.tokens) == (0, 0, None)

    def test_the_reasoning_tokens_are_read_from_usage(self) -> None:
        reasoning = ec._planner_reasoning({"usage": {"reasoning_tokens": 9}})
        assert reasoning.tokens == 9

    def test_production_and_the_probe_read_the_same_number(self) -> None:
        """The probe is the instrument that qualifies the deployment; production must agree."""
        block = {"type": "thinking", "thinking": "t" * 33, "signature": "sig"}
        response = _model_response(json.dumps(FULL_PLAN), thinking_blocks=[block]).model_dump()
        llm_response = {"content": json.dumps(FULL_PLAN), "reasoning_trace": None, "raw": response}

        probe_row = row_from_response(llm_response, 1.0)

        assert ec._planner_reasoning(llm_response).chars == probe_row["reasoning_chars"] == 33
