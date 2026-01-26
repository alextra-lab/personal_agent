

# Control Loops & Sensors — v0.1
*A practical specification of what the agent measures, how, and for which homeostatic loop.*

This document makes the **Homeostasis Model** concrete by defining:
- which variables the agent will sense,
- where those signals come from,
- how often they are updated,
- and which control loop(s) they feed.

It is deliberately pragmatic: you should be able to hand this to an implementation assistant and get real code that emits these metrics.

For conceptual background, see:
- `./HOMEOSTASIS_MODEL.md`
- `./HUMAN_SYSTEMS_MAPPING.md`

---

## 0. Naming & Conventions

To keep sensors coherent across the system, we adopt a simple naming scheme:

- **Metric IDs**: `loopCategory_subsystem_signalName`
  - Example: `perf_system_cpu_load`, `safety_tool_high_risk_call_rate`
- **Types**:
  - `gauge` – instantaneous value (e.g., CPU %, queue length)
  - `counter` – monotonically increasing count (e.g., calls, errors)
  - `histogram` – distribution of values (e.g., latency)
  - `state` – discrete state (e.g., mode: NORMAL/ALERT/...)
- **Collection Modes**:
  - `interval` – collected on a schedule (e.g., every 5s)
  - `event` – emitted when an event occurs (e.g., a tool call)
  - `derived` – computed from other metrics (e.g., moving average)

These sensors are **logical signals**; implementation can use OpenTelemetry, custom logging, or a minimal in-memory metrics store, as long as the semantics are preserved.

---

## 1. Overview of Homeostatic Loops & Sensors

We define sensors for five primary loops:

1. Performance & Load
2. Safety & Risk
3. Knowledge Integrity & Staleness
4. Resource Usage
5. Learning & Self-Modification Pace

Each section below describes the **core signals** we want from the system.

---

## 2. Performance & Load Sensors

**Goal:** prevent the agent from overloading the Mac or its own internal queues.

These sensors correspond to the **cardiovascular + respiratory** analogy: they tell us how “hard” the system is working and whether it can sustain that.

### 2.1 System-Level Metrics

| ID | Name | Description | Type | Source | Collection | Notes |
|----|------|-------------|------|--------|------------|-------|
| `perf_system_cpu_load` | CPU Load (%) | Overall CPU percentage used on the Mac | gauge | OS tools (e.g., `psutil`, `top` APIs) | interval (e.g., 5s) | Key indicator for throttling concurrency |
| `perf_system_mem_used` | Memory Used (%) | Percentage of RAM used | gauge | OS metrics | interval | Helps prevent swapping / slowdown |
| `perf_system_gpu_load` | GPU / NPU Load (%) | Utilization of GPU/NPU if accessible | gauge | OS / model server | interval | Optional but useful for local LLMs |
| `perf_system_load_avg` | Load Average | 1/5/15-minute load average | histogram or gauge set | OS | interval | Captures medium-term stress |

### 2.2 Orchestrator & Queue Metrics

| ID | Name | Description | Type | Source | Collection | Notes |
|----|------|-------------|------|--------|------------|-------|
| `perf_orch_active_tasks` | Active Tasks | Number of tasks currently running | gauge | Orchestrator state | interval | Used to limit concurrent work |
| `perf_orch_pending_tasks` | Pending Tasks | Number of queued tasks | gauge | Orchestrator | interval | High values may trigger DEGRADED mode |
| `perf_orch_task_latency` | Task Latency | Time from task submission to completion | histogram | Orchestrator | event (per task) | Core UX metric |

### 2.3 Model & Tool Call Metrics

| ID | Name | Description | Type | Source | Collection | Notes |
|----|------|-------------|------|--------|------------|-------|
| `perf_model_call_latency` | Model Call Latency | Time per LLM request | histogram | Model client | event | Drives model choice / routing |
| `perf_model_error_rate` | Model Error Rate | Errors per N calls | counter + derived rate | Model client | event + derived | Detects instability |
| `perf_tool_call_latency` | Tool Call Latency | Time per tool execution | histogram | Tools layer | event | Helps identify slow tools |
| `perf_tool_error_rate` | Tool Error Rate | Errors per N tool calls | counter + derived rate | Tools layer | event + derived | Safety + UX impact |

