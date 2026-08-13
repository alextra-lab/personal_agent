# FRE-1069 — ADR-0129 B4: root spans on every background entrypoint

Ticket: https://linear.app/frenchforest/issue/FRE-1069
Backing ADR: ADR-0129 D3 ("every entrypoint opens a root span — including background ones"), D4
(bootstrap + root span + structlog processor already shipped for the *request* boundary via FRE-1064).

**Revision 2** — incorporates a codex:rescue plan review (round 1). Round-1 findings and how each is
addressed are folded inline below rather than kept as a separate changelog.

## Problem, precisely

`configure_tracing()` (`telemetry/otel_bootstrap.py`) and the structlog `_add_span_context` processor
(`telemetry/logger.py`) already exist and already stamp `trace_id`/`span_id` onto every log record
emitted **while an OTel span is the active context**. `SystemTraceContext.new(source)`
(`telemetry/trace.py`) already mints a `kind="system:<source>"` trace context and *reads* the active
span's trace id when one exists — but background entrypoints never open a span, so today it always
falls onto the "mint a fresh id" branch, disconnected from any span, and every log line emitted during
that background run carries no `trace_id` at all (`_add_span_context` finds no active span and leaves
the record untouched).

The fix is mechanical once framed this way: **open a real OTel span, attach it as the current context,
before each entrypoint's existing `SystemTraceContext.new(source)` call** — after that, the existing
call reads the just-attached span's id instead of minting a disconnected one, and every nested log call
already threading `trace_id=...ctx.trace_id` continues to work unchanged, now backed by a real span.

## New shared helper — `telemetry/spans.py`

```python
def open_root_span(
    source: str, *, tracer: Tracer | None = None
) -> tuple[Span, Token[Context], Mapping[str, "contextvars.Token[Any]"]]:
    """Open a background entrypoint's root span (ADR-0129 D3, FRE-1069).

    `context=Context()` is passed explicitly so the new span is a genuine ROOT
    regardless of any span already current on the caller's task — round-1 review
    finding #1: without this, a span opened inside e.g. the scheduler's lifecycle
    loop would silently parent onto whatever happened to be active, defeating the
    "exactly one root span" AC.

    Attaches the span as the current OTel context so `SystemTraceContext.new(source)`
    and `_add_span_context` (telemetry/logger.py) read its identity instead of minting
    a disconnected one, and binds `kind` via structlog.contextvars so log records carry
    it directly (not just joinably through trace_id), matching how `session_id` already
    propagates (ADR-0129 D4 docstring in otel_middleware.py).

    Callers that spawn long-lived `asyncio.create_task()` children while this span is
    open MUST pass `context=contextvars.Context()` to `create_task()` for each such
    child — otherwise the child copies this span (and the bound `kind`) into its own
    task-local context at creation time, and `close_root_span`'s detach/reset on THIS
    task never reaches that copy, so the child logs a dead span's identity forever
    (round-1 review finding #1). See `service/app.py`'s `lifespan()` for the pattern.
    """
    kind = f"{SYSTEM_KIND_PREFIX}{source}"  # trace.py's SYSTEM_KIND_PREFIX = "system:"
    span = (tracer or get_tracer()).start_span(
        source, context=context_api.Context(), attributes={namespaced("kind"): kind}
    )
    token = context_api.attach(trace.set_span_in_context(span))
    cv_tokens = structlog.contextvars.bind_contextvars(kind=kind)
    return span, token, cv_tokens


def close_root_span(
    span: Span, token: Token[Context], cv_tokens: Mapping[str, "contextvars.Token[Any]"]
) -> None:
    """End a background entrypoint's root span (ADR-0129 D3, FRE-1069).

    Nested `finally` so a `span.end()` exception can't skip the OTel detach or the
    structlog reset (round-1 review finding #1's closure-safety point).
    """
    try:
        span.end()
    finally:
        try:
            context_api.detach(token)
        finally:
            structlog.contextvars.reset_contextvars(**cv_tokens)
```

