"""LiteLLMClient + CostGate integration test (FRE-305).

Verifies the call sequence in ``LiteLLMClient.respond``:

- success path: ``reserve → litellm.acompletion → commit(actual_cost)``
- failure path: ``reserve → litellm.acompletion(raises) → refund``
- denied path: ``reserve(raises BudgetDenied) → litellm.acompletion never called``

Uses a real ``CostGate`` registered against the live Postgres so the
``budget_reservations`` row transitions are actually exercised, but mocks
the litellm + budget-config touchpoints so this stays a fast integration
test (no API calls, no YAML on disk).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import ExitStack
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.config import settings
from personal_agent.cost_gate import (
    BudgetConfig,
    BudgetDenied,
    CapEntry,
    CostGate,
    OnDenialBehaviour,
    ReservationStatus,
    RoleConfig,
    set_default_gate,
)
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn
from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.types import LLMClientError, ModelRole
from tests._helpers.trace import make_test_ctx

pytestmark = pytest.mark.integration


def _build_test_config(role: str, daily_cap: Decimal = Decimal("1.00")) -> BudgetConfig:
    return BudgetConfig(
        version=1,
        roles={
            role: RoleConfig(
                default_output_tokens=128,
                safety_factor=1.2,
                on_denial=OnDenialBehaviour.RAISE,
            ),
        },
        caps=[
            CapEntry(time_window="daily", role=role, cap_usd=daily_cap),
            CapEntry(time_window="weekly", role="_total", cap_usd=Decimal("100.00")),
        ],
    )


@pytest_asyncio.fixture
async def gate_for_role() -> AsyncIterator[tuple[CostGate, str]]:
    """Connected CostGate registered as the default; unique role per test."""
    role = f"test_lc_{uuid4().hex[:8]}"
    config = _build_test_config(role)
    gate = CostGate(config=config, db_url=settings.database_url)
    await gate.connect()
    set_default_gate(gate)
    try:
        yield gate, role
    finally:
        set_default_gate(None)
        await gate.disconnect()


@pytest_asyncio.fixture
async def cleanup_pool() -> AsyncIterator[asyncpg.Pool]:
    """Pool used by tests + the post-test counter cleanup."""
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.database_url),
        min_size=1,
        max_size=2,
        command_timeout=10,
    )
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_after(gate_for_role: tuple[CostGate, str]) -> AsyncIterator[None]:
    """Drop test_* counter / reservation rows + revert _total weekly delta."""
    _, role = gate_for_role
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.database_url), min_size=1, max_size=1
    )
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT running_total FROM budget_counters
                 WHERE user_id IS NULL AND time_window = 'weekly'
                   AND provider IS NULL AND role = '_total'
                """
            )
            pre_total = row["running_total"] if row else None

        yield

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM budget_reservations WHERE role = $1", role)
            await conn.execute("DELETE FROM budget_counters WHERE role = $1", role)
            if pre_total is not None:
                await conn.execute(
                    """
                    UPDATE budget_counters SET running_total = $1, updated_at = NOW()
                     WHERE user_id IS NULL AND time_window = 'weekly'
                       AND provider IS NULL AND role = '_total'
                    """,
                    pre_total,
                )
    finally:
        await pool.close()


def _fake_completion_response(content: str = "ok", cost: float = 0.04) -> SimpleNamespace:
    """Build a minimal litellm-shaped response for the mock."""
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30)
    return SimpleNamespace(choices=[choice], usage=usage, id="resp_test_123", model="m")


def _patched(
    gate_for_role: tuple[CostGate, str],
    *,
    acompletion: AsyncMock,
    cost_value: float = 0.04,
) -> ExitStack:
    """Enter all the litellm + budget-config patches the test needs.

    Returns an ExitStack the caller uses as a ``with`` block; on exit the
    stack rolls every patch back in LIFO order.
    """
    _, role = gate_for_role
    config = _build_test_config(role)
    stack = ExitStack()
    stack.enter_context(
        patch("personal_agent.llm_client.litellm_client.litellm.acompletion", new=acompletion)
    )
    stack.enter_context(
        patch(
            "personal_agent.llm_client.litellm_client.litellm.completion_cost",
            return_value=cost_value,
        )
    )
    stack.enter_context(
        patch(
            "personal_agent.llm_client.cost_estimator.estimate_reservation_for_call",
            return_value=Decimal("0.30"),
        )
    )
    stack.enter_context(
        patch("personal_agent.cost_gate.policy.load_budget_config", return_value=config)
    )
    stack.enter_context(patch("personal_agent.cost_gate.load_budget_config", return_value=config))
    return stack


