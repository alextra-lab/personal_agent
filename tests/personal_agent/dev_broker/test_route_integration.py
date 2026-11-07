# ruff: noqa: D102
"""The model route end to end on the test stack (FRE-1683 AC-1, AC-4, AC-6).

AC-1 and AC-6 drive ten parallel requests through the HTTP app, with the real ledger as the
broker's own role and a fake provider. AC-4 drives a call that hangs through the real
``LiteLLMClient`` and the real ``CostGate``: only ``litellm.acompletion`` is replaced.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import httpx
import pytest
import pytest_asyncio

from personal_agent.config import load_model_config, settings
from personal_agent.config.config_guard import repo_root
from personal_agent.cost_gate import (
    BudgetConfig,
    CapEntry,
    CostGate,
    OnDenialBehaviour,
    RoleConfig,
    load_budget_config,
    set_default_gate,
)
from personal_agent.dev_broker.config import load_broker_config
from personal_agent.dev_broker.dispatch import LiteLLMDispatcher, ModelRequest
from personal_agent.dev_broker.ledger import SpendLedger
from personal_agent.dev_broker.model_route import ModelRoute, build_model_app
from personal_agent.dev_broker.tokens import issue_job_token
from personal_agent.llm_client.pricing import register_model_pricing
from personal_agent.llm_client.types import LLMResponse
from personal_agent.telemetry.trace import TraceContext

from .conftest import delete_job_rows
from .fakes import FakeClock, FakeDispatcher, llm_response

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_MODEL = "deepseek-v4.1-flash-scaleway"
# A month in the far future keeps the month counter of these tests apart from real data.
_NOW = datetime(2911, 3, 10, 12, 0, tzinfo=timezone.utc)


class _Job:
    def __init__(self, job_id: UUID, trace_id: UUID, token: str) -> None:
        self.job_id = job_id
        self.trace_id = trace_id
        self.token = token


@pytest_asyncio.fixture
async def job(admin_pool: asyncpg.Pool) -> AsyncIterator[_Job]:
    job_id, trace_id = uuid4(), uuid4()
    token = await issue_job_token(
        admin_pool,
        job_id=job_id,
        model_key=_MODEL,
        trace_id=trace_id,
        session_id=uuid4(),
        expires_at=_NOW + timedelta(days=1),
    )
    try:
        yield _Job(job_id, trace_id, token)
    finally:
        await delete_job_rows(admin_pool, job_id)
        await admin_pool.execute(
            "DELETE FROM dev_month_spend WHERE month_start = $1", _NOW.date().replace(day=1)
        )


def _request(token: str) -> tuple[bytes, dict[str, str]]:
    body = {"model": _MODEL, "messages": [{"role": "user", "content": "fix the page"}]}
    return json.dumps(body).encode(), {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


async def test_ten_parallel_calls_never_pass_the_job_cap(
    broker_pool: asyncpg.Pool, job: _Job
) -> None:
    """AC-1 and AC-6: the cap is the EUR 0.05 equivalent, and each call has one ledger row."""
    cap = Decimal("0.05") * Decimal("1.18")

    async def provider(_r: ModelRequest, _t: TraceContext) -> LLMResponse:
        await asyncio.sleep(0.05)  # keep the calls in flight together
        return llm_response(cost_usd=0.0002)

    dispatcher = FakeDispatcher(provider)
    route = ModelRoute(
        config=load_broker_config(),
        ledger=SpendLedger(broker_pool, job_cap_usd=cap, month_cap_usd=Decimal("35.40")),
        dispatcher=dispatcher,
        catalog=load_model_config().models,
        clock=FakeClock(_NOW),
    )
    content, headers = _request(job.token)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_model_app(route)), base_url="http://broker"
    ) as client:
        responses = await asyncio.gather(
            *(
                client.post("/v1/chat/completions", content=content, headers=headers)
                for _ in range(10)
            )
        )

    statuses = sorted(r.status_code for r in responses)
    admitted = statuses.count(200)
    assert admitted >= 1
    # A refusal while the admitted calls are in flight is temporary (429 cap_busy).
    assert set(statuses) <= {200, 429}
    rows = await broker_pool.fetch(
        "SELECT reserved_usd, status FROM dev_job_calls WHERE job_id = $1", job.job_id
    )
    reserved_total = sum((row["reserved_usd"] for row in rows), Decimal(0))
    assert reserved_total <= cap, f"admitted reservations {reserved_total} pass the cap {cap}"
    # AC-6: one row per dispatched call, and no row for a refused one.
    assert len(dispatcher.calls) == admitted == len(rows)
    assert {row["status"] for row in rows} == {"settled"}
    token = await broker_pool.fetchrow(
        "SELECT status, stop_reason FROM dev_job_tokens WHERE job_id = $1", job.job_id
    )
    # The admitted calls settled at a fraction of their worst case, so the job is not done.
    assert (token["status"], token["stop_reason"]) == ("active", None)


@pytest_asyncio.fixture
async def dev_agent_gate() -> AsyncIterator[CostGate]:
    """A real cost gate on the test stack with the dev_agent role, set as the process gate."""
    config = BudgetConfig(
        version=1,
        roles={
            "dev_agent": RoleConfig(
                default_output_tokens=8192, safety_factor=1.0, on_denial=OnDenialBehaviour.RAISE
            )
        },
        caps=[CapEntry(time_window="daily", role="dev_agent", cap_usd=Decimal("50"))],
    )
    gate = CostGate(config=config, db_url=settings.database_url)
    await gate.connect()
    set_default_gate(gate)
    try:
        yield gate
    finally:
        set_default_gate(None)
        await gate.disconnect()


async def test_a_call_held_past_the_limit_settles_before_the_gate_ttl(
    broker_pool: asyncpg.Pool,
    admin_pool: asyncpg.Pool,
    job: _Job,
    dev_agent_gate: CostGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-4: the call is ended at the limit; the dev_agent reservation never reaches expired."""

    async def hang(**_kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(settings, "scaleway_secret_key", "test-key")
    # CI has no budget.yaml (it is gitignored); respond() reads the role's estimate from it.
    example = repo_root() / "config" / "governance" / "budget.yaml.example"
    monkeypatch.setattr(
        "personal_agent.cost_gate.load_budget_config", lambda: load_budget_config(example)
    )
    register_model_pricing(load_model_config())
    config = load_broker_config().model_copy(update={"call_timeout_seconds": 0.5})
    route = ModelRoute(
        config=config,
        ledger=SpendLedger(broker_pool, job_cap_usd=Decimal("2.36"), month_cap_usd=Decimal("35.4")),
        dispatcher=LiteLLMDispatcher([_MODEL]),
        catalog=load_model_config().models,
        clock=FakeClock(_NOW),
    )
    content, headers = _request(job.token)
    try:
        with patch("litellm.acompletion", side_effect=hang):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=build_model_app(route)),
                base_url="http://broker",
            ) as client:
                response = await client.post(
                    "/v1/chat/completions", content=content, headers=headers
                )

        assert response.status_code == 504
        reservations = await admin_pool.fetch(
            "SELECT role, status, amount_usd, actual_cost_usd FROM budget_reservations "
            "WHERE trace_id = $1",
            job.trace_id,
        )
        assert [r["role"] for r in reservations] == ["dev_agent"]
        [reservation] = reservations
        # Settled at the estimate, now, by the cancellation branch: not active, not expired.
        assert reservation["status"] == "committed"
        assert reservation["actual_cost_usd"] == reservation["amount_usd"]
        call = await broker_pool.fetchrow(
            "SELECT status, reserved_usd, settled_usd FROM dev_job_calls WHERE job_id = $1",
            job.job_id,
        )
        assert call["status"] == "timed_out"
        assert call["settled_usd"] == call["reserved_usd"]
    finally:
        await admin_pool.execute(
            "DELETE FROM budget_reservations WHERE trace_id = $1", job.trace_id
        )
        await admin_pool.execute("DELETE FROM budget_counters WHERE role = 'dev_agent'")
