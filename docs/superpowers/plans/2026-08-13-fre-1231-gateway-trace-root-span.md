# FRE-1231: Gateway `/chat` mints its own trace_id with no root span — ADR-0129 D3 instrumentation + response-contract migration

**Ticket:** FRE-1231 · **Backing ADR:** ADR-0129 D1/D3 (root spans, trace-identity bridge) · **Precedent:** FRE-1215 (`f8b91e9d`, service-side fix + `tests/personal_agent/service/test_chat_trace_identity.py`)

**Revision 2** — incorporates codex plan-review corrections (session `019ffcfe-c898-79e1-93cb-7f7e0bfec043`,
verdict "needs rework" on rev 1). All five corrections are folded in below; rev 1's AC-4 design is replaced.

## Deployment reality — discovered during plan review, must be stated in the PR/ticket handoff

**`gateway_app` (`create_gateway_app()`) is currently dormant in production.** `Dockerfile.gateway:86`'s
`CMD` runs `personal_agent.service.app:app`, not `personal_agent.gateway.app:gateway_app` — confirmed by
its own comment: *"ADR-0044/FRE-207: switched from thin gateway to full service app so the cloud profile
can dispatch to LiteLLM via the profile-aware LLM factory."* `docker-compose.cloud.yml`'s `seshat-gateway`
service's `command:` block matches (`exec uv run uvicorn personal_agent.service.app:app --port 9001`).
`gateway_mount_local` defaults to `True` (`config/settings.py:2024`), and no compose file, Makefile, or
script anywhere launches `gateway.app:gateway_app` — confirmed by repo-wide grep.

**This means `gateway/chat_api.py`'s `/chat` endpoint (the one with the bug) is unreachable in *any*
current deployment topology**, standalone or mounted: standalone (`create_gateway_app()`, which *does*
register `chat_router`) isn't deployed; mounted-local (`create_gateway_router()`, which the running
`:9001` process actually uses) never included `chat_router` in the first place (confirmed by grep — only
`gateway/app.py:29` imports it, and only `create_gateway_app()` at line 416 mounts it).

**This does not change the plan.** ADR-0129 D3's "every entrypoint opens a root span" and this ticket's
AC-1..AC-4 are all constructible and testable in isolation, independent of what's currently deployed —
and instrumenting dormant code correctly now is cheaper than instrumenting it wrong later when it *is*
turned on. But this fact goes in the PR body and the ticket handoff comment verbatim, so master and the
owner know today's live blast radius is zero — this is not a live-traffic trace-identity bug the way
FRE-1215 was.

## Design decisions

1. **Bootstrap + middleware land in `_gateway_lifespan()` / `create_gateway_app()`**, mirroring
   `service/app.py`'s `lifespan()` (bootstrap call site) and its `app.add_middleware(RequestRootSpanMiddleware)`.
2. **Response-contract migration: move to 32-hex.** Grep evidence (`seshat-pwa/src`, `src/personal_agent/ui/`,
   `tests/evaluation/`) shows no consumer reads the `/chat` POST response body's `trace_id` field
   synchronously — the PWA's chat flow calls `service/app.py`'s `/chat/stream` (SSE) and never parses this
   endpoint's JSON body (`agui-client.ts:238` posts to `/chat/stream`, not the gateway's `/chat`); the CLI
   and eval harness target the main service's `/chat` contract, whose trace identity already comes from
   `read_or_mint_trace_id()` (FRE-1215). The only repository consumer is
   `tests/personal_agent/gateway/test_chat_api.py:107-109`. **This proves repository-internal safety, not
   safety for any undocumented external caller** — stated explicitly in the PR/ticket handoff as the
   authorization for a breaking response-format change, not asserted silently.
3. **`_stream_to_queue` and its helpers need no changes** — they take `trace_id: str` as an explicit
   parameter threaded from `chat()`.
4. **Gateway gets its own `service.name`** (`"personal-agent-gateway"`) in `configure_tracing()`.

## Steps

