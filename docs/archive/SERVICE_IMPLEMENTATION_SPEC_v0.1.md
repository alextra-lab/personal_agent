# Service Implementation Specification v0.1

> **Purpose**: Detailed specs for implementing Phase 2 service architecture
> **Audience**: AI coding assistants (less powerful models) and developers
> **Related**: ADR-0016, SESSION-2026-01-19-service-architecture-planning.md

---

## Overview

This document provides implementation-ready specifications for the service-based architecture. Each component includes:

- Data models (Pydantic/SQLAlchemy)
- Interface definitions (function signatures)
- Acceptance tests (what must pass)
- Example usage

---

## 1. Infrastructure Stack

### 1.1 Docker Compose Services

**File**: `docker-compose.yml`

**Version Notes** (as of January 2026):
- **PostgreSQL**: 17.x (latest) or 16.x (conservative). Using 17 with pgvector for embeddings.
- **Elasticsearch**: 8.19.x (stable) or 9.0.x (newest major). Using 8.19 for stability.
- **Neo4j**: 5.26.x (LTS) or 2025.11.x (calendar versioning). Using 5.26 LTS.
- **Kibana**: Match Elasticsearch version.

```yaml
version: '3.8'

services:
  # PostgreSQL 17 with pgvector for sessions, metrics, and future embeddings
  postgres:
    image: pgvector/pgvector:pg17  # PostgreSQL 17 with pgvector pre-installed
    environment:
      POSTGRES_USER: agent
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-agent_dev_password}
      POSTGRES_DB: personal_agent
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./docker/postgres/init.sql:/docker-entrypoint-initdb.d/01-init.sql
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U agent -d personal_agent"]
      interval: 10s
      timeout: 5s
      retries: 5

  # Elasticsearch 8.19 for logs and events
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.19.0
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
      - "ES_JAVA_OPTS=-Xms512m -Xmx512m"
    volumes:
      - es_data:/usr/share/elasticsearch/data
    ports:
      - "9200:9200"
    healthcheck:
      test: ["CMD-SHELL", "curl -s http://localhost:9200/_cluster/health | grep -q 'green\\|yellow'"]
      interval: 10s
      timeout: 5s
      retries: 5

  # Kibana 8.19 (must match Elasticsearch version)
  kibana:
    image: docker.elastic.co/kibana/kibana:8.19.0
    environment:
      - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
    ports:
      - "5601:5601"
    depends_on:
      elasticsearch:
        condition: service_healthy

  # Neo4j 5.26 LTS for memory graph
  neo4j:
    image: neo4j:5.26-community
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:-neo4j_dev_password}
      NEO4J_PLUGINS: '["apoc"]'  # APOC procedures for advanced queries
    volumes:
      - neo4j_data:/data
    ports:
      - "7474:7474"  # HTTP (Browser)
      - "7687:7687"  # Bolt (Driver)
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://localhost:7474 || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  postgres_data:
  es_data:
  neo4j_data:
```

**Alternative: Use existing Elasticsearch instance**

If you already have Elasticsearch running via Docker MCP Gateway or elsewhere:

```yaml
# docker-compose.override.yml (for local dev without ES)
services:
  elasticsearch:
    profiles: ["full"]  # Only start with --profile full
  kibana:
    profiles: ["full"]
```

Then set `AGENT_ELASTICSEARCH_URL=http://host.docker.internal:9200` to use existing instance.

### 1.2 Database Initialization

**File**: `docker/postgres/init.sql`

