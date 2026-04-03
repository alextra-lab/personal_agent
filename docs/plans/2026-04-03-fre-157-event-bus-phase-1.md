# FRE-157: Event Bus Phase 1 — Implementation Plan

## Context

The Personal Agent uses polling-based coordination for cross-component communication. The brainstem scheduler polls every 60s for new task captures, meaning consolidation can lag up to 70 minutes. ADR-0041 (Approved) introduces Redis Streams as a lightweight event bus to replace polling with push-based delivery.

Phase 1 scope: add Redis infrastructure, implement the EventBus protocol, and migrate one path (`request.captured` -> consolidator) to validate the architecture end-to-end. Feature flag defaults to `False` — zero behavior change unless opted in.

---

## Implementation Order

```
1. docker-compose.yml        — add redis:7-alpine service
2. pyproject.toml             — add redis[hiredis] dependency
3. config/settings.py         — add 6 event_bus_* flat fields
4. events/models.py           — frozen Pydantic event models
5. events/bus.py              — EventBus protocol + NoOpBus + singleton
6. events/redis_backend.py    — RedisStreamBus (XADD, XGROUP, XACK, dead-letter)
7. events/consumer.py         — ConsumerRunner (XREADGROUP async loops)
8. events/__init__.py          — public API exports
9. brainstem/scheduler.py     — add on_request_captured() method
10. orchestrator/executor.py  — publish request.captured after write_capture()
11. service/app.py            — wire lifecycle (init, consumers, shutdown)
12. .env.example              — document new config vars
13. Tests                     — unit tests for all new modules
```

Steps 1-3 are independent. Steps 4-8 are sequential (build up the events package). Steps 9-11 are the wiring layer. Step 13 after all source code is done.

---

## File Changes

### New Files (5 source + 5 test)

| File | Purpose |
|------|---------|
| `src/personal_agent/events/__init__.py` | Package init, public API exports |
| `src/personal_agent/events/models.py` | `EventBase` (frozen), `RequestCapturedEvent`, stream/group constants |
| `src/personal_agent/events/bus.py` | `EventBus` Protocol, `NoOpBus`, `get_event_bus()`/`set_global_event_bus()` singleton |
| `src/personal_agent/events/redis_backend.py` | `RedisStreamBus`: XADD publish, XGROUP subscribe, XACK, dead-letter routing |
| `src/personal_agent/events/consumer.py` | `ConsumerRunner`: one asyncio.Task per subscription, XREADGROUP loop |
| `tests/personal_agent/events/__init__.py` | Test package |
| `tests/personal_agent/events/test_models.py` | Event model tests (frozen, discriminator, serde) |
| `tests/personal_agent/events/test_bus.py` | NoOpBus + singleton + Protocol tests |
| `tests/personal_agent/events/test_redis_backend.py` | Mocked Redis: publish, subscribe, ack, dead-letter, serde |
| `tests/personal_agent/events/test_consumer.py` | Mocked runner: start/stop, process, retry, dead-letter |

### Modified Files (6)

| File | Change |
|------|--------|
| `docker-compose.yml` | Add `redis:7-alpine` service (port 6379, healthcheck, `redis_data` volume) |
| `pyproject.toml` | Add `"redis[hiredis]>=5.0.0"` to dependencies |
| `src/personal_agent/config/settings.py` | Add 6 fields: `event_bus_enabled`, `event_bus_redis_url`, `event_bus_consumer_poll_interval_ms`, `event_bus_max_retries`, `event_bus_dead_letter_stream`, `event_bus_ack_timeout_seconds` |
| `src/personal_agent/brainstem/scheduler.py` | Add `async on_request_captured(trace_id, session_id)` after line 198 — calls `_should_consolidate()` then `_trigger_consolidation()` |
| `src/personal_agent/orchestrator/executor.py` | After `write_capture(capture)` (line 630): fire-and-forget `get_event_bus().publish()` via `run_in_background()` |
| `src/personal_agent/service/app.py` | Wire event bus lifecycle in `lifespan()`: init RedisStreamBus or NoOpBus, register `cg:consolidator` consumer, start ConsumerRunner, shutdown |
| `.env.example` | Add EVENT BUS section documenting all 6 config vars |

---

## Key Design Decisions

1. **Feature flag `False` by default** — no behavior change until `AGENT_EVENT_BUS_ENABLED=true`. Polling continues as fallback.
2. **Graceful degradation** — Redis unreachable at startup -> log warning, fall back to NoOpBus. No user impact.
3. **No conditional publish logic** — `get_event_bus()` returns `NoOpBus` when disabled, so executor.py always calls `publish()` uniformly.
4. **Singleton pattern** — follows `set_global_metrics_daemon()`/`get_global_metrics_daemon()` from `brainstem/sensors/metrics_daemon.py`.
5. **Flat config fields** — matches existing `AppConfig` pattern (no nested sub-models).
6. **Fire-and-forget publish** — uses existing `run_in_background()` pattern from `captains_log/background.py`.
7. **Double-fire prevention** — both event-driven and polling paths converge on `_should_consolidate()` which checks `min_consolidation_interval_seconds` (1h default).

---

## Critical Integration Points

### Executor (publish point)
- File: `src/personal_agent/orchestrator/executor.py`
- Location: after `write_capture(capture)` at line 630, inside the `try` block
- Pattern: lazy imports + `run_in_background()` (matches Captain's Log reflection at line 643)

### Scheduler (consumer handler)
- File: `src/personal_agent/brainstem/scheduler.py`
- New method: `on_request_captured(trace_id, session_id)` after line 198
- Reuses existing `_should_consolidate()` + `_trigger_consolidation()`

### Service lifecycle (wiring)
- File: `src/personal_agent/service/app.py`
- Global vars: add `event_bus`, `consumer_runner` at line 39
- Init: after `set_global_metrics_daemon()` (line 188), before scheduler creation (line 222)
- Consumer registration: after `scheduler.start()` (line 248)
- Shutdown: before `scheduler.stop()` (line 264)

---

## Verification

```bash
# Unit tests
uv run pytest tests/personal_agent/events/ -v

# Type checking
uv run mypy src/personal_agent/events/

# Linting
uv run ruff check src/personal_agent/events/ && uv run ruff format --check src/personal_agent/events/

# Infrastructure
docker compose up -d redis
docker compose exec redis redis-cli ping  # Expect: PONG

# Feature flag off (default): service starts normally, NoOpBus, polling works
# Feature flag on: Redis connects, consumer starts, events flow, consolidation triggers
# Graceful degradation: flag on + Redis down -> warning logged, NoOpBus fallback
```