### 1. Standalone gateway: SDK bootstrap + root-span middleware
**File:** `src/personal_agent/gateway/app.py`

- In `_gateway_lifespan()`, immediately after `log.info("gateway_starting_standalone")`, call:
  ```python
  from personal_agent.telemetry.otel_bootstrap import configure_tracing
  provider = configure_tracing(
      service_name="personal-agent-gateway", otlp_endpoint=settings.otel_exporter_endpoint
  )
  ```
  **Correction (codex #2): retain `provider` as a local variable and shut down *that specific object* at
  teardown — do NOT re-fetch `otel_trace.get_tracer_provider()` (the global) the way `service/app.py`'s
  shutdown does.** `configure_tracing()` creates a *new* `TracerProvider` on every call, but
  `trace.set_tracer_provider()` only takes effect on the first call in a process (its own docstring). In
  production this distinction never bites (one process, one call) — but `_gateway_lifespan()` runs
  repeatedly across the existing test suite (`test_gateway_lifespan_es.py`'s four tests each enter it as
  an async context manager), and a global re-fetch at teardown would shut down whichever provider happened
  to be first-registered globally in the *whole pytest process* — potentially another test file's provider,
  or another gateway-lifespan invocation's — causing cross-test interference. Fetching the local `provider`
  variable this call created and shutting down *that* object is correct regardless of global registration
  state, and `TracerProvider.shutdown()` is safe to call on a non-global provider (it only flushes its own
  span processors).
- At shutdown, right before `log.info("gateway_stopped")`:
  ```python
  from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
  if isinstance(provider, SDKTracerProvider):
      await asyncio.to_thread(provider.shutdown)
  ```
  (add `import asyncio` to the module if not already present — check first).
- In `create_gateway_app()`, after `add_error_handlers(app)`, add:
  `app.add_middleware(RequestRootSpanMiddleware)` (import from `personal_agent.telemetry.otel_middleware`).

### 2. `PRE_BOOTSTRAP_LOGGERS` gains the gateway's pre-bootstrap emission
**File:** `src/personal_agent/telemetry/otel_bootstrap.py`

**Correction (codex #3):** `_gateway_lifespan()` logs `gateway_starting_standalone` via
`personal_agent.gateway.app`'s logger *before* step 1's new bootstrap call — so
`PRE_BOOTSTRAP_LOGGERS`'s docstring claim ("names every logger that can emit before
`configure_tracing()` runs") becomes false unless this logger is added. Add
`"personal_agent.gateway.app"` to the frozenset, with a comment in the same style as the existing three
entries explaining the emission site (`gateway_starting_standalone`, one line before the new
`configure_tracing()` call).

**Test:** extend `tests/personal_agent/service/test_pre_bootstrap_loggers.py` (or add a gateway-scoped
sibling in `tests/personal_agent/gateway/`) with a test that drives `_gateway_lifespan()` up through the
new bootstrap call (mirroring `test_startup_root_span_ac1_ac2`'s marker-exception technique — monkeypatch
`get_route_trace_ledger`/`init_db` to raise a marker right after the bootstrap line runs), captures logs,
and asserts every record up to and including `gateway_starting_standalone` carries no `trace_id`/`kind`,
while `"personal_agent.gateway.app" in PRE_BOOTSTRAP_LOGGERS`.

### 3. Isolate the four existing gateway-lifespan tests from the new real bootstrap call
**File:** `tests/personal_agent/gateway/test_gateway_lifespan_es.py`

**Correction (codex #2/#3):** the shared fixture that drives `_gateway_lifespan()` (around line 130-156)
must patch `personal_agent.gateway.app.configure_tracing` (or the `otel_bootstrap` source attribute, per
`test_startup_root_span_ac1_ac2`'s pattern — patch where it's *called from*, not where it's defined, since
it's imported inside the function) to return a lightweight `TracerProvider()` with no span processor
attached, for **all four** tests in this file — not only new tests added by this ticket. Without this,
every test in the file starts a real `BatchSpanProcessor` background thread against
`settings.otel_exporter_endpoint`, which is unnecessary I/O-adjacent surface for tests whose subject is
the Elasticsearch handler, not tracing.

### 4. Gateway `/chat` adopts the active span's identity
**File:** `src/personal_agent/gateway/chat_api.py`

- Add `from personal_agent.telemetry.trace import read_or_mint_trace_id` to the imports.
- Line 481: `trace_id = str(uuid4())` → `trace_id = read_or_mint_trace_id()`. Add a short comment
  referencing FRE-1231/ADR-0129 D1 stating the response contract is now 32-hex.
- `uuid4` import stays (still used at line 213 for the span-id fallback).

### 5. Update the tested response contract
**File:** `tests/personal_agent/gateway/test_chat_api.py`

- `test_chat_starts_streaming` (~line 107-110): replace the 36-char/4-dash assertions with 32-lowercase-hex
  (`len(...) == 32`, `count("-") == 0`, all chars in `0123456789abcdef`), and update the inline comment to
  state the new contract and why (ADR-0129 D1, FRE-1231, response-contract migration authorized by the
  grep evidence in Design Decision 2).

### 6. New test — AC-1: standalone gateway opens exactly one root span
**File:** `tests/personal_agent/gateway/test_chat_trace_identity.py` (new)

Mirror `test_otel_root_span.py`'s `traced_app` fixture (`TracerProvider` + `InMemorySpanExporter`,
`RequestRootSpanMiddleware(app, tracer=tracer)`) combined with `test_chat_api.py`'s `_build_app`/
`_override_user` mocking (`asyncio.create_task` patched out, `AsyncSessionLocal`/`SessionRepository.get`
mocked, `get_default_gate_or_none` → `None`). Drive `POST /chat` through `TestClient`, capture logs via
`structlog.testing.capture_logs(processors=[structlog.contextvars.merge_contextvars, _add_span_context])`.
Assert:
- `exporter.get_finished_spans()` has length 1 and `spans[0].parent is None`.
- every captured log record's `trace_id` equals `format(spans[0].context.trace_id, "032x")`.
- the response body's `data["trace_id"]` equals that same 32-hex string.

### 7. New test — AC-2: ledger row and ES telemetry name the same trace, independently derived
**Same file.** Mirror `test_chat_trace_identity.py`'s (service-side)
`test_cost_bearing_turn_produces_a_joinable_es_event_and_ledger_row` structure — but **make explicit
what that precedent leaves implicit (codex #4): the root-span fixture must stay active (in scope) across
BOTH the ledger call and the ES-emit call in the same test function.** This is not decorative: `_add_span_context`
(`telemetry/logger.py:132-158`) unconditionally overwrites `event_dict["trace_id"]` from
`trace.get_current_span()` on every structlog record, *regardless of* what `trace_id` value the caller
passed into `emit_model_call_completed`'s `trace_ctx=TraceContext(trace_id=..., ...)` — so as long as the
span is still current when `_emit_gateway_model_call_completed()` runs, the ES side's `trace_id` is
independently re-derived from the live OTel context, not merely copied from a shared Python variable. Test
body:
1. Obtain `trace_id` by driving `chat()` (step 6's helper) inside the `root_span` fixture's block.
2. **Still inside that same block**, call `CostTrackerService.record_api_call(...)` against a `_FakePool`
   (mirroring the service-side precedent's `_FakeConnection`/`_FakePool`) with `trace_id=UUID(trace_id)` —
   this is the ledger side, a literal explicit value, which is fine: the ledger has no independent
   identity source to re-derive from, it only ever gets what's passed.
3. **Still inside the same block**, call `_emit_gateway_model_call_completed(trace_id=trace_id, ...)`
   (the real gateway function) under `_capture()` (the real `_add_span_context` processor chain) — its ES
   event's `trace_id` comes from the processor reading the *active span*, independently of the passed value.
4. Reconcile both sides via `_normalize_trace_id` (`observability.joinability.walk`) and assert equality —
   *fails if* the span were allowed to exit before step 3, since then `_add_span_context` would no-op and
   the "independent" derivation would silently degrade back into "same passed string."

### 8. New test — AC-4: standalone and mounted-local wiring agree
**Same file.**

**Correction (codex #5): replace rev 1's toy-router comparison with real-object assertions.** Rev 1's
design (reconstructed minimal apps + middleware-class-only check) was rejected because it doesn't prove
the two *actual* deployment compositions serve the same route through the same mechanism. The corrected
design, in place of literally re-running the full `service/app.py` lifespan (impractical here — it starts
the orchestrator, LLM client, brainstem scheduler, and MCP gateway, none of which this ticket touches, and
the OTel API's "first `set_tracer_provider()` call wins per-process" restriction means a second, isolated
in-memory-exporter provider cannot be reliably injected into the already-imported `service.app.app`'s
middleware mid-test-session without racing whichever test set the global provider first):

- **Real-object structural equivalence (new):** import both real apps —
  `from personal_agent.gateway.app import gateway_app` and `from personal_agent.service.app import app as
  main_app` — and assert:
  - `gateway_app.user_middleware[0].cls is RequestRootSpanMiddleware` (new assertion, parallels the
    existing `test_root_span_middleware_wraps_cors_in_the_real_app` assertion for `main_app`, which
    already proves the mounted-local side's middleware wiring and is not duplicated here).
  - **The literal same route handler is registered on both** — not just "a route exists at this path" but
    the same Python function object: `next(r for r in gateway_app.routes if r.path ==
    "/api/v1/health").endpoint is next(r for r in main_app.routes if r.path ==
    "/api/v1/health").endpoint`. This proves both deployment modes route the identical `create_gateway_router()`
    endpoint code through the identical middleware class — not two independently-behaving copies.
- **Behavioral leg on the standalone side (already covered):** step 6's AC-1 test already proves
  `RequestRootSpanMiddleware` + a real request through `create_gateway_app()`'s composition produces a
  real span-derived trace id. Combined with the two structural facts above and the *pre-existing*
  `test_otel_root_span.py` behavioral coverage of the same middleware class on `main_app`'s own toy
  composition (which already exists and is not re-litigated here), the chain is: (a) the middleware class's
  behavior is independently proven, (b) both real apps carry that exact class as their outermost
  middleware, (c) both real apps route the identical handler object through it. That is the "both
  deployment modes agree" proof — anchored in real production objects rather than a reconstruction, and
  without fighting OTel's per-process global-provider singleton in a shared test session.
- State this reasoning inline as a docstring on the test, since it is not obvious from the assertions alone
  why structural equivalence + pre-existing behavioral coverage together satisfy AC-4 rather than requiring
  a fresh end-to-end request through each real app.

## Test commands

```bash
make test-file FILE=tests/personal_agent/gateway/test_chat_api.py
make test-file FILE=tests/personal_agent/gateway/test_chat_trace_identity.py
make test-file FILE=tests/personal_agent/gateway/test_gateway_lifespan_es.py
make test-file FILE=tests/personal_agent/service/test_pre_bootstrap_loggers.py
make test-file FILE=tests/personal_agent/service/test_otel_root_span.py
make test-file FILE=tests/personal_agent/service/test_chat_trace_identity.py
make test   # full suite
make mypy
make ruff-check
```

## Acceptance criteria → evidence map

| AC | Test |
|----|------|
| AC-1 (standalone opens exactly one root span) | Step 6 test |
| AC-2 (ledger + ES name the same trace, independently derived) | Step 7 test |
| AC-3 (response contract explicit, test updated, consumers grepped) | Step 5 (test update) + Design Decision 2 (grep evidence, stated as repo-internal only) |
| AC-4 (both modes agree) | Step 8 tests |

## Risk tier

**Standard** — touches `src/personal_agent/gateway/app.py` and `chat_api.py` (production request path,
telemetry bootstrap), plus a shared telemetry constant (`otel_bootstrap.py`). Codex plan-review completed
(rev 1 → needs rework → rev 2 above incorporates all five corrections).