```sql
-- Enable pgvector extension for embedding storage
CREATE EXTENSION IF NOT EXISTS vector;

-- Sessions table
CREATE TABLE IF NOT EXISTS sessions (
    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mode VARCHAR(20) NOT NULL DEFAULT 'NORMAL',
    channel VARCHAR(50),
    metadata JSONB DEFAULT '{}',
    messages JSONB DEFAULT '[]'
);

CREATE INDEX idx_sessions_last_active ON sessions(last_active_at DESC);

-- Metrics table (time-series style)
CREATE TABLE IF NOT EXISTS metrics (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trace_id UUID,
    metric_name VARCHAR(100) NOT NULL,
    metric_value DOUBLE PRECISION NOT NULL,
    unit VARCHAR(20),
    tags JSONB DEFAULT '{}'
);

CREATE INDEX idx_metrics_timestamp ON metrics(timestamp DESC);
CREATE INDEX idx_metrics_trace_id ON metrics(trace_id);
CREATE INDEX idx_metrics_name ON metrics(metric_name);

-- Captain's Log captures (fast writes during request)
CREATE TABLE IF NOT EXISTS captains_log_captures (
    id BIGSERIAL PRIMARY KEY,
    trace_id UUID NOT NULL UNIQUE,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_message TEXT,
    steps JSONB DEFAULT '[]',
    tools_used TEXT[] DEFAULT '{}',
    duration_ms INTEGER,
    metrics_summary JSONB DEFAULT '{}',
    outcome VARCHAR(50)
);

CREATE INDEX idx_captures_timestamp ON captains_log_captures(timestamp DESC);

-- Captain's Log reflections (written by second brain)
CREATE TABLE IF NOT EXISTS captains_log_reflections (
    id BIGSERIAL PRIMARY KEY,
    trace_id UUID NOT NULL REFERENCES captains_log_captures(trace_id),
    reflection_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rationale TEXT,
    entities_extracted TEXT[] DEFAULT '{}',
    connections_found TEXT[] DEFAULT '{}',
    proposed_changes JSONB DEFAULT '[]'
);

CREATE INDEX idx_reflections_timestamp ON captains_log_reflections(reflection_timestamp DESC);

-- API cost tracking
CREATE TABLE IF NOT EXISTS api_costs (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    provider VARCHAR(50) NOT NULL,  -- 'anthropic', 'openai', etc.
    model VARCHAR(100) NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd DECIMAL(10, 6) NOT NULL DEFAULT 0,
    trace_id UUID,
    purpose VARCHAR(50)  -- 'user_request', 'second_brain', 'entity_extraction'
);

CREATE INDEX idx_api_costs_timestamp ON api_costs(timestamp DESC);
CREATE INDEX idx_api_costs_provider ON api_costs(provider);

-- Embeddings table (for future semantic search)
-- Uses pgvector for efficient similarity search
CREATE TABLE IF NOT EXISTS embeddings (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_type VARCHAR(50) NOT NULL,  -- 'conversation', 'entity', 'reflection'
    source_id UUID NOT NULL,           -- Reference to source record
    content_hash VARCHAR(64),          -- SHA256 of embedded content (dedup)
    embedding vector(1536),            -- OpenAI ada-002 dimension (adjust as needed)
    metadata JSONB DEFAULT '{}'
);

-- HNSW index for fast approximate nearest neighbor search
CREATE INDEX idx_embeddings_vector ON embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_embeddings_source ON embeddings(source_type, source_id);
CREATE INDEX idx_embeddings_hash ON embeddings(content_hash);
```

**Note on pgvector**:
- The `vector(1536)` dimension matches OpenAI's text-embedding-ada-002
- Adjust dimension if using different embedding models (e.g., 768 for many open models)
- HNSW index enables fast similarity search (~10ms for millions of vectors)
- Alternative: IVFFlat index uses less memory but slower queries

---

## 2. Session Storage

### 2.1 Data Models

**File**: `src/personal_agent/service/models.py`

```python
"""Data models for service layer."""

from datetime import datetime
from uuid import UUID, uuid4
from typing import Any
from pydantic import BaseModel, Field
from sqlalchemy import Column, String, DateTime, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
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
    metadata: dict[str, Any]
    messages: list[dict[str, Any]]

    class Config:
        from_attributes = True


class Message(BaseModel):
    """A single message in conversation."""
    role: str  # 'user', 'assistant', 'system', 'tool'
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    metadata = Column(JSONB, default=dict)
    messages = Column(JSONB, default=list)
```

### 2.2 Session Repository

**File**: `src/personal_agent/service/repositories/session_repository.py`

