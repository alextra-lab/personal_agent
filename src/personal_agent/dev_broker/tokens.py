"""The gateway's side of the job token table (ADR-0159 D5.4, D6; FRE-1683).

The gateway issues a token before it starts a job's agent container and revokes it at every end
state. The broker's role cannot insert a token, so these functions run on the gateway's own pool.
Only the SHA-256 of a token is stored. The raw token goes to the agent container and nowhere else.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import structlog

log = structlog.get_logger(__name__)


def hash_token(raw_token: str) -> str:
    """Return the stored form of a job token.

    Args:
        raw_token: The token as the agent sends it.

    Returns:
        The SHA-256 hex digest.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def issue_job_token(
    pool: asyncpg.Pool,
    *,
    job_id: UUID,
    model_key: str,
    trace_id: UUID,
    session_id: UUID,
    expires_at: datetime,
) -> str:
    """Create the job's token row and return the raw token.

    Args:
        pool: The gateway's pool.
        job_id: The job.
        model_key: The catalog key of the job's model. The broker accepts no other model.
        trace_id: The job's trace. ``api_costs`` rows join to the job on it (D5.9).
        session_id: The session that the job belongs to.
        expires_at: The instant after which the broker refuses the token.

    Returns:
        The raw token, for the agent container's environment only.
    """
    raw_token = secrets.token_urlsafe(32)
    await pool.execute(
        """
        INSERT INTO dev_job_tokens
            (token_hash, job_id, model_key, trace_id, session_id, status, expires_at)
        VALUES ($1, $2, $3, $4, $5, 'active', $6)
        """,
        hash_token(raw_token),
        job_id,
        model_key,
        trace_id,
        session_id,
        expires_at,
    )
    log.info(
        "dev_job_token_issued",
        job_id=str(job_id),
        model_key=model_key,
        trace_id=trace_id.hex,
        session_id=str(session_id),
    )
    return raw_token


async def revoke_job_token(pool: asyncpg.Pool, job_id: UUID) -> None:
    """Set the job's token to ``revoked``. The broker then refuses it on every listener.

    Args:
        pool: The gateway's pool.
        job_id: The job.
    """
    trace_id = await pool.fetchval(
        """
        UPDATE dev_job_tokens SET status = 'revoked', updated_at = NOW()
         WHERE job_id = $1
        RETURNING trace_id
        """,
        job_id,
    )
    log.info(
        "dev_job_token_revoked",
        job_id=str(job_id),
        trace_id=trace_id.hex if trace_id is not None else None,
    )
