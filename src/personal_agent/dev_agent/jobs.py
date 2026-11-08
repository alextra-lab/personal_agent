"""The dev-agent job record and its lease (ADR-0159 D9.1–D9.5; FRE-1684).

A supervisor takes a job by writing a lease to its row: an owner, an expiry and a lease number
(``lease_epoch``) that grows by one on each take. Every write on the row requires the current lease
number, the owner and an unexpired lease, so a supervisor that lost its lease cannot change the job.

**Fencing.** :meth:`JobStore.fenced` is the only way to do an external action. It locks the job
row in a transaction, extends the lease, records the action in ``dev_job_actions``, runs the
action, records its outcome and commits. Recovery takes jobs with ``SKIP LOCKED``, so it cannot
take a job while one of its actions runs. A stale supervisor therefore either finds its lease gone
(the action does not run and writes nothing), or its action ends before any takeover, and the
cleanup after the takeover removes what it did.

**Time.** Postgres time (``clock_timestamp()``) is the authority for lease expiry, the container
start and the 30-minute limit. SQL takes durations, never instants from the caller.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, TypeVar
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import structlog

from personal_agent.dev_agent.git_mirror import MAX_PAGE_BYTES
from personal_agent.exceptions import LeaseLostError

log = structlog.get_logger(__name__)

T = TypeVar("T")


class JobState(StrEnum):
    """The states of a job (D9.2)."""

    QUEUED = "queued"
    RUNNING = "running"
    CHECKING = "checking"
    DELIVERING = "delivering"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    FAILED_CHECK = "failed_check"
    CAP_REACHED = "cap_reached"
    COST_UNKNOWN = "cost_unknown"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


#: The states that hold the one-job slot (D9.3). Mirrors the index ``dev_jobs_one_active``.
ACTIVE_STATES: frozenset[JobState] = frozenset(
    {JobState.QUEUED, JobState.RUNNING, JobState.CHECKING, JobState.DELIVERING}
)
#: The end states. A job in one of them never changes again.
END_STATES: frozenset[JobState] = frozenset(set(JobState) - ACTIVE_STATES)

_END_STATE_VALUES: list[str] = sorted(s.value for s in END_STATES)
#: The SQL predicate "the job is not at an end state". One copy, used by every query.
_NOT_ENDED = f"state <> ALL('{{{','.join(_END_STATE_VALUES)}}}'::text[])"

JobMode = Literal["build", "fix"]

#: Why :meth:`JobStore.create_approved_job` wrote no row.
RefusalReason = Literal["job_active", "month_cap_reached", "approval_missing", "approval_used"]


@dataclass(frozen=True)
class ApprovalEvidence:
    """The approved card that a new job must match in ``tool_approval_decisions`` (D10.3).

    Attributes:
        request_id: The card's request id.
        tool_call_id: The model's tool call id, or ``None``.
        tool: The tool that asked.
        args_hash: The hash of the arguments that the card showed.
    """

    request_id: str
    tool_call_id: str | None
    tool: str
    args_hash: str


@dataclass(frozen=True)
class JobRefused:
    """No job was created.

    Attributes:
        reason: Why.
    """

    reason: RefusalReason


@dataclass(frozen=True)
class DevJob:
    """One ``dev_jobs`` row, as the supervisor needs it.

    Attributes:
        id: The job.
        user_id: The requesting user.
        session_id: The session that the job belongs to.
        trace_id: The job's own trace (D5.9).
        approval_request_id: The approval that created the job (D10.3).
        mode: ``build`` or ``fix``.
        task_text: The task for the agent.
        source_artifact_id: The artifact to fix, for a ``fix`` job.
        model_id: The catalog key of the job's model.
        state: The current state.
        lease_epoch: The lease number of the last take.
        started_at: The agent container start, or ``None`` before it.
        result_commit: The result commit, once the handoff stored it.
        result_artifact_id: The preallocated result artifact id (D9.8).
        repo: The repo name in the GitHub organization, once ``prepare`` stored it (D8.3).
        base_commit: The commit that the agent started from, once ``prepare`` stored it (D8.4).
    """

    id: UUID
    user_id: UUID
    session_id: UUID
    trace_id: UUID
    approval_request_id: str
    mode: JobMode
    task_text: str
    source_artifact_id: UUID | None
    model_id: str
    state: JobState
    lease_epoch: int
    started_at: datetime | None
    result_commit: str | None
    result_artifact_id: UUID | None
    repo: str | None = None
    base_commit: str | None = None


@dataclass(frozen=True)
class JobCreated:
    """A job was created.

    Attributes:
        job: The new ``queued`` job.
    """

    job: DevJob


@dataclass(frozen=True)
class Lease:
    """A supervisor's hold on one job.

    Attributes:
        job_id: The job.
        owner: The supervisor that holds it.
        epoch: The lease number of this take.
    """

    job_id: UUID
    owner: str
    epoch: int


@dataclass(frozen=True)
class TokenSignal:
    """The job token's state, read on each renewal (D5.8).

    Attributes:
        status: ``active``, ``stopped`` or ``revoked``, or ``None`` before the token exists.
        stop_reason: ``cap_reached`` or ``cost_unknown`` when the broker stopped the token.
    """

    status: str | None
    stop_reason: str | None


_JOB_COLUMNS = """
    id, user_id, session_id, trace_id, approval_request_id, mode, task_text, source_artifact_id,
    model_id, state, lease_epoch, started_at, result_commit, result_artifact_id, repo, base_commit
