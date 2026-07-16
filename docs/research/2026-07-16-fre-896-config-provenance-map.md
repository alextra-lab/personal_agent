# FRE-896 — Config never-read provenance map + curated removal

> **Ticket:** [FRE-896](https://linear.app/frenchforest/issue/FRE-896) · **Backing ADR:**
> [ADR-0099](../architecture_decisions/ADR-0099-configuration-management-and-validation.md) ·
> **Source audit:** [2026-07-16-fre-893-config-parameter-usage-audit.md](2026-07-16-fre-893-config-parameter-usage-audit.md)
> (FRE-893) · **Generated:** 2026-07-16

## Why this doc exists

FRE-893's audit categorizes every `AppConfig` field by read/override evidence. Its
`never-read` bucket is a **removal *candidate* list, not a delete list** — the owner's
binding caveat on FRE-896 is that "never-read" conflates two opposite things:

- **Outgrown** — the origin feature was removed or its mechanism superseded → the field is
  genuinely dead → safe to delete.
- **Forward-declaration / wiring-gap** — the origin ADR is *live or planned* and the field
  is a knob awaiting (or bypassed by) wiring → deleting it quietly amputates a live stream.

This doc maps every field in the (alias-corrected) never-read set to its **origin ADR/FRE**
and its **feature's current status**, then classifies it. Only the **confirmed-outgrown**
subset is deleted in this ticket's PR.

## Prerequisite fix (AC1) — alias-aware read detection

The audit's read detection was line-oriented `git grep`, blind to reads reaching a field
through an alias. FRE-896 replaced it with an AST scan
(`scripts/audit/settings_reads.py`) that resolves: local aliases (`cfg = settings`),
factory chains (`get_settings().<field>`), multi-line `getattr(settings, "<field>")`,
`AppConfig`-typed params, `self._settings` attribute aliases (incl. plain DI
`self._settings = config`), direct `AppConfig()` construction, aliased factory imports
(`get_settings as _gs`), and `from personal_agent.config import settings as X` import
aliases. A high-effort code-review pass hardened four wrong-deletion-direction gaps in the
first cut (a file-global shadow check that dropped sibling-function reads — now the
`settings` name is always seeded, biasing to *keep*; and the `AppConfig()`/aliased-factory/
DI-attribute misses above). Regenerating shrank the never-read set from **43 → 28** (rescued live fields — `proactive_memory_*` ×11,
`insights_wiring_enabled`, `quality_monitor_*`, `slm_health_cache_ttl_seconds`,
`freshness_backfill_confirm`) and also **removed 5 old false-positives** whose only
"reads" were `AGENTS.md` markdown code examples the text-grep counted
(`log_level`/`log_format` moved to never-read for the *right* reason — see below). The
28-field never-read set below is the trustworthy input to provenance.

## Provenance table (28 originally-never-read fields)

| Field | Origin ADR / FRE | Feature status | Classification | Action |
|---|---|---|---|---|
| `insights_daily_run_hour_utc` | Insights Engine (FRE-24) → **superseded by ADR-0057** (Accepted/Impl, FRE-247) | clock scheduling **removed**; runs event-driven | **Outgrown** | **DELETE** |
| `insights_weekly_day` | Insights Engine (FRE-24) → superseded by ADR-0057 | same | **Outgrown** | **DELETE** |
| `insights_weekly_run_hour_utc` | Insights Engine (FRE-24) → superseded by ADR-0057 | same | **Outgrown** | **DELETE** |
| `router_role` | Inference/routing ADR-0082/0094/0095 | active/planned | Forward-declaration | KEEP |
| `router_timeout_seconds` | ADR-0082/0094/0095 | active/planned | Forward-declaration | KEEP |
| `routing_policy` | ADR-0082/0094/0095 | active/planned | Forward-declaration | KEEP |
| `routing_heuristic_threshold` | ADR-0082/0094/0095 | active/planned | Forward-declaration | KEEP |
| `enable_reasoning_role` | ADR-0082/0094/0095 | active/planned | Forward-declaration | KEEP |
| `event_bus_ack_timeout_seconds` | Event Bus (ADR-0041, Accepted) | feature live; knob unwired | Wiring-bug | KEEP + follow-up |
| `cloud_weekly_budget_usd` | Cost caps (ADR-0065) | superseded by **ADR-0120** cost-governance (FRE-898–905) | Cost-gov | DEFER |
| `brainstem_sensor_poll_interval_seconds` | Brainstem homeostasis | live; superseded by `metrics_daemon_poll_interval_seconds` | Wiring-gap (sibling-superseded) | KEEP |
| `context_budget_comfortable_tokens` | Request Gateway Phase 2.4 | live; `context_budget_max_tokens` wired | Wiring-gap | KEEP |
| `context_budget_generation_reserve_tokens` | Request Gateway Phase 2.4 | live; reserve knob unwired | Wiring-gap | KEEP |
| `embedding_batch_size` | Embedding (ADR-0035, Accepted) | live; batch knob unwired | Wiring-gap | KEEP |
| `feedback_defer_revisit_days` | Feedback loop (ADR-0040, Accepted) | live; "future archive hook" per its own description | Forward-declaration | KEEP |
| `log_format` | Telemetry (core infra) | live; consumed via bootstrap/env `APP_LOG_FORMAT` | Wiring-gap (bootstrap-bypassed) | KEEP |
| `log_level` | Telemetry (core infra) | live; consumed via `config/bootstrap.py` + `APP_LOG_LEVEL` | Wiring-gap (bootstrap-bypassed) | KEEP |
| `orchestrator_max_concurrent_tasks` | Orchestrator (core) | live; concurrency via `expansion_budget_max` | Wiring-gap | KEEP |
| `orchestrator_max_repeated_tool_calls` | Orchestrator (core) | live; loop-guard via `orchestrator_max_tool_iterations` | Wiring-gap | KEEP |
| `orchestrator_task_timeout_seconds` | Orchestrator (core) | live; timeouts via sub-agent/worker knobs | Wiring-gap | KEEP |
| `profiles_dir` | Execution Profiles (ADR-0044, Accepted) | live; `load_profile` uses a default arg | Wiring-gap | KEEP |
| `request_monitoring_include_gpu` | Request Monitoring (ADR-0012) | live; GPU path (Apple-Silicon powermetrics) | Wiring-gap | KEEP |
| `request_monitoring_interval_seconds` | Request Monitoring (ADR-0012) | live; interval knob unwired | Wiring-gap | KEEP |
| `service_host` | Service Configuration (core) | live; host set via uvicorn CLI | Wiring-gap | KEEP |
| `synthesis_timeout_seconds` | Expansion controller (ADR-0036, Accepted) | live; synthesis phase exists | Wiring-gap | KEEP |
| `tool_result_digest_keep` | Tool-result compression (ADR-0085) | **parked/dormant** (flag-off); code in-tree | Forward-declaration (dormant) | KEEP |
| `url_guard_enabled` | Egress URL guard (FRE-225) | live; disable is via `url_guard_mode="off"` | Wiring-gap (sibling-superseded) | KEEP |
| `use_service_mode` | Feature flags (core) | service mode is the live default; flag vestigial | Wiring-gap (vestigial) | KEEP |

**Summary:** 3 Outgrown (deleted) · 6 Forward-declaration · 1 Wiring-bug (follow-up) ·
1 Cost-gov (deferred) · 17 Wiring-gap (kept). The remaining 25 never-read fields are
**not** deleted — each backs a live or planned feature.

## Deleted this PR (confirmed-outgrown, with justification)

| Field | Origin | Outgrown because |
|---|---|---|
| `insights_daily_run_hour_utc` | Insights Engine, FRE-24 | ADR-0057 (Accepted/Impl, FRE-247) replaced clock-scheduled insights with event-driven runs on `consolidation.completed`; `brainstem/scheduler.py` comments "Insights and promotion run reactively via consolidation.completed events" — zero consumers. |
| `insights_weekly_day` | Insights Engine, FRE-24 | Same supersession — weekly cron day no longer scheduled by clock. |
| `insights_weekly_run_hour_utc` | Insights Engine, FRE-24 | Same supersession — weekly proposal hour no longer scheduled by clock. |

**Non-Python-consumer check (per deleted field):** absent from `config/substrate.yaml`,
all 5 `docker-compose*.yml` `environment:` blocks, and the deployed `.env` as an *active*
key. The deployed `/opt/seshat/.env` carries the three keys only as **commented-out** lines
(`# AGENT_INSIGHTS_DAILY_RUN_HOUR_UTC=6`, etc.) — inert, not overrides; master may optionally
delete those three comment lines during deploy (harmless if left).

## Carved out (not deleted here)

- **Wiring-bug follow-up:** `event_bus_ack_timeout_seconds` — a reader-missing-vs-truly-dead
  investigation (a new Needs-Approval ticket).
- **Cost-gov deferral:** `cloud_weekly_budget_usd` — owned by ADR-0120 (cost governance,
  superseding ADR-0065; FRE-898–905); untouched here.
- **Future owner-gated pass (optional):** the sibling-superseded/vestigial knobs
  (`use_service_mode`, `brainstem_sensor_poll_interval_seconds`, `url_guard_enabled`,
  `log_level`/`log_format` bootstrap-bypass) are removal *candidates* but their features are
  live, so they are kept pending an explicit owner decision — not deleted on a hunch.
