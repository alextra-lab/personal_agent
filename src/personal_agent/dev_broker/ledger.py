"""The broker's spend ledger: the job token check and the job and month counters (ADR-0159 D5).

This is the security-critical part of the broker. The rules:

- A reservation takes the token row, then the job row, then the month row, each ``FOR UPDATE``,
  in one transaction. It refuses the call if either counter would pass its cap. So parallel calls
  cannot pass a cap together (D5.6).
- A reservation has no time limit. Only a settle releases it (D5.7).
- A settle takes the call row, then the token, job and month rows, in that order. A reservation
  never waits on an existing call row, so the two orders cannot form a cycle.
- A settle with no usable cost keeps the worst case as spent and stops the token with
  ``cost_unknown`` in the same transaction (D5.8). A refusal at a cap stops it with ``cap_reached``.
- Every time value is a parameter from the caller's clock, never ``NOW()`` in a check, so a test
  can run a call at 23:59 UTC on a month's last day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import structlog

from personal_agent.dev_broker.clock import month_start

log = structlog.get_logger(__name__)

RefusalReason = Literal[
    "unknown_token", "inactive_token", "expired_token", "wrong_model", "cap_reached"
]


@dataclass(frozen=True)
class Refusal:
    """The broker refuses the call.

    Attributes:
        reason: Why.
        trace_id: The job's trace, when the token is known.
    """

    reason: RefusalReason
    trace_id: UUID | None = None


@dataclass(frozen=True)
class TokenGrant:
    """A token that the broker accepts, and the job it belongs to.

    Attributes:
        job_id: The job.
        model_key: The job's model.
        trace_id: The job's trace.
        session_id: The job's session.
    """

    job_id: UUID
    model_key: str
    trace_id: UUID
    session_id: UUID


@dataclass(frozen=True)
class Admitted:
    """A reserved call.

    Attributes:
        call_id: The ``dev_job_calls`` row.
        job_id: The job.
        trace_id: The job's trace.
        session_id: The job's session.
        reserved_usd: The worst case that the call reserved.
    """

    call_id: UUID
    job_id: UUID
    trace_id: UUID
    session_id: UUID
    reserved_usd: Decimal


@dataclass(frozen=True)
class Settled:
    """The provider reported usage and the cost was computed."""

    cost_usd: Decimal
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class CostUnknown:
    """The call ran, but its cost cannot be computed. The worst case stays spent."""


@dataclass(frozen=True)
class TimedOut:
    """The broker ended the call at its time limit. The worst case stays spent."""


@dataclass(frozen=True)
class Cancelled:
    """The call was cancelled after its reservation. The worst case stays spent."""


@dataclass(frozen=True)
class Failed:
    """The provider returned an error. Nothing was billed."""


@dataclass(frozen=True)
class GateDenied:
    """The cost gate denied the call before any request. Nothing was billed."""


Outcome = Settled | CostUnknown | TimedOut | Cancelled | Failed | GateDenied


def _token_refusal(row: asyncpg.Record | None, model_key: str, now: datetime) -> Refusal | None:
    """Return why the token row does not grant ``model_key`` at ``now``, or ``None``."""
    if row is None:
        return Refusal("unknown_token")
    if row["status"] != "active":
        return Refusal("inactive_token", row["trace_id"])
    if row["expires_at"] <= now:
        return Refusal("expired_token", row["trace_id"])
    if row["model_key"] != model_key:
        return Refusal("wrong_model", row["trace_id"])
    return None


class SpendLedger:
    """The job token check and the atomic job and month counters."""

    def __init__(self, pool: asyncpg.Pool, *, job_cap_usd: Decimal, month_cap_usd: Decimal) -> None:
        """Initialise the ledger.

        Args:
            pool: A pool as the broker's role.
            job_cap_usd: The per-job cap.
            month_cap_usd: The per-UTC-month cap.
        """
        self._pool = pool
        self._job_cap = job_cap_usd
        self._month_cap = month_cap_usd

    async def check_token(
        self, *, token_hash: str, model_key: str, now: datetime
    ) -> TokenGrant | Refusal:
        """Check a token without a lock. :meth:`reserve` checks it again under its lock.

        Args:
            token_hash: SHA-256 hex of the bearer token.
            model_key: The model that the request names.
            now: The current instant.

        Returns:
            The grant, or the refusal.
        """
        row = await self._pool.fetchrow(
            """
            SELECT job_id, model_key, trace_id, session_id, status, expires_at
              FROM dev_job_tokens WHERE token_hash = $1
            """,
            token_hash,
        )
        refusal = _token_refusal(row, model_key, now)
        if refusal is not None:
            return refusal
        assert row is not None
        return TokenGrant(row["job_id"], row["model_key"], row["trace_id"], row["session_id"])

    async def relay_open(self, job_id: UUID, *, now: datetime) -> UUID | None:
        """Return the job's trace id if the control relay may forward to its container.

        A job is open while its token is ``active`` or ``stopped`` and not expired. A stopped job
        still needs the relay, so the supervisor can end its OpenCode session. A revoked one does
        not: revocation is the first step of every end state (D8.8.1).

        Args:
            job_id: The job that the gateway names.
            now: The current instant.

        Returns:
            The job's trace id, or ``None`` when the job is not open.
        """
        row = await self._pool.fetchrow(
            "SELECT status, expires_at, trace_id FROM dev_job_tokens WHERE job_id = $1", job_id
        )
        if row is None or row["status"] not in ("active", "stopped") or row["expires_at"] <= now:
            return None
        trace_id: UUID = row["trace_id"]
        return trace_id

    async def reserve(
        self,
        *,
        token_hash: str,
        model_key: str,
        amount_usd: Decimal,
        prompt_token_bound: int,
        max_output_tokens: int,
        now: datetime,
    ) -> Admitted | Refusal:
        """Reserve a call's worst case on the job and month counters, or refuse it.

        Args:
            token_hash: SHA-256 hex of the bearer token.
            model_key: The model that the request names.
            amount_usd: The call's worst-case cost.
            prompt_token_bound: The prompt-token bound that priced it.
            max_output_tokens: The clamped output tokens that priced it.
            now: The current instant. It also selects the month row.

        Returns:
            The admitted call, or the refusal.
        """
        month = month_start(now)
        async with self._pool.acquire() as conn, conn.transaction():
            token = await conn.fetchrow(
                """
                SELECT job_id, model_key, trace_id, session_id, status, expires_at
                  FROM dev_job_tokens WHERE token_hash = $1
                   FOR UPDATE
                """,
                token_hash,
            )
            refusal = _token_refusal(token, model_key, now)
            if refusal is not None:
                return refusal
            assert token is not None
            job_id: UUID = token["job_id"]
            trace_id: UUID = token["trace_id"]

            await conn.execute(
                "INSERT INTO dev_job_spend (job_id) VALUES ($1) ON CONFLICT DO NOTHING", job_id
            )
            await conn.execute(
                "INSERT INTO dev_month_spend (month_start) VALUES ($1) ON CONFLICT DO NOTHING",
                month,
            )
            job_total: Decimal = await conn.fetchval(
                "SELECT committed_usd + reserved_usd FROM dev_job_spend WHERE job_id = $1 "
                "FOR UPDATE",
                job_id,
            )
            month_total: Decimal = await conn.fetchval(
                "SELECT committed_usd + reserved_usd FROM dev_month_spend WHERE month_start = $1 "
                "FOR UPDATE",
                month,
            )
            if job_total + amount_usd > self._job_cap or month_total + amount_usd > self._month_cap:
                await conn.execute(
                    """
                    UPDATE dev_job_tokens
                       SET status = 'stopped', stop_reason = 'cap_reached', updated_at = $2
                     WHERE token_hash = $1
                    """,
                    token_hash,
                    now,
                )
                log.warning(
                    "dev_broker_cap_reached",
                    job_id=str(job_id),
                    trace_id=trace_id.hex,
                    amount_usd=float(amount_usd),
                    job_total_usd=float(job_total),
                    month_total_usd=float(month_total),
                )
                return Refusal("cap_reached", trace_id)

            await conn.execute(
                "UPDATE dev_job_spend SET reserved_usd = reserved_usd + $2, updated_at = $3 "
                "WHERE job_id = $1",
                job_id,
                amount_usd,
                now,
            )
            await conn.execute(
                "UPDATE dev_month_spend SET reserved_usd = reserved_usd + $2, updated_at = $3 "
                "WHERE month_start = $1",
                month,
                amount_usd,
                now,
            )
            call_id = uuid4()
            await conn.execute(
                """
                INSERT INTO dev_job_calls
                    (call_id, job_id, model_key, month_start, prompt_token_bound,
                     max_output_tokens, reserved_usd, status, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'reserved', $8)
                """,
                call_id,
                job_id,
                model_key,
                month,
                prompt_token_bound,
                max_output_tokens,
                amount_usd,
                now,
            )
        log.info(
            "dev_broker_reserved",
            job_id=str(job_id),
            call_id=str(call_id),
            trace_id=trace_id.hex,
            amount_usd=float(amount_usd),
        )
        return Admitted(call_id, job_id, trace_id, token["session_id"], amount_usd)

    async def settle(self, admitted: Admitted, outcome: Outcome, *, now: datetime) -> None:
        """Release a call's reservation and commit what it spent.

        A second settle of the same call changes nothing.

        Args:
            admitted: The reserved call.
            outcome: What happened.
            now: The current instant.
        """
        status, spent, stop_reason, input_tokens, output_tokens = self._resolve(admitted, outcome)
        async with self._pool.acquire() as conn, conn.transaction():
            call = await conn.fetchrow(
                "SELECT job_id, month_start, reserved_usd, status FROM dev_job_calls "
                "WHERE call_id = $1 FOR UPDATE",
                admitted.call_id,
            )
            if call is None or call["status"] != "reserved":
                log.warning(
                    "dev_broker_settle_skipped",
                    call_id=str(admitted.call_id),
                    trace_id=admitted.trace_id.hex,
                    status=None if call is None else call["status"],
                )
                return
            reserved: Decimal = call["reserved_usd"]
            await conn.execute(
                "SELECT 1 FROM dev_job_tokens WHERE job_id = $1 FOR UPDATE", call["job_id"]
            )
            await conn.execute(
                "SELECT 1 FROM dev_job_spend WHERE job_id = $1 FOR UPDATE", call["job_id"]
            )
            await conn.execute(
                "SELECT 1 FROM dev_month_spend WHERE month_start = $1 FOR UPDATE",
                call["month_start"],
            )
            await conn.execute(
                """
                UPDATE dev_job_spend
                   SET reserved_usd = reserved_usd - $2, committed_usd = committed_usd + $3,
                       updated_at = $4
                 WHERE job_id = $1
                """,
                call["job_id"],
                reserved,
                spent,
                now,
            )
            await conn.execute(
                """
                UPDATE dev_month_spend
                   SET reserved_usd = reserved_usd - $2, committed_usd = committed_usd + $3,
                       updated_at = $4
                 WHERE month_start = $1
                """,
                call["month_start"],
                reserved,
                spent,
                now,
            )
            if stop_reason is not None:
                await conn.execute(
                    """
                    UPDATE dev_job_tokens
                       SET status = 'stopped', stop_reason = $2, updated_at = $3
                     WHERE job_id = $1 AND status = 'active'
                    """,
                    call["job_id"],
                    stop_reason,
                    now,
                )
            await conn.execute(
                """
                UPDATE dev_job_calls
                   SET status = $2, settled_usd = $3, input_tokens = $4, output_tokens = $5,
                       settled_at = $6
                 WHERE call_id = $1
                """,
                admitted.call_id,
                status,
                spent,
                input_tokens,
                output_tokens,
                now,
            )
        log_method = log.warning if stop_reason is not None else log.info
        log_method(
            "dev_broker_settled",
            job_id=str(admitted.job_id),
            call_id=str(admitted.call_id),
            trace_id=admitted.trace_id.hex,
            status=status,
            reserved_usd=float(reserved),
            spent_usd=float(spent),
            stop_reason=stop_reason,
        )

    async def settle_stale(self, *, now: datetime, older_than_seconds: float) -> int:
        """Settle every call still ``reserved`` after ``older_than_seconds``, keeping the worst case.

        A live call settles within the call limit, so an older reservation belongs to a settle
        that failed or to a broker that stopped mid-call. It is settled as ``Cancelled``: the worst
        case stays spent and the reservation is released. A settle that races this one is safe,
        because both take the call row's lock and only a ``reserved`` row changes.

        Args:
            now: The current instant.
            older_than_seconds: The age past which a reservation is stale.

        Returns:
            The number of calls settled.
        """
        rows = await self._pool.fetch(
            """
            SELECT c.call_id, c.job_id, c.reserved_usd, t.trace_id, t.session_id
              FROM dev_job_calls c JOIN dev_job_tokens t ON t.job_id = c.job_id
             WHERE c.status = 'reserved'
               AND c.created_at < $1::timestamptz - make_interval(secs => $2::float8)
            """,
            now,
            older_than_seconds,
        )
        for row in rows:
            admitted = Admitted(
                row["call_id"],
                row["job_id"],
                row["trace_id"],
                row["session_id"],
                row["reserved_usd"],
            )
            log.warning(
                "dev_broker_stale_reservation",
                job_id=str(admitted.job_id),
                call_id=str(admitted.call_id),
                trace_id=admitted.trace_id.hex,
                reserved_usd=float(admitted.reserved_usd),
            )
            await self.settle(admitted, Cancelled(), now=now)
        return len(rows)

    @staticmethod
    def _resolve(
        admitted: Admitted, outcome: Outcome
    ) -> tuple[str, Decimal, str | None, int | None, int | None]:
        """Map an outcome to (call status, spent, token stop reason, input tokens, output tokens)."""
        reserved = admitted.reserved_usd
        match outcome:
            case Settled(cost_usd=cost, input_tokens=tokens_in, output_tokens=tokens_out):
                # A cost above the worst case means the bound was wrong: keep the real cost and
                # stop the job, so one call can overshoot at most once.
                stop = "cost_unknown" if cost > reserved else None
                return "settled", cost, stop, tokens_in, tokens_out
            case CostUnknown():
                return "cost_unknown", reserved, "cost_unknown", None, None
            case TimedOut():
                return "timed_out", reserved, None, None, None
            case Cancelled():
                return "cancelled", reserved, None, None, None
            case Failed():
                return "failed", Decimal(0), None, None, None
            case GateDenied():
                return "gate_denied", Decimal(0), None, None, None
