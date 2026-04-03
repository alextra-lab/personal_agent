# ADR-0041: Event Bus via Redis Streams

**Status**: Accepted (Phases 1–2 implemented 2026-04-03)
**Date**: 2026-04-02
**Deciders**: Project owner
**Related**: ADR-0030 (Captain's Log & Self-Improvement Pipeline), ADR-0040 (Linear Async Feedback Channel)
**Enables**: Follow-on ADR — Knowledge Graph Freshness via Access Tracking

---

## Context

The Personal Agent uses **polling-based coordination** and **fire-and-forget async tasks** for all cross-component communication. This creates three problems:

### 1. Reliability gaps

Background tasks — DB message appends, Elasticsearch indexing, task capture writes — use `asyncio.create_task()` wrapped in a thin `run_in_background()` helper (`captains_log/background.py`). If a task fails, the error is logged and the work is lost. There is no persistence, retry mechanism, or dead-letter handling.

Concrete example: if `_append_assistant_message_background()` fails in `service/app.py`, the next request sees incomplete message history. The current workaround is a "rapid follow-ups await the pending task" pattern — fragile and best-effort.

### 2. Latency floors

All cross-component signaling uses scheduled polling:

| Signal path | Current latency | Mechanism |
|------------|----------------|-----------|
| Task capture → consolidation trigger | Up to 70 minutes | 60s poll interval + 1hr minimum consolidation interval |
| Consolidation → promotion | Up to 7 days | Weekly lifecycle loop scheduled job (Sunday 10 AM UTC) |
| Feedback label applied → agent processes it | Up to 24 hours | Daily Linear polling (ADR-0040) |
| Captain's Log → ES backfill | Up to 10 minutes | Periodic bulk indexing |

These are **architectural latency floors** — they cannot be improved without changing the coordination model from pull to push.

### 3. Tight coupling

The brainstem scheduler (`brainstem/scheduler.py`) directly instantiates and orchestrates:

- `SecondBrainConsolidator` (line ~75)
- `InsightsEngine` (line ~90)
- `PromotionPipeline` (line ~99)
- `QualityMonitor` (line ~108)
- `FeedbackPoller` (via ADR-0040)

Each job executes inline within the scheduler's async loops. Testing any single job requires mocking the full dependency graph. Adding a new consumer of consolidation results requires modifying the scheduler. Components cannot be stopped, restarted, or scaled independently.

### Future demand

Two planned workstreams require async event delivery the current model cannot support:

- **Knowledge graph freshness** (follow-on ADR): Tracking `last_accessed_at` and `access_count` on Neo4j entities requires publishing access events from the memory query hot path without adding write latency.
- **Slice 3 programmatic delegation**: Autonomous sub-agent coordination, self-improvement execution, and feedback loops will generate significantly more async events than the current polling model can handle.

---

## Decision

Adopt **Redis Streams** as a lightweight, durable event bus for inter-component async communication. Redis runs as a Docker Compose service alongside existing infrastructure (~30 MB RAM).

### Why Redis Streams

| Option | Evaluated | Rejected because |
|--------|-----------|-----------------|
| **In-process event bus** (blinker, pyee) | Solves decoupling (C) and latency (B) | No persistence — does not solve reliability (A). Events lost on process crash. |
| **PostgreSQL LISTEN/NOTIFY + outbox** | Zero new infra; transactional outbox is durable | LISTEN/NOTIFY is ephemeral (missed if consumer is down). Outbox requires custom polling (~100ms latency). Consumer groups require custom implementation. More code than Redis client. |
| **Redpanda / Kafka** | True append-only log with replay + compaction | Over-engineered for ~dozens of events/hour on a single node. 200–500 MB RAM. Heavier client libraries. Operational complexity unjustified at this scale. |
| **RabbitMQ** | Mature, well-understood | Heavier than Redis. No stream replay semantics. Broker model adds complexity without corresponding benefit at this scale. |

**Redis Streams provides:**

- Durable append-only log (persisted to disk via RDB/AOF)
- Consumer groups with explicit acknowledgment
- Pending entry list (PEL) for automatic retry of unacknowledged messages
- `XCLAIM` for reassigning stuck messages
- Sub-millisecond delivery latency
- ~30 MB RAM baseline footprint
- Single Docker container, minimal operational overhead
- Likely needed for Slice 3 regardless (caching, rate limiting, distributed locking)

### Event Taxonomy

Events carry identifiers and metadata, not large payloads. Consumers fetch full data from the source (Postgres, disk, Neo4j) if needed.

#### Request lifecycle events

| Event | Stream | Published by | Consumers | Replaces |
|-------|--------|-------------|-----------|----------|
| `request.completed` | `stream:request.completed` | Service (`/chat`, after orchestrator returns; carries full `RequestTimer` snapshot) | ES indexer (`cg:es-indexer`), Session writer (`cg:session-writer`); `cg:scheduler` (future) | Fire-and-forget DB append + ES trace index on `/chat` hot path |
| `request.captured` | `stream:request.captured` | Orchestrator (after TaskCapture disk write) | Consolidator (`cg:consolidator`) | Scheduler 60s polling loop checking disk for new captures |

#### Memory & consolidation events

| Event | Stream | Published by | Consumers | Replaces |
|-------|--------|-------------|-----------|----------|
| `consolidation.completed` | `stream:consolidation.completed` | Consolidator | Insights engine (`cg:insights`), Promotion pipeline (`cg:promotion`) | Scheduled daily/weekly jobs running without knowledge of consolidation state |
| `memory.entities_updated` | `stream:memory.entities_updated` | Consolidator | (Future) Knowledge graph freshness tracker | Nothing — currently no signal |
| `memory.accessed` | `stream:memory.accessed` | Memory service (query path) | (Future) Access tracking consumer (follow-on ADR) | Nothing — access is currently invisible |

#### Self-improvement pipeline events

| Event | Stream | Published by | Consumers | Replaces |
|-------|--------|-------------|-----------|----------|
| `promotion.issue_created` | `stream:promotion.issue_created` | Promotion pipeline | Captain's Log reflection (`cg:captain-log`) | Direct function call in consolidator |
| `feedback.received` | `stream:feedback.received` | Feedback poller | Insights engine (`cg:insights`), Promotion suppression (`cg:promotion`) | Tightly coupled inline processing |

#### System events

| Event | Stream | Published by | Consumers | Replaces |
|-------|--------|-------------|-----------|----------|
| `system.idle` | `stream:system.idle` | Scheduler (after idle threshold met) | Consolidator (`cg:consolidator`), deferred work consumers | `_should_consolidate()` polling check with resource gates |
| `telemetry.indexed` | `stream:telemetry.indexed` | ES indexer (on successful index) | (Future) Dashboard refresh, alerting | Nothing — currently fire-and-forget |

### Architecture

```
                    ┌─────────────────────────────┐
                    │       Redis Streams          │
                    │                              │
                    │  stream:request.completed    │
                    │  stream:request.captured     │
                    │  stream:consolidation.*      │
                    │  stream:memory.*             │
                    │  stream:promotion.*          │
                    │  stream:feedback.*           │
                    │  stream:system.*             │
                    └──────┬──────────┬────────────┘
                           │          │
              ┌────────────┘          └──────────────┐
              │                                      │
    ┌─────────▼──────────┐              ┌────────────▼────────────┐
    │    Publishers       │              │     Consumer Groups     │
    │                     │              │                         │
    │  Orchestrator       │              │  cg:consolidator        │
    │  Consolidator       │              │  cg:insights            │
    │  Memory Service     │              │  cg:promotion           │
    │  Feedback Poller    │              │  cg:feedback            │
    │  Promotion Pipeline │              │  cg:es-indexer          │
    │  Scheduler          │              │  cg:session-writer      │
    │                     │              │  cg:captain-log         │
    │                     │              │  cg:freshness (future)  │
    └─────────────────────┘              └─────────────────────────┘
```

#### Implementation components

1. **`EventBus` protocol** (`src/personal_agent/events/bus.py`): Abstract interface with `publish()` and `subscribe()`. Concrete backends implement it. Test suite uses an in-memory stub.

2. **`RedisStreamBus`** (`src/personal_agent/events/redis_backend.py`): Wraps `redis.asyncio` client. Manages stream creation, consumer group initialization, acknowledgment, and dead-letter routing.

3. **Event models** (`src/personal_agent/events/models.py`): Frozen Pydantic models for each event type. Discriminated union via `event_type: Literal[...]` field — consistent with project coding standards (§ Discriminated Unions for State Modeling).

4. **Consumer runner** (`src/personal_agent/events/consumer.py`): Async loop per consumer group. Reads from stream via `XREADGROUP`, deserializes with `parse_stream_event()` in `events/models.py` (subclass fields preserved), dispatches to registered handler, acknowledges on success (`XACK`), retries the handler up to `max_retries` (default 3) with short backoff, then routes to the dead-letter stream.

5. **Dead-letter stream** (`stream:dead_letter`): Failed events land here with error context (original stream, consumer group, error message, attempt count). A brainstem lifecycle job logs dead-letter counts to telemetry for visibility.

### Migration Strategy

Phased adoption. Each phase is independently valuable. The system remains functional if any phase is deferred.

#### Phase 1: Infrastructure + first event

- Add Redis 7.x to `docker-compose.yml`
- Implement `EventBus` protocol, `RedisStreamBus`, consumer runner, event models
- Migrate one path: `request.captured` → `cg:consolidator`
- Consolidator listens for `request.captured` instead of relying on scheduler polling
- Scheduler polling remains as fallback (if Redis is unavailable, polling still works)
- **Validation**: Consolidation fires within seconds of request completion instead of up to 70 minutes

#### Phase 2: Reliability

- Migrate ES indexing: `request.completed` → `cg:es-indexer` (with retry + dead-letter)
- Migrate DB message append: `request.completed` → `cg:session-writer` (with retry + dead-letter)
- Remove `asyncio.create_task` hot-path work for chat trace indexing and assistant DB append when the Redis bus is active; `NoOpBus` retains legacy `create_task` behavior
- **Implemented (FRE-158)**: `RequestCompletedEvent`, handlers in `events/request_completed_handlers.py`, `ElasticsearchLogger.index_request_trace_from_snapshot`, session ordering via `events/session_write_waiter.py` (FRE-51); terminal session-writer failure releases the waiter after dead-letter so `/chat` does not deadlock
- **Validation**: Kill Elasticsearch mid-request; verify event is retried and indexed after ES recovers (operational). Automated: consumer retry/dead-letter, model round-trip, snapshot indexer unit tests

#### Phase 3: Pipeline decoupling

- `consolidation.completed` → triggers `cg:insights` + `cg:promotion` consumers
- `promotion.issue_created` → triggers `cg:captain-log` reflection consumer
- `feedback.received` → triggers `cg:insights` signal consumer
- Scheduler lifecycle loop becomes a thin event publisher (`system.idle`, time-based triggers) rather than directly executing jobs
- **Validation**: Each consumer can be stopped/restarted independently; others continue working

#### Phase 4: Memory access tracking foundation

- Publish `memory.accessed` events from memory query path
- Consumer is a no-op stub — the follow-on knowledge graph freshness ADR designs the actual consumer
- **Validation**: Events appear in stream with < 1ms overhead on memory query latency

### Configuration

Via `personal_agent.config.settings` (Pydantic), consistent with project conventions:

```python
class EventBusSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    redis_url: str = "redis://localhost:6379/0"
    consumer_poll_interval_ms: int = 100
    max_retries: int = 3
    dead_letter_stream: str = "stream:dead_letter"
    ack_timeout_seconds: int = 300
```

When `event_bus.enabled` is `False`, the `EventBus` protocol routes to a no-op implementation. All existing polling paths continue working unchanged. Rollback is a config change.

---

## Consequences

### Positive

- Fire-and-forget patterns gain durability and retry semantics — silent data loss becomes visible and recoverable
- Consolidation latency drops from ~70 minutes to seconds
- Feedback pipeline agent-side processing becomes event-driven (Linear polling latency remains, but agent reaction is immediate)
- Components become independently testable — inject a mock `EventBus`, assert events published, no need to mock the full scheduler dependency graph
- Dead-letter stream provides visibility into failures that are currently silent log lines
- Foundation for knowledge graph freshness tracking (follow-on ADR)
- Foundation for Slice 3 programmatic delegation coordination
- Redis likely needed for Slice 3 regardless (caching, rate limiting, distributed locking)

### Negative

- New infrastructure dependency (Redis container) — increases Docker Compose surface area
- Operational monitoring required — Redis memory usage, stream lengths, consumer lag, dead-letter depth
- Event schema evolution requires coordination across publishers and consumers (mitigated by versioned Pydantic models)
- Dual-path period (polling + events) during migration adds temporary complexity

### Risks

| Risk | Mitigation |
|------|-----------|
| Redis as single point of failure | Feature flag fallback to polling; Redis is optional, not load-bearing, until Phase 3 completes |
| Event ordering across streams not guaranteed | Consumers designed to be idempotent; events carry timestamps for causal ordering where needed |
| Over-eventing (publishing events nobody consumes) | Only events with at least one consumer in the taxonomy are published; prune unused events during review |
| Stream unbounded growth | Configure `MAXLEN` per stream with approximate trimming; retention policy aligned with telemetry retention |

---

## References

- [Redis Streams documentation](https://redis.io/docs/data-types/streams/)
- ADR-0030: Captain's Log Dedup & Self-Improvement Pipeline
- ADR-0040: Linear as Async Feedback Channel
- Cognitive Architecture Redesign v2: `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md`