Import `structlog`, `contextvars` (for the type hint only — `Context()` itself comes from
`opentelemetry.context`, not stdlib `contextvars`, don't confuse the two: OTel's `Context` object is a
different type from `contextvars.Context`; use `opentelemetry.context.Context` for the empty-parent
argument), and `SYSTEM_KIND_PREFIX` (from `telemetry.trace` — `trace.py` does not import `spans.py`, so
this direction is safe).

**New unit tests in `tests/test_telemetry/test_spans.py`** (round-1 finding #1, explicitly requested):
- `open_root_span` under an ALREADY-active unrelated span still produces `parent is None` on the child.
- `close_root_span` restores a PRE-EXISTING bound `kind` (not just clears to absent) — bind `kind="outer"`
  before calling `open_root_span`, close it, assert `kind` reads back as `"outer"` afterward (proves
  `reset_contextvars` restores rather than blindly clears).
- Two interleaved `asyncio.create_task()` children each calling `open_root_span`/`close_root_span`
  concurrently produce two independent root spans with no cross-contamination (contextvars are
  task-local by design, but this is the regression test proving it holds here).

## The six named entrypoints, plus one folded-in sibling

Insert `open_root_span(source)` immediately before the file's existing trace-minting call, store the
returned tuple, and `close_root_span(...)` in a `finally`. No other line in each function changes — the
existing `trace_id=ctx.trace_id` threading throughout each body is untouched, now backed by a real span.

1. **`brainstem/scheduler.py`** — `BrainstemScheduler._lifecycle_loop`, per iteration (source
   `"scheduler.lifecycle"`), AND `_session_summary_sweep_loop`, per iteration (source
   `"scheduler.session_summary"`). `BrainstemScheduler.__init__` gains optional `tracer: Tracer | None =
   None` → `self._tracer`, threaded into both `open_root_span(source, tracer=self._tracer)` calls.
   - `_lifecycle_loop`: open right before `iteration_trace_id = _new_scheduler_trace_id(...)` inside the
     `while self.running:` block; add `finally: close_root_span(...)` to the existing
     `try: ... except CancelledError: raise / except Exception: ...` (one `try` can carry both `except`
     and `finally` — no reindentation of the ~350-line body).
   - `_session_summary_sweep_loop`: same shape, smaller body.
   - No `asyncio.create_task()` context-leak concern here (round-1 finding #1's general risk): both
     loops open their OWN span as the very first statement inside the `while`, before any log call, so
     there is no window where a log line could observe a stale inherited identity.

2. **`second_brain/consolidator.py`** — `SecondBrainConsolidator.consolidate_recent_captures` (source
   `"consolidation"`). `SecondBrainConsolidator.__init__` gains optional `tracer: Tracer | None = None`
   → `self._tracer`. Open right before `run_trace_id = _new_consolidation_trace_id()`; wrap the whole
   body from that point to `return summary` in `try: ... finally: close_root_span(...)` — requires
   indenting the function body one level (~200 lines) since there's no existing try/finally to attach
   to. Mechanical, whitespace-only diff outside the two new lines.

3. **`observability/joinability/scheduler_runner.py`** — `run_scheduled_probe` (source
   `"joinability_probe"`). **Round-1 finding #6**: the function currently opens PG/Neo4j/Redis
   *before* its `try:` (`pg_pool = await _open_pg_pool()` etc., ahead of the existing `try: ... finally:
   await _close(...)`), so simply adding the span close to the existing `finally` would leak the span if
   resource acquisition raised first. In practice `_open_pg_pool`/`_open_neo4j_driver`/`_open_redis` each
   already catch everything internally and return `None` rather than raising — but restructure anyway so
   the guarantee doesn't depend on that being true forever: move the span open, then the resource-open
   calls, and the `try:` all together, so a hypothetical future exception from resource acquisition is
   still caught by the same `finally` that now also closes the span. Add optional `tracer` param.

4. **`observability/slm_health/scheduler_runner.py`** — `run_scheduled_slm_health_probe` (source
   `"slm_health_probe"`). Open right after the `settings.slm_health_probe_enabled` early-return (no span
   for a disabled probe — nothing happened to trace); wrap the remaining body in `try/finally`. Add
   optional `tracer` param.

5. **`observability/cache_erosion/scheduler_runner.py`** — `run_scheduled_cache_erosion_probe` (source
   `"cache_erosion_probe"`). **Scope note, reviewed and confirmed at round 1 (finding #7)**: the ticket
   names `observability/cache_erosion/monitor.py`, but `monitor.py`'s `compute_erosion_report()` is pure
   ES-query/Jaccard computation with no logging or scheduler-facing entrypoint of its own — instrumenting
   it too would create a nested/duplicate span under the one already opened here, violating the
   single-root intent. `scheduler_runner.py` is the actual scheduler-invoked entrypoint, exactly mirroring
   3 and 4. There is also a separate manual CLI entrypoint (`scripts/monitors/cache_erosion_monitor.py`)
   that writes to stdout, not structlog — explicitly out of scope (not log-correlated, not scheduler-driven).
   Open after the two early-return guards (probe disabled / no ES client — no span for a tick that did no
   work), wrap the remainder in `try/finally`. Add optional `tracer` param.

6. **`service/app.py`** — `lifespan()` startup (source `"startup"`), **materially revised at round 1**:

   **Round-1 finding #1 (critical) and finding #2 together force two changes to the original design:**

   a. **Guaranteed closure via `try/finally`, not a bare open/close before `yield`.** The original plan's
      "close only on the happy path" was flagged as a real correctness bug (a failed startup that doesn't
      immediately kill the process — a test harness, an embedded ASGI runner — would leave OTel/structlog
      context attached and leaking), and the proposed AC-3 test for it was vacuous (it patched
      `configure_tracing` to raise, which is BEFORE the span would even open, so it could only ever
      observe zero spans). Fixed: `try: <startup body> finally: close_root_span(...)`.

   b. **The `try/finally` — and the span's coverage — is deliberately narrowed to end right before
      `await metrics_daemon.start()` (current line ~945), not right before `yield` (line ~1418).**
      This is the direct fix for finding #1's task-context-leak: from that point on, `lifespan()` calls
      `.start()` on several long-lived background components (`metrics_daemon`, `scheduler`,
      `freshness_consumer`, `consumer_runner`) that each spawn their own `asyncio.create_task()` children
      internally, in files well outside this ticket's six-entrypoint scope. If the `"startup"` span were
      still the active OTel/structlog context when THOSE internal task-creations happen, each child would
      copy the span (and bound `kind="system:startup"`) into its own permanent task-local context —
      `close_root_span`'s later detach/reset happens on the `lifespan()` coroutine's own context, which
      never reaches those already-forked copies, so the children would keep stamping every log line with
      a dead span's identity for the rest of the process's life. That is a strictly worse outcome than
      today's status quo (no identity at all) and is not an acceptable trade for coverage.

      Verified by reading the actual file (not assumed): between `configure_tracing()` (~line 671) and
      `await metrics_daemon.start()` (~line 945) there are exactly four `asyncio.create_task()` calls —
      `cost_gate_reaper_task` (744), `cost_gate_snapshotter_task` (749), the Captain's Log ES-backfill task
      (807), and `cost_gate_silence_monitor_task` (825) — all inside this ticket's own file, all long-lived
      except the backfill task. **Each of these four gets `context=contextvars.Context()` added to its
      `create_task()` call** (Python 3.12+ supports the `context=` kwarg; confirmed against this repo's
      `requires-python = ">=3.12"`), giving each child a genuinely fresh, empty context at creation time
      instead of a copy of the current one — closing the leak for exactly the tasks actually created while
      the span is open, without needing to touch `metrics_daemon.py` / `scheduler.py`'s internal
      `.start()` / `freshness_consumer.py` / `consumer_runner.py` (`scheduler.py`'s OWN two internal
      `create_task()` calls inside `BrainstemScheduler.start()` are now safe unmodified, precisely because
      `scheduler.start()` is called at line ~1025 — after the span already closed).

      No other `create_task()` calls exist between lines 836–945 (verified). Everything from `await
      metrics_daemon.start()` onward — Neo4j-adjacent work already covered, scheduler start, MCP adapter,
      freshness consumer, consumer runner, WS/dedup/upload/session-retention cleanup loops — is **not**
      covered by the startup span under this design. This is a real, stated coverage limit (not every log
      line emitted during the full ~750-line startup carries `trace_id`), consistent with the ADR's own
      "honest limit, stated rather than papered over" framing; document it plainly in a code comment at
      the open call site and in the PR/ticket handoff. What IS covered — cost_gate init, ES/Captain's Log
      wiring, the silence monitor, Neo4j connect — is the majority of startup's own decision-relevant
      logging and is a genuine, non-trivial closure of the gap.

   c. Open using the **provider `configure_tracing()` itself returns**, not the ambient global one
      (`configure_tracing()`'s own docstring: "only takes effect on the first call in a process" — a test
      process with an earlier global provider must not be relied on):
      ```python
      provider = configure_tracing(service_name=..., otlp_endpoint=...)
      startup_span, startup_token, startup_cv_tokens = open_root_span(
          "startup", tracer=provider.get_tracer(__name__)
      )
      try:
          ... # unchanged existing startup code, through the silence-monitor block
      finally:
          close_root_span(startup_span, startup_token, startup_cv_tokens)
      # unchanged existing code continues: Neo4j connect, metrics_daemon.start(), ...
      ```

## AC-3 — pre-bootstrap logger census, **materially revised at round 1**

**Round-1 finding #3 (critical):** the original design (in-process `structlog.testing.capture_logs()`
tests only) cannot observe two real sources of pre-bootstrap emission:
- Production boots via Uvicorn (`docker-compose.cloud.yml`), and the installed Uvicorn itself logs
  through the stdlib `logging.getLogger("uvicorn.error")` logger (confirmed by reading the installed
  package: `.venv/lib/python3.12/site-packages/uvicorn/server.py:41` — `logger =
  logging.getLogger("uvicorn.error")`; message `"Started server process [%d]"` etc.) **before** the
  ASGI app's `lifespan()` — and therefore `configure_tracing()` — ever runs. `structlog.testing.capture_logs()`
  only sees structlog-emitted records, so it is structurally blind to this.
- An in-process pytest test runs long after the real interpreter/import boundary its own test file
  already crossed — it cannot observe what happened at true process-start.

**Revised census:**
```python
PRE_BOOTSTRAP_LOGGERS: Final[frozenset[str]] = frozenset({
    "personal_agent.config.settings",
    "personal_agent.service.app",
    "uvicorn.error",
})
```

**Revised tests**, `tests/personal_agent/service/test_pre_bootstrap_loggers.py`:

- **Settings census** (unchanged from round 1, not challenged by review): reset `settings_mod._settings`
  to `None`, call `configure_logging()` first (idempotent, guarantees the real processor chain — matching
  genuine production ordering), `capture_logs(processors=[structlog.stdlib.add_logger_name])` around
  `settings_mod.get_settings()`, assert observed logger names ⊆ allowlist and that
  `"personal_agent.config.settings"` is among them.

- **Startup census, in-process** (fixes round-1 finding #4's vacuous-test problem): do **not** patch
  `configure_tracing` to raise (that happens before any span opens, so a capture around it is trivially
  empty and proves nothing). Instead patch something **inside** the now-bounded try/finally window — e.g.
  `personal_agent.service.database.init_db` — to raise a private marker exception, wrapped in
  `capture_logs(...)`. Assert: (a) records captured **before** `"service_starting"` — actually
  `"service_starting"` fires first and is itself the marker; split the ordered capture list at the
  `event == "service_starting"` record — everything up to and including it must carry no `trace_id` (still
  pre-bootstrap, expected); assert (b) every record captured **after** it must carry the
  `trace_id`/`span_id`/`kind` of the (locally-scoped, injected-provider) startup span, proving the
  try/finally's guaranteed closure fires correctly on the exception path too — the span is still exported
  even though `lifespan()` raised before reaching its own `yield`.

- **Startup census, real process boundary (new — closes round-1 finding #3):**
  `tests/personal_agent/service/_uvicorn_pre_bootstrap_probe.py` (leading underscore, not a `test_*`
  filename, so pytest does not try to collect it as a test module) — a standalone script that:
  1. Installs a plain `logging.Handler` on the root stdlib logger, recording `(logger_name, event_or_msg)`
     pairs **in order** — this sees both stdlib (Uvicorn) and structlog-routed-through-stdlib records,
     since `configure_logging()`'s `ProcessorFormatter.wrap_for_formatter` funnels structlog into the
     stdlib logging pipeline.
  2. Imports `personal_agent.service.app` and constructs a real `uvicorn.Config` + `uvicorn.Server` bound
     to an ephemeral port, then runs `server.serve()` under a bounded `asyncio.wait_for(..., timeout=...)`.
     No mocking of `configure_tracing` or Postgres — this is deliberately the unmodified real path;
     because the test sandbox has no reachable production Postgres, the run is expected to fail on its
     own at the DB preflight check shortly after `configure_tracing()` succeeds, which is fine — capture
     stops mattering past that point.
  3. Prints the ordered `(logger_name, event)` list as JSON to stdout.

  The pytest wrapper (`test_uvicorn_boot_pre_bootstrap_loggers`) runs this via
  `subprocess.run([sys.executable, str(script_path)], capture_output=True, timeout=...)`, parses stdout,
  finds the index of the `"service_starting"` record (from `personal_agent.service.app`), takes the
  **prefix** up to and including it, and asserts every logger name in that prefix ⊆
  `PRE_BOOTSTRAP_LOGGERS`. Records after that index are deliberately not asserted on — this test's only
  job is the pre-bootstrap population, not steady-state startup behaviour. Marked `@pytest.mark.slow`
  or similar if this repo has such a marker convention (check `pyproject.toml`/`pytest.ini` markers
  first) since it spawns a real subprocess and a real (short-lived) server.

## Delivery-ratio scheduler runner — folded in (round-1 finding #5)

`observability/delivery_ratio/scheduler_runner.py`'s `run_scheduled_delivery_ratio_probe` is structurally
identical to 3/4/5 above (mints `SystemTraceContext.new("delivery_ratio_probe")`, invoked from the same
`_lifecycle_loop` tick) but is **not** one of the ticket's named six entrypoints, and AC-1/AC-2 do not
require a dedicated test for it. Applying the identical, zero-risk, already-proven recipe here closes the
exact same gap ADR-0129 D3 describes, in the same file family, at negligible incremental cost — folded in
per this project's "meet the objective, don't over-ticket" standard (a supporting change needed for
internal consistency, not separate scope) rather than filed as a new ticket. State this explicitly in the
PR body and ticket handoff as a deliberate, documented deviation from the ticket's literal enumeration.
Same recipe, same file conventions as 3/4/5; a lightweight test in
`tests/observability/test_delivery_ratio_scheduler_runner.py` (check whether this file already exists —
extend if so) mirroring the others, but not required to satisfy AC-1/AC-2 as written.

## Tests — AC-1/AC-2, one file per entrypoint, pattern from `test_otel_root_span.py` / `test_spans.py`

Each test: `TracerProvider()` + `InMemorySpanExporter` via `SimpleSpanProcessor`,
`provider.get_tracer("test")` passed as the entrypoint's `tracer=` param,
`structlog.testing.capture_logs(processors=[structlog.stdlib.add_logger_name, _add_span_context])`
around the invocation, then assert: `len(exporter.get_finished_spans()) == 1`, `span.parent is None`,
`span.attributes["personal_agent.kind"] == "system:<source>"`, and every captured log event's
`trace_id`/`span_id` match `format(span.context.trace_id, "032x")`/`"016x"`, and `kind == "system:<source>"`.

New/extended test files:
- `tests/test_brainstem/test_scheduler.py` — two new tests (`_lifecycle_loop`, `_session_summary_sweep_loop`
  one-iteration each), using the existing `patch("personal_agent.brainstem.scheduler.asyncio.sleep", ...)`
  stop-after-one-iteration idiom already established in this file.
- `tests/test_second_brain/test_consolidator_root_span.py` (new) — `consolidate_recent_captures` with a
  mocked `MemoryService`/`read_captures` returning zero captures (cheapest path that still emits ≥1 log
  line — `no_captures_to_consolidate`).
- `tests/observability/test_slm_health_scheduler_runner.py`,
  `tests/observability/test_cache_erosion_scheduler_runner.py` — extend both with a root-span test.
- `tests/observability/test_joinability_scheduler_runner.py` (new — no file exists yet for this module;
  match the sibling naming convention) — `run_scheduled_probe` with `es_client=None` and mocked substrate
  opens returning `None` (reaches the `session_id is None` skip path).
- `tests/observability/test_delivery_ratio_scheduler_runner.py` — folded-in sibling, see above.
- `tests/personal_agent/service/test_otel_root_span.py` — extend with the in-process startup test
  described above (exception-path evidence via the injected-provider tracer).
- `tests/personal_agent/service/test_pre_bootstrap_loggers.py` (new) + its
  `_uvicorn_pre_bootstrap_probe.py` helper script (new, non-collected).

## Order of work

1. `telemetry/spans.py`: `open_root_span`/`close_root_span` (with `context=Context()` + nested
   try/finally) + the three round-1-driven unit tests in `tests/test_telemetry/test_spans.py`.
2. `telemetry/otel_bootstrap.py`: `PRE_BOOTSTRAP_LOGGERS` (three entries) + settings-census test — decoupled
   from 3-9, do early.
3. `observability/slm_health/scheduler_runner.py` (smallest) — proof of the recipe.
4. `observability/joinability/scheduler_runner.py` (restructured per finding #6).
5. `observability/cache_erosion/scheduler_runner.py`.
6. `observability/delivery_ratio/scheduler_runner.py` (folded-in sibling).
7. `second_brain/consolidator.py` (largest single-function reindent).
8. `brainstem/scheduler.py` (two call sites, constructor DI param).
9. `service/app.py` (narrowed try/finally boundary + four `create_task(context=...)` fixes).
10. `service/app.py`'s pre-bootstrap census test + the new subprocess probe script.
11. `make test` (module-scoped first: `test_telemetry`, `test_brainstem`, `test_second_brain`,
    `observability/`, `personal_agent/service`), then full suite.
12. `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Test commands

```
make test-file FILE=tests/test_telemetry/test_spans.py
make test-file FILE=tests/personal_agent/service/test_pre_bootstrap_loggers.py
make test-file FILE=tests/test_brainstem/test_scheduler.py
make test-file FILE=tests/test_second_brain/test_consolidator_root_span.py
make test-k K=scheduler_runner
make test-file FILE=tests/personal_agent/service/test_otel_root_span.py
make test
make mypy
make ruff-check
```

## Risk tier

**Standard** — touches `src/` logic across seven files (six named + delivery_ratio folded in) plus a new
shared helper. Codex plan review completed (round 1, findings addressed above); proceeding to
implementation.
