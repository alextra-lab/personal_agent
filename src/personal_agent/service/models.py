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
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models."""


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
    execution_profile: str | None = None


class SessionUpdate(BaseModel):
    """Request to update session."""

    mode: str | None = None
    channel: str | None = None
    metadata: dict[str, Any] | None = None
    messages: list[dict[str, Any]] | None = None
    execution_profile: str | None = None


class SessionProfileUpdate(BaseModel):
    """Request to change a session's server-owned execution profile (ADR-0079)."""

    profile: str


class ConstraintPreferenceUpdate(BaseModel):
    """Request to set a standing constraint governance preference (ADR-0076).

    ``preferred_action`` must be the literal ``always_pause`` or a valid stable
    ``action_id`` for the named constraint; the endpoint validates it against
    the action-ID registry.
    """

    constraint_name: str
    preferred_action: str


class LocationPreferenceUpdate(BaseModel):
    """Request to update location consent and client coordinates (FRE-230).

    Latitude/longitude are bounded to valid WGS84 ranges so malformed input is
    rejected at the API boundary before it can reach the Neo4j ``point()`` call.
    """

    consent_enabled: bool | None = None
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    timezone: str | None = None


class SessionResponse(BaseModel):
    """Session data returned by API."""

    session_id: UUID
    created_at: datetime
    last_active_at: datetime
    mode: str
    channel: str | None
    metadata: dict[str, Any] = Field(validation_alias="metadata_", serialization_alias="metadata")
    messages: list[dict[str, Any]]
    primary_model_at_creation: str | None = None
    model_config_path: str | None = None
    execution_profile: str = "local"

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
    # ADR-0074 (FRE-376) — row-level model attribution. NULL on historical
    # rows; populated for every new session by SessionRepository.create.
    primary_model_at_creation = Column(String(120), nullable=True)
    model_config_path = Column(String(255), nullable=True)
    # ADR-0079 (FRE-416) — server-authoritative execution profile. Explicit
    # stored value ('local' | 'cloud'); never a silent request-time fallback.
    execution_profile = Column(String(50), nullable=False, default="local", server_default="local")


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


# ============================================================================
# Artifact substrate (ADR-0069 / FRE-227)
#
# Metadata canon for every byte-string parked in R2. Bytes live in R2 keyed
# by r2_key; this table is the source of truth for ownership, type, and
# (for notes) the pgvector embedding used by notes_search. The vector
# column is intentionally omitted from the ORM model — embedding writes and
# vector-distance reads happen via raw SQL through ``text()`` so the ORM
# never touches the ``vector(1024)`` type.
# ============================================================================


class ArtifactModel(Base):
    """SQLAlchemy view over the artifacts table.

    Used by the gateway's ``/internal/artifacts/{id}`` resolve endpoint and
    by future metadata-only consumers. Writes happen via raw SQL elsewhere
    because they include the pgvector embedding column.
    """

    __tablename__ = "artifacts"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.user_id"),
        nullable=False,
    )
    session_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("sessions.session_id"),
        nullable=True,
    )
    type = Column(Text, nullable=False)
    slug = Column(Text, nullable=True)
    title = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    content_type = Column(Text, nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    r2_key = Column(Text, nullable=False, unique=True)
    # tags TEXT[] and embedding vector(1024) are managed at the SQL layer.
    created_by = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


# ============================================================================
# WebSocket session event buffer (ADR-0075 / FRE-388)
#
# Durable, Postgres-sequenced buffer for AG-UI transport events.
# On reconnect the client sends last_seq; server replays seq > last_seq.
# ============================================================================


class SessionEventModel(Base):
    """SQLAlchemy model for the session_events table."""

    __tablename__ = "session_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("sessions.session_id"),
        nullable=False,
    )
    seq = Column(Integer, nullable=False)
    event_type = Column(Text, nullable=False)
    payload = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="session_events_session_id_seq_key"),
    )


class UserConstraintPreferenceModel(Base):
    """SQLAlchemy model for the user_constraint_preferences table (ADR-0076).

    Standing per-user preferences for harness constraint pauses. A missing row
    for a (user, constraint) pair means ``always_pause``. ``preferred_action``
    holds a stable ``action_id`` (e.g. ``continue_10``) or the literal
    ``always_pause`` — never a display label.
    """

    __tablename__ = "user_constraint_preferences"

    user_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    constraint_name = Column(Text, primary_key=True)
    preferred_action = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now)
    source_session_id = Column(PG_UUID(as_uuid=True), nullable=True)


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