```python
"""Session storage repository using Postgres."""

from datetime import datetime
from uuid import UUID
from typing import Optional
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.service.models import SessionModel, SessionCreate, SessionUpdate


class SessionRepository:
    """Repository for session CRUD operations.

    Usage:
        async with get_db_session() as db:
            repo = SessionRepository(db)
            session = await repo.create(SessionCreate(channel="CHAT"))
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, data: SessionCreate) -> SessionModel:
        """Create a new session.

        Args:
            data: Session creation parameters

        Returns:
            Created session model
        """
        now = datetime.utcnow()
        session = SessionModel(
            created_at=now,
            last_active_at=now,
            mode=data.mode,
            channel=data.channel,
            metadata=data.metadata,
            messages=[]
        )
        self.db.add(session)
        await self.db.commit()
        await self.db.refresh(session)
        return session

    async def get(self, session_id: UUID) -> Optional[SessionModel]:
        """Get session by ID.

        Args:
            session_id: UUID of session

        Returns:
            Session model or None if not found
        """
        result = await self.db.execute(
            select(SessionModel).where(SessionModel.session_id == session_id)
        )
        return result.scalar_one_or_none()

    async def update(self, session_id: UUID, data: SessionUpdate) -> Optional[SessionModel]:
        """Update session.

        Args:
            session_id: UUID of session
            data: Fields to update (None values are skipped)

        Returns:
            Updated session model or None if not found
        """
        update_data = {
            k: v for k, v in data.model_dump().items()
            if v is not None
        }
        update_data["last_active_at"] = datetime.utcnow()

        await self.db.execute(
            update(SessionModel)
            .where(SessionModel.session_id == session_id)
            .values(**update_data)
        )
        await self.db.commit()
        return await self.get(session_id)

    async def append_message(self, session_id: UUID, message: dict) -> Optional[SessionModel]:
        """Append message to session.

        Args:
            session_id: UUID of session
            message: Message dict with role, content, etc.

        Returns:
            Updated session model or None if not found
        """
        session = await self.get(session_id)
        if not session:
            return None

        messages = list(session.messages or [])
        messages.append(message)

        await self.db.execute(
            update(SessionModel)
            .where(SessionModel.session_id == session_id)
            .values(
                messages=messages,
                last_active_at=datetime.utcnow()
            )
        )
        await self.db.commit()
        return await self.get(session_id)

    async def delete(self, session_id: UUID) -> bool:
        """Delete session.

        Args:
            session_id: UUID of session

        Returns:
            True if deleted, False if not found
        """
        result = await self.db.execute(
            delete(SessionModel).where(SessionModel.session_id == session_id)
        )
        await self.db.commit()
        return result.rowcount > 0

    async def list_recent(self, limit: int = 50) -> list[SessionModel]:
        """List recent sessions.

        Args:
            limit: Maximum number to return

        Returns:
            List of sessions ordered by last_active_at DESC
        """
        result = await self.db.execute(
            select(SessionModel)
            .order_by(SessionModel.last_active_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
```

### 2.3 Acceptance Tests

```python
# tests/test_service/test_session_repository.py

import pytest
from uuid import uuid4

@pytest.mark.asyncio
async def test_create_session(db_session):
    """Session creation stores all fields."""
    repo = SessionRepository(db_session)
    session = await repo.create(SessionCreate(channel="CHAT", mode="NORMAL"))

    assert session.session_id is not None
    assert session.channel == "CHAT"
    assert session.mode == "NORMAL"
    assert session.messages == []

@pytest.mark.asyncio
async def test_get_session(db_session):
    """Can retrieve session by ID."""
    repo = SessionRepository(db_session)
    created = await repo.create(SessionCreate())

    retrieved = await repo.get(created.session_id)
    assert retrieved.session_id == created.session_id

@pytest.mark.asyncio
async def test_get_nonexistent_session(db_session):
    """Returns None for unknown session ID."""
    repo = SessionRepository(db_session)
    result = await repo.get(uuid4())
    assert result is None

@pytest.mark.asyncio
async def test_append_message(db_session):
    """Messages append correctly."""
    repo = SessionRepository(db_session)
    session = await repo.create(SessionCreate())

    await repo.append_message(session.session_id, {
        "role": "user",
        "content": "Hello"
    })

    updated = await repo.get(session.session_id)
    assert len(updated.messages) == 1
    assert updated.messages[0]["content"] == "Hello"

@pytest.mark.asyncio
async def test_session_persists_across_connections(db_session):
    """Session data survives database reconnection."""
    repo = SessionRepository(db_session)
    session = await repo.create(SessionCreate(channel="TEST"))
    session_id = session.session_id

    # Simulate reconnection by creating new repository
    repo2 = SessionRepository(db_session)
    retrieved = await repo2.get(session_id)

    assert retrieved.channel == "TEST"
```

---

## 3. Metrics Storage

### 3.1 Data Models

**File**: `src/personal_agent/service/models.py` (add to existing)

```python
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


class MetricStats(BaseModel):
    """Statistical summary of metrics."""
    metric_name: str
    count: int
    min_value: float
    max_value: float
    avg_value: float
    p50_value: float | None = None
    p95_value: float | None = None


# SQLAlchemy Model
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
```

### 3.2 Metrics Repository

**File**: `src/personal_agent/service/repositories/metrics_repository.py`

