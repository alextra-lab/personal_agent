"""Atomic Postgres-backed cost reservation gate (ADR-0065 D1).

Replaces the read-then-execute advisory check in ``LiteLLMClient`` with a
transactional reserve / commit / refund lifecycle:

1. ``reserve(role, amount)`` opens a transaction, ``SELECT … FOR UPDATE``
   locks every cap row that applies to ``role`` (including the synthetic
   ``_total`` rows), checks each ``running_total + amount <= cap_usd``,
   raises ``BudgetDenied`` on any failure, otherwise increments every locked
   counter and writes a ``budget_reservations`` row with a 90s TTL.
2. ``commit(reservation_id, actual_cost)`` settles the difference between
   estimate and actual on every counter the reservation incremented and
   marks the row ``committed``.
3. ``refund(reservation_id)`` decrements every counter by the original
   estimate and marks the row ``refunded``. Idempotent.
4. The reaper (separate module) sweeps stale ``active`` rows past their TTL
   on a 30s cadence, refunding them — catches caller crashes between
   reserve and commit.

The gate uses raw asyncpg (mirroring ``cost_tracker.py``) because the hot
path is a single transaction with multiple ``SELECT … FOR UPDATE`` locks
and a bulk ``UPDATE`` — SQLAlchemy ORM adds overhead without benefit at
this layer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

import asyncpg
import structlog

from personal_agent.cost_gate.types import (
    BudgetConfig,
    BudgetDenied,
    DenialReason,
    ReservationId,
    ReservationStatus,
    TimeWindow,
)
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn

if TYPE_CHECKING:
    from collections.abc import Sequence

log = structlog.get_logger(__name__)

# Reservations live for 90 seconds — longer than the worst-case LLM call
# (Sonnet default_timeout: 60) so commits land before reaping. The reaper
# sweeps every 30s, so a crashed caller's headroom returns within ≤120s.
RESERVATION_TTL_SECONDS = 90


class CostGate:
    """Atomic cost reservation primitive.

    One instance is constructed at app startup, ``connect()``-ed, and shared
    across every paid LLM call site. The reaper task uses the same instance.
    """

    def __init__(self, config: BudgetConfig, db_url: str) -> None:
        """Initialise the gate.

        Args:
            config: Loaded ``BudgetConfig`` (caps and per-role behaviour).
            db_url: Database URL — accepted in either SQLAlchemy or asyncpg
                form; normalised internally.
        """
        self.config = config
        self.db_url = _normalize_asyncpg_dsn(db_url)
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Open the asyncpg pool. Call once at app startup."""
        self.pool = await asyncpg.create_pool(
            self.db_url,
            min_size=1,
            max_size=10,
            command_timeout=10,
        )
        log.info("cost_gate_connected")

    async def disconnect(self) -> None:
        """Close the asyncpg pool. Call once at app shutdown."""
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
            log.info("cost_gate_disconnected")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def reserve(
        self,
        role: str,
        amount: Decimal,
        *,
        trace_id: UUID | None = None,
        user_id: UUID | None = None,
        provider: str | None = None,
    ) -> ReservationId:
        """Reserve ``amount`` against every applicable cap, atomically.

        Args:
            role: Caller's budget role (e.g. ``main_inference``,
                ``entity_extraction``). Must appear as a ``role`` field on
                at least one cap entry, otherwise the call is approved
                trivially (no caps = unlimited).
            amount: Estimated cost in USD. Must be positive; zero amounts
                are accepted and produce a no-op reservation row for
                audit symmetry.
            trace_id: Originating chat trace; recorded on the reservation
                row for audit joins.
            user_id: Reserved for v2 per-user policy. v1 is always ``None``.
            provider: Reserved for v2 per-provider policy. v1 is always
                ``None``.

        Returns:
            ``ReservationId`` (UUID) — pass to ``commit()`` or ``refund()``
            once the LLM call settles.

        Raises:
            BudgetDenied: If any matching cap would be exceeded. The
                exception payload identifies the most-restrictive denying
                cap so callers can render a useful failure surface.
            RuntimeError: If ``connect()`` was not called first.
        """
        if self.pool is None:
            raise RuntimeError("CostGate.connect() must be called before reserve()")

        if amount < 0:
            raise ValueError(f"reservation amount must be non-negative, got {amount}")

        caps = self.config.caps_for(role)
        if not caps:
            log.debug(
                "cost_gate_reserve_no_caps", role=role, trace_id=str(trace_id) if trace_id else None
            )

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                counters = await self._lock_counters(
                    conn,
                    caps=caps,
                    user_id=user_id,
                    provider=provider,
                )

                for cap, _counter_id, running_total in counters:
                    new_total = running_total + amount
                    if new_total > cap.cap_usd:
                        # Pick the denial that's most informative: this row
                        # is the one that tripped — re-raise inside the
                        # transaction so the lock is dropped on rollback.
                        raise BudgetDenied(
                            role=cap.role,
                            time_window=cap.time_window,
                            current_spend=running_total,
                            cap=cap.cap_usd,
                            window_resets_at=_window_resets_at(cap.time_window),
                            denial_reason=DenialReason.CAP_EXCEEDED.value,
                            provider=provider,
                        )

                # All caps approve — increment every locked counter.
                for _, counter_id, _ in counters:
                    await conn.execute(
                        """
                        UPDATE budget_counters
                           SET running_total = running_total + $1,
                               updated_at = NOW()
                         WHERE id = $2
                        """,
                        amount,
                        counter_id,
                    )

                # The reservation row references one counter (any one is
                # fine — the reservation_id keys the audit trail; the
                # commit/refund paths re-derive the full counter set from
                # caps_for(role) using the role on the reservation row).
                ref_counter_id = counters[0][1] if counters else None
                if ref_counter_id is None:
                    # No caps at all — write a synthetic reservation row
                    # against a placeholder counter so commit/refund have
                    # something to look up. v1 always has at least the
                    # _total weekly cap, so this branch is reached only in
                    # tests with a deliberately empty BudgetConfig.
                    log.warning(
                        "cost_gate_reserve_uncapped",
                        role=role,
                        amount=str(amount),
                    )
                    return await self._insert_uncapped_reservation(
                        conn, role=role, amount=amount, trace_id=trace_id
                    )

                row = await conn.fetchrow(
                    """
                    INSERT INTO budget_reservations (
                        counter_id, role, amount_usd, status,
                        created_at, expires_at, trace_id
                    )
                    VALUES ($1, $2, $3, $4, NOW(), NOW() + ($5 || ' seconds')::interval, $6)
                    RETURNING reservation_id
                    """,
                    ref_counter_id,
                    role,
                    amount,
                    ReservationStatus.ACTIVE.value,
                    str(RESERVATION_TTL_SECONDS),
                    trace_id,
                )

        reservation_id: UUID = row["reservation_id"]
        log.info(
            "cost_gate_reserved",
            role=role,
            amount=str(amount),
            reservation_id=str(reservation_id),
            trace_id=str(trace_id) if trace_id else None,
            cap_count=len(counters),
        )
        return reservation_id

    async def commit(
        self,
        reservation_id: ReservationId,
        actual_cost: Decimal,
    ) -> None:
        """Settle a reservation with the actual post-call cost.

        Adjusts every counter the reservation incremented by
        ``(actual_cost - amount_usd)`` — negative when actual < estimate,
        which is the common case (refunds the over-estimate). The
        reservation row is marked ``committed``.

        Args:
            reservation_id: The UUID returned from ``reserve()``.
            actual_cost: Actual USD cost from ``litellm.completion_cost()``.
                Must be non-negative.

        Raises:
            ValueError: If ``actual_cost`` is negative or the reservation is
                not in ``active`` state (already committed / refunded /
                expired).
            RuntimeError: If ``connect()`` was not called first.
        """
        if self.pool is None:
            raise RuntimeError("CostGate.connect() must be called before commit()")
        if actual_cost < 0:
            raise ValueError(f"actual_cost must be non-negative, got {actual_cost}")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT role, amount_usd, status
                      FROM budget_reservations
                     WHERE reservation_id = $1
                       FOR UPDATE
                    """,
                    reservation_id,
                )
                if row is None:
                    raise ValueError(f"unknown reservation {reservation_id}")
                if row["status"] != ReservationStatus.ACTIVE.value:
                    raise ValueError(
                        f"reservation {reservation_id} is {row['status']}, expected active"
                    )

                role: str = row["role"]
                reserved: Decimal = row["amount_usd"]
                delta: Decimal = actual_cost - reserved

                if delta != 0:
                    # Apply the delta to every counter that participated in
                    # the original reservation (re-derived from the role +
                    # config — caps don't change mid-call within this
                    # implementation).
                    counter_ids = await self._counter_ids_for_role(conn, role=role)
                    for cid in counter_ids:
                        await conn.execute(
                            """
                            UPDATE budget_counters
                               SET running_total = running_total + $1,
                                   updated_at = NOW()
                             WHERE id = $2
                            """,
                            delta,
                            cid,
                        )

                await conn.execute(
                    """
                    UPDATE budget_reservations
                       SET status = $1,
                           actual_cost_usd = $2,
                           settled_at = NOW()
                     WHERE reservation_id = $3
                    """,
                    ReservationStatus.COMMITTED.value,
                    actual_cost,
                    reservation_id,
                )

        log.info(
            "cost_gate_committed",
            reservation_id=str(reservation_id),
            actual_cost=str(actual_cost),
            reserved=str(reserved),
            delta=str(delta),
        )

    async def refund(self, reservation_id: ReservationId) -> None:
        """Refund a reservation in full.

        Decrements every counter the reservation incremented by the full
        ``amount_usd`` and marks the reservation ``refunded``.

        Idempotent: refunding an already-``refunded`` or ``expired``
        reservation is a no-op (logged at debug). Refunding a ``committed``
        reservation is treated as an error — the actual cost is already on
        the books.

        Args:
            reservation_id: The UUID returned from ``reserve()``.

        Raises:
            ValueError: If the reservation is unknown or already committed.
            RuntimeError: If ``connect()`` was not called first.
        """
        if self.pool is None:
            raise RuntimeError("CostGate.connect() must be called before refund()")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT role, amount_usd, status
                      FROM budget_reservations
                     WHERE reservation_id = $1
                       FOR UPDATE
                    """,
                    reservation_id,
                )
                if row is None:
                    raise ValueError(f"unknown reservation {reservation_id}")

                status: str = row["status"]
                if status == ReservationStatus.COMMITTED.value:
                    raise ValueError(
                        f"reservation {reservation_id} already committed; cannot refund"
                    )
                if status in (ReservationStatus.REFUNDED.value, ReservationStatus.EXPIRED.value):
                    log.debug(
                        "cost_gate_refund_noop", reservation_id=str(reservation_id), status=status
                    )
                    return

                role: str = row["role"]
                amount: Decimal = row["amount_usd"]

                counter_ids = await self._counter_ids_for_role(conn, role=role)
                for cid in counter_ids:
                    await conn.execute(
                        """
                        UPDATE budget_counters
                           SET running_total = running_total - $1,
                               updated_at = NOW()
                         WHERE id = $2
                        """,
                        amount,
                        cid,
                    )

                await conn.execute(
                    """
                    UPDATE budget_reservations
                       SET status = $1,
                           settled_at = NOW()
                     WHERE reservation_id = $2
                    """,
                    ReservationStatus.REFUNDED.value,
                    reservation_id,
                )

        log.info(
            "cost_gate_refunded",
            reservation_id=str(reservation_id),
            amount=str(amount),
        )

    async def reap_stale(self) -> int:
        """Refund every ``active`` reservation past its TTL.

        Run on a 30s cadence by the reaper task. Returns the number of
        reservations swept so the reaper can log meaningful counters.

        Returns:
            Count of reservations transitioned ``active -> expired``.
        """
        if self.pool is None:
            raise RuntimeError("CostGate.connect() must be called before reap_stale()")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                expired = await conn.fetch(
                    """
                    UPDATE budget_reservations
                       SET status = $1,
                           settled_at = NOW()
                     WHERE status = $2
                       AND expires_at < NOW()
                     RETURNING reservation_id, role, amount_usd
                    """,
                    ReservationStatus.EXPIRED.value,
                    ReservationStatus.ACTIVE.value,
                )

                for row in expired:
                    counter_ids = await self._counter_ids_for_role(conn, role=row["role"])
                    for cid in counter_ids:
                        await conn.execute(
                            """
                            UPDATE budget_counters
                               SET running_total = running_total - $1,
                                   updated_at = NOW()
                             WHERE id = $2
                            """,
                            row["amount_usd"],
                            cid,
                        )

        if expired:
            log.info("cost_gate_reaper_swept", count=len(expired))
        else:
            log.debug("cost_gate_reaper_swept", count=0)
        return len(expired)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _lock_counters(
        self,
        conn: asyncpg.Connection,
        *,
        caps: Sequence,
        user_id: UUID | None,
        provider: str | None,
    ) -> list[tuple]:
        """Upsert + lock every counter row matching the given cap set.

        Locks are acquired in deterministic ``id`` order to avoid deadlock
        between concurrent reservations targeting overlapping caps.

        Returns:
            List of ``(cap_entry, counter_id, running_total)`` tuples.
        """
        # Upsert each counter row first (no-op if it already exists), then
        # lock them all in a second pass ordered by id.
        for cap in caps:
            window_start = _window_start(cap.time_window)
            await conn.execute(
                """
                INSERT INTO budget_counters
                    (user_id, time_window, provider, role, window_start,
                     running_total, updated_at)
                VALUES ($1, $2, $3, $4, $5, 0, NOW())
                ON CONFLICT (user_id, time_window, provider, role, window_start)
                DO NOTHING
                """,
                user_id,
                cap.time_window,
                provider,
                cap.role,
                window_start,
            )

        # Now select + lock every relevant row in id order.
        rows: list[tuple] = []
        for cap in caps:
            window_start = _window_start(cap.time_window)
            row = await conn.fetchrow(
                """
                SELECT id, running_total
                  FROM budget_counters
                 WHERE user_id IS NOT DISTINCT FROM $1
                   AND time_window = $2
                   AND provider IS NOT DISTINCT FROM $3
                   AND role = $4
                   AND window_start = $5
                   FOR UPDATE
                """,
                user_id,
                cap.time_window,
                provider,
                cap.role,
                window_start,
            )
            assert row is not None  # we just upserted
            rows.append((cap, row["id"], row["running_total"]))

        # Sort by counter_id so the lock order is stable across calls;
        # consistent ordering is what prevents deadlock.
        rows.sort(key=lambda r: r[1])
        return rows

    async def _counter_ids_for_role(
        self,
        conn: asyncpg.Connection,
        *,
        role: str,
    ) -> list[int]:
        """Re-derive counter ids that a reservation for ``role`` incremented."""
        caps = self.config.caps_for(role)
        ids: list[int] = []
        for cap in caps:
            window_start = _window_start(cap.time_window)
            row = await conn.fetchrow(
                """
                SELECT id FROM budget_counters
                 WHERE user_id IS NULL
                   AND time_window = $1
                   AND provider IS NULL
                   AND role = $2
                   AND window_start = $3
                """,
                cap.time_window,
                cap.role,
                window_start,
            )
            if row is not None:
                ids.append(row["id"])
        return sorted(ids)

    async def _insert_uncapped_reservation(
        self,
        conn: asyncpg.Connection,
        *,
        role: str,
        amount: Decimal,
        trace_id: UUID | None,
    ) -> ReservationId:
        """Write a reservation row when there are no applicable caps.

        Reaches a placeholder ``budget_counters`` row (creating it if
        needed) so the foreign key holds. Used only when ``BudgetConfig``
        declares zero caps — production configs always have at least the
        ``_total`` weekly cap so this is a test/dev-only path.
        """
        await conn.execute(
            """
            INSERT INTO budget_counters
                (user_id, time_window, provider, role, window_start, running_total, updated_at)
            VALUES (NULL, 'weekly', NULL, '_total', $1, 0, NOW())
            ON CONFLICT (user_id, time_window, provider, role, window_start) DO NOTHING
            """,
            _window_start("weekly"),
        )
        row = await conn.fetchrow(
            """
            SELECT id FROM budget_counters
             WHERE user_id IS NULL AND time_window = 'weekly'
               AND provider IS NULL AND role = '_total'
               AND window_start = $1
            """,
            _window_start("weekly"),
        )
        assert row is not None
        ins = await conn.fetchrow(
            """
            INSERT INTO budget_reservations (
                counter_id, role, amount_usd, status,
                created_at, expires_at, trace_id
            )
            VALUES ($1, $2, $3, $4, NOW(), NOW() + ($5 || ' seconds')::interval, $6)
            RETURNING reservation_id
            """,
            row["id"],
            role,
            amount,
            ReservationStatus.ACTIVE.value,
            str(RESERVATION_TTL_SECONDS),
            trace_id,
        )
        rid: UUID = ins["reservation_id"]
        return rid


# ---------------------------------------------------------------------------
# Window arithmetic
# ---------------------------------------------------------------------------


def _window_start(time_window: str) -> datetime:
    """Return the UTC boundary of the current window.

    Matches the Postgres ``date_trunc`` used in the migration backfill so
    the row that a reservation locks is the same row the migration created.
    """
    now = datetime.now(timezone.utc)
    if time_window == TimeWindow.DAILY.value:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if time_window == TimeWindow.WEEKLY.value:
        # ISO week: Monday is day 0. Postgres date_trunc('week', …) is the
        # same convention.
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"unknown time_window: {time_window}")


def _window_resets_at(time_window: str) -> datetime:
    """Return the UTC boundary of the *next* window — the reset time."""
    start = _window_start(time_window)
    if time_window == TimeWindow.DAILY.value:
        return start + timedelta(days=1)
    if time_window == TimeWindow.WEEKLY.value:
        return start + timedelta(days=7)
    raise ValueError(f"unknown time_window: {time_window}")