"""

#: The row predicate of the lease's own row, without the expiry. Valid only inside a fence: the
#: fence checked the expiry and holds the row lock, so no one else can take the lease meanwhile.
_OWN_ROW = f"""
    id = $1 AND lease_epoch = $2 AND lease_owner = $3
    AND {_NOT_ENDED}
"""

#: The row predicate of a valid lease. $1 job id, $2 epoch, $3 owner.
_HELD = f"""
    id = $1 AND lease_epoch = $2 AND lease_owner = $3
    AND lease_expires_at > clock_timestamp()
    AND {_NOT_ENDED}
"""


#: The fix source of D8.6: the latest succeeded job of the user that built the page. $1 artifact
#: id, $2 user id. ``repo_pipeline`` and the pre-card check of ``start_dev_job`` share it.
SOURCE_BUILT_SQL = """
    SELECT id, repo, result_commit FROM dev_jobs
     WHERE result_artifact_id = $1 AND user_id = $2 AND state = 'succeeded'
       AND repo IS NOT NULL AND result_commit IS NOT NULL
     ORDER BY ended_at DESC LIMIT 1
"""

#: The fix source of D8.7: an HTML artifact of the user, uploaded, at most the page cap. $1
#: artifact id, $2 user id, $3 the byte cap.
SOURCE_PAGE_SQL = """
    SELECT slug, r2_key FROM artifacts
     WHERE id = $1 AND user_id = $2 AND type = 'artifact' AND upload_pending = FALSE
       AND (content_type = 'text/html' OR content_type LIKE 'text/html;%')
       AND size_bytes <= $3