```python
"""Metrics storage repository using Postgres."""

from datetime import datetime, timedelta
from uuid import UUID
from typing import Optional
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.service.models import (
    MetricModel, MetricWrite, MetricQuery, MetricStats
)


class MetricsRepository:
    """Repository for metrics storage and querying.

    Usage:
        async with get_db_session() as db:
            repo = MetricsRepository(db)
            await repo.write(MetricWrite(metric_name="cpu_percent", metric_value=45.2))
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def write(self, metric: MetricWrite) -> MetricModel:
        """Write a single metric.

        Args:
            metric: Metric to write

        Returns:
            Created metric model
        """
        model = MetricModel(
            timestamp=datetime.utcnow(),
            trace_id=metric.trace_id,
            metric_name=metric.metric_name,
            metric_value=metric.metric_value,
            unit=metric.unit,
            tags=metric.tags
        )
        self.db.add(model)
        await self.db.commit()
        await self.db.refresh(model)
        return model

    async def write_batch(self, metrics: list[MetricWrite]) -> int:
        """Write multiple metrics efficiently.

        Args:
            metrics: List of metrics to write

        Returns:
            Number of metrics written
        """
        now = datetime.utcnow()
        models = [
            MetricModel(
                timestamp=now,
                trace_id=m.trace_id,
                metric_name=m.metric_name,
                metric_value=m.metric_value,
                unit=m.unit,
                tags=m.tags
            )
            for m in metrics
        ]
        self.db.add_all(models)
        await self.db.commit()
        return len(models)

    async def query(self, params: MetricQuery) -> list[MetricModel]:
        """Query metrics with filters.

        Args:
            params: Query parameters

        Returns:
            List of matching metrics
        """
        stmt = select(MetricModel)

        if params.metric_name:
            stmt = stmt.where(MetricModel.metric_name == params.metric_name)
        if params.trace_id:
            stmt = stmt.where(MetricModel.trace_id == params.trace_id)
        if params.start_time:
            stmt = stmt.where(MetricModel.timestamp >= params.start_time)
        if params.end_time:
            stmt = stmt.where(MetricModel.timestamp <= params.end_time)

        stmt = stmt.order_by(MetricModel.timestamp.desc()).limit(params.limit)

        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_stats(
        self,
        metric_name: str,
        hours: int = 24
    ) -> Optional[MetricStats]:
        """Get statistical summary for a metric.

        Args:
            metric_name: Name of metric
            hours: Time window in hours

        Returns:
            MetricStats or None if no data
        """
        start_time = datetime.utcnow() - timedelta(hours=hours)

        result = await self.db.execute(
            select(
                func.count(MetricModel.id).label("count"),
                func.min(MetricModel.metric_value).label("min_value"),
                func.max(MetricModel.metric_value).label("max_value"),
                func.avg(MetricModel.metric_value).label("avg_value")
            )
            .where(MetricModel.metric_name == metric_name)
            .where(MetricModel.timestamp >= start_time)
        )
        row = result.one()

        if row.count == 0:
            return None

        return MetricStats(
            metric_name=metric_name,
            count=row.count,
            min_value=row.min_value,
            max_value=row.max_value,
            avg_value=float(row.avg_value)
        )

    async def get_recent_by_trace(self, trace_id: UUID) -> list[MetricModel]:
        """Get all metrics for a specific trace.

        Args:
            trace_id: Trace UUID

        Returns:
            All metrics for that trace
        """
        result = await self.db.execute(
            select(MetricModel)
            .where(MetricModel.trace_id == trace_id)
            .order_by(MetricModel.timestamp.asc())
        )
        return list(result.scalars().all())
```

---

## 4. Elasticsearch Logging

### 4.1 Elasticsearch Client

**File**: `src/personal_agent/telemetry/es_logger.py`

