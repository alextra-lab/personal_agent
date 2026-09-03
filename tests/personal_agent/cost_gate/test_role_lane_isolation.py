"""FRE-989 AC-4: skill_routing and study bill their OWN lanes, proved on counters.

The ticket's wording is "a call made through the role-name path for
skill_routing or study is charged to that role's budget and denied by that
role's cap, verified against the counters rather than by reading the code."

Two things about that, both established by this module:

**The role-name path cannot make a paid call for either role** (finding six).
Neither has a Layer-3 binding in ``config/model_roles.yaml``, so
``resolve_role_target`` falls back to treating the role name as a deployment
key and finds no definition. Since ADR-0141 D1 ``_build_client`` raises there
rather than returning a key-ignoring client.
``test_role_name_path_for_skill_routing_refuses_to_build_a_client`` pins that,
so nobody assumes the ticket's stated path is paid.

**So the criterion is proved against the path each role actually uses**:
key-based acquisition for ``skill_routing`` (``executor.py``'s skill router) and
direct construction for ``study`` (``scripts/study/categorizer.py``). Both run a
real ``respond()`` against a real ``CostGate`` on the real Postgres, with the
provider mocked at the ``litellm.acompletion`` boundary only — so a regression
anywhere between the door and the counter fails this test.

The REAL ``budget.yaml`` is used, not a synthetic one: the point is that
``skill_routing``'s $0.10 daily and ``study``'s $5.00 daily isolation actually
apply, and a fixture config would prove nothing about the shipped caps.
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
    BudgetDenied,
    CostGate,
    load_budget_config,
    set_default_gate,
)
from personal_agent.cost_gate.gate import _window_start
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn
from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.types import ModelRole
from personal_agent.telemetry.trace import SystemTraceContext
from tests._helpers.trace import make_test_ctx

pytestmark = pytest.mark.integration

# Every lane this module reads or could move. Snapshotted and restored so a run
# against the shared test substrate leaves the real counters as it found them.
_TOUCHED_ROLES = ("skill_routing", "study", "main_inference", "_total")


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.database_url), min_size=1, max_size=3, command_timeout=10
    )
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def real_gate() -> AsyncIterator[CostGate]:
    """A gate over the REAL budget config, registered as the process default."""
    gate = CostGate(config=load_budget_config(), db_url=settings.database_url)
    await gate.connect()
    set_default_gate(gate)
    try:
        yield gate
    finally:
        set_default_gate(None)
        await gate.disconnect()


@pytest_asyncio.fixture(autouse=True)
async def restore_counters(pool: asyncpg.Pool) -> AsyncIterator[None]:
    """Snapshot every touched counter and put it back afterwards."""
    async with pool.acquire() as conn:
        before = await conn.fetch(
            """
            SELECT id, running_total FROM budget_counters
             WHERE role = ANY($1::text[]) AND user_id IS NULL AND provider IS NULL
            """,
            list(_TOUCHED_ROLES),
        )
        seen_ids = {row["id"]: row["running_total"] for row in before}

    yield

    async with pool.acquire() as conn:
        for counter_id, total in seen_ids.items():
            await conn.execute(
                "UPDATE budget_counters SET running_total = $1 WHERE id = $2", total, counter_id
            )
        # Rows this test created (roles with no counter before it ran).
        await conn.execute(
            """
            DELETE FROM budget_counters
             WHERE role = ANY($1::text[]) AND user_id IS NULL AND provider IS NULL
               AND NOT (id = ANY($2::bigint[]))
            """,
            list(_TOUCHED_ROLES),
            list(seen_ids) or [0],
        )


async def _counter(pool: asyncpg.Pool, role: str, window: str = "daily") -> Decimal:
    """Current-window running total for a role — zero when there is no row.

    Constrained to the CURRENT window on purpose: a query that omits
    ``window_start`` happily returns a closed window's total, which is how a
    stale figure was once reported as today's spend.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT running_total FROM budget_counters
             WHERE user_id IS NULL AND provider IS NULL
               AND role = $1 AND time_window = $2 AND window_start = $3
            """,
            role,
            window,
            _window_start(window),
        )
    return Decimal(row["running_total"]) if row else Decimal("0")


async def _preload(pool: asyncpg.Pool, role: str, amount: Decimal, window: str = "daily") -> None:
    """Push a lane's current-window counter up to ``amount``."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO budget_counters
                (user_id, time_window, provider, role, window_start, running_total, updated_at)
            VALUES (NULL, $1, NULL, $2, $3, $4, NOW())
            ON CONFLICT (user_id, time_window, provider, role, window_start)
            DO UPDATE SET running_total = $4
            """,
            window,
            role,
            _window_start(window),
            amount,
        )


def _response(cost: float) -> SimpleNamespace:
    msg = SimpleNamespace(content="ok", tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        id="resp_test",
        model="m",
    )


def _provider_mocked(acompletion: AsyncMock, cost: float, tracker: AsyncMock) -> ExitStack:
    """Mock ONLY the provider boundary and the ledger sink — gate and DB are real."""
    stack = ExitStack()
    stack.enter_context(
        patch("personal_agent.llm_client.litellm_client.litellm.acompletion", new=acompletion)
    )
    stack.enter_context(
        patch("personal_agent.llm_client.litellm_client.litellm.completion_cost", return_value=cost)
    )
    stack.enter_context(
        patch(
            "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
            return_value=tracker,
        )
    )
    return stack


class TestSkillRoutingLane:
    """The production acquisition is key-based (``executor.py``'s skill router)."""

    @pytest.mark.asyncio
    async def test_charges_skill_routing_and_not_main_inference(
        self, real_gate: CostGate, pool: asyncpg.Pool
    ) -> None:
        """AC-4: the isolated lane moves; the user-facing lane does not."""
        before_own = await _counter(pool, "skill_routing")
        before_main = await _counter(pool, "main_inference")

        acompletion = AsyncMock(return_value=_response(0.002))
        tracker = AsyncMock()
        client = LiteLLMClient(
            model_id="claude-haiku-4-5",
            provider="anthropic",
            max_tokens=64,
            budget_role="skill_routing",
        )

        with _provider_mocked(acompletion, 0.002, tracker):
            await client.respond(
                role=ModelRole.SKILL_ROUTING,
                messages=[{"role": "user", "content": "route this"}],
                trace_ctx=make_test_ctx("fre989_skill_routing"),
            )

        assert await _counter(pool, "skill_routing") > before_own
        assert await _counter(pool, "main_inference") == before_main

    @pytest.mark.asyncio
    async def test_ledger_row_is_tagged_skill_routing(
        self, real_gate: CostGate, pool: asyncpg.Pool
    ) -> None:
        """AC-3: the spend is attributable to the lane in api_costs.purpose."""
        acompletion = AsyncMock(return_value=_response(0.002))
        tracker = AsyncMock()
        client = LiteLLMClient(
            model_id="claude-haiku-4-5",
            provider="anthropic",
            max_tokens=64,
            budget_role="skill_routing",
        )

        # A session id is required for the ledger write: LiteLLMClient skips
        # api_costs when trace_ctx carries none (ADR-0074's identity contract).
        # That gap is real and named in the audit doc as a known residue — it is
        # not what this test is about, so supply the identity the path needs.
        with _provider_mocked(acompletion, 0.002, tracker):
            await client.respond(
                role=ModelRole.SKILL_ROUTING,
                messages=[{"role": "user", "content": "route this"}],
                trace_ctx=SystemTraceContext.new(
                    "fre989_skill_routing_ledger", session_id=str(uuid4())
                ),
            )

        tracker.record_api_call.assert_awaited_once()
        assert tracker.record_api_call.await_args.kwargs["purpose"] == "skill_routing"

    @pytest.mark.asyncio
    async def test_denied_by_its_own_cap_before_any_spend(
        self, real_gate: CostGate, pool: asyncpg.Pool
    ) -> None:
        """AC-4: skill_routing's $0.10 daily cap denies, and denial precedes the call."""
        cap = next(
            c.cap_usd
            for c in load_budget_config().caps
            if c.role == "skill_routing" and c.time_window == "daily"
        )
        await _preload(pool, "skill_routing", cap)

        acompletion = AsyncMock(return_value=_response(0.002))
        tracker = AsyncMock()
        client = LiteLLMClient(
            model_id="claude-haiku-4-5",
            provider="anthropic",
            max_tokens=64,
            budget_role="skill_routing",
        )

        with _provider_mocked(acompletion, 0.002, tracker), pytest.raises(BudgetDenied) as excinfo:
            await client.respond(
                role=ModelRole.SKILL_ROUTING,
                messages=[{"role": "user", "content": "route this"}],
                trace_ctx=make_test_ctx("fre989_skill_routing_denied"),
            )

        assert excinfo.value.role == "skill_routing"
        assert excinfo.value.time_window == "daily"
        acompletion.assert_not_awaited()


class TestStudyLane:
    """The production acquisition is direct construction (``study/categorizer.py``)."""

    @pytest.mark.asyncio
    async def test_charges_study_and_not_main_inference(
        self, real_gate: CostGate, pool: asyncpg.Pool
    ) -> None:
        """FRE-839's isolation actually applies: a corpus run cannot touch the user lane."""
        before_own = await _counter(pool, "study")
        before_main = await _counter(pool, "main_inference")

        acompletion = AsyncMock(return_value=_response(0.01))
        tracker = AsyncMock()
        client = LiteLLMClient(
            model_id="gpt-5.4-mini",
            provider="openai",
            max_tokens=512,
            budget_role="study",
        )

        with _provider_mocked(acompletion, 0.01, tracker):
            await client.respond(
                role=ModelRole.STUDY,
                messages=[{"role": "user", "content": "categorize this"}],
                trace_ctx=make_test_ctx("fre989_study"),
            )

        assert await _counter(pool, "study") > before_own
        assert await _counter(pool, "main_inference") == before_main

    @pytest.mark.asyncio
    async def test_denied_by_its_own_cap_before_any_spend(
        self, real_gate: CostGate, pool: asyncpg.Pool
    ) -> None:
        """AC-4: study's $5.00 daily cap denies on its own lane, not main_inference's."""
        cap = next(
            c.cap_usd
            for c in load_budget_config().caps
            if c.role == "study" and c.time_window == "daily"
        )
        await _preload(pool, "study", cap)

        acompletion = AsyncMock(return_value=_response(0.01))
        tracker = AsyncMock()
        client = LiteLLMClient(
            model_id="gpt-5.4-mini",
            provider="openai",
            max_tokens=512,
            budget_role="study",
        )

        with _provider_mocked(acompletion, 0.01, tracker), pytest.raises(BudgetDenied) as excinfo:
            await client.respond(
                role=ModelRole.STUDY,
                messages=[{"role": "user", "content": "categorize this"}],
                trace_ctx=make_test_ctx("fre989_study_denied"),
            )

        assert excinfo.value.role == "study"
        acompletion.assert_not_awaited()


def test_role_name_path_for_skill_routing_refuses_to_build_a_client() -> None:
    """FRE-989 finding six, pinned so it is never silently assumed paid.

    ``skill_routing`` has no Layer-3 binding, so ``resolve_role_target`` treats
    the name as a deployment key and finds nothing. The ticket's "role-name
    path" for this role therefore cannot bill anything — which is why AC-4 is
    proved above through the key-based door the skill router actually uses.

    ADR-0141 D1 changed *how* it cannot bill. The factory used to hand back a
    bare ``LocalLLMClient()``, which took no model key and dispatched against
    whatever the catalog happened to resolve — the key-ignoring door of
    FRE-1343. With one client per resolved key there is nothing to fall through
    to, so an unresolvable role now fails loudly at the factory instead. The
    property FRE-989 pinned is preserved and strengthened: this path books no
    spend, and it no longer silently books the wrong *model* either.
    """
    from personal_agent.llm_client.factory import get_llm_client
    from personal_agent.llm_client.types import LLMClientError

    for role_name in ("skill_routing", "study"):
        with pytest.raises(LLMClientError, match="resolves to no definition"):
            get_llm_client(role_name=role_name)
