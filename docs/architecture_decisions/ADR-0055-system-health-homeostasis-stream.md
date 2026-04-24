# ADR-0055: System Health & Homeostasis Stream

**Status:** Proposed — In Review
**Date:** 2026-04-24
**Deciders:** Single maintainer (FrenchForest)
**Depends on:** ADR-0053 (Feedback Stream ADR Template), ADR-0054 (Feedback Stream Bus Convention), ADR-0041 (Event Bus — Redis Streams)
**Related:** ADR-0042 (brainstem / homeostasis model), ADR-0036 (Expansion Controller)
**Enables:** ADR-0057 Phase 2 (cost-anomaly → ALERT mode response), ADR-0063 PIVOT-2 (action-boundary governance reads live mode state)
**Linear:** [FRE-246](https://linear.app/frenchforest/issue/FRE-246)

---

## Context

### The Mode Manager is built and never runs

The brainstem ships a complete 5-state homeostasis FSM (`NORMAL → ALERT → DEGRADED → LOCKDOWN → RECOVERY`) defined in `src/personal_agent/brainstem/mode_manager.py` and driven by the YAML rules in `config/governance/modes.yaml`. `ModeManager.evaluate_transitions(sensor_data)` reads the active rules, calls `_check_transition_rule()` per target, and — if a rule fires and `_is_transition_allowed()` approves the edge — invokes `transition_to()`, which logs the `MODE_TRANSITION` structlog event.

Every piece exists. None of it runs in production. `evaluate_transitions()` is never called. The singleton `ModeManager` instantiated at startup sits on `Mode.NORMAL` for the lifetime of the process.

Downstream, `service/app.py` compounds the disconnect by hardcoding `Mode.NORMAL` at four call sites when it constructs the gateway `GovernanceContext` — lines 175, 198, 913, 944. Even if `ModeManager` *were* evaluating transitions, the gateway would never see the result. The expansion-gating check at `governance.py:69` (`mode == Mode.NORMAL`) is a `True` constant by construction.

### This is Level 1 / Stream 5 of the four-level observability framework

The feedback-stream catalogue (`docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md`) lists nine streams across four observability levels:

| Level | Observes                                               | Timescale      | State                                           |
|-------|--------------------------------------------------------|----------------|-------------------------------------------------|
| **1** | **System metrics → operational mode**                  | **5 s poll**   | **Built, disconnected — this ADR closes it**    |
| 2     | Gate / pipeline decisions                              | Per-request    | ADR-0053 drafted                                |
| 3     | Application errors — log events, exceptions           | Rolling        | ADR-0056 drafted                                |
| 4     | Self-reflection (Captain's Log)                       | Per-task       | ADR-0030 / ADR-0040 working                     |

Level 1's job is to translate hardware and process signals (CPU, memory, GPU, disk, safety-violation counters) into a machine-readable *mode* that every downstream component can condition on. Stream 5 is the single producer of that signal. Right now the signal is constant.

### Dual-write convention (ADR-0054) applies — and the current wiring violates it

ADR-0054 requires every feedback stream to (a) write durably and (b) publish a typed bus event. `MODE_TRANSITION` already satisfies (a) via `ElasticsearchHandler` — transitions are durable in `agent-logs-*`. It fails (b): there is no `mode.transition` bus event. `stream:mode.transition` is a *reserved* stream name in ADR-0054 D1 and `cg:mode-controller` is a *reserved* consumer group in ADR-0054 D2; both reservations were made against this ADR. Nothing composable exists today.

### ADR-0057 Phase 2 and ADR-0063 PIVOT-2 are blocked on live mode state

Two subsequent ADRs assume a *real* mode signal:

- **ADR-0057 Phase 2** (deferred) routes `severity=high` `InsightsCostAnomalyEvent` to an ALERT transition. That response path is a no-op until `ModeManager.transition_to(ALERT, …)` can actually move the FSM at runtime.
- **ADR-0063 PIVOT-2** (action-boundary governance) reads the live mode in the Stage-3 governance evaluator to gate tool actions by operational state. It requires `get_current_mode()` to return something other than `NORMAL`.

Both ADRs are drafted; both will remain untestable until Stream 5 closes.

### Calibration feedback: mode transitions are themselves observable

A secondary opportunity: once transitions are a typed bus event, *transition cadence* becomes a first-class signal. If a given edge fires 3+ times within 10 minutes under normal load, the most likely explanation is that the threshold is mis-tuned for this deployment. The same feedback-loop pattern used by ADR-0056 (error patterns) and ADR-0057 (insights) applies: emit a `CaptainLogEntry(RELIABILITY, scope=mode_calibration)`, let the ADR-0030 dedup/promotion pipeline surface a Linear issue, close the loop through human review.

---

## Decision Drivers

1. **Expansion gating is permanently wedged open.** `governance.py:69` permits HYBRID unconditionally because `mode` is `NORMAL` by construction, regardless of system stress.
2. **The four hardcoded `Mode.NORMAL` sites in `service/app.py` prevent any degraded-safe gateway configuration.** Even a correct `ModeManager` cannot reach the gateway today.
3. **ADR-0054 dual-write convention.** Sensor data that flows to the bus is composable and observable. Mode transitions are the exact case the convention was written for.
4. **`stream:mode.transition` and `cg:mode-controller` are already reserved.** ADR-0054 D1/D2 reserved both names against this ADR — implement them now, before ADR-0057 Phase 2 and ADR-0063 PIVOT-2 land and need them.
5. **Downstream ADRs block on this signal.** ADR-0057 Phase 2 (cost-anomaly → ALERT) and ADR-0063 PIVOT-2 (live-mode-aware governance) both require `get_current_mode()` to return a real value.
6. **`MetricsDaemon` already collects the samples.** A 5 s `psutil + powermetrics` poll loop with a bounded in-memory ring buffer exists. Wiring the ring buffer to a bus stream extends what is there — no new sensor code.
7. **Calibration feedback closes the loop.** Transition cadence → Captain's Log → Linear "System Health & Homeostasis" project follows the same ADR-0030/ADR-0040 pattern as ADR-0056 and ADR-0057.

---

## Decision

### D1: Sources — Two bus streams

Two streams carry Stream 5's signal, in keeping with ADR-0054's rule that one stream = one signal shape.

| Stream                     | Producer                                   | Cadence            | Event type                        |
|----------------------------|--------------------------------------------|--------------------|-----------------------------------|
| `stream:metrics.sampled`   | `brainstem.sensors.metrics_daemon`         | 5 s (configurable) | `MetricsSampledEvent`             |
| `stream:mode.transition`   | `brainstem.mode_manager`                   | On transition      | `ModeTransitionEvent`             |

**`stream:metrics.sampled`** carries raw 5-second samples. Producer is `MetricsDaemon._poll_loop()` — the publish is fire-and-forget (bus failures logged at `warning`, never propagated) and gated behind `settings.mode_controller_enabled`. The stream is bounded by `MAXLEN ~ 720` (≈ 1 h of samples at 5 s), configurable via `metrics_sampled_stream_maxlen`.

**`stream:mode.transition`** carries FSM transitions. Producer is `ModeManager.transition_to()` — dual-write: the existing `MODE_TRANSITION` structlog call is preserved verbatim (Layer C durable write) and a `ModeTransitionEvent` is published after the structlog succeeds (Layer B bus write). The stream is bounded by `MAXLEN ~ 1000` (transitions are rare; this is months of history).

ADR-0054 D1 reserved `stream:mode.transition` explicitly for this ADR. `stream:metrics.sampled` is a new name introduced here following the `<domain>.<signal>` convention (`metrics` as domain, `sampled` as signal). No conflict with existing streams.

### D2: Collection — `cg:mode-controller` consumer

One consumer group, `cg:mode-controller`, subscribes to both streams. ADR-0054 D2 reserved the group name for this ADR.

**On `MetricsSampledEvent`:** append to an in-memory `deque(maxlen=mode_window_size)` (default 12 samples = 60 s at 5 s cadence). If the deque is full and ≥ `mode_evaluation_interval_seconds` (default 30) have elapsed since the last evaluation, aggregate the window (see D4), construct a `sensor_data: dict[str, Any]`, and call `ModeManager.evaluate_transitions(sensor_data)`. Evaluation is throttled — a fresh sample arriving every 5 s does not trigger 5-second evaluation cadence.

**On `ModeTransitionEvent`:** update a per-edge `(from_mode, to_mode)` cadence counter in a 10-minute rolling window. If the count in the window ≥ `mode_calibration_anomaly_threshold` (default 3), emit a Captain's Log calibration proposal (D5). The proposal is fingerprinted on `(from_mode, to_mode)` so repeated breaches within the same ADR-0040 suppression window do not re-propose.

**Fallback when Redis is down:** `NoOpBus.subscribe()` silently discards; no evaluation runs. The FSM stays at whatever mode it was last in (NORMAL on cold start). This is acceptable — homeostasis is best-effort under bus failure, not on-call alerting. On bus recovery, the next `MetricsSampledEvent` re-seeds the window.

**Fallback when `evaluate_transitions()` raises:** the consumer catches (no bare `except`) and logs `mode_controller_evaluation_failed` at `warning`. The window is not advanced; the next sample re-attempts. One bad rule evaluation does not poison the consumer.

### D3: Data model — Three layers

Following the ADR-0054 Layer A / B / C convention.

#### Layer A — Ephemeral (in-memory)

- **`MetricsDaemon` ring buffer:** unchanged; already exists. Holds the last N 5-second samples in process.
- **Consumer rolling window:** `deque[MetricsSampledEvent]` of size `mode_window_size`, discarded on process restart.
- **Consumer cadence counter:** `dict[tuple[Mode, Mode], deque[datetime]]` for 10-minute rolling edge cadence.

No persistence. Process restart resets the window; the next sample re-seeds it. Consumer lag beyond the 60 s window means some samples are dropped from evaluation — acceptable, because the next evaluation sees a fresh window.

#### Layer B — Bus events

```python
class MetricsSampledEvent(EventBase):
    """One 5-second sample from MetricsDaemon.

    Publishers: brainstem.sensors.metrics_daemon
    Consumers:  cg:mode-controller  (this ADR)
                future: cg:cost-dashboard, cg:thermal-monitor  (out of scope)
    """

    event_type: Literal["metrics.sampled"] = "metrics.sampled"
    sample_timestamp: datetime
    metrics: Mapping[str, float]          # e.g. {"perf_system_cpu_load": 0.72, "perf_system_mem_used": 0.58, "perf_system_gpu_load": 0.31, ...}
    sample_interval_seconds: float        # nominal cadence (5.0 by default)
    # trace_id / session_id: None (system sample, not request-correlated; ADR-0054 D3)
    # source_component: "brainstem.sensors.metrics_daemon"


The `metrics` dict is the raw output of `poll_system_metrics()` — keys use the `perf_system_*` / `safety_*` prefix convention from the sensor layer (e.g. `perf_system_cpu_load`, `perf_system_mem_used`, `perf_system_gpu_load`, `perf_system_disk_used`, `safety_violations`).

class ModeTransitionEvent(EventBase):
    """FSM transition fired by ModeManager.transition_to().

    Publishers: brainstem.mode_manager
    Consumers:  cg:mode-controller  (this ADR — cadence counter)
                future: cg:governance-live, cg:insights-router  (ADR-0063, ADR-0057 Phase 2)
    """

    event_type: Literal["mode.transition"] = "mode.transition"
    from_mode: Mode
    to_mode: Mode
    reason: str                           # matched rule name / operator reason
    sensor_snapshot: Mapping[str, float] = Field(default_factory=dict)
    transition_index: int                 # monotonic counter within the process lifetime
    # trace_id / session_id: None (system transition; ADR-0054 D3)
    # source_component: "brainstem.mode_manager"
```

Both events inherit the flattened `EventBase` per ADR-0054 D3: `trace_id` / `session_id` are nullable (system-scoped, not request-correlated), `source_component` is required, `schema_version` defaults to `1`. Forward compatibility follows Rule 1 (additive fields) and Rule 2 (new `event_type` for breaking changes).

#### Layer C — Durable writes

- **`MODE_TRANSITION` structlog event** → `ElasticsearchHandler` → `agent-logs-YYYY-MM-DD` (existing behaviour, preserved verbatim).
- **Captain's Log calibration proposals** → `telemetry/captains_log/CL-YYYYMMDD-*.json` (existing ADR-0030 machinery) → promotion → Linear "System Health & Homeostasis" project.

No new durable sink is introduced. The ES index is the authoritative transition history; Captain's Log is the authoritative calibration-proposal store.

### D4: Transition algorithm

The FSM's `_check_transition_rule` + `_is_transition_allowed` logic is unchanged. What this ADR changes is *how `sensor_data` is produced* before `evaluate_transitions()` is called.

**Window aggregation** (consumer-side, every 30 s over the last 60 s of samples):

| Metric field                | Aggregation | Rationale                                                      |
|-----------------------------|-------------|----------------------------------------------------------------|
| `perf_system_cpu_load`      | `mean`      | Smooths momentary spikes; rule thresholds match steady load    |
| `perf_system_mem_used`      | `mean`      | Same as CPU                                                    |
| `perf_system_gpu_load`      | `max`       | GPU bursts are the interesting signal; mean hides them         |
| `perf_system_disk_used`     | `last`      | Slow-changing gauge; mean and last are near-identical          |
| `safety_violations`         | `sum`       | Count semantic — any violation in the window counts            |
| `safety_violations_5m`      | default 0   | Rolling counter maintained elsewhere; default when absent     |

_Note: `metrics_daemon.py` currently looks up `perf_system_disk_usage_percent` internally — the consumer should use the key present in the published `MetricsSampledEvent.metrics` dict and verify the exact key during implementation._

The dict is flat (`{metric_name: aggregated_value}`) to match the existing `evaluate_transitions(sensor_data: dict[str, Any])` signature. Rule authors writing `config/governance/modes.yaml` continue to reference metrics by name; only the producer of the dict changes.

**Per-condition windowing is *not* implemented in Phase 1.** `TransitionCondition` in `modes.yaml` carries `duration_seconds` / `window_seconds` fields that the current `_check_transition_rule` ignores. Honouring them requires per-rule sample queues, which is a scope expansion. See Open Question 1.

`modes.yaml` defines `ALERT_to_NORMAL` with `requires_human_approval: true`, but `ModeManager._check_transition_rule()` does not read this field — the flag is silently ignored. The consumer-driven `ALERT→NORMAL` transition will therefore proceed automatically. Honouring `requires_human_approval` is tracked in Open Question 2 (recovery path) and deferred to Phase 2.

Only two transition rules are currently defined in `config/governance/modes.yaml`: `NORMAL_to_ALERT` and `ALERT_to_NORMAL`. Other edges permitted by `_is_transition_allowed` (e.g. `NORMAL → DEGRADED`, `ALERT → LOCKDOWN`) will not fire without corresponding rule entries — Phase 2 rule expansion is out of scope.

### D5: Signal — `CaptainLogEntry(RELIABILITY, scope=mode_calibration)`

When the cadence counter trips (`count(edge) ≥ mode_calibration_anomaly_threshold` in the 10-minute window), the consumer emits a Captain's Log entry via the existing `CaptainLogManager.save_entry()` path.

```python
CaptainLogEntry(
    entry_id="",                          # generated by CaptainLogManager
    type=CaptainLogEntryType.CONFIG_PROPOSAL,
    title=f"[Mode Calibration] {from_mode}→{to_mode} threshold too sensitive — {count} transitions in 10 min",
    rationale=(
        f"{count} {from_mode}→{to_mode} transitions fired in the last 10 minutes. "
        f"Typical cadence under normal load is ≤ 1 per hour per edge. "
        f"Most recent sensor snapshot: {snapshot}. "
        f"Matched rule: {reason}."
    ),
    proposed_change=ProposedChange(
        what=f"Re-tune {from_mode}→{to_mode} threshold in config/governance/modes.yaml",
        why=(
            "Excessive transition cadence on a single edge indicates the threshold "
            "or window is mis-calibrated for this deployment's steady-state load. "
            "Either the trigger metric is noisier than the rule assumes, or the "
            "threshold is too close to normal operating range."
        ),
        how=(
            "1) Review agent-logs-* MODE_TRANSITION entries over the last 10 min in Kibana.\n"
            "2) Correlate with stream:metrics.sampled values at each transition point.\n"
            "3) Decide between widening the window (duration_seconds), raising the threshold, "
            "or adding a hysteresis guard. Update config/governance/modes.yaml accordingly."
        ),
        category=ChangeCategory.RELIABILITY,
        scope="mode_calibration",
        fingerprint=_calibration_fingerprint(from_mode, to_mode),
    ),
    supporting_metrics=[
        f"edge: {from_mode}->{to_mode}",
        f"count_10min: {count}",
        f"window_minutes: 10",
    ],
    metrics_structured=[
        Metric(name="transition_count_10min", value=count, unit="count"),
        Metric(name="window_minutes", value=10, unit="min"),
    ],
)
```

**Fingerprint formula:** `sha256(f"mode_calibration|{from_mode}->{to_mode}".encode()).hexdigest()[:16]`. Fingerprint-based dedup is handled by `CaptainLogManager.save_entry()` per ADR-0030; the same edge tripping multiple times within the ADR-0040 suppression window increments `seen_count` instead of creating duplicate files.

### D6: Surfacing Channels

**Primary — Linear "System Health & Homeostasis":**

`CaptainLogManager` entry → ADR-0030 consolidation → `PromotionPipeline.scan_promotable_entries()` after `seen_count ≥ 3`, `age ≥ 7 d` → Linear issue in the dedicated project. Issue format in the Linear-project section below. Labels inherit from ADR-0040.

**Secondary — Kibana `agent-logs-*` MODE_TRANSITION:**

The existing `MODE_TRANSITION` event already lands in ES. The "Agent Reliability" Kibana dashboard gains two panels:

- "Mode transitions — 7 d timeline": date histogram on `event_type:"MODE_TRANSITION"`, stacked by `from_mode → to_mode`.
- "Mode edge cadence — 24 h top-N": terms aggregation on `{from_mode}->{to_mode}`, sorted by count desc.

No new dashboard is introduced.

**Tertiary — Programmatic (`redis-cli XREAD`):**

`redis-cli XREAD COUNT 10 STREAMS stream:mode.transition $` streams transitions live for operator debugging. Out-of-process consumers (future ADRs) subscribe via the `cg:*` convention.

### D7: Full automation cycle

```
1. MetricsDaemon._poll_loop() polls psutil + powermetrics every 5 s
   └─ builds metrics dict, appends to in-process ring buffer (DURABLE-in-memory)
   └─ if settings.mode_controller_enabled: publishes MetricsSampledEvent to
      stream:metrics.sampled (fire-and-forget, bus-failure swallowed at warning)

2. cg:mode-controller receives MetricsSampledEvent
   └─ appends to rolling deque (maxlen=mode_window_size, default 12 = 60 s)
   └─ if ≥ mode_evaluation_interval_seconds since last eval AND deque full:
      a) aggregate window per D4 → sensor_data dict
      b) call ModeManager.evaluate_transitions(sensor_data)
      c) update last_evaluated_at timestamp

3. evaluate_transitions() iterates active rules
   └─ if a rule fires AND _is_transition_allowed(from, to):
      a) ModeManager.transition_to(new_mode, reason, sensor_data)
      b) existing MODE_TRANSITION structlog call → ES (DURABLE)
      c) publish ModeTransitionEvent to stream:mode.transition (BUS)

4. cg:mode-controller also receives ModeTransitionEvent
   └─ appends timestamp to per-edge cadence deque (10-min rolling)
   └─ if count(edge) ≥ mode_calibration_anomaly_threshold:
      - build CaptainLogEntry(CONFIG_PROPOSAL, category=RELIABILITY,
        scope="mode_calibration", fingerprint=…)
      - CaptainLogManager.save_entry()  (fingerprint dedup — ADR-0030)

5. Next consolidation.completed → cg:promotion → PromotionPipeline
   └─ filters: status=AWAITING_APPROVAL, seen_count ≥ 3, age ≥ 7 d
   └─ creates Linear issue in "System Health & Homeostasis" project
   └─ publishes PromotionIssueCreatedEvent

6. Human reviews Linear issue
   └─ labels: Approved / Rejected / Deepen / Too Vague / Defer
   └─ FeedbackPoller daily dispatch (ADR-0040) handles the label:
      - Rejected  → 30-day suppression on fingerprint; cancel issue
      - Approved  → human tunes modes.yaml; closes issue
      - Deepen    → LLM re-analysis with extended sensor context; comment

7. Downstream consumers (future, out of scope here) subscribe to
   stream:mode.transition:
   - ADR-0057 Phase 2: cg:insights-router routes cost-anomaly → transition_to(ALERT)
   - ADR-0063 PIVOT-2: governance evaluator reads current mode from
     get_current_mode() which is now backed by a real FSM, not a constant
```

Loop closed. Every rejection suppresses a false-positive calibration for 30 days; every approval becomes a concrete `modes.yaml` edit.

### D8: Scope Boundary

In scope:

- `MetricsSampledEvent` / `ModeTransitionEvent` definitions and parse dispatch.
- `MetricsDaemon` producer hook (fire-and-forget publish).
- `ModeManager.transition_to` dual-write (structlog preserved; bus event added).
- `cg:mode-controller` consumer, rolling window, throttled evaluation, cadence counter, calibration-proposal emission.
- Replacement of the four hardcoded `Mode.NORMAL` sites in `service/app.py` with `get_current_mode()`.
- Five new config settings (see Module Placement).
- Two Kibana panels on the existing "Agent Reliability" dashboard.
- Unit tests for window aggregation, throttle, cadence counter, fingerprint stability.

Out of scope:

- **`duration_seconds` / `window_seconds` per-condition honouring** in `evaluate_transitions`. Requires per-rule sample queues — Phase 2. See Open Question 1.
- **`DEGRADED → NORMAL` direct recovery path**. Not currently allowed by `_is_transition_allowed`; recovery goes `DEGRADED → LOCKDOWN → RECOVERY → NORMAL`. May be intentional (homeostasis model) or an oversight — flagged for Phase 2 ADR. See Open Question 2.
- **Multi-host metrics topology.** The agent runs single-host today; `stream:metrics.sampled` from multiple producers would need a host-identity field. Out of scope.
- **Refactoring the two `sensors.py` files** (`brainstem/sensors.py` and `brainstem/sensors/sensors.py`). Separate cleanup PR. See Open Question 4.
- **Automatic `modes.yaml` edits** in response to calibration proposals. Proposals surface to humans; humans apply. Slice 3 / self-modification is a separate ADR.

---

## Alternatives Considered

### Collection mechanism

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Scheduler loop polls the MetricsDaemon ring buffer directly | `BrainstemScheduler` wakes every 30 s, reads the in-process buffer, calls `evaluate_transitions()` | Rejected — couples scheduler to daemon internals; bypasses bus; future consumers (ADR-0057 Phase 2, ADR-0063 PIVOT-2) cannot subscribe without re-plumbing |
| B. In-process call from `MetricsDaemon._poll_loop` directly to `ModeManager.evaluate_transitions()` | Every 5 s sample triggers a full evaluation in the daemon task | Rejected — evaluates 12× too often; sensor task and FSM task become one; violates ADR-0054 (no bus event = not composable) |
| **C. Bus consumer on `stream:metrics.sampled` with throttled window evaluation** | Daemon publishes 5-s samples; `cg:mode-controller` windows 60 s, evaluates every 30 s | **Selected** — ADR-0054 convention; `cg:mode-controller` name already reserved in ADR-0054 D2; evaluation cadence decoupled from sample cadence |
| D. Periodic scheduled job (cron) independent of MetricsDaemon | New cron entry calls `MetricsDaemon.get_snapshot()` + `evaluate_transitions()` | Rejected — two scheduling surfaces; daemon snapshot API does not exist; bus path is simpler |

### Stream shape

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Single `stream:mode.driver` carrying both samples and transitions | One stream, two event types, consumers filter by `event_type` | Rejected — ADR-0054 D1 Rule 3 ("one stream = one signal shape"); mixing rates (5 s samples vs. rare transitions) makes `MAXLEN` tuning meaningless |
| **B. Two streams: `stream:metrics.sampled` (hot) + `stream:mode.transition` (rare)** | Separate streams, bounded independently, one consumer reads both | **Selected** — each stream has a coherent cadence and `MAXLEN`; downstream consumers can subscribe to transitions without ingesting the sample firehose |
| C. Three streams: add `stream:mode.evaluated` for non-transition evaluations | Emit a bus event even when no rule fires, for observability | Rejected — adds ~2 events/min of nothing; Kibana `MODE_TRANSITION` + `stream:metrics.sampled` already cover the observability |

### Dual-write vs. bus-only for transitions

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Drop the `MODE_TRANSITION` structlog; publish bus event only | Single source of truth; bus → ES indexer fills Kibana | Rejected — structlog already works, is the durable write per ADR-0054 D4 ordering rule (durable before bus), and the Kibana dashboard depends on the existing field names. Changing the durable path risks breaking the dashboard |
| **B. Dual-write: structlog + bus event** | Preserve the existing MODE_TRANSITION call; add a bus publish after success | **Selected** — ADR-0054 D4 compliant; structlog remains the primary durable write; bus publish failures do not block the transition |
| C. Bus publish before structlog | Bus is the "primary" | Rejected — violates ADR-0054 D4 (durable-write precedes bus-publish) |

### Calibration surfacing

| Option | Description | Verdict |
|--------|-------------|---------|
| A. Log-only | Emit `mode_calibration_anomaly` at `warning`; humans watch Kibana | Rejected — repeats the failure mode the framework exists to fix; Kibana is for humans only |
| B. Direct Linear issue creation from the consumer | Bypass Captain's Log; write Linear API calls directly | Rejected — skips ADR-0030 dedup (same edge would create N issues per day); skips ADR-0040 suppression (rejected calibrations would re-surface) |
| **C. `CaptainLogEntry(RELIABILITY, scope=mode_calibration)` → promotion → Linear** | Standard pipeline; fingerprint dedup; 30-day suppression on `Rejected` | **Selected** — mirrors ADR-0056 D5 and ADR-0057 D7; reuses everything |

---

## Consequences

### Positive

- **Expansion gating becomes live.** `governance.py:69` reads a real `mode` — HYBRID is suppressed under ALERT, DEGRADED, or LOCKDOWN.
- **Mode-aware tool checks work.** The `governance/` layer (and, once ADR-0063 PIVOT-2 lands, the action-boundary evaluator) can condition behaviour on operational state.
- **Unblocks downstream ADRs.** ADR-0057 Phase 2 can wire `severity=high` cost anomalies to ALERT transitions. ADR-0063 PIVOT-2 can gate tool actions by live mode.
- **Zero new infrastructure.** Reuses Redis Streams, the existing `EventBus`, `MetricsDaemon`, `ModeManager`, structlog, Captain's Log, promotion pipeline, Linear. Two new event types; one new consumer group; five new config fields.
- **Composability.** Any future module can subscribe to `stream:mode.transition` without touching `mode_manager.py`. Metrics consumers (thermal monitor, cost dashboard) can subscribe to `stream:metrics.sampled` without touching `MetricsDaemon`.
- **Calibration feedback loop.** Bad thresholds surface as Linear issues automatically; good thresholds are confirmed by silence.

### Negative

- **New consumer group `cg:mode-controller`.** One more subscription to register, start, and stop in `service/app.py` lifespan.
- **Event volume on `stream:metrics.sampled` ≈ 17 280/day** (5 s cadence × 86 400 s). Bounded by `MAXLEN=720`, so stream size is ≈ 1 h of samples. Per-event size ≈ 250 B → stream memory ≈ 180 KB steady-state. Acceptable.
- **Two new event types.** `MetricsSampledEvent` and `ModeTransitionEvent`; `parse_stream_event()` gains two dispatch arms.
- **`service/app.py` diff touches 4 call sites.** Each `Mode.NORMAL` literal replaced with `get_current_mode()`; behaviour-neutral under cold start, but the wiring path is now live.
- **Window aggregation is deliberately simple.** Mean/max/last/sum do not honour the `duration_seconds` field on individual `TransitionCondition` rules. Phase 2 work.

### Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Spurious transitions from momentary CPU spikes | Medium | 60 s window + 30 s evaluation throttle absorb blips; calibration proposals surface genuinely bad thresholds |
| Consumer lag blocks evaluation under bus backpressure | Low | `cg:mode-controller` reads one stream at 5 s cadence — well below any plausible Redis saturation; `NoOpBus` fallback if Redis is down |
| `DEGRADED → NORMAL` gap leaves the system stuck in LOCKDOWN/RECOVERY | Low | Pre-existing behaviour; flagged as Open Question 2 for Phase 2. Operators can manually call `transition_to(NORMAL, reason="manual")` as a workaround |
| Calibration proposals flood Captain's Log during an actual outage | Medium | Fingerprint dedup at `CaptainLogManager.save_entry()` coalesces per edge; 30-day ADR-0040 suppression blocks repeat proposals after a `Rejected` label |
| `MetricsDaemon` publish blocks sensor loop under bus failure | Low | Publish is fire-and-forget (`asyncio.create_task` + suppressed exceptions); daemon loop never awaits the publish result |
| Two-stream design confuses future consumers | Low | ADR-0054 D1/D2 reservations + this ADR's D1 table document both streams explicitly |

---

## Implementation Priority

### Phase 1 — MVP (ordered for a working loop at step 6)

| Order | Work | Rationale | Tier |
|-------|------|-----------|------|
| 1 | `MetricsSampledEvent`, `ModeTransitionEvent`, stream/cg constants in `events/models.py` | Types first | Tier-3: Haiku |
| 2 | `parse_stream_event()` dispatch arms for both events | Deserialisation | Tier-3: Haiku |
| 3 | `MetricsDaemon._poll_loop()` publish hook behind `settings.mode_controller_enabled` | Producer A | Tier-2: Sonnet |
| 4 | `ModeManager.transition_to()` dual-write — structlog preserved, bus event added | Producer B | Tier-2: Sonnet |
| 5 | `brainstem/consumers/mode_controller.py::ModeControllerConsumer` — window, throttle, cadence counter, calibration proposal | Core consumer | Tier-2: Sonnet |
| 6 | `service/app.py` lifespan — subscribe `cg:mode-controller` to both streams; replace 4 `Mode.NORMAL` literals with `get_current_mode()` | Integration | Tier-2: Sonnet |
| 7 | Config settings: `mode_controller_enabled` (default False for MVP, flipped after soak), `mode_window_size` (12), `mode_evaluation_interval_seconds` (30), `mode_calibration_anomaly_threshold` (3), `metrics_sampled_stream_maxlen` (720) | Safe rollout | Tier-3: Haiku |
| 8 | Unit tests — window aggregation, throttle, cadence counter, fingerprint stability, `NoOpBus` fallback | Quality gate | Tier-2: Sonnet |
| 9 | Kibana panels on "Agent Reliability" — transitions timeline + edge cadence top-N | Visualisation | Tier-3: Haiku |
| 10 | Linear project "System Health & Homeostasis" — confirm labels/priority mapping per ADR-0040 | Operational | Tier-3: Haiku |

Steps 1–6 constitute the MVP (produce, consume, transition, live mode reaches gateway). Steps 7–10 add the feedback loop, visualisation, and operational polish.

### Phase 2 — Deferred

| Order | Work | Rationale |
|-------|------|-----------|
| 1 | Per-condition `duration_seconds` / `window_seconds` honouring in `_check_transition_rule` (per-rule sample queues) | Closes Open Question 1 |
| 2 | `DEGRADED → NORMAL` recovery path resolution (decision + code) | Closes Open Question 2 |
| 3 | Multi-host metrics stream topology (host-identity field + aggregation) | Future multi-deployment story |
| 4 | `sensors.py` consolidation (two files → one) | Closes Open Question 4; separate cleanup PR |

---

## Module Placement

Following ADR-0043 (Three-Layer Separation): all components live in the Observation/Control Layer. Execution Layer modules (gateway, orchestrator, tools) import *from* `brainstem` but not vice versa.

| Component | Module | Layer |
|-----------|--------|-------|
| `MetricsSampledEvent`, `ModeTransitionEvent` | `src/personal_agent/events/models.py` | B (Infrastructure / event types) |
| `STREAM_METRICS_SAMPLED`, `STREAM_MODE_TRANSITION`, `CG_MODE_CONTROLLER` constants | `src/personal_agent/events/models.py` | — |
| `parse_stream_event()` dispatch | `src/personal_agent/events/models.py` | — |
| Metrics publish hook | `src/personal_agent/brainstem/sensors/metrics_daemon.py` | A → B |
| Transition publish hook (inside `transition_to`) | `src/personal_agent/brainstem/mode_manager.py` | A → B |
| `ModeControllerConsumer` — window, throttle, cadence, calibration emission | `src/personal_agent/brainstem/consumers/mode_controller.py` (new) | B → FSM → C |
| Config settings (5 fields) | `src/personal_agent/config/settings.py` | — |
| Service lifespan wiring + `Mode.NORMAL` → `get_current_mode()` replacement | `src/personal_agent/service/app.py` | — |

No Execution Layer module is modified except `service/app.py` (wiring only — no business-logic change).

---

## Open Questions

These are unresolved at ADR acceptance time and will be answered during implementation or in follow-up ADRs.

1. **Should `duration_seconds` / `window_seconds` on `TransitionCondition` be honoured in Phase 1?** The YAML schema allows per-condition windowing ("CPU > 0.85 for 30 s" vs. "CPU > 0.85 right now") but `_check_transition_rule` ignores those fields today. Honouring them requires per-rule sample queues — a scope expansion. Recommendation: Phase 2. Risk: Phase 1 transitions fire faster than rule authors expect for conditions marked with long `duration_seconds`. Mitigate with documentation and calibration proposals.

2. **`DEGRADED → NORMAL` is not in the allowed-transitions set.** Recovery path is `DEGRADED → LOCKDOWN → RECOVERY → NORMAL`. Is this intentional (homeostasis model: once degraded, you must prove recovery) or an oversight? Flag for a Phase 2 ADR. In the meantime, operators can manually invoke `ModeManager.transition_to(NORMAL, reason="manual")` as an escape hatch.

3. **Calibration-proposal cadence threshold default (3 transitions / 10 min).** This is a guess. After 30 days of Phase 1 data, revisit — some edges may need `≥ 5`, others `≥ 2`. Implemented as `mode_calibration_anomaly_threshold` for runtime tuning.

4. **Two `sensors.py` files (`brainstem/sensors.py` and `brainstem/sensors/sensors.py`).** Confusing but not harmful; the `MetricsDaemon` imports the right one. Consolidate in a separate cleanup PR; not blocking this ADR.

5. **`stream:metrics.sampled` `MAXLEN=720` (≈ 1 h at 5 s) — right size or parameterise?** Implemented as `metrics_sampled_stream_maxlen` settings field so it is tunable without code change. 1 h is ample for a 60 s rolling window; much larger values would cost Redis memory without benefit. If future consumers want longer history, revisit.

---

## Dedicated Linear Project — System Health & Homeostasis

Mode-calibration proposals and sensor-threshold adjustments land in a dedicated Linear project named **"System Health & Homeostasis"** (existing project; see `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md`).

### Project configuration

| Field | Value |
|-------|-------|
| Project name | System Health & Homeostasis |
| Team | FrenchForest |
| Default issue state | Needs Approval |
| Labels on creation | `PersonalAgent`, `Improvement`, `Tier-2:Sonnet` |
| Priority mapping | `count_10min ≥ 10` → High; `count_10min ≥ 5` → Normal; else Low |

### Issue format

```
Title: [Mode Calibration] {from_mode}→{to_mode} threshold too sensitive — N transitions in T min
  e.g. "[Mode Calibration] NORMAL→ALERT threshold too sensitive — 6 transitions in 10 min"

Body:
  ## Observed cadence
  Edge:                NORMAL → ALERT
  Transitions (10m):   6
  First in window:     2026-04-24T09:12:03Z
  Last in window:      2026-04-24T09:19:47Z
  Fingerprint:         b7c1e9f3d4a5b6c7
  Seen count:          3 (pattern has fired 3 consolidations)

  ## Affected rule
  Name:       high_cpu_load_alert
  Trigger:    cpu_load > 0.85
  From:       NORMAL
  To:         ALERT

  ## Proposed adjustment
  Either widen the detection window (`duration_seconds` on the condition),
  raise the threshold (e.g. 0.85 → 0.90), or introduce a hysteresis guard
  to avoid rapid re-triggering. See stream:metrics.sampled in Redis and
  MODE_TRANSITION events in Kibana for the full sample record.

  ## Evidence
    mean(cpu_load) across 6 transitions:   0.87
    peak(cpu_load):                         0.94
    mean(mem_used):                         0.61
    mean(gpu_load):                         0.23
    representative reason:                  "cpu_load > 0.85"

  ## Sample transitions (trace pointers)
  - agent-logs-2026-04-24 _id=abc123
  - agent-logs-2026-04-24 _id=def456
  - agent-logs-2026-04-24 _id=ghi789
```

### Feedback labels (inherited from ADR-0040)

| Label | Meaning for calibration proposals |
|-------|-----------------------------------|
| Approved | Proceed with the proposed adjustment; human edits `modes.yaml` |
| Rejected | Cadence is expected for this workload; suppress fingerprint for 30 days |
| Deepen | Re-run cadence analysis over a wider window; post refined proposal as comment |
| Too Vague | Refined proposal with per-sample metric breakdown at each transition |
| Defer | Re-evaluate on next breach; no suppression |

---

## End State — What Exists, What Is Automated, What Is Visible

### After Phase 1 MVP (Implementation Priority steps 1–6, flag off)

| What exists | What is automated | What is visible |
|-------------|-------------------|-----------------|
| `MetricsSampledEvent`, `ModeTransitionEvent`, stream/cg constants in `events/models.py` | Nothing yet — `settings.mode_controller_enabled=False` by default | `service/app.py` reads `get_current_mode()` — returns `NORMAL` on cold start (identical to today's behaviour) |
| `ModeControllerConsumer` registered in service lifespan | `cg:mode-controller` subscribed but dormant until flag flips | Bus streams exist in Redis (`XLEN stream:metrics.sampled` = 0 until enabled) |
| `MetricsDaemon` publish hook gated on flag | Sensor loop runs as today; publish is skipped while flag is off | `MODE_TRANSITION` structlog events flow to ES unchanged |

Human action required: none during the dark period. Flip the flag after verifying the consumer registers cleanly and the publish path does not leak memory.

### After Phase 1 complete (flag on; steps 7–10 finished)

| What exists | What is automated | What is visible |
|-------------|-------------------|-----------------|
| `stream:metrics.sampled` receives one `MetricsSampledEvent` every 5 s | `cg:mode-controller` aggregates 60 s windows and calls `evaluate_transitions()` every 30 s | Kibana panels on "Agent Reliability" show mode transitions over time + edge cadence |
| `stream:mode.transition` receives a `ModeTransitionEvent` per FSM edge | Transitions dual-write: structlog (existing) + bus event (new) | `redis-cli XREAD STREAMS stream:mode.transition $` streams live transitions |
| `get_current_mode()` returns the live FSM state | Calibration proposals flow through ADR-0030 dedup → promotion → Linear | Linear "System Health & Homeostasis" issues appear when an edge trips ≥ 3 times in 10 min |

Human action required: review and label Linear issues in "System Health & Homeostasis". Everything else is automatic.

### After Phase 2 (per-condition windowing + recovery path)

| What exists | What is automated | What is visible |
|-------------|-------------------|-----------------|
| Per-rule sample queues honouring `TransitionCondition.duration_seconds` | Rules that require "N seconds of sustained breach" fire only when the sustained breach is real | Fewer spurious transitions in Kibana; calibration-proposal cadence drops |
| `DEGRADED → NORMAL` path decision (allowed / deliberately not allowed) | Recovery either auto-completes the cycle or explicitly requires operator action | Recovery-path metric on the dashboard; `MODE_TRANSITION` distribution includes `DEGRADED→NORMAL` edge (if allowed) |
| Multi-host metric stream topology | Out-of-process producers publish to `stream:metrics.sampled` with `host_id` field | Mode decisions aggregate across hosts |

Human action required: review Open Question 2 decision; evaluate Phase 1 calibration-proposal volume before committing to Phase 2.

---

## Loop Completeness Criteria

The stream is verified closed and working when, over a trailing 14-day window, all five hold:

1. **Production (samples):** `XLEN stream:metrics.sampled` ≥ 1 event per 10 s during operating hours (bounded by `MAXLEN=720`; observed via rate of `XADD` over a 1 h sample).
2. **Production (transitions):** `stream:mode.transition` receives at least 1 event when `perf_system_cpu_load` exceeds 0.85 sustained for 30 s (smoke-tested via `stress-ng --cpu 0 --timeout 60s`).
3. **Propagation:** the gateway `GovernanceContext.mode` field reflects `ALERT` during the simulated CPU stress (verified by tracing one request end-to-end and inspecting the `gateway.governance` span).
4. **Gating:** expansion is suppressed (`expansion_permitted=False`) in gateway output when mode is `ALERT` or higher (verified by comparing two traces at the same load — NORMAL permits HYBRID; ALERT does not).
5. **Calibration:** at least one `CaptainLogEntry(CONFIG_PROPOSAL, category=RELIABILITY, scope="mode_calibration")` is created in response to a deliberately triggered threshold anomaly (smoke-tested by lowering `cpu_load` threshold in a test fixture to force repeated transitions).

If (1)–(4) hold but (5) does not, the calibration threshold (3 transitions / 10 min) is tuned too conservatively for the induced-anomaly test; tune in config, not in this ADR.

---

## Feedback Stream ADR Template — Compliance Checklist

Per the Feedback Stream ADR Template established in ADR-0053:

- [x] **1. Stream identity** — Level 1 observability (system metrics → operational mode); Observation Layer; depends on ADR-0041 / ADR-0053 / ADR-0054.
- [x] **2. Source** — `MetricsDaemon` 5-s poll (per-sample) + `ModeManager.transition_to()` (per-transition); two streams per D1.
- [x] **3. Collection mechanism** — `cg:mode-controller` consumer; rolling 60-s window; 30-s evaluation throttle; `NoOpBus` fallback and `evaluate_transitions()` failure handling documented in D2.
- [x] **4. Processing algorithm** — Window aggregation (mean/max/last/sum) per D4; per-edge cadence counter in a 10-min rolling window per D2; minimum sample = full deque (12 samples).
- [x] **5. Signal produced** — `MetricsSampledEvent` on `stream:metrics.sampled`; `ModeTransitionEvent` on `stream:mode.transition`; `CaptainLogEntry(RELIABILITY, scope=mode_calibration)` with fingerprint `sha256(mode_calibration|{from}->{to})[:16]` per D5.
- [x] **6. Full automation cycle** — D7 traces the 7-step loop end to end.
- [x] **7. Human review interface** — "System Health & Homeostasis" Linear project; issue format; label semantics inherited from ADR-0040 with calibration-specific wording; priority mapping documented.
- [x] **8. End state table** — Phase 1 MVP (flag off), Phase 1 complete (flag on), Phase 2.
- [x] **9. Loop completeness criteria** — 5-point check, 14-day evaluation window, smoke-test procedure for points 2 and 5.

---

## References

- `docs/architecture/FEEDBACK_STREAM_ARCHITECTURE.md` — feedback-stream catalogue; Stream 5 row and Phase 2 ADR sequence updated by this ADR
- `docs/architecture/HOMEOSTASIS_MODEL.md` — homeostasis model backing the FSM
- `docs/architecture_decisions/ADR-0053-gate-feedback-monitoring.md` — Feedback Stream ADR Template
- `docs/architecture_decisions/ADR-0054-feedback-stream-bus-convention.md` — dual-write, stream naming, `EventBase` contract fields, reserved names (`stream:mode.transition`, `cg:mode-controller`)
- `docs/architecture_decisions/ADR-0056-error-pattern-monitoring.md` — style reference; compliance-checklist pattern inherited
- `docs/architecture_decisions/ADR-0057-insights-pattern-analysis.md` — style reference; Linear project section pattern inherited
- `docs/architecture_decisions/ADR-0041-event-bus-redis-streams.md` — transport layer
- `docs/architecture_decisions/ADR-0042-knowledge-graph-freshness.md` — brainstem / homeostasis model companion
- `docs/architecture_decisions/ADR-0036-expansion-controller.md` — expansion gating that depends on live mode
- `src/personal_agent/brainstem/mode_manager.py` — FSM implementation; `evaluate_transitions()` and `transition_to()` are the producer surfaces
- `src/personal_agent/brainstem/sensors/metrics_daemon.py` — sample producer; publish hook target
- `src/personal_agent/governance/models.py` — `Mode` enum (5 states)
- `src/personal_agent/governance/governance.py` — expansion gating at L69 that reads `mode`
- `src/personal_agent/service/app.py` — 4 hardcoded `Mode.NORMAL` sites (L175, L198, L913, L944) replaced by `get_current_mode()`
- `config/governance/modes.yaml` — YAML rule definitions consumed by `evaluate_transitions()`
- `src/personal_agent/events/models.py` — target module for `MetricsSampledEvent`, `ModeTransitionEvent`, stream/cg constants, `parse_stream_event` dispatch