```python
"""Elasticsearch logger for structured events."""

from datetime import datetime
from typing import Any
from uuid import UUID
import structlog
from elasticsearch import AsyncElasticsearch

log = structlog.get_logger()


class ElasticsearchLogger:
    """Async Elasticsearch logger for structured events.

    Usage:
        es_logger = ElasticsearchLogger("http://localhost:9200")
        await es_logger.connect()
        await es_logger.log_event("task_started", {"task_id": "123"})
    """

    def __init__(
        self,
        es_url: str = "http://localhost:9200",
        index_prefix: str = "agent-logs"
    ):
        self.es_url = es_url
        self.index_prefix = index_prefix
        self.client: AsyncElasticsearch | None = None

    async def connect(self) -> bool:
        """Connect to Elasticsearch.

        Returns:
            True if connected successfully
        """
        try:
            self.client = AsyncElasticsearch([self.es_url])
            info = await self.client.info()
            log.info("elasticsearch_connected", version=info["version"]["number"])
            return True
        except Exception as e:
            log.error("elasticsearch_connection_failed", error=str(e))
            return False

    async def disconnect(self):
        """Close Elasticsearch connection."""
        if self.client:
            await self.client.close()
            self.client = None

    def _get_index_name(self) -> str:
        """Get index name with date suffix (daily rotation)."""
        date_str = datetime.utcnow().strftime("%Y.%m.%d")
        return f"{self.index_prefix}-{date_str}"

    async def log_event(
        self,
        event_type: str,
        data: dict[str, Any],
        trace_id: UUID | str | None = None,
        span_id: str | None = None
    ) -> str | None:
        """Log a structured event to Elasticsearch.

        Args:
            event_type: Type of event (e.g., 'task_started', 'tool_executed')
            data: Event data (will be indexed)
            trace_id: Optional trace ID for correlation
            span_id: Optional span ID

        Returns:
            Document ID if successful, None if failed
        """
        if not self.client:
            log.warning("elasticsearch_not_connected", event=event_type)
            return None

        doc = {
            "@timestamp": datetime.utcnow().isoformat(),
            "event_type": event_type,
            "trace_id": str(trace_id) if trace_id else None,
            "span_id": span_id,
            **data
        }

        try:
            result = await self.client.index(
                index=self._get_index_name(),
                document=doc
            )
            return result["_id"]
        except Exception as e:
            log.error("elasticsearch_log_failed", event=event_type, error=str(e))
            return None

    async def log_batch(
        self,
        events: list[tuple[str, dict[str, Any], UUID | None]]
    ) -> int:
        """Log multiple events efficiently.

        Args:
            events: List of (event_type, data, trace_id) tuples

        Returns:
            Number of events logged successfully
        """
        if not self.client:
            return 0

        from elasticsearch.helpers import async_bulk

        index_name = self._get_index_name()
        actions = [
            {
                "_index": index_name,
                "_source": {
                    "@timestamp": datetime.utcnow().isoformat(),
                    "event_type": event_type,
                    "trace_id": str(trace_id) if trace_id else None,
                    **data
                }
            }
            for event_type, data, trace_id in events
        ]

        try:
            success, _ = await async_bulk(self.client, actions)
            return success
        except Exception as e:
            log.error("elasticsearch_bulk_failed", error=str(e))
            return 0

    async def search_events(
        self,
        event_type: str | None = None,
        trace_id: UUID | str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        query_text: str | None = None,
        limit: int = 100
    ) -> list[dict[str, Any]]:
        """Search events with filters.

        Args:
            event_type: Filter by event type
            trace_id: Filter by trace ID
            start_time: Start of time range
            end_time: End of time range
            query_text: Full-text search query
            limit: Maximum results

        Returns:
            List of matching events
        """
        if not self.client:
            return []

        must_clauses = []

        if event_type:
            must_clauses.append({"term": {"event_type": event_type}})
        if trace_id:
            must_clauses.append({"term": {"trace_id": str(trace_id)}})
        if start_time or end_time:
            range_clause = {"range": {"@timestamp": {}}}
            if start_time:
                range_clause["range"]["@timestamp"]["gte"] = start_time.isoformat()
            if end_time:
                range_clause["range"]["@timestamp"]["lte"] = end_time.isoformat()
            must_clauses.append(range_clause)
        if query_text:
            must_clauses.append({"query_string": {"query": query_text}})

        query = {"bool": {"must": must_clauses}} if must_clauses else {"match_all": {}}

        try:
            result = await self.client.search(
                index=f"{self.index_prefix}-*",
                query=query,
                size=limit,
                sort=[{"@timestamp": "desc"}]
            )
            return [hit["_source"] for hit in result["hits"]["hits"]]
        except Exception as e:
            log.error("elasticsearch_search_failed", error=str(e))
            return []
```

### 4.2 Index Template Setup

**File**: `docker/elasticsearch/index-template.json`

**Note**: For Elasticsearch 8.19+, use composable index templates.