@pytest.mark.asyncio
async def test_success_path_reserve_then_commit_with_actual_cost(
    gate_for_role: tuple[CostGate, str], cleanup_pool: asyncpg.Pool
) -> None:
    """A successful call leaves a 'committed' reservation row at actual_cost."""
    _, role = gate_for_role
    fake_acompletion = AsyncMock(return_value=_fake_completion_response(cost=0.04))

    client = LiteLLMClient(
        model_id="claude-sonnet-4-6",
        provider="anthropic",
        max_tokens=128,
        budget_role=role,
    )

    with _patched(gate_for_role, acompletion=fake_acompletion, cost_value=0.04):
        await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "hi"}],
            trace_ctx=make_test_ctx("gate_wiring_success"),
        )

    # Inspect the DB: there should be exactly one reservation for this role,
    # status='committed', actual_cost_usd=0.04.
    async with cleanup_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status, actual_cost_usd, amount_usd FROM budget_reservations WHERE role = $1",
            role,
        )
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == ReservationStatus.COMMITTED.value
    assert r["actual_cost_usd"] == Decimal("0.040000")
    assert r["amount_usd"] == Decimal("0.300000")


@pytest.mark.asyncio
async def test_failure_path_reserve_then_refund(
    gate_for_role: tuple[CostGate, str], cleanup_pool: asyncpg.Pool
) -> None:
    """A litellm failure refunds the reservation; counter returns to baseline."""
    _, role = gate_for_role
    fake_acompletion = AsyncMock(side_effect=RuntimeError("boom"))

    client = LiteLLMClient(
        model_id="claude-sonnet-4-6",
        provider="anthropic",
        max_tokens=128,
        budget_role=role,
    )

    with _patched(gate_for_role, acompletion=fake_acompletion):
        with pytest.raises(LLMClientError, match="LiteLLM call failed"):
            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=make_test_ctx("gate_wiring_failure"),
            )

    async with cleanup_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status FROM budget_reservations WHERE role = $1",
            role,
        )
    assert len(rows) == 1
    assert rows[0]["status"] == ReservationStatus.REFUNDED.value


@pytest.mark.asyncio
async def test_denied_path_does_not_call_litellm(
    gate_for_role: tuple[CostGate, str], cleanup_pool: asyncpg.Pool
) -> None:
    """When the gate denies, litellm.acompletion is never reached."""
    _, role = gate_for_role
    fake_acompletion = AsyncMock(return_value=_fake_completion_response())

    client = LiteLLMClient(
        model_id="claude-sonnet-4-6",
        provider="anthropic",
        max_tokens=128,
        budget_role=role,
    )

    # Pre-seed the daily counter to its cap so the next reserve() denies.
    async with cleanup_pool.acquire() as conn:
        # Upsert the daily row at-cap so reserve() will deny.
        await conn.execute(
            """
            INSERT INTO budget_counters
                (user_id, time_window, provider, role, window_start, running_total, updated_at)
            VALUES
                (NULL, 'daily', NULL, $1, date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC',
                 1.00, NOW())
            ON CONFLICT (user_id, time_window, provider, role, window_start) DO UPDATE SET running_total = 1.00
            """,
            role,
        )

    with _patched(gate_for_role, acompletion=fake_acompletion):
        with pytest.raises(BudgetDenied):
            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=make_test_ctx("gate_wiring_denied"),
            )

    fake_acompletion.assert_not_called()


@pytest_asyncio.fixture
async def real_gate() -> AsyncIterator[CostGate]:
    """A connected CostGate against the REAL config/governance/budget.yaml.

    Unlike ``gate_for_role`` (a synthetic per-test role + config), this exercises the
    actually-declared ``artifact_builder`` / ``main_inference`` policies so a regression that
    removes either role's YAML declaration fails this test too (ticket AC-2, ADR-0118 T1 /
    FRE-879).
    """
    from personal_agent.cost_gate import load_budget_config

    gate = CostGate(config=load_budget_config(), db_url=settings.database_url)
    await gate.connect()
    set_default_gate(gate)
    try:
        yield gate
    finally:
        set_default_gate(None)
        await gate.disconnect()


