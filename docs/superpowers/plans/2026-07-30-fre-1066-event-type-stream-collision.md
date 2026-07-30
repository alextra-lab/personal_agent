# FRE-1066: event_type overwritten by the Redis stream name

**Status:** Plan — pending codex review
**Ticket:** [FRE-1066](https://linear.app/frenchforest/issue/FRE-1066/event-type-is-overwritten-with-the-redis-stream-name-172m-documents)
**Related:** ADR-0128 (telemetry naming convention — this ticket is explicitly independent of it)

## Root cause

Confirmed by reading `telemetry/es_handler.py` and `telemetry/es_logger.py` directly (not by
inference from the ADR):

1. `es_handler.py:121` computes the canonical ES `event_type` from structlog's own `event` key
   (the log message name, e.g. `"event_published"`): `event_type = event_dict.get("event",
   record.levelname.lower())`.
2. `es_logger.py`'s `log_event()` builds `doc = {"event_type": event_type, ..., **data}` —
   `**data` is spread *after* the initial `event_type` key, so anything in `data` with the key
   `"event_type"` silently overwrites the message-derived value.
3. The custom-field copy loop at `es_handler.py:144-165` does **not** exclude `"event_type"` from
   the fields it copies from the caller's structlog kwargs into `data`. So any call site that
   passes `event_type=<something>` as an explicit kwarg gets that value written into `data`,
   which then clobbers the correct value in step 2.
4. `personal_agent.events.redis_backend.publish()` (the ticket's named site) passes both
   `stream=stream` and `event_type=event.event_type` to `log.debug("event_published", ...)`. For
   every single-purpose stream (the vast majority — `metrics.sampled`, `mode.transition`, etc.,
   per the `STREAM_*` constants in `events/models.py`), `event.event_type` is identical to the
   stream name minus its `stream:` prefix, by the codebase's own naming convention (confirmed:
   `MetricsSampledEvent.event_type: Literal["metrics.sampled"] = "metrics.sampled"` pairs with
   `STREAM_METRICS_SAMPLED = "stream:metrics.sampled"`). The result: `event_type` in ES ends up
   equal to `stream` minus its prefix, and the true record type (`"event_published"`) is lost —
   exactly the defect described in the ticket, at the volume the ticket measures (1.72M docs).

## Scope: which call sites actually need fixing

The ticket's acceptance criterion is corpus-wide ("zero new documents carry an `event_type` value
equal to their `stream` value, minus prefix") — not scoped to one call site. Grepping every
`event_type=` kwarg in `src/personal_agent` turns up more call sites than the ticket names. Only
sites that pair `event_type=event.event_type` with a `stream`/`source_stream` field can produce
the specific equality this AC measures, so that grouping is the actual scope boundary — not "one
file" or "every match for the string `event_type=`":

**In scope (same `personal_agent.events` module, same defect, needed for the AC to hold):**
- `events/redis_backend.py::publish()` (~L118-125) — the ticket's named site, `stream=` present.
- `events/redis_backend.py::dead_letter()` (~L218-227) — `source_stream=` present (not `stream=`,
  so this exact instance is not measured by the AC, but it is the identical bug in the same
  method-pair of the same file; fixing it is a same-file, same-defect fold-in, not new scope).
- `events/consumer.py` — 3 call sites (`event_processed` ~L237-245, `consumer_budget_denied`
  ~L253-266, `consumer_handler_error` ~L270-279), all with `stream=sub.stream` present. These
  consume from the same single-purpose streams `publish()` writes to, so leaving them unfixed
  would leave the AC failing in production regardless of the `redis_backend.py` fix.
- `events/bus.py::NoOpBus.publish()` (~L90) — `stream=stream` present; active when the event bus
  is disabled or Redis is unreachable (graceful-degradation path), same defect.

**Out of scope (same `event_type=` kwarg pattern exists, but no paired `stream` field, so these
do not produce the equality the AC checks — left untouched):**
- `observability/topology/seam.py:109` — `trace_id=` only, no `stream=`.
- `transport/agui/transport.py`, `transport/agui/ws_endpoint.py` — UI/session event transport, a
  different `event_type` meaning entirely (frontend event type, not `EventBase.event_type`).
- `second_brain/quality_monitor.py:483` — a literal string, not `event.event_type`, and no stream.

Not attempting ADR-0128's Tier-1/Tier-2 registry machinery here — this ticket is explicitly
independent of that ADR and scoped to the live bug.

**Ticket's aside** ("Consider whether a DEBUG-level publish receipt per message is worth emitting
at this volume at all"): considered, not acted on. Downgrading/removing the log line is a
volume/cost tradeoff orthogonal to the correctness bug, and the receipt remains useful for
debugging; noting the consideration in the PR handoff rather than silently changing log volume.

## Fix

Rename the colliding kwarg from `event_type=event.event_type` to `payload_event_type=
event.event_type` at all five in-scope call sites. Pure rename, no other field changes:

- The message-derived `event_type` (`"event_published"`, `"event_processed"`,
  `"consumer_budget_denied"`, `"consumer_handler_error"`, `"event_discarded_noop_bus"`,
  `"event_dead_lettered"`) flows through unclobbered — matching every other log call in these
  files that never passed an explicit `event_type=` kwarg in the first place.
- The domain event's own type is preserved (not dropped) under `payload_event_type`, so
  multi-event streams (e.g. `STREAM_TURN_OBSERVED`, which carries several distinct event types)
  keep that signal instead of losing it.
- `stream` (or `source_stream`) is untouched — already correct per the ticket.

## Files changed

1. `src/personal_agent/events/redis_backend.py` — `publish()`, `dead_letter()`.
2. `src/personal_agent/events/consumer.py` — 3 call sites in `_process_message` (or equivalent).
3. `src/personal_agent/events/bus.py` — `NoOpBus.publish()`.

## Tests (TDD — write first, confirm failing, then implement)

Following the existing `_capturing_log()` convention (`tests/test_tools/test_perplexity.py`) —
patch the module's `log` object directly rather than `structlog.testing.capture_logs()`, which
this suite's own comments note is unreliable under `cache_logger_on_first_use` in the shared
suite.

1. `tests/personal_agent/events/test_redis_backend.py` — capture `publish()`'s and
   `dead_letter()`'s log kwargs; assert `payload_event_type` carries `event.event_type` and
   `"event_type"` is absent from the kwargs (so nothing can collide downstream).
2. `tests/personal_agent/events/test_consumer.py` — same assertion for the 3 consumer log calls
   (happy path, budget-denied path, handler-error path).
3. `tests/personal_agent/events/test_bus.py` — same assertion for `NoOpBus.publish()`.
4. `tests/test_telemetry/test_es_handler.py` — an outcome-level regression test that is the real
   proof for the AC: build a `logging.LogRecord` shaped like the actual post-fix
   `redis_backend.publish()` call (`msg={"event": "event_published", "stream":
   "stream:metrics.sampled", "payload_event_type": "metrics.sampled", ...}`), run it through
   `ElasticsearchHandler.emit()`, and assert the `event_type` argument reaching
   `es_logger.log_event()` is `"event_published"` — proving the collision cannot reproduce even
   with a payload field present, using the real pipeline code path (same technique as the
   existing `test_emit_forwards_session_id_to_es_logger`).

## Acceptance-criteria mapping

The ticket's AC is a **live, 7-day-window production measurement** ("zero new documents carry an
`event_type` value equal to their `stream` value, minus prefix") that cannot be proven in CI. What
this PR proves in-repo: (a) the code-level defect is fixed at every call site that can produce it,
(b) a regression test demonstrates the exact failure mode no longer reproduces through the real ES
pipeline code. The handoff comment will give master the exact post-deploy query to run against
production 7 days out to confirm the AC holds live, per D7 (history is not migrated/rewritten).

## Quality gates

`make test` (module: `test_events`, `test_telemetry`, then full) · `make mypy` · `make ruff-check`
+ `make ruff-format` · `pre-commit run --all-files`.
