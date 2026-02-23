"""Data models for service layer."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, DateTime, Float, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import declarative_base

Base = declarative_base()


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