```json
{
  "index_patterns": ["agent-logs-*"],
  "priority": 100,
  "template": {
    "settings": {
      "number_of_shards": 1,
      "number_of_replicas": 0,
      "index.lifecycle.name": "agent-logs-policy",
      "index.lifecycle.rollover_alias": "agent-logs"
    },
    "mappings": {
      "properties": {
        "@timestamp": { "type": "date" },
        "event_type": { "type": "keyword" },
        "trace_id": { "type": "keyword" },
        "span_id": { "type": "keyword" },
        "session_id": { "type": "keyword" },
        "component": { "type": "keyword" },
        "level": { "type": "keyword" },
        "message": { "type": "text" },
        "duration_ms": { "type": "float" },
        "success": { "type": "boolean" },
        "error": {
          "type": "text",
          "fields": {
            "keyword": { "type": "keyword", "ignore_above": 256 }
          }
        },
        "user_message": { "type": "text" },
        "tool_name": { "type": "keyword" },
        "model_role": { "type": "keyword" },
        "model_name": { "type": "keyword" },
        "tokens_used": { "type": "integer" },
        "input_tokens": { "type": "integer" },
        "output_tokens": { "type": "integer" },
        "cost_usd": { "type": "float" },
        "metrics": {
          "type": "object",
          "properties": {
            "cpu_percent": { "type": "float" },
            "memory_percent": { "type": "float" },
            "gpu_percent": { "type": "float" },
            "disk_percent": { "type": "float" }
          }
        },
        "tags": { "type": "keyword" }
      }
    }
  }
}
```

**ILM Policy** (Index Lifecycle Management):

**File**: `docker/elasticsearch/ilm-policy.json`

```json
{
  "policy": {
    "phases": {
      "hot": {
        "min_age": "0ms",
        "actions": {
          "rollover": {
            "max_age": "7d",
            "max_size": "1gb"
          }
        }
      },
      "warm": {
        "min_age": "7d",
        "actions": {
          "shrink": { "number_of_shards": 1 },
          "forcemerge": { "max_num_segments": 1 }
        }
      },
      "delete": {
        "min_age": "30d",
        "actions": {
          "delete": {}
        }
      }
    }
  }
}
```

This policy:
- Keeps hot data for 7 days (fast queries)
- Moves to warm tier and compacts
- Deletes after 30 days (adjust for your needs)

---

## 5. FastAPI Service

### 5.1 Application Setup

**File**: `src/personal_agent/service/app.py`

```python
"""FastAPI service application."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator
from fastapi import FastAPI, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from personal_agent.service.database import get_db_session, init_db
from personal_agent.service.repositories.session_repository import SessionRepository
from personal_agent.service.models import (
    SessionCreate, SessionUpdate, SessionResponse
)
from personal_agent.telemetry.es_logger import ElasticsearchLogger
from personal_agent.config.settings import get_settings

log = structlog.get_logger()
settings = get_settings()

# Global instances (initialized in lifespan)
es_logger: ElasticsearchLogger | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan management."""
    global es_logger

    # Startup
    log.info("service_starting")

    # Initialize database
    await init_db()
    log.info("database_initialized")

    # Connect to Elasticsearch
    es_logger = ElasticsearchLogger(settings.elasticsearch_url)
    await es_logger.connect()

    # TODO: Initialize MCP gateway singleton
    # TODO: Start Brainstem monitoring tasks

    log.info("service_ready", port=settings.service_port)

    yield

    # Shutdown
    log.info("service_shutting_down")

    if es_logger:
        await es_logger.disconnect()

    log.info("service_stopped")


app = FastAPI(
    title="Personal Agent Service",
    description="Cognitive architecture service with persistent memory",
    version="2.0.0",
    lifespan=lifespan
)


# ============================================================================
# Health Check
# ============================================================================

@app.get("/health")
async def health_check():
    """Service health check endpoint."""
    return {
        "status": "healthy",
        "components": {
            "database": "connected",
            "elasticsearch": "connected" if es_logger and es_logger.client else "disconnected",
            # "neo4j": "connected",  # TODO
            # "mcp_gateway": "connected",  # TODO
        }
    }


# ============================================================================
# Session Endpoints
# ============================================================================

@app.post("/sessions", response_model=SessionResponse)
async def create_session(
    data: SessionCreate,
    db: AsyncSession = Depends(get_db_session)
):
    """Create a new session."""
    repo = SessionRepository(db)
    session = await repo.create(data)

    if es_logger:
        await es_logger.log_event("session_created", {
            "session_id": str(session.session_id),
            "channel": session.channel,
            "mode": session.mode
        })

    return session


@app.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db_session)
):
    """Get session by ID."""
    from uuid import UUID

    repo = SessionRepository(db)
    session = await repo.get(UUID(session_id))

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return session


@app.patch("/sessions/{session_id}", response_model=SessionResponse)
async def update_session(
    session_id: str,
    data: SessionUpdate,
    db: AsyncSession = Depends(get_db_session)
):
    """Update session."""
    from uuid import UUID

    repo = SessionRepository(db)
    session = await repo.update(UUID(session_id), data)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return session


@app.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    limit: int = 50,
    db: AsyncSession = Depends(get_db_session)
):
    """List recent sessions."""
    repo = SessionRepository(db)
    return await repo.list_recent(limit)


# ============================================================================
# Chat Endpoint (Main Entry Point)
# ============================================================================

@app.post("/chat")
async def chat(
    message: str,
    session_id: str | None = None,
    db: AsyncSession = Depends(get_db_session)
):
    """Process a chat message.

    This is the main entry point for user interactions.

    Args:
        message: User's message
        session_id: Optional existing session ID (creates new if not provided)

    Returns:
        Response with assistant message and session_id
    """
    from uuid import UUID

    repo = SessionRepository(db)

    # Get or create session
    if session_id:
        session = await repo.get(UUID(session_id))
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
    else:
        session = await repo.create(SessionCreate())

    # Append user message
    await repo.append_message(session.session_id, {
        "role": "user",
        "content": message
    })

    # TODO: Call orchestrator
    # For now, return placeholder
    response_content = f"[TODO: Orchestrator response to: {message}]"

    # Append assistant message
    await repo.append_message(session.session_id, {
        "role": "assistant",
        "content": response_content
    })

    return {
        "session_id": str(session.session_id),
        "response": response_content
    }
```