"""


async def _month_cap_reached(
    conn: asyncpg.Connection, month: date, month_cap_usd: Decimal, *, lock: bool
) -> bool:
    """Return whether the month's spend is at the cap (D5.5).

    Args:
        conn: A connection. With ``lock``, it must be in a transaction.
        month: The first day of the UTC month, from the broker's ``month_start``.
        month_cap_usd: The cap.
        lock: Take the month row ``FOR UPDATE``, as the broker does before a reservation.

    Returns:
        ``True`` when committed plus reserved spend is at least the cap. No row is zero spend.
    """
    spent = await conn.fetchval(
        f"""
        SELECT committed_usd + reserved_usd FROM dev_month_spend
         WHERE month_start = $1 {"FOR UPDATE" if lock else ""}
        """,
        month,
    )
    # No row yet: the broker made no call this month, so the spend is zero.
    return Decimal(spent or 0) >= month_cap_usd


def _job(row: asyncpg.Record) -> DevJob:
    return DevJob(
        id=row["id"],
        user_id=row["user_id"],
        session_id=row["session_id"],
        trace_id=row["trace_id"],
        approval_request_id=row["approval_request_id"],
        mode=row["mode"],
        task_text=row["task_text"],
        source_artifact_id=row["source_artifact_id"],
        model_id=row["model_id"],
        state=JobState(row["state"]),
        lease_epoch=row["lease_epoch"],
        started_at=row["started_at"],
        result_commit=row["result_commit"],
        result_artifact_id=row["result_artifact_id"],
        repo=row["repo"],
        base_commit=row["base_commit"],
    )


class JobStore:
    """The ``dev_jobs`` and ``dev_job_actions`` tables, on the gateway's own pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Create the store.

        Args:
            pool: A pool of the gateway's role (``seshat_app``) or ``agent``.
        """
        self._pool = pool

    async def create_job(
        self,
        *,
        job_id: UUID,
        user_id: UUID,
        session_id: UUID,
        trace_id: UUID,
        approval_request_id: str,
        mode: JobMode,
        task_text: str,
        model_id: str,
        source_artifact_id: UUID | None = None,
    ) -> DevJob:
        """Insert a ``queued`` job with no approval check. Tests and recovery fixtures only.

        Production creates a job through :meth:`create_approved_job`, which requires the
        persisted approval (ADR-0159 D10.3).

        Args:
            job_id: The new job's id.
            user_id: The requesting user.
            session_id: The session.
            trace_id: The job's trace.
            approval_request_id: The approval that allows the job. Unique (D10.3).
            mode: ``build`` or ``fix``.
            task_text: The task.
            model_id: The catalog key of the model.
            source_artifact_id: The artifact to fix, required for ``fix``.

        Returns:
            The new job.

        Raises:
            asyncpg.UniqueViolationError: If a job is active already (``dev_jobs_one_active``,
                D9.3), or the approval was used already.
        """
        row = await self._pool.fetchrow(
            f"""
            INSERT INTO dev_jobs (id, user_id, session_id, trace_id, approval_request_id, mode,
                                  task_text, source_artifact_id, model_id, state, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'queued', clock_timestamp())
            RETURNING {_JOB_COLUMNS}
            """,
            job_id,
            user_id,
            session_id,
            trace_id,
            approval_request_id,
            mode,
            task_text,
            source_artifact_id,
            model_id,
        )
        job = _job(row)
        log.info(
            "dev_job_created",
            job_id=str(job.id),
            session_id=str(session_id),
            trace_id=trace_id.hex,
            mode=mode,
            model_id=model_id,
        )
        return job

    async def create_approved_job(
        self,
        *,
        job_id: UUID,
        user_id: UUID,
        session_id: UUID,
        trace_id: UUID,
        approval: ApprovalEvidence,
        mode: JobMode,
        task_text: str,
        model_id: str,
        month: date,
        month_cap_usd: Decimal,
        source_artifact_id: UUID | None = None,
    ) -> JobCreated | JobRefused:
        """Insert a ``queued`` job only for an approved card that no job used (ADR-0159 D10.3, D10.5).

        This is the production path. One transaction:

        1. refuse ``job_active`` if a job holds the one-job slot (D9.3);
        2. lock the month's spend row, as the broker does, and refuse
           ``month_cap_reached`` if committed plus reserved spend is at the cap (D5.5);
        3. insert the job only if ``tool_approval_decisions`` holds an ``approve`` row with the same
           request id, session, tool call id, tool and argument hash. Else refuse
           ``approval_missing``;
        4. a second use of the request id refuses ``approval_used``.

        Args:
            job_id: The new job's id.
            user_id: The requesting user.
            session_id: The session of the card.
            trace_id: The job's own trace (D5.9).
            approval: The approved card that the permission layer passed.
            mode: ``build`` or ``fix``.
            task_text: The task.
            model_id: The catalog key of the model.
            month: The first day of the current UTC month, from the broker's ``month_start``.
            month_cap_usd: The broker's per-month cap in USD.
            source_artifact_id: The artifact to fix, required for ``fix``.

        Returns:
            The new job, or the refusal and its reason. A refusal writes no row.
        """
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    if await conn.fetchval(f"SELECT 1 FROM dev_jobs WHERE {_NOT_ENDED} LIMIT 1"):
                        return JobRefused("job_active")
                    if await _month_cap_reached(conn, month, month_cap_usd, lock=True):
                        return JobRefused("month_cap_reached")
                    row = await conn.fetchrow(
                        f"""
                        INSERT INTO dev_jobs (id, user_id, session_id, trace_id, approval_request_id,
                                              mode, task_text, source_artifact_id, model_id, state,
                                              created_at)
                        SELECT $1, $2, $3, $4, d.request_id, $5, $6, $7, $8, 'queued',
                               clock_timestamp()
                          FROM tool_approval_decisions d
                         WHERE d.request_id = $9
                           AND d.session_id = $3
                           AND d.tool_call_id IS NOT DISTINCT FROM $10
                           AND d.tool = $11
                           AND d.args_hash = $12
                           AND d.decision = 'approve'
                        RETURNING {_JOB_COLUMNS}
                        """,
                        job_id,
                        user_id,
                        session_id,
                        trace_id,
                        mode,
                        task_text,
                        source_artifact_id,
                        model_id,
                        approval.request_id,
                        approval.tool_call_id,
                        approval.tool,
                        approval.args_hash,
                    )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name == "dev_jobs_one_active":
                return JobRefused("job_active")
            if exc.constraint_name == "dev_jobs_approval_request_id_key":
                return JobRefused("approval_used")
            raise
        if row is None:
            return JobRefused("approval_missing")
        job = _job(row)
        log.info(
            "dev_job_created",
            job_id=str(job.id),
            session_id=str(session_id),
            approval_request_id=approval.request_id,
            trace_id=trace_id.hex,
            mode=mode,
            model_id=model_id,
        )
        return JobCreated(job)

    async def fix_source_exists(self, artifact_id: UUID, user_id: UUID) -> bool:
        """Return whether a fix job of this artifact would find its source (D8.6, D8.7).

        ``start_dev_job`` calls it before the card. The pipeline's own lookup decides later.

        Args:
            artifact_id: The artifact to fix.
            user_id: The requesting user.

        Returns:
            ``True`` when the user's agent-built page or uploaded HTML artifact exists.
        """
        async with self._pool.acquire() as conn:
            if await conn.fetchrow(SOURCE_BUILT_SQL, artifact_id, user_id) is not None:
                return True
            row = await conn.fetchrow(SOURCE_PAGE_SQL, artifact_id, user_id, MAX_PAGE_BYTES)
        return row is not None

    async def refusal_before_card(
        self, month: date, month_cap_usd: Decimal
    ) -> RefusalReason | None:
        """Read whether a new job would be refused now, without a write (FRE-1687).

        ``start_dev_job`` calls it before the approval card, so the user is not asked to approve a
        job that is certain to be refused. :meth:`create_approved_job` checks again.

        Args:
            month: The first day of the current UTC month, from the broker's ``month_start``.
            month_cap_usd: The broker's per-month cap in USD.

        Returns:
            ``job_active``, ``month_cap_reached``, or ``None``.
        """
        async with self._pool.acquire() as conn:
            if await conn.fetchval(f"SELECT 1 FROM dev_jobs WHERE {_NOT_ENDED} LIMIT 1"):
                return "job_active"
            if await _month_cap_reached(conn, month, month_cap_usd, lock=False):
                return "month_cap_reached"
        return None

    async def _take(
        self, where: str, owner: str, lease_seconds: float
    ) -> tuple[DevJob, Lease] | None:
        row = await self._pool.fetchrow(
            f"""
            UPDATE dev_jobs
               SET lease_owner = $1,
                   lease_epoch = lease_epoch + 1,
                   lease_expires_at = clock_timestamp() + make_interval(secs => $2)
             WHERE id = (SELECT id FROM dev_jobs WHERE {where}
                          ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
            RETURNING {_JOB_COLUMNS}
            """,
            owner,
            float(lease_seconds),
        )
        if row is None:
            return None
        job = _job(row)
        return job, Lease(job_id=job.id, owner=owner, epoch=job.lease_epoch)

    async def claim_new(self, owner: str, lease_seconds: float) -> tuple[DevJob, Lease] | None:
        """Take the oldest ``queued`` job that no supervisor took before.

        Args:
            owner: This supervisor's id.
            lease_seconds: The lease length.

        Returns:
            The job and the new lease, or ``None`` when no such job exists.
        """
        return await self._take("state = 'queued' AND lease_epoch = 0", owner, lease_seconds)

    async def take_expired(self, owner: str, lease_seconds: float) -> tuple[DevJob, Lease] | None:
        """Take the oldest job that is not at an end state and whose lease expired (D9.6).

        A job whose row is locked by a running fenced action is skipped. A later scan takes it.

        Args:
            owner: This supervisor's id.
            lease_seconds: The lease length.

        Returns:
            The job and the new lease, or ``None`` when no such job exists.
        """
        where = f"{_NOT_ENDED} AND lease_epoch > 0 AND lease_expires_at <= clock_timestamp()"
        return await self._take(where, owner, lease_seconds)

    async def renew(self, lease: Lease, lease_seconds: float) -> TokenSignal | None:
        """Extend the lease, and read the job token's state.

        Args:
            lease: The lease to extend.
            lease_seconds: The new lease length from now.

        Returns:
            The token's state, or ``None`` when the lease is lost (another epoch, another owner, an
            expired lease or an end state).
        """
        row = await self._pool.fetchrow(
            f"""
            WITH renewed AS (
                UPDATE dev_jobs
                   SET lease_expires_at = clock_timestamp() + make_interval(secs => $4)
                 WHERE {_HELD}
                RETURNING id
            )
            SELECT r.id, t.status, t.stop_reason
              FROM renewed r LEFT JOIN dev_job_tokens t ON t.job_id = r.id
            """,
            lease.job_id,
            lease.epoch,
            lease.owner,
            float(lease_seconds),
        )
        if row is None:
            return None
        return TokenSignal(status=row["status"], stop_reason=row["stop_reason"])

    async def fenced(
        self,
        lease: Lease,
        action: str,
        operation: Callable[[asyncpg.Connection], Awaitable[T]],
        *,
        lease_seconds: float,
        mark_started: bool = False,
    ) -> T:
        """Run one external action under the lease (D9.5).

        The transaction locks the job row only while the lease is valid, extends it, logs the
        action, runs ``operation`` with the transaction's connection and logs its outcome. The row
        stays locked until the action returns, so no other supervisor can take the job meanwhile.

        Args:
            lease: The lease that must be valid.
            action: The action name for the log (for example ``start_container``).
            operation: The action. It receives the connection, so a database write can join the
                transaction.
            lease_seconds: The new lease length from now.
            mark_started: Set ``started_at`` to now, the start of the 30-minute limit (D9.7).

        Returns:
            What ``operation`` returned.

        Raises:
            LeaseLostError: If the lease is not valid. ``operation`` did not run.
            Exception: Whatever ``operation`` raised. Its database writes roll back, and the action
                row records ``error``.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                locked = await conn.fetchval(
                    f"SELECT id FROM dev_jobs WHERE {_HELD} FOR UPDATE",
                    lease.job_id,
                    lease.epoch,
                    lease.owner,
                )
                if locked is None:
                    log.warning(
                        "dev_job_fence_refused",
                        job_id=str(lease.job_id),
                        lease_epoch=lease.epoch,
                        action=action,
                        trace_id=None,
                    )
                    raise LeaseLostError(f"lease {lease.epoch} on job {lease.job_id} is not held")
                await conn.execute(
                    f"""
                    UPDATE dev_jobs
                       SET lease_expires_at = clock_timestamp() + make_interval(secs => $2)
                           {", started_at = clock_timestamp()" if mark_started else ""}
                     WHERE id = $1
                    """,
                    lease.job_id,
                    float(lease_seconds),
                )
                action_id = await conn.fetchval(
                    """
                    INSERT INTO dev_job_actions (job_id, lease_epoch, lease_owner, action, created_at)
                    VALUES ($1, $2, $3, $4, clock_timestamp())
                    RETURNING id
                    """,
                    lease.job_id,
                    lease.epoch,
                    lease.owner,
                    action,
                )
                failure: BaseException | None = None
                try:
                    # A savepoint: a failed action rolls back its own database writes, and an
                    # aborted statement does not abort the fence's transaction.
                    async with conn.transaction():
                        result = await operation(conn)
                except BaseException as exc:  # noqa: BLE001 — recorded, committed, then re-raised
                    # A cancel (gateway stop) is recorded too: the action may have acted already.
                    failure = exc
                # The fence commits in both cases, so the log keeps an action that raised.
                await self._record_outcome(conn, action_id, "error" if failure else "done")
                # The row stayed locked, so no one took the lease. Extend it again, or an action
                # longer than the lease would leave its own holder with an expired lease.
                await conn.execute(
                    """
                    UPDATE dev_jobs
                       SET lease_expires_at = clock_timestamp() + make_interval(secs => $2)
                     WHERE id = $1 AND lease_epoch = $3 AND ended_at IS NULL
                    """,
                    lease.job_id,
                    float(lease_seconds),
                    lease.epoch,
                )
        if failure is not None:
            raise failure
        return result

    @staticmethod
    async def _record_outcome(conn: asyncpg.Connection, action_id: int, outcome: str) -> None:
        await conn.execute(
            "UPDATE dev_job_actions SET outcome = $2, completed_at = clock_timestamp() WHERE id = $1",
            action_id,
            outcome,
        )

    async def transition(self, lease: Lease, to_state: JobState) -> None:
        """Move the job to a non-end state under the lease.

        Args:
            lease: The lease that must be valid.
            to_state: ``running``, ``checking`` or ``delivering``.

        Raises:
            LeaseLostError: If the lease is not valid.
            ValueError: If ``to_state`` is an end state. End states go through :meth:`finish`.
        """
        if to_state in END_STATES:
            raise ValueError(f"{to_state} is an end state; use finish() inside the cleanup fence")
        done = await self._pool.fetchval(
            f"UPDATE dev_jobs SET state = $4 WHERE {_HELD} RETURNING id",
            lease.job_id,
            lease.epoch,
            lease.owner,
            to_state.value,
        )
        if done is None:
            raise LeaseLostError(f"lease {lease.epoch} on job {lease.job_id} is not held")

    @staticmethod
    async def finish(
        conn: asyncpg.Connection,
        lease: Lease,
        state: JobState,
        failure_reason: str | None,
    ) -> None:
        """Write the end state. Call it only inside the cleanup fence, after the cleanup.

        It does not check the lease expiry: the fence checked it and holds the row lock, so a
        cleanup that outlasts the lease still writes its end state.

        Args:
            conn: The fenced transaction's connection.
            lease: The lease of the fence.
            state: The end state.
            failure_reason: Why the job did not succeed, or ``None``.

        Raises:
            ValueError: If ``state`` is not an end state.
            LeaseLostError: If the row did not change.
        """
        if state not in END_STATES:
            raise ValueError(f"{state} is not an end state")
        done = await conn.fetchval(
            f"""
            UPDATE dev_jobs
               SET state = $4, failure_reason = $5, ended_at = clock_timestamp(),
                   lease_expires_at = NULL,
                   spend_usd = (SELECT committed_usd FROM dev_job_spend WHERE job_id = $1)
             WHERE {_OWN_ROW}
            RETURNING id
            """,
            lease.job_id,
            lease.epoch,
            lease.owner,
            state.value,
            failure_reason,
        )
        if done is None:
            raise LeaseLostError(f"lease {lease.epoch} on job {lease.job_id} is not held")

    @staticmethod
    async def record(
        conn: asyncpg.Connection,
        lease: Lease,
        *,
        repo: str | None = None,
        base_commit: str | None = None,
        result_commit: str | None = None,
        main_moved: bool | None = None,
        check_result: str | None = None,
    ) -> None:
        """Store repo facts and the check result on the job. Call it only inside a fence, with the fence's connection.

        It runs in the same transaction as the fenced action, so a fact is stored only when its
        action returned (FRE-1685). A ``None`` argument leaves its column as it is.

        Args:
            conn: The fenced transaction's connection.
            lease: The lease of the fence.
            repo: The repo name (D8.3).
            base_commit: The commit that the agent starts from (D8.4).
            result_commit: The head that the gateway pushed to ``job/<job id>`` (D8.8.4).
            main_moved: Whether ``main`` is at the result commit (D8.9).
            check_result: The publish check result as JSON text (D7.5).

        Raises:
            LeaseLostError: If the row did not change.
        """
        done = await conn.fetchval(
            f"""
            UPDATE dev_jobs
               SET repo = COALESCE($4, repo),
                   base_commit = COALESCE($5, base_commit),
                   result_commit = COALESCE($6, result_commit),
                   main_moved = COALESCE($7, main_moved),
                   check_result = COALESCE($8::jsonb, check_result)
             WHERE {_OWN_ROW}
            RETURNING id
            """,
            lease.job_id,
            lease.epoch,
            lease.owner,
            repo,
            base_commit,
            result_commit,
            main_moved,
            check_result,
        )
        if done is None:
            raise LeaseLostError(f"lease {lease.epoch} on job {lease.job_id} is not held")

    @staticmethod
    async def preallocate_artifact(conn: asyncpg.Connection, lease: Lease, candidate: UUID) -> UUID:
        """Return the job's result artifact id, and store ``candidate`` if it has none (D9.8).

        Call it only inside a fence. A retry after a crash gets the id of the first call, so the
        R2 key and the ``artifacts`` row stay the same.

        Args:
            conn: The fenced transaction's connection.
            lease: The lease of the fence.
            candidate: A new id, used only when the job has none yet.

        Returns:
            The job's result artifact id.

        Raises:
            LeaseLostError: If the row did not change.
        """
        artifact_id = await conn.fetchval(
            f"""
            UPDATE dev_jobs
               SET result_artifact_id = COALESCE(result_artifact_id, $4)
             WHERE {_OWN_ROW}
            RETURNING result_artifact_id
            """,
            lease.job_id,
            lease.epoch,
            lease.owner,
            candidate,
        )
        if artifact_id is None:
            raise LeaseLostError(f"lease {lease.epoch} on job {lease.job_id} is not held")
        return UUID(str(artifact_id))

    async def seconds_left(self, job_id: UUID, limit_seconds: float) -> float:
        """Return the time left before the job's limit, from Postgres time (D9.7).

        Args:
            job_id: The job.
            limit_seconds: The limit from ``started_at``.

        Returns:
            Seconds left. Zero or less when the limit passed. The full limit before the start.
        """
        left = await self._pool.fetchval(
            """
            SELECT EXTRACT(EPOCH FROM (started_at + make_interval(secs => $2)) - clock_timestamp())
              FROM dev_jobs WHERE id = $1
            """,
            job_id,
            float(limit_seconds),
        )
        return float(limit_seconds) if left is None else float(left)

    async def get(self, job_id: UUID) -> DevJob | None:
        """Read one job.

        Args:
            job_id: The job.

        Returns:
            The job, or ``None`` when no row exists.
        """
        row = await self._pool.fetchrow(
            f"SELECT {_JOB_COLUMNS} FROM dev_jobs WHERE id = $1", job_id
        )
        return None if row is None else _job(row)

    async def ended_or_missing(self, job_ids: Iterable[UUID]) -> set[UUID]:
        """Return the ids that have no row or whose job is at an end state.

        Args:
            job_ids: Job ids found on Docker labels.

        Returns:
            The subset that the orphan sweep may remove.
        """
        ids = list(job_ids)
        if not ids:
            return set()
        live = await self._pool.fetch(
            f"""
            SELECT id FROM dev_jobs
             WHERE id = ANY($1::uuid[])
               AND {_NOT_ENDED}
            """,
            ids,
        )
        return set(ids) - {row["id"] for row in live}