async def _daily_counter_total(pool: asyncpg.Pool, role: str) -> Decimal:
    from personal_agent.cost_gate.gate import _window_start

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT running_total FROM budget_counters
             WHERE user_id IS NULL AND time_window = 'daily'
               AND provider IS NULL AND role = $1
               AND window_start = $2
            """,
            role,
            _window_start("daily"),
        )
        return Decimal(row["running_total"]) if row else Decimal("0")


@pytest.mark.asyncio
async def test_artifact_builder_lane_isolated_from_main_inference(
    real_gate: CostGate, cleanup_pool: asyncpg.Pool
) -> None:
    """Ticket AC-2 (ADR-0118 T1, FRE-879): a real artifact-builder cost-gate call.

    Debits its own lane and leaves main_inference untouched — proving the role was truly
    extracted, not just renamed. Uses get_llm_client_for_key directly (not the retired
    matrix resolution — artifact_builder is ExecutionProfile-resolved now, see
    test_factory_artifact_builder.py for that seam; this test only cares about cost-lane
    isolation).
    """
    from personal_agent.config import load_model_config
    from personal_agent.llm_client.factory import get_llm_client_for_key

    # Snapshot real (possibly pre-existing) daily counters before this test's call.
    artifact_before = await _daily_counter_total(cleanup_pool, "artifact_builder")
    main_inference_before = await _daily_counter_total(cleanup_pool, "main_inference")
    async with cleanup_pool.acquire() as conn:
        total_row = await conn.fetchrow(
            """
            SELECT running_total FROM budget_counters
             WHERE user_id IS NULL AND time_window = 'weekly'
               AND provider IS NULL AND role = '_total'
            """
        )
        total_weekly_before: Decimal | None = total_row["running_total"] if total_row else None

    builder_key = "claude_haiku"
    model_def = load_model_config().models[builder_key]
    assert model_def.id  # sanity: a real model definition backs the artifact-builder lane

    client = get_llm_client_for_key(builder_key, budget_role="artifact_builder")
    assert isinstance(client, LiteLLMClient)
    assert client.budget_role == "artifact_builder"

    fake_acompletion = AsyncMock(return_value=_fake_completion_response(cost=0.01))
    ctx = make_test_ctx("gate_wiring_artifact_builder")
    with (
        patch("personal_agent.llm_client.litellm_client.litellm.acompletion", new=fake_acompletion),
        patch(
            "personal_agent.llm_client.litellm_client.litellm.completion_cost",
            return_value=0.01,
        ),
        patch(
            "personal_agent.llm_client.cost_estimator.estimate_reservation_for_call",
            return_value=Decimal("0.02"),
        ),
    ):
        await client.respond(
            role=ModelRole.ARTIFACT_BUILDER,
            messages=[{"role": "user", "content": "hi"}],
            trace_ctx=ctx,
        )

    try:
        artifact_after = await _daily_counter_total(cleanup_pool, "artifact_builder")
        main_inference_after = await _daily_counter_total(cleanup_pool, "main_inference")

        assert artifact_after - artifact_before == Decimal("0.010000")
        assert main_inference_after == main_inference_before

        async with cleanup_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, status, actual_cost_usd FROM budget_reservations WHERE trace_id = $1",
                ctx.trace_id,
            )
        assert len(rows) == 1
        assert rows[0]["role"] == "artifact_builder"
        assert rows[0]["status"] == ReservationStatus.COMMITTED.value
        assert rows[0]["actual_cost_usd"] == Decimal("0.010000")
    finally:
        # Snapshot-and-restore only the delta this test introduced — never a
        # delete-by-role, which would destroy other legitimate shared state
        # on these real (not synthetic) role rows.
        async with cleanup_pool.acquire() as conn:
            await conn.execute("DELETE FROM budget_reservations WHERE trace_id = $1", ctx.trace_id)
            if artifact_before == Decimal("0"):
                await conn.execute(
                    """
                    DELETE FROM budget_counters
                     WHERE user_id IS NULL AND time_window = 'daily'
                       AND provider IS NULL AND role = 'artifact_builder'
                    """
                )
            else:
                await conn.execute(
                    """
                    UPDATE budget_counters SET running_total = $1, updated_at = NOW()
                     WHERE user_id IS NULL AND time_window = 'daily'
                       AND provider IS NULL AND role = 'artifact_builder'
                    """,
                    artifact_before,
                )
            if total_weekly_before is not None:
                await conn.execute(
                    """
                    UPDATE budget_counters SET running_total = $1, updated_at = NOW()
                     WHERE user_id IS NULL AND time_window = 'weekly'
                       AND provider IS NULL AND role = '_total'
                    """,
                    total_weekly_before,
                )