---

## 3. Safety & Risk Sensors

**Goal:** detect and mitigate dangerous or unwanted behavior before it impacts the system or external world.

These align with **renal, immune, and integumentary** functions.

### 3.1 Tool & Action Risk

| ID | Name | Description | Type | Source | Collection | Notes |
|----|------|-------------|------|--------|------------|-------|
| `safety_tool_calls_total` | Tool Calls | Total number of tool invocations | counter | Tools layer | event | Base volume metric |
| `safety_tool_high_risk_calls` | High-Risk Tool Calls | Count of calls classified as high risk | counter | Risk assessor | event | Drives LOCKDOWN / approvals |
| `safety_tool_blocked_calls` | Blocked Tool Calls | Calls blocked by safety layer | counter | Safety gate | event | Indicates effectiveness of guardrails |
| `safety_tool_manual_approvals` | Human-Approved Calls | Tool calls that required human confirmation | counter | Safety gate / UI | event | Measures human-in-the-loop volume |

### 3.2 Output / Content Risk

| ID | Name | Description | Type | Source | Collection | Notes |
|----|------|-------------|------|--------|------------|-------|
| `safety_output_policy_violations` | Policy Violations | Number of outputs flagged by policies | counter | Output filter | event | Key safety metric |
| `safety_output_redactions` | Output Redactions | Number of redactions performed | counter | Output filter | event | Indicates how often filters intervene |
| `safety_output_confidence` | Output Confidence Score | Self-assessed confidence per answer | histogram | Reasoning layer | event | Used for ALERT mode and self-review |

### 3.3 Anomaly & Threat Signals

| ID | Name | Description | Type | Source | Collection | Notes |
|----|------|-------------|------|--------|------------|-------|
| `safety_anomaly_events` | Anomaly Events | Count of detected anomalies (e.g., behavior deviation) | counter | Immune/monitoring subsystem | event | May directly trigger ALERT/LOCKDOWN |
| `safety_mode_state` | Current Mode | NORMAL/ALERT/DEGRADED/LOCKDOWN/RECOVERY | state | Homeostasis controller | event on change | Global regulator |

---

## 4. Knowledge Integrity & Staleness Sensors

**Goal:** ensure that the knowledge base (KB/world model) remains useful, relevant, and not polluted with junk.

These mirror **digestive + liver (hepatic)** systems.

### 4.1 Document & Chunk Usage

| ID | Name | Description | Type | Source | Collection | Notes |
|----|------|-------------|------|--------|------------|-------|
| `kb_docs_total` | Total Documents | Count of documents in KB | gauge | KB metadata | interval | High-level size indicator |
| `kb_docs_hot_access` | Hot Document Accesses | Count of accesses for top N hottest docs | counter | KB / retrieval layer | event | Identifies high-value docs |
| `kb_docs_cold_access` | Cold Document Accesses | Accesses for rarely used docs | counter | KB / retrieval | event | Guides pruning and archiving |

### 4.2 Retrieval Quality & Feedback

| ID | Name | Description | Type | Source | Collection | Notes |
|----|------|-------------|------|--------|------------|-------|
| `kb_retrieval_relevance_score` | Relevance Score | Internal metric: how well retrieved docs match query | histogram | Retrieval + eval | event | Can be estimated via heuristics or eval harnesses |
| `kb_user_feedback_positive` | Positive Feedback | Thumbs-up / “this helped” | counter | UI / feedback layer | event | Ground truth from you |
| `kb_user_feedback_negative` | Negative Feedback | Thumbs-down / “not helpful / wrong” | counter | UI / feedback | event | Triggers KB review / experiments |

### 4.3 Staleness & Age

