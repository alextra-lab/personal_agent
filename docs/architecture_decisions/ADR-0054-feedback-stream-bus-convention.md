# ADR-0054: Feedback Stream Bus Convention

**Status**: Accepted
**Date**: 2026-04-22 (initial draft) · 2026-04-23 (flatten decision — D3 rewritten, alternatives inverted, implementation landed)
**Deciders**: Project owner
**Depends on**: ADR-0041 (Event Bus — Redis Streams), ADR-0043 (Three-Layer Separation), ADR-0053 (Gate Feedback Monitoring — introduces the Feedback Stream ADR Template)
**Related**: ADR-0030 (Captain's Log & Self-Improvement Pipeline), ADR-0040 (Linear Async Feedback Channel), ADR-0042 (Knowledge Graph Freshness)
**Enables**: ADR-0055 (System Health & Homeostasis), ADR-0056 (Error Pattern Monitoring), ADR-0057 (Insights & Pattern Analysis), ADR-0058 (Self-Improvement Pipeline Stream), ADR-0059 (Context Quality), ADR-0060 (Knowledge Graph Quality)
**Linear Issue**: FRE-245

---

## Context

### Five of nine feedback streams are not composable

The Personal Agent has grown **nine distinct feedback streams** — subsystems that observe the agent's own behaviour and generate signals intended to drive self-improvement (catalogued in `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md`). Every stream executes at least two steps: it detects or computes a signal, and it records that signal somewhere. But the third step — making the signal available to other subsystems — is missing from five of the nine:

| Stream | Detects | Bus? | Closed loop? |
|--------|---------|------|--------------|
| 1. Captain's Log self-reflection | Per-task | Partial (promotion only) | ✅ |
| 2. Linear human feedback | Human label | ✅ | ✅ |
| 3. Promotion pipeline | Threshold | ✅ | ✅ |
| 4. Insights engine | Patterns | ❌ | ❌ |
| 5. Mode manager | System metrics | ❌ | ❌ (hardcoded `Mode.NORMAL`) |
| 6. Memory freshness | Access patterns | ✅ | ⚠️ partial |
| 7. Compaction quality | Context loss | ❌ | ❌ |
| 8. Consolidation quality | Graph health | ❌ | ❌ |
| 9. Cost anomaly | Spend spikes | ❌ | ❌ |
| NEW. Gate monitoring (ADR-0053) | Pipeline decisions | ❌ → ✅ | ❌ → ✅ |

For the five unbussed streams, the signal dies at a `log.warning(...)` call, a Kibana index entry, or an in-memory object that nobody reads. The Mode Manager case is particularly illustrative: `MetricsDaemon` polls hardware every 5 seconds, `ModeManager` computes a sophisticated state-machine transition, and the resulting mode is then **never consulted** because `service/app.py` hardcodes `Mode.NORMAL` when building the gateway context. A fully-implemented detection pipeline with no subscriber.

### Cross-stream composition is currently impossible

The value of feedback streams is multiplicative: a single anomalous signal is noise; the same signal co-occurring with a different anomaly on a different stream is evidence. Examples that ADR-0053 already anticipated but cannot build today:

- *"Error pattern fires **AND** confidence is low **AND** cost is anomalous → escalate to a dedicated Linear project."*
- *"Memory freshness shows dormancy **AND** recall miss rate is rising → propose consolidation tuning."*
- *"Gate monitor flags sustained DELEGATE rate **AND** cost is anomalous on external providers → propose intent-pattern tightening."*

Today each of these would require modifying every producer to cross-wire their detections into a new coordinator. That is exactly the tight coupling that ADR-0041 was written to eliminate. Without a bus event on every stream, ADR-0041's decoupling benefit is only partially realized.

### Stream ADRs ahead need a shared contract

FRE-244 through FRE-250 each produce a stream-specific ADR using the Feedback Stream ADR Template from ADR-0053. Without a shared contract for event shapes, stream names, consumer group names, durability requirements, and versioning, each of those ADRs would reinvent those decisions — with the near-certainty that six independently-authored conventions would diverge.

### The missing half of ADR-0041

ADR-0041 solved the **transport** problem: Redis Streams, consumer groups, dead-letter routing, retries, graceful degradation via `NoOpBus`. It did not prescribe:

- How a new stream picks its name.
- How a new consumer group picks its name.
- What fields every feedback event must carry.
- When a durable write (disk / ES) is required in addition to the bus publish.
- How event schemas evolve without breaking consumers.
- What to do when the bus is down, ES is down, or the disk write fails.

ADR-0053 implicitly adopted conventions for the gate monitoring stream. This ADR extracts those conventions, reconciles them with the existing bus taxonomy in `src/personal_agent/events/models.py`, and elevates them into a mandatory contract for all subsequent feedback stream ADRs.

---

## Decision Drivers

1. **No new infrastructure.** The event bus, Elasticsearch, structlog, Captain's Log, and Linear projects all exist. The convention must reuse them.
2. **Durability independent of Redis.** A Redis outage or a NoOpBus fallback must not silently lose feedback signals. Every stream needs a durable record that survives bus unavailability.
3. **Composability.** Any future capability must be able to subscribe to any combination of streams without modifying producers.
4. **Typed discipline.** Every feedback event is a frozen Pydantic model with a `Literal` discriminator — consistent with ADR-0041 §Event models and the project's §Discriminated Unions coding standard.
5. **Schema evolution without downtime.** Fields may be added; consumers must gracefully handle old events; breaking changes require a new event type, not a silent schema mutation.
6. **Three-Layer compliance (ADR-0043).** Execution-Layer producers publish; Observation-Layer consumers subscribe. Producers never import consumer modules; consumers never import from `request_gateway/` internals.
7. **Explicit failure behaviour.** Every dual-write step has a defined failure mode (log-only, retry-and-degrade, fail-the-request). No silent swallowing.
8. **Reference implementation, not just rules.** The convention is validated by documenting one existing stream end-to-end using the 9-section Feedback Stream ADR Template. Authors of later ADRs follow that exemplar.

---

## Decision

### D1: Stream Naming Convention

Stream names are strings with a mandatory `stream:` prefix, followed by a **domain** and a **signal** separated by a dot. Both parts are lowercase `snake_case`. Multi-word signals may contain additional dots that indicate a subtype.

```
stream:<domain>.<signal>[.<subtype>]
```

**Domain** names a producing subsystem or concept, not a consumer. **Signal** names what happened, past-tense where possible.

Current streams re-read through this rule:

| Current name | Domain | Signal | Fits rule |
|--------------|--------|--------|-----------|
| `stream:request.captured` | request | captured | ✅ |
| `stream:request.completed` | request | completed | ✅ |
| `stream:consolidation.completed` | consolidation | completed | ✅ |
| `stream:promotion.issue_created` | promotion | issue_created | ✅ |
| `stream:feedback.received` | feedback | received | ✅ |
| `stream:system.idle` | system | idle | ✅ |
| `stream:memory.accessed` | memory | accessed | ✅ |
| `stream:memory.entities_updated` | memory | entities_updated | ✅ |

**Feedback stream additions** (reserved namespace for Phase 2 ADRs):

| New name | Producer ADR | Notes |
|----------|-------------|-------|
| `stream:gateway.decision` | ADR-0053 | Published from `service/app.py` after `RequestCompletedEvent`; carries `GateSummary` |
| `stream:mode.transition` | ADR-0055 | Mode state machine transitions; replaces log-only events |
| `stream:error.pattern_detected` | ADR-0056 | Rolling window fires when a log pattern crosses threshold |
| `stream:insights.pattern_detected` | ADR-0057 | InsightsEngine output made composable |
| `stream:insights.cost_anomaly` | ADR-0057 | Cost anomaly separated from generic patterns for direct subscription |
| `stream:context.compaction_poor` | ADR-0059 | Compaction quality alerts |
| `stream:graph.quality_anomaly` | ADR-0060 | Consolidation quality anomalies |
| `stream:captain_log.entry_created` | ADR-0058 | Stream 1 bus hook; closes the gap noted in the Stream Catalog |

**Reserved prefixes** (not currently in use but forbidden for new streams):
- `stream:dead_letter` — owned by ADR-0041 retry machinery.
- `stream:test.*` — reserved for test fixtures.

**Naming rules:**
1. `stream:` prefix is mandatory; code must reference the constant from `events/models.py`, not a string literal.
2. Domain is singular (`stream:memory.*`, not `stream:memories.*`).
3. Signal is past-tense or noun-form: `captured`, `completed`, `decision`, `pattern_detected`.
4. Subtype is optional and dot-separated when needed to avoid a stream containing unrelated signals: `stream:graph.quality_anomaly.entity` vs `stream:graph.quality_anomaly.relationship`.
5. No pluralization of the signal. `pattern_detected`, not `patterns_detected`.
6. No human names, no temporary identifiers, no ADR numbers embedded in stream names.

**Enforcement:**
Every stream constant lives in `src/personal_agent/events/models.py` with a module-level docstring that names the producing ADR. Code review rejects any `xadd` to a stream name that is not a constant in that module.

---

### D2: Consumer Group Naming Convention

Consumer group names use the `cg:` prefix followed by a hyphenated descriptor that names the **consumer role**, not the stream it reads. One consumer group may read multiple streams; the group name is about *what the consumer does*, not *where it listens*.

```
cg:<role>
```

Existing groups re-read through this rule:

| Current name | Role | Fits rule |
|--------------|------|-----------|
| `cg:consolidator` | Runs consolidation | ✅ |
| `cg:es-indexer` | Indexes traces to Elasticsearch | ✅ |
| `cg:session-writer` | Appends assistant messages to Postgres | ✅ |
| `cg:insights` | Runs insights analysis | ✅ |
| `cg:promotion` | Runs promotion pipeline | ✅ |
| `cg:captain-log` | Writes reflection entries | ✅ |
| `cg:feedback` | Updates suppression on feedback labels | ✅ |
| `cg:freshness` | Updates Neo4j freshness metadata | ✅ |

**Feedback stream additions** (paired with D1):

| New name | Producer ADR | Reads streams |
|----------|-------------|---------------|
| `cg:gateway-monitor` | ADR-0053 | `stream:request.completed` (for gateway subdict) — also referenced by `stream:gateway.decision` in Phase 2 |
| `cg:mode-controller` | ADR-0055 | `stream:mode.transition` + hardware metric streams |
| `cg:error-monitor` | ADR-0056 | Consumes error-log stream generator output; emits `stream:error.pattern_detected` |
| `cg:insights-router` | ADR-0057 | Consumes `stream:insights.*`, routes to Captain's Log or Linear |
| `cg:context-monitor` | ADR-0059 | Consumes `stream:context.compaction_poor` |
| `cg:graph-monitor` | ADR-0060 | Consumes `stream:graph.quality_anomaly.*`, `stream:insights.cost_anomaly` |

**Naming rules:**
1. `cg:` prefix is mandatory and references a constant from `events/models.py`.
2. Hyphen-separated, lowercase. Underscores and dots are forbidden to keep visual separation from stream names.
3. Role is verb-form or noun-form-of-verb: `indexer`, `monitor`, `writer`, `controller`, `consolidator`.
4. One consumer group per role per process. Multiple processes may instantiate the same group — Redis Streams assigns messages among group members.
5. Consumer names within a group follow `<host>-<pid>` (set by the consumer runner, not by the ADR).

---

### D3: Required Event Fields

Every event — feedback stream or otherwise — carries the same correlation and evolution fields. These live directly on `EventBase` rather than on a secondary `FeedbackEventBase` root. The four fields (`trace_id`, `session_id`, `source_component`, `schema_version`) are observability hygiene that benefits every event type equally; a two-base hierarchy would invent a soft "feedback vs not" distinction that can only be enforced by review, not by the type system.

```python
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field


class EventBase(BaseModel):
    """Base class for all event bus events (ADR-0041, ADR-0054).

    All feedback-stream contract fields live on this single base.  Subclasses
    that always carry a request trace (``RequestCaptured``, ``RequestCompleted``,
    ``MemoryAccessed``) narrow ``trace_id`` / ``session_id`` to required.
    Scheduled / system-triggered events leave them ``None``.

    Attributes:
        event_id: Unique per-event UUID.
        event_type: Literal discriminator set by each concrete subclass;
            dispatched in ``parse_stream_event()``.
        created_at: UTC timestamp at event construction.
        trace_id: Request trace identifier the event is correlated with, or
            ``None`` for scheduled/system events (consolidation, idle,
            feedback poller).  Subclasses narrow to required where a trace
            always exists.
        session_id: Originating session id when available; ``None`` for
            system-level events with no session scope.
        source_component: Dotted module path of the emitting component
            (e.g. ``"request_gateway.monitoring"``).  Required so producer
            identity is visible independently of stream name.
        schema_version: Monotonically increasing integer; bumped when a
            field is added or semantics change.  Additive changes keep
            backward compatibility (Rule 1, D5); breaking changes take a new
            ``event_type`` (Rule 2, D5).  Consumers tolerate any version by
            default — Pydantic ignores unknown fields (Rule 3, D5).
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    event_type: str  # overridden as Literal in subclasses
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trace_id: str | None = None
    session_id: str | None = None
    source_component: str
    schema_version: int = 1
```

Every event type therefore has:

| Field | Purpose | Nullability |
|-------|---------|-------------|
| `event_id` | Unique per-event UUID | Required (default factory) |
| `event_type` | Discriminator; dispatches in `parse_stream_event()` | Required (Literal override per subclass) |
| `created_at` | UTC timestamp at event construction | Required (default factory) |
| `trace_id` | Correlation across streams and ES traces | Nullable on base; narrowed to required by `RequestCaptured`, `RequestCompleted`, `MemoryAccessed` |
| `session_id` | Session scoping | Nullable on base; narrowed to required by `RequestCaptured`, `RequestCompleted` |
| `source_component` | Producer identity, independent of stream name | **Required — no default.** Forces producers to name themselves |
| `schema_version` | Forward-compat gate (see D5) | Defaults to `1` |

Scheduled / background events (`Consolidation`, `Promotion`, `Feedback`, `SystemIdle`, `MemoryEntitiesUpdated`) inherit nullable `trace_id` / `session_id` and leave them `None` — expressing the absence of a request context explicitly rather than omitting the fields. Consumers joining across streams can distinguish "no trace" from "unknown field" without ambiguity.

**Redis message-id (`stream_id`) note.** The per-message Redis id assigned by `XADD` is the durable acknowledge token and is available on the consumer side through the reader loop. It is **not** a field on the event model — embedding it in the publish payload would be circular. Consumers that need the stream id for logging receive it as a side-channel parameter from `ConsumerRunner` (already the case in `events/consumer.py`).

**What flattening costs.** The ten existing `xadd` sites are updated to pass `source_component=<dotted-module-path>`. Three of the eight existing event classes (`RequestCapturedEvent`, `RequestCompletedEvent`, `MemoryAccessedEvent`) already declared `trace_id` / `session_id` as required and continue to do so — the subclass override narrows the base's nullable default exactly as it did before the flatten. No data migration, no re-indexing, no schema replay.

**What flattening avoids.** A two-base hierarchy enforced only by review; the near-certainty of opportunistic half-migrations accumulating over time; denial of `schema_version` to "Phase 1" events that will eventually need to evolve; and the cognitive overhead of reviewers having to decide which root a new event should inherit from.

---

### D4: Durable Write Requirements

Every feedback stream publishes to the bus. In addition, every stream **must** have a durable record that outlives a Redis outage. The durable record may be **disk (JSON file)**, **Elasticsearch**, or **both**, selected by a decision rule keyed to the signal's purpose:

| Signal purpose | Durable target | Rationale |
|----------------|---------------|-----------|
| Causes a Linear issue (Captain's Log proposal) | **Disk + ES** | Disk survives ES outages and is the authoritative record for promotion logic (already the case in ADR-0030 / ADR-0040). ES copy makes it Kibana-queryable. |
| Queried analytically (distributions, percentiles) | **ES only** | High write volume; no point re-reading individual events after analysis; trace-correlated via `trace_id`. |
| Updates a mutable store (Neo4j, Postgres) | **Target store write IS the durable record** | Do not double-write. The Cypher/SQL transaction is the durability boundary; the bus event is the async notification. (Matches ADR-0042 Memory Freshness pattern.) |
| Used by agent in conversation (tool answer) | **Disk + ES** | Disk is source of truth for tool queries; ES enables cross-session aggregation. |
| Short-lived operational signal (idle-transition, quality-degraded) | **ES only** | No downstream replay needed; log trail is sufficient. |

**The decision rule, stated as code:**

```python
# Authoring a new feedback stream ADR:
if signal_creates_linear_issue or signal_used_as_tool_answer:
    durable = "disk + es"
elif signal_updates_mutable_store:
    durable = "target_store_only"  # the transaction is the record
elif signal_is_analytical_aggregate:
    durable = "es_only"
else:
    durable = "es_only"  # default — signals are cheap to re-derive
```

**Ordering requirement.** The durable write **must precede** the bus publish. If the bus is down, the durable record still exists and Phase 2 readers (rolling windows, aggregations) can recover it. If the bus publish is up but the durable write fails, the system logs an error and does not publish — a silent bus-only signal is a bug.

```python
# Canonical ordering
await write_durable(...)       # may raise — never swallowed
await bus.publish(stream, evt) # may raise — caught and logged, not propagated
```

**The bus is *assumed unreliable*, the durable write is *assumed authoritative*.** This is the inverse of the naive "bus is the source of truth" assumption and is the reason the convention is called "dual-write", not "bus-only".

---

### D5: Schema Versioning

Event schemas evolve as new fields become necessary. The convention handles evolution with three rules:

**Rule 1 — Additive changes bump `schema_version` and preserve backward compatibility.**
New fields MUST have a Pydantic default. Consumers written against `schema_version=1` continue to parse `schema_version=2` events — `model_validate` ignores unknown fields by default, and missing new fields are supplied by the default.

**Rule 2 — Breaking changes require a new `event_type`, not a version bump.**
Renaming a field, changing its type, or changing its semantics is a breaking change. In this case:
- Introduce a new event subclass with a new `Literal["domain.signal.v2"]` discriminator.
- Register it in `parse_stream_event()` alongside the old type.
- Either emit on a new stream (`stream:<domain>.<signal>.v2`) OR continue emitting on the same stream with both event types interleaved during a migration window.
- Old consumers ignore the new `event_type`; new consumers handle both until the old is retired.

**Rule 3 — Consumers MUST tolerate higher `schema_version` values.**
A consumer compiled at `max_supported_version=1` receiving an event with `schema_version=2` MUST either:
- Accept it (the Pydantic model will load; unknown fields are ignored), or
- Skip it and log `event_schema_too_new` with both versions.

Never raise. The point of forward-compat is that a producer upgrade does not pause the consumer fleet.

**Enforcement:**
- `parse_stream_event()` handles Rule 2 dispatch (new type → new validator) transparently.
- A unit test exercises a round-trip where a `schema_version=2` payload deserializes into a `schema_version=1`-compiled consumer successfully.
- ADR authors document the initial `schema_version` for the event; bumps are logged in the event model's docstring.

---

### D6: Dual-Write Failure Handling

Dual-writes have three potential failure points: the durable write, the bus publish, and (for mutable-store targets) the target transaction. Each has a defined behaviour:

| Failure | Behaviour | Rationale |
|---------|-----------|-----------|
| Durable write raises (disk full, ES unavailable) | **Propagate** — log `durable_write_failed` at ERROR, re-raise to the caller | The signal has no authoritative record; emitting a bus-only event would create an observable inconsistency |
| Bus publish raises (Redis down, `NoOpBus` swallows) | **Log and continue** — log `event_publish_failed` at WARN, do not re-raise | The durable record still exists; a future replay job (FRE-TBD in Phase 3) can re-emit unpublished durable records |
| Target-store transaction fails (Neo4j constraint violation) | **Propagate** — log `target_write_failed` at ERROR, re-raise | The mutable-store write IS the durability boundary; failure must surface |
| `FreshnessConsumer`-style batched target write fails | **Per-batch retry then dead-letter** | Existing pattern from ADR-0042 §Consumer; do not change |
| Feature flag `event_bus_enabled=False` | **Durable write still happens** — bus publish is a silent no-op via `NoOpBus` | Dual-write is not a feature flag; durability is non-negotiable |

**Graceful degradation order (when multiple backends are down):**

```
1. Disk (local filesystem) — always available unless disk is full
2. Elasticsearch — may be unavailable; fall back to disk + log warning
3. Redis (bus) — may be unavailable; fall back to NoOpBus (silent discard)
```

Streams that write to **disk + ES** treat ES as best-effort: if ES is down, the disk write still succeeds and the event is still published. A background re-indexing job (already used by Captain's Log) reconciles later. Streams that write to **ES only** log the degradation and continue — the failure is visible in ERROR logs but does not fail the producing operation.

**Code pattern (reference):**

```python
from personal_agent.events.bus import get_event_bus
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

async def emit_feedback_signal(
    *,
    durable_writer: DurableWriter,
    stream: str,
    event: EventBase,
) -> None:
    """Dual-write a feedback signal.

    Ordering:
        1. Durable write (disk / ES / target store).
        2. Bus publish.

    Durable failures propagate; bus failures are logged and swallowed.
    """
    try:
        await durable_writer.write(event)
    except Exception:
        log.error(
            "durable_write_failed",
            stream=stream,
            event_type=event.event_type,
            event_id=event.event_id,
            trace_id=event.trace_id,
            exc_info=True,
        )
        raise

    bus = get_event_bus()
    try:
        await bus.publish(stream, event)
    except Exception as exc:
        log.warning(
            "event_publish_failed",
            stream=stream,
            event_type=event.event_type,
            event_id=event.event_id,
            trace_id=event.trace_id,
            error=str(exc),
        )
        # intentionally does not re-raise
```

This pattern is sufficiently small that each Phase 2 producer inlines it rather than depending on a shared helper; the helper would create an Observation-Layer dependency that producers must not acquire.

---

### D7: Reference Implementation — Stream 2 (Linear Human Feedback)

To anchor the convention in a real, running stream, this ADR documents **Stream 2: Linear Human Feedback** end-to-end using the 9-section Feedback Stream ADR Template from ADR-0053. Stream 2 is chosen because:

1. It already publishes to the bus (`FeedbackReceivedEvent` on `stream:feedback.received`).
2. It already has a durable disk record (`telemetry/feedback_history/`).
3. It already has a dedicated Linear surface and label semantics (ADR-0040).
4. It successfully implements the dual-write ordering rule from D4 (Linear state change → suppression file write → bus publish).
5. Authors of ADR-0055 through ADR-0060 can model their stream on this exemplar.

The sections below ARE the template. Future stream ADRs replicate these headings 1:1.

#### 1. Stream identity

- **Name:** `stream:feedback.received`
- **Purpose:** Notify downstream consumers that a human has applied a feedback label to a Linear issue.
- **Layer:** Observation Layer generates the source signal (the label is applied in Linear, observed by `FeedbackPoller` in `captains_log/feedback.py`).
- **Depends on:** ADR-0041 (transport), ADR-0040 (Linear protocol, label semantics, issue budget).

#### 2. Source

- **Trigger:** `FeedbackPoller._poll_once()` executes once per configured interval (default: daily), queries Linear GraphQL for issues updated in the last 3 days bearing any label in `FEEDBACK_LABEL_NAMES`, and emits a `FeedbackEvent` for each unseen (`issue_id`, `label`) pair.
- **Granularity:** Per-label-application, at-most-once per (issue, label) thanks to the poller's state file (`telemetry/feedback_poller_state.json`).

#### 3. Collection mechanism

- **Capture:** Direct poll of Linear GraphQL via `LinearClient.list_issues(...)` + per-issue `get_issue(...)`.
- **Buffering / batching:** None — each feedback event is processed sequentially; labels on a single issue are prioritized by `_LABEL_PRIORITY` (Rejected > Duplicate > Approved > Deepen > Too Vague > Defer).
- **Graceful degradation:** When Linear is unreachable the poller logs `feedback_poll_failed` and retries on the next scheduled tick. Missed labels are not lost because the poller's state file only records *processed* labels — a label applied during an outage is picked up on the next successful poll.

#### 4. Processing algorithm

- **Analyzer:** `FeedbackPoller._handle_label()` dispatches by label type to one of six handlers in `captains_log/feedback.py`:
  - `Rejected` → archive issue, write `suppressed_fingerprints.json` entry (30-day window).
  - `Approved` → move state to `Approved`.
  - `Deepen` → invoke LLM re-analysis, post comment, apply `Re-evaluated` label.
  - `Too Vague` → invoke LLM refinement, post comment, apply `Refined` label.
  - `Duplicate` → archive issue, link to original, log dedup miss.
  - `Defer` → no-op; re-evaluation at 90 days.
- **Where it runs:** Background — triggered by `BrainstemScheduler._check_linear_feedback()` on a daily schedule.
- **Window / aggregation:** Per-event, no window.
- **Minimum sample size:** N/A — single-event processing.

#### 5. Signal produced

- **Type A — Durable record:** `telemetry/feedback_history/<issue_identifier>.json` (`FeedbackRecord`) for terminal labels; `telemetry/feedback_history/suppressed_fingerprints.json` for `Rejected`.
- **Type B — Bus event:** `FeedbackReceivedEvent` on `stream:feedback.received`.
- **Schema (FeedbackReceivedEvent):**
  ```python
  class FeedbackReceivedEvent(EventBase):
      event_type: Literal["feedback.received"] = "feedback.received"
      issue_id: str
      issue_identifier: str
      label: str
      fingerprint: str | None = None
  ```
  Inherits `trace_id` / `session_id` / `source_component` / `schema_version` from the flattened `EventBase` (D3). The Linear poller has no active request trace, so `trace_id` and `session_id` are left `None`; `source_component="brainstem.scheduler"` identifies the producer.
- **Deduplication:** The poller state file `telemetry/feedback_poller_state.json` ensures each (issue, label) is processed at-most-once; consequently each `FeedbackReceivedEvent` is emitted at-most-once per Linear label application.

#### 6. Full automation cycle

```
1. Human applies label (e.g. "Rejected") to a Linear issue FRE-XXX
   └─ Linear records the label change; no agent signal yet

2. BrainstemScheduler tick (daily, configurable hour)
   └─ calls FeedbackPoller._poll_once()

3. FeedbackPoller queries Linear GraphQL
   └─ list_issues(label=<AgentFeedback.*>, updatedAt="-P3D")
   └─ returns issues whose labels changed recently

4. For each unseen (issue, label):
   └─ FeedbackPoller._handle_label(issue_id, label)
      ├─ Rejected → archive + write suppression entry (disk write)
      ├─ Approved → move to Approved state (Linear write)
      ├─ Deepen   → LLM re-analysis + Linear comment + label swap
      ├─ Too Vague→ LLM refinement + Linear comment + label swap
      ├─ Duplicate→ archive + link
      └─ Defer    → no-op

5. Poller state updated
   └─ telemetry/feedback_poller_state.json records the handled label

6. BrainstemScheduler._publish_feedback_events(feedback_events)
   └─ for each FeedbackEvent:
      ├─ fetch fingerprint from issue description
      ├─ construct FeedbackReceivedEvent
      └─ bus.publish(STREAM_FEEDBACK_RECEIVED, event)
         (best-effort — WARN-logged on failure, not re-raised)

7. cg:insights receives FeedbackReceivedEvent
   └─ InsightsEngine records feedback signal for pattern analysis
      (acceptance rate by category, rejection clusters, etc.)

8. cg:feedback receives FeedbackReceivedEvent
   └─ For Rejected: ensures suppression entry exists (idempotent with step 4)
   └─ For others: no-op

9. Next promotion pipeline run
   └─ checks is_fingerprint_suppressed(fp)
   └─ suppressed fingerprints produce NO new Linear issue for 30 days
   └─ Loop closed: human feedback → agent behaviour change
```

#### 7. Human review interface

- **Dedicated Linear project:** Self-Improvement Pipeline
- **Issue format:** Title `[<category>] <summary>`; body contains `**Category**: \`<cat>\``, `**Scope**: \`<scope>\``, `Observed **N** times`, proposed change block, fingerprint footer (ADR-0030 §Issue format).
- **Label semantics:** Approved / Rejected / Deepen / Too Vague / Duplicate / Defer; full table in ADR-0040 §Decision 2.
- **SLA expectation:** Unreviewed issues with `seen_count >= 3` and `age >= 7 days` are eligible for promotion; unreviewed *labelled* issues have no SLA — the human triages on their own cadence from mobile Linear.

#### 8. End state table

**After Phase 1 (implemented, ADR-0030 + ADR-0040):**

| What exists | What is automated | What is visible |
|-------------|------------------|-----------------|
| `FeedbackPoller`, six label handlers | Daily Linear poll + label dispatch + suppression writes | Linear issues; `telemetry/feedback_history/*.json`; `feedback_handled` logs in ES |
| `FeedbackReceivedEvent` published to `stream:feedback.received` | `cg:insights` and `cg:feedback` subscribers | Insights entries in `agent-insights-*` ES index |
| Suppression file + 30-day fingerprint block | Promotion pipeline auto-suppression | `suppressed_fingerprints.json` |

**After Phase 2 (this ADR — flattened `EventBase`):**

| What exists | What is automated | What is visible |
|-------------|------------------|-----------------|
| Flattened `EventBase` with `trace_id` / `session_id` / `source_component` / `schema_version` on every event; `FeedbackReceivedEvent` inherits them directly | Cross-stream correlation via `trace_id`; producer identity visible without name-matching; all events gain forward-compat via `schema_version` | Kibana saved search "all events by source component"; cross-stream `trace_id` join works without caveats |

#### 9. Loop completeness criteria

The loop is closed when **all** of the following hold:
1. A label applied in Linear produces a `FeedbackReceivedEvent` on the bus within one poll interval (observable in Redis `XLEN stream:feedback.received` and in ES).
2. A `Rejected` label causes `is_fingerprint_suppressed(fp)` to return `True` for 30 days, verifiable against the next promotion-pipeline scan.
3. An `Approved` label changes the Linear issue state, verifiable by re-reading the issue.
4. The same label, applied twice, does not produce two events (idempotency via poller state).
5. A Redis outage during step 1 does not lose the label — on recovery, the poller's state file still has not recorded it, and the next poll re-emits it.

Condition 5 is the durability test that validates the dual-write pattern end-to-end.

---

## Alternatives Considered

### Stream naming

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Hierarchical (`stream:feedback.linear.received`) | Nested domains | Rejected — deeper nesting does not help composability and makes constants longer without clarifying |
| B. Verb-first (`stream:received.feedback`) | Reverse order | Rejected — inconsistent with existing streams; parse-unfriendly |
| **C. `stream:<domain>.<signal>`** | Selected — matches existing taxonomy | Adopted |

### Event base class

| Option | Description | Verdict |
|--------|-------------|---------|
| **A. Flatten `trace_id` etc. into `EventBase`** | Single base; every event carries correlation + evolution fields | **Adopted** — the four fields are observability hygiene, not feedback-specific semantics. Three of eight existing events already carry `trace_id`/`session_id` ad-hoc; nullability on the base cleanly expresses "no trace context" for scheduled events. One-time cost: ten producer call-sites gain `source_component=...`. No data migration. |
| B. Separate `FeedbackEventMixin` | Compose via multiple inheritance | Rejected — complicates `parse_stream_event()` dispatch; Pydantic mixins are awkward |
| C. `FeedbackEventBase(EventBase)` as a second root | New base class for feedback events only; existing events stay on `EventBase` | Rejected — enforced only by code review, not the type system; near-certain to produce a half-migrated hierarchy over time; denies `schema_version` forward-compat to "Phase 1" events that will eventually need to evolve. Earlier drafts of this ADR adopted this option on back-compat grounds that did not survive inspection (no persisted data is lost by flattening; ten producer call-sites is the entire migration cost). |

### Durable write target

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Bus is the durable record (replay from `XRANGE`) | Trust Redis persistence | Rejected — Redis is configured with RDB only; a crash between writes can lose unpersisted data; also, moving Redis off-box or switching backends would lose the record |
| B. Disk-only | No ES writes | Rejected — forgoes Kibana analytics; no cross-session aggregation |
| C. ES-only | No disk writes | Rejected for signals that drive Linear issues — ES outage would lose the promotion trigger |
| **D. Decision-rule selection** | Disk + ES / ES-only / target-store-only by purpose | Adopted |

### Failure handling on bus publish

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Propagate bus failures to caller | Strict consistency | Rejected — would fail the request when Redis is down; violates ADR-0041 §Risks "Redis as single point of failure" mitigation |
| **B. Log-and-swallow bus failures** | Durable write is authoritative | Adopted — matches existing `NoOpBus` semantics |
| C. Buffer and retry in-process | Local retry queue | Rejected — adds complexity; re-indexing job in Phase 3 is a better replay surface |

### Schema evolution

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Versioned streams (`stream:feedback.received.v2`) | New stream per version | Rejected — consumer subscriptions fragment; bus-side schema versioning is a known anti-pattern |
| B. No versioning, break and redeploy | YOLO | Rejected — consumers cannot be upgraded atomically |
| **C. `schema_version` field + new `event_type` for breaks** | Forward-compat for additions, discriminator swap for breaks | Adopted |

### Reference implementation choice

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| Stream 2 (Linear feedback) | Bus-integrated today; has disk durability; dedicated Linear project | Human-driven cadence is unusual — not every stream will have a human in the middle | **Selected** — matches all convention touchpoints and is fully implemented |
| Stream 6 (Memory freshness) | Bus-integrated; high-volume | Target-store-only durability is one of three patterns, not the canonical dual-write | Rejected as reference — too atypical |
| Gate monitoring (ADR-0053) | Newest; most thoroughly spec'd | Not yet implemented; cannot be used as a "real, running" exemplar | Rejected as reference — not yet real |

---

## Consequences

### Positive

- **Every future feedback stream ships with a bus event.** Phase 2 ADRs inherit a contract; they argue *what signal*, not *what naming scheme*.
- **Cross-stream composition becomes buildable.** "Error pattern AND low confidence AND cost anomaly" requires one new consumer subscribing to three existing streams — no producer changes.
- **Durability is no longer a per-stream negotiation.** The D4 decision rule answers the question "do I need a disk write?" deterministically.
- **Forward compatibility is explicit on every event.** All events carry `schema_version`; consumers written today keep working when a producer adds a field tomorrow. This applies uniformly to Phase 1 events too — they are no longer stuck at an implicit v1 forever.
- **Dual-write ordering is explicit.** Production failures follow a documented pattern rather than the current ad-hoc "log and hope" behaviour.
- **Single type root.** `EventBase` is the only base. New event authors cannot pick the "wrong" root; reviewers have no soft rule to enforce. `source_component` being required on the base forces every producer to name itself.
- **ADR-0053 retroactively benefits.** The gate monitoring stream's dual-write posture matches the convention — no rework needed.
- **Stream 2 is documented using the template.** Future ADR authors have a worked, running example to copy.

### Negative

- **Ten producer call-sites carry a `source_component` literal.** One-time migration cost, done as part of this ADR. Value (producer identity visible in every payload) outweighs the addition.
- **The convention is enforced by review, not by the compiler.** A new stream that violates D1 or D2 is only caught if someone notices. Mitigated by code-review checklist line `ADR-0054: new stream uses constant from events/models.py and inherits EventBase` and by `parse_stream_event()` dispatch (an event type not registered there fails at consumer-side parse). Note that `source_component` being required *is* compiler-enforced — any construction without it fails at `model_validate` time.
- **Old ephemeral payloads in Redis become un-parseable on deploy.** A payload sitting in a Redis Stream at the moment of deploy that lacks `source_component` will fail `model_validate` in the upgraded consumer. In practice Redis is RDB-only and consumer groups track `last-delivered-id`, so this affects at most a handful of in-flight events per stream; durable records on disk (for dual-write streams) are preserved.

### Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Phase 2 ADR authors diverge from the convention unintentionally | Medium | Per-ADR review by the FRE-245 owner (project owner) against the 9-section template in D7 |
| Schema version skew between producer and consumer at deploy time | Low | Additive-only rule (Rule 1); unit test round-trip (Rule 3); breaking changes always take a new `event_type` |
| Durable-write failure masks a silent bug (e.g. disk full) | Low | ERROR-level log on durable failure; the producing operation fails loudly, not silently |
| NoOpBus + durable-only operation creates an invisible backlog | Medium | Phase 3 replay job (FRE-TBD) reads durable records and re-emits to the bus; until then, operators are alerted by the WARN-level `event_publish_failed` log |
| `source_component` values drift over time (e.g. renamed modules) | Low | Dotted module path is a reasonable default but not rigidly enforced; consumers that group-by `source_component` can tolerate rename by updating a mapping |

---

## Implementation Priority

| Order | Work | Rationale | Tier |
|-------|------|-----------|------|
| 1 | Flatten `EventBase` in `src/personal_agent/events/models.py` — add `trace_id` / `session_id` / `source_component` / `schema_version` | Foundation — all Phase 2 ADRs (and existing consumers) benefit from the unified contract | Tier-2: Sonnet |
| 2 | Update the ten existing `xadd` producer call-sites to pass `source_component=<dotted-module-path>` | Required field — constructions without it fail `model_validate` | Tier-2: Sonnet |
| 3 | Update event-constructing tests (6 test files, ~40 constructions) to pass `source_component="test"` | Keep the suite green | Tier-3: Haiku |
| 4 | Add unit tests: `source_component` required; `schema_version` default; nullable `trace_id` / `session_id` on scheduled events; forward-compat round-trip (Rule 3) | Quality gate for the convention contract | Tier-2: Sonnet |
| 5 | Add stream-name constants for Phase 2 streams (D1 table) to `events/models.py` | Concrete constants before ADR-0055..0060 draft cite them | Tier-3: Haiku |
| 6 | Add consumer-group constants for Phase 2 groups (D2 table) to `events/models.py` | Same rationale as Step 5 | Tier-3: Haiku |
| 7 | Extend `parse_stream_event()` dispatch to handle Phase 2 types once defined | Dispatcher must know about new types to deserialize | Tier-3: Haiku — per new type |
| 8 | Update `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md` to link to this ADR under D7 template reference | Keep architecture doc canonical | Tier-3: Haiku |
| 9 | Add code-review checklist item `ADR-0054: new stream uses constant from events/models.py; event inherits EventBase; construction includes source_component` to `docs/reference/PR_REVIEW_RUBRIC.md` | Enforcement without compiler support for stream-name discipline | Tier-3: Haiku |

Steps 1–4 constitute the MVP and are implemented together in one change: the contract exists in code, is tested, and is ready to be referenced by ADR-0055 through ADR-0060. Steps 5–9 are follow-on once Phase 2 ADRs are drafted.

---

## Module Placement

Following ADR-0043 (Three-Layer Separation):

| Component | Module | Layer |
|-----------|--------|-------|
| Flattened `EventBase` (correlation + evolution fields) | `src/personal_agent/events/models.py` | Infrastructure (events) |
| Stream / consumer-group constants for Phase 2 | `src/personal_agent/events/models.py` | Infrastructure (events) |
| `parse_stream_event()` dispatch extensions | `src/personal_agent/events/models.py` | Infrastructure (events) |
| Per-stream dual-write producer code (Phase 2) | Owning subsystem (e.g. `brainstem/`, `request_gateway/`) | Execution / Observation per ADR |
| Per-stream consumers (Phase 2) | `src/personal_agent/events/consumers/` | Observation |
| Review checklist | `docs/reference/PR_REVIEW_RUBRIC.md` | Documentation |

All Phase 2 producers live in their owning subsystem module. Consumers live in `events/consumers/` alongside `FreshnessConsumer`. No producer imports a consumer module; no consumer imports from `request_gateway/` internals (it reads events, not producer state). This is the dependency-direction rule from ADR-0043.

---

## Open Questions

1. **Should `source_component` use the dotted module path, or a stable short-code?** Dotted path is the default (e.g. `"request_gateway.monitoring"`), but stable short-codes (`"gate_monitor"`) survive refactors better. Recommend module-path default, with short-code override allowed per ADR for subsystems expecting rename. Decide per stream ADR.

2. **Where does the Phase 3 replay job live?** When the bus is down during a durable write, the event is written to disk/ES but never published. A replay job that rediscovers unpublished durable records and re-emits is noted in D6 but not scoped here. This should be a new FRE issue owned by ADR-0041 maintenance.

3. **Should `EventBase.schema_version` skew trigger a noisier signal than silent tolerance?** Rule 3 makes consumers tolerant of higher versions (Pydantic loads them, unknown fields ignored). The tradeoff is noisy logs vs. silent drops. Keep current design (accept higher versions if Pydantic loads them) and revisit if skew becomes a real operational concern.

4. **How does FRE-233 (ADR-0053) relate to the flattened `EventBase`?** ADR-0053 is not yet implemented; its `GatewayDecisionEvent` inherits from `EventBase` from the start and automatically gains the four contract fields — no two-step migration, no reference to a defunct `FeedbackEventBase`.

---

## References

- FRE-245: Draft ADR — Feedback Stream Bus Convention (this issue)
- FRE-244..FRE-250: Phase 2 feedback stream ADRs that depend on this convention
- ADR-0041: Event Bus via Redis Streams — transport layer
- ADR-0043: Three-Layer Architectural Separation — layering constraints
- ADR-0053: Deterministic Gate Feedback-Loop Monitoring Framework — source of the Feedback Stream ADR Template (§ "Feedback Stream ADR Template")
- ADR-0040: Linear Async Feedback Channel — source of the D7 reference implementation
- ADR-0030: Captain's Log Dedup & Self-Improvement Pipeline — durability pattern for Captain's Log entries
- ADR-0042: Knowledge Graph Freshness via Access Tracking — target-store-only durability pattern
- `src/personal_agent/events/models.py` — home of `EventBase` and all stream/consumer-group constants
- `src/personal_agent/events/bus.py` — `EventBus` protocol and `NoOpBus`
- `src/personal_agent/events/redis_backend.py` — `RedisStreamBus` implementation
- `src/personal_agent/events/consumers/freshness_consumer.py` — exemplar consumer
- `src/personal_agent/brainstem/scheduler.py` (`_publish_feedback_events`) — exemplar publisher
- `src/personal_agent/captains_log/feedback.py` — `FeedbackPoller`, `FeedbackEvent`, `FeedbackRecord`
- `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md` — stream catalog this ADR operationalizes