### 5.2 Database Connection

**File**: `src/personal_agent/service/database.py`

```python
"""Database connection management."""

from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
    async_sessionmaker
)

from personal_agent.config.settings import get_settings

settings = get_settings()

# Create async engine
engine = create_async_engine(
    settings.database_url,
    echo=settings.database_echo,
    pool_size=5,
    max_overflow=10
)

# Session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)


async def init_db():
    """Initialize database (create tables if needed)."""
    from personal_agent.service.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting database session.

    Usage (in FastAPI):
        @app.get("/endpoint")
        async def endpoint(db: AsyncSession = Depends(get_db_session)):
            ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
```

---

## 6. Thin CLI Client

**File**: `src/personal_agent/ui/service_client.py`

```python
"""Thin CLI client for service mode."""

import asyncio
from typing import Optional
import httpx
import typer
from rich.console import Console
from rich.markdown import Markdown

console = Console()

# Default service URL
DEFAULT_SERVICE_URL = "http://localhost:8000"


class ServiceClient:
    """HTTP client for Personal Agent Service.

    Usage:
        client = ServiceClient()
        response = await client.chat("Hello!")
    """

    def __init__(self, base_url: str = DEFAULT_SERVICE_URL):
        self.base_url = base_url
        self.session_id: Optional[str] = None

    async def health_check(self) -> dict:
        """Check service health.

        Returns:
            Health status dict

        Raises:
            httpx.ConnectError: If service is not running
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{self.base_url}/health")
            response.raise_for_status()
            return response.json()

    async def chat(self, message: str) -> str:
        """Send chat message and get response.

        Args:
            message: User's message

        Returns:
            Assistant's response
        """
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self.base_url}/chat",
                params={
                    "message": message,
                    "session_id": self.session_id
                }
            )
            response.raise_for_status()
            data = response.json()

            # Store session ID for continuity
            self.session_id = data.get("session_id")

            return data["response"]

    async def list_sessions(self) -> list[dict]:
        """List recent sessions."""
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{self.base_url}/sessions")
            response.raise_for_status()
            return response.json()


# ============================================================================
# CLI Commands
# ============================================================================

app = typer.Typer(help="Personal Agent CLI (Service Mode)")


@app.command()
def chat(message: str):
    """Send a chat message to the agent."""
    client = ServiceClient()

    try:
        response = asyncio.run(client.chat(message))
        console.print(Markdown(response))
    except httpx.ConnectError:
        console.print("[red]Error: Service not running. Start with 'agent serve'[/red]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def health():
    """Check service health."""
    client = ServiceClient()

    try:
        status = asyncio.run(client.health_check())
        console.print(f"[green]Status: {status['status']}[/green]")
        for component, state in status.get("components", {}).items():
            color = "green" if state == "connected" else "yellow"
            console.print(f"  [{color}]{component}: {state}[/{color}]")
    except httpx.ConnectError:
        console.print("[red]Service not running[/red]")
        raise typer.Exit(1)


@app.command()
def sessions():
    """List recent sessions."""
    client = ServiceClient()

    try:
        sessions = asyncio.run(client.list_sessions())
        for s in sessions[:10]:
            console.print(f"  {s['session_id'][:8]}... - {s['channel'] or 'default'} - {s['last_active_at']}")
    except httpx.ConnectError:
        console.print("[red]Service not running[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
```

---

## 7. Configuration

