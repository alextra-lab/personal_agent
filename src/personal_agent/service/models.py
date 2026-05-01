"""Data models for service layer."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import declarative_base

Base = declarative_base()


# ============================================================================
# User Identity (inbound CF Access identity — ADR-0064)
# ============================================================================


class UserModel(Base):
    """SQLAlchemy model for the users table.

    Populated automatically on first authenticated request via CF Access.
    user_id is the durable FK used for ownership; email may be updated if
    the CF Access email changes.
    """

    __tablename__ = "users"

    user_id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    email = Column(Text, unique=True, nullable=False)
    display_name = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)


# ============================================================================
# Pydantic Models (API/Validation)
# ============================================================================


class SessionCreate(BaseModel):
    """Request to create a new session."""

    channel: str | None = None
    mode: str = "NORMAL"
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionUpdate(BaseModel):
    """Request to update session."""

    mode: str | None = None
    channel: str | None = None
    metadata: dict[str, Any] | None = None
    messages: list[dict[str, Any]] | None = None


class SessionResponse(BaseModel):
    """Session data returned by API."""

    session_id: UUID
    created_at: datetime
    last_active_at: datetime
    mode: str
    channel: str | None
    metadata: dict[str, Any] = Field(validation_alias="metadata_", serialization_alias="metadata")
    messages: list[dict[str, Any]]

    class Config:  # noqa: D106
        """Pydantic configuration."""

        from_attributes = True


class Message(BaseModel):
    """A single message in conversation."""

    role: str  # 'user', 'assistant', 'system', 'tool'
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# Metrics Models
# ============================================================================


class MetricWrite(BaseModel):
    """Request to write a metric."""

    metric_name: str
    metric_value: float
    unit: str | None = None
    trace_id: UUID | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class MetricQuery(BaseModel):
    """Query parameters for metrics."""

    metric_name: str | None = None
    trace_id: UUID | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    limit: int = 1000


class MetricResponse(BaseModel):
    """Single metric response."""

    id: int
    timestamp: datetime
    trace_id: UUID | None
    metric_name: str
    metric_value: float
    unit: str | None
    tags: dict[str, str]

    class Config:  # noqa: D106
        """Pydantic configuration."""

        from_attributes = True


class MetricStats(BaseModel):
    """Statistical summary of metrics."""

    metric_name: str
    count: int
    min_value: float
    max_value: float
    avg_value: float
    p50_value: float | None = None
    p95_value: float | None = None


# ============================================================================
# SQLAlchemy Models (Database)
# ============================================================================


class SessionModel(Base):
    """SQLAlchemy model for sessions table."""

    __tablename__ = "sessions"

    session_id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.user_id"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False)
    last_active_at = Column(DateTime(timezone=True), nullable=False)
    mode = Column(String(20), nullable=False, default="NORMAL")
    channel = Column(String(50), nullable=True)
    metadata_ = Column("metadata", JSONB, default=dict)
    messages = Column(JSONB, default=list)


class MetricModel(Base):
    """SQLAlchemy model for metrics table."""

    __tablename__ = "metrics"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    trace_id = Column(PG_UUID(as_uuid=True), nullable=True)
    metric_name = Column(String(100), nullable=False)
    metric_value = Column(Float, nullable=False)
    unit = Column(String(20), nullable=True)
    tags = Column(JSONB, default=dict)


# ============================================================================
# Cost Check Gate (ADR-0065 — FRE-303)
#
# ORM views over the gate's four tables. The gate primitive itself
# (cost_gate/) uses raw asyncpg for the SELECT … FOR UPDATE hot-path —
# matches the cost_tracker.py raw-pool pattern. These ORM models exist for
# consumers that need to read or write attempts (FRE-307 telemetry) and for
# admin / audit tooling.
# ============================================================================


class BudgetPolicyModel(Base):
    """SQLAlchemy model for the budget_policies table.

    Layered cap definitions keyed by (user_id, time_window, provider, role).
    v1 only populates the unscoped (user_id NULL, provider NULL) rows;
    columns exist from day 1 so v2 per-user / per-provider caps drop in
    without migration.
    """

    __tablename__ = "budget_policies"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(PG_UUID(as_uuid=True), nullable=True)
    time_window = Column(String(16), nullable=False)
    provider = Column(String(32), nullable=True)
    role = Column(String(64), nullable=False)
    cap_usd = Column(Numeric(10, 6), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "time_window",
            "provider",
            "role",
            name="budget_policies_user_id_time_window_provider_role_key",
        ),
    )


class BudgetCounterModel(Base):
    """SQLAlchemy model for the budget_counters table.

    The row that ``SELECT … FOR UPDATE`` locks during reservation. ``window_start``
    is normalised to UTC midnight (daily) or UTC Monday midnight (weekly) so
    windows roll automatically without a cron job.
    """

    __tablename__ = "budget_counters"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(PG_UUID(as_uuid=True), nullable=True)
    time_window = Column(String(16), nullable=False)
    provider = Column(String(32), nullable=True)
    role = Column(String(64), nullable=False)
    window_start = Column(DateTime(timezone=True), nullable=False)
    running_total = Column(Numeric(10, 6), nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "time_window",
            "provider",
            "role",
            "window_start",
            name="budget_counters_user_id_time_window_provider_role_window_start_key",
        ),
    )


class BudgetReservationModel(Base):
    """SQLAlchemy model for the budget_reservations table.

    Status enum: ``active`` | ``committed`` | ``refunded`` | ``expired``.
    ``expires_at`` is ``created_at + 90s``; the reaper sweeps active rows past
    their TTL on a 30s cadence and refunds them to the counter.
    """

    __tablename__ = "budget_reservations"

    reservation_id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    counter_id = Column(BigInteger, ForeignKey("budget_counters.id"), nullable=False)
    role = Column(String(64), nullable=False)
    amount_usd = Column(Numeric(10, 6), nullable=False)
    actual_cost_usd = Column(Numeric(10, 6), nullable=True)
    status = Column(String(16), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    trace_id = Column(PG_UUID(as_uuid=True), nullable=True)


class ConsolidationAttemptModel(Base):
    """SQLAlchemy model for the consolidation_attempts table.

    Per-attempt telemetry for entity-extraction / promotion retries (D6).
    ``outcome`` enum: ``success`` | ``budget_denied`` | ``model_error`` |
    ``extraction_returned_fallback`` | ``transient_failure`` | ``dead_letter``.
    ``denial_reason`` is set when ``outcome='budget_denied'``: ``cap_exceeded``
    | ``policy_violation`` | ``reservation_failed`` | ``provider_error``.
    """

    __tablename__ = "consolidation_attempts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    trace_id = Column(PG_UUID(as_uuid=True), nullable=False)
    attempt_number = Column(Integer, nullable=False)
    role = Column(String(64), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    outcome = Column(String(32), nullable=False)
    denial_reason = Column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "trace_id",
            "attempt_number",
            "role",
            name="consolidation_attempts_trace_id_attempt_number_role_key",
        ),
    )