| ID | Name | Description | Type | Source | Collection | Notes |
|----|------|-------------|------|--------|------------|-------|
| `kb_docs_avg_age_days` | Average Document Age | Mean age of documents in days | gauge | KB metadata | derived (interval) | Rough staleness indicator |
| `kb_docs_stale_fraction` | Stale Docs Fraction | Percentage of docs older than threshold | gauge | KB metadata | derived (interval) | Guides re-ingestion or archiving |

---

## 5. Resource Usage Sensors

**Goal:** make sure the agent does not silently consume excessive disk space or model capacity.

These align with **renal + skeletal** functions.

### 5.1 Disk & Storage

| ID | Name | Description | Type | Source | Collection | Notes |
|----|------|-------------|------|--------|------------|-------|
| `res_disk_usage_percent` | Disk Usage (%) | Percentage of disk used (or used by agent data) | gauge | OS / agent storage | interval | Critical for local device health |
| `res_logs_size_bytes` | Logs Size | Total size of log files | gauge | Filesystem | interval | Guides log rotation |
| `res_kb_storage_bytes` | KB Storage Size | Disk space consumed by KB | gauge | Storage layer | interval | Guides pruning / compaction |

### 5.2 Model & Cache

| ID | Name | Description | Type | Source | Collection | Notes |
|----|------|-------------|------|--------|------------|-------|
| `res_model_cache_bytes` | Model Cache Size | Space used by local model artifacts / cache | gauge | Model subsystem | interval | Ensures models don’t overwhelm storage |

---

## 6. Learning & Self-Modification Pace Sensors

**Goal:** allow the agent to grow and evolve without becoming unstable or opaque.

These are the **developmental / reproductive** signals.

### 6.1 Hypotheses & Proposals

| ID | Name | Description | Type | Source | Collection | Notes |
|----|------|-------------|------|--------|------------|-------|
| `learn_hypotheses_logged` | Hypotheses Logged | Count of new hypotheses added to Captain’s Log | counter | Self-improvement module | event | Indicates learning activity |
| `learn_config_change_proposals` | Config Change Proposals | Number of proposed config changes | counter | Governance / agent | event | Drives review & approval load |
| `learn_config_changes_accepted` | Accepted Changes | Number of proposals accepted | counter | Governance | event | Used to measure evolution rate |

### 6.2 Evaluation & Quality

| ID | Name | Description | Type | Source | Collection | Notes |
|----|------|-------------|------|--------|------------|-------|
| `learn_eval_runs_total` | Evaluation Runs | Count of evaluation experiments executed | counter | Experiment runner | event | Measures how often we test the system |
| `learn_eval_score_distribution` | Eval Scores | Distribution of evaluation scores | histogram | Experiment runner | event | Indicates whether changes help or hurt |

### 6.3 Human Feedback on Evolution

| ID | Name | Description | Type | Source | Collection | Notes |
|----|------|-------------|------|--------|------------|-------|
| `learn_user_adjusted_scores` | Manual Score Adjustments | Times you override or adjust the agent’s self-score | counter | UI / feedback | event | Critical for aligning self-view with your judgment |

---

## 7. How These Sensors Feed Control Loops

Each homeostatic loop will:
- read a subset of these sensors,
- compute a control decision (e.g., stay NORMAL, move to ALERT, throttle, block, refresh),
- emit its own decision logs and possibly new derived metrics (e.g., mode transitions).

In practice, the **Brainstem Service** (always-on homeostasis process) will:
- periodically sample interval metrics,
- subscribe to event metrics,
- update global mode (`safety_mode_state`),
- trigger reflexes (LOCKDOWN, DEGRADED) when thresholds are crossed.

A future version of this document can:
- attach exact **thresholds** per sensor (e.g., CPU > 85% for N seconds → ALERT),
- specify **alerting rules** (when to notify you),
- define **tuning strategies** (how thresholds evolve over time).

For now, this v0.1 is enough to:
- guide implementation of a basic metrics layer,
- inform the design of the Brainstem Service,
- and keep the architecture grounded in observable reality.