**File**: `src/personal_agent/config/settings.py` (additions)

```python
"""Configuration settings for service mode."""

from pydantic import Field
from pydantic_settings import BaseSettings


class ServiceSettings(BaseSettings):
    """Service-specific settings."""

    # Service
    service_host: str = Field(default="0.0.0.0")
    service_port: int = Field(default=8000)

    # Database (Postgres)
    database_url: str = Field(
        default="postgresql+asyncpg://agent:agent@localhost:5432/personal_agent"
    )
    database_echo: bool = Field(default=False)

    # Elasticsearch
    elasticsearch_url: str = Field(default="http://localhost:9200")
    elasticsearch_index_prefix: str = Field(default="agent-logs")

    # Neo4j
    neo4j_uri: str = Field(default="bolt://localhost:7687")
    neo4j_user: str = Field(default="neo4j")
    neo4j_password: str = Field(default="password")

    # Claude API (Second Brain)
    anthropic_api_key: str | None = Field(default=None)
    claude_model: str = Field(default="claude-sonnet-4-5-20250514")
    claude_max_tokens: int = Field(default=4096)
    claude_weekly_budget_usd: float = Field(default=5.0)

    # Feature flags
    use_service_mode: bool = Field(default=True)
    enable_second_brain: bool = Field(default=False)  # Enable after Phase 2.2
    enable_memory_graph: bool = Field(default=False)  # Enable after Phase 2.2

    class Config:
        env_prefix = "AGENT_"
        env_file = ".env"
```

---

## 8. Acceptance Criteria Summary

### Phase 2.1 Complete When

| Criterion | Test |
|-----------|------|
| Service starts | `agent serve` runs, health check returns 200 |
| Sessions persist | Create session, restart service, session exists |
| Chat works | `POST /chat` returns response |
| Metrics write | Write metric, query returns it |
| Logs to ES | Event logged, searchable in Kibana |
| CLI works | `agent chat "Hello"` via HTTP |

### Integration Test Script

```bash
#!/bin/bash
# tests/integration/test_service_e2e.sh

set -e

echo "=== Starting E2E Service Tests ==="

# 1. Health check
echo "Testing health check..."
curl -f http://localhost:8000/health

# 2. Create session
echo "Testing session creation..."
SESSION=$(curl -s -X POST http://localhost:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"channel": "TEST"}' | jq -r '.session_id')
echo "Created session: $SESSION"

# 3. Send chat message
echo "Testing chat..."
RESPONSE=$(curl -s -X POST "http://localhost:8000/chat?message=Hello&session_id=$SESSION")
echo "Response: $RESPONSE"

# 4. Verify session has messages
echo "Verifying session state..."
MESSAGES=$(curl -s http://localhost:8000/sessions/$SESSION | jq '.messages | length')
if [ "$MESSAGES" -ge 2 ]; then
  echo "✅ Messages stored correctly"
else
  echo "❌ Messages not stored"
  exit 1
fi

# 5. Check Elasticsearch logs
echo "Checking Elasticsearch logs..."
curl -s "http://localhost:9200/agent-logs-*/_search?q=event_type:session_created" | jq '.hits.total.value'

echo "=== All E2E Tests Passed ==="
```

---

## 9. Implementation Order

For a less powerful LLM to implement, follow this order:

1. **docker-compose.yml** - Infrastructure (copy and modify)
2. **init.sql** - Database schema (execute directly)
3. **models.py** - Data models (pure Pydantic/SQLAlchemy)
4. **database.py** - Connection setup (small, focused)
5. **session_repository.py** - CRUD operations (well-defined interface)
6. **metrics_repository.py** - Same pattern as sessions
7. **es_logger.py** - Elasticsearch client (isolated)
8. **app.py** - FastAPI application (ties everything together)
9. **service_client.py** - CLI client (simple HTTP calls)
10. **settings.py** - Configuration additions

Each file is self-contained with clear interfaces. A smaller LLM can implement one file at a time, running tests after each.

---

## 10. Dependencies to Add

**File**: `pyproject.toml` (additions)

```toml
[project]
dependencies = [
    # ... existing ...

    # Service layer
    "fastapi>=0.109.0",
    "uvicorn[standard]>=0.27.0",
    "httpx>=0.26.0",

    # Database
    "sqlalchemy[asyncio]>=2.0.25",
    "asyncpg>=0.29.0",
    "alembic>=1.13.0",

    # Elasticsearch
    "elasticsearch[async]>=8.11.0",

    # Neo4j
    "neo4j>=5.15.0",

    # Claude API
    "anthropic>=0.18.0",
]
```
