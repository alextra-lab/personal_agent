# FRE-1033 — restore `request.completed` publication on the real live path

## Ticket

FRE-1033 (Approved, stream:build2). `request.completed` has not been published since
2026-06-13; the whole request-trace ES surface (`request_trace` / `request_trace_step`
docs) has been dark for two months, silently voiding FRE-739's second acceptance
criterion.

## What the ticket assumes vs. what's actually true

The ticket names exactly two publish sites and asks to confirm which one live traffic
takes: the service app's `/chat` (`_chat_impl` in `service/app.py`) and the gateway's
`/chat` (`_stream_to_queue` in `gateway/chat_api.py`).

Investigation (this session) found a third fact the ticket didn't have:

- **Neither named site is the live path.** Production PWA traffic goes through
  `POST /chat/stream` → `_process_chat_stream_background()` in `service/app.py`
  (confirmed via `seshat-pwa/src/lib/agui-client.ts:238`, which calls `/chat/stream`).
  That function has **zero** `request.completed` publish logic today — not a wrong
  branch, no branch at all.
- `gateway/chat_api.py`'s `/chat` is dead code in production: `docker-compose.cloud.yml`
  runs the `seshat-gateway` container as `personal_agent.service.app:app`, not
  `personal_agent.gateway.app:gateway_app` (the only place `chat_api.router` is
  mounted). `gateway/app.py`'s own lifespan never calls `set_global_event_bus(...)`
  either, so even if that process were deployed, `isinstance(bus, RedisStreamBus)`
  could never be true there — the fallback branch is the only one structurally
  reachable.
- `service/app.py`'s `/chat` (`_chat_impl`, Site B in the ticket) is real, live code,
  but only eval harnesses and tests call it — not the PWA.

So "confirm which path live turns take, restore publication there" (the ticket's own
instruction for exactly this situation) means: **fix `_process_chat_stream_background`**,
not either site the ticket names by number.

## The double-append hazard (why this isn't a one-line relocation)

`RequestCompletedEvent` has two subscribers, wired only when the process runs a
`RedisStreamBus` (`service/app.py` lifespan, `event_bus_enabled` gate):

- `cg:es-indexer` → `build_request_trace_es_handler` — ES writes only.
- `cg:session-writer` → `build_session_writer_handler` — **unconditionally appends
  `event.assistant_response` as an assistant message** to the session
  (`events/request_completed_handlers.py:42-86`), then releases the FRE-51
  session-write waiter.

`_process_chat_stream_background` already appends the assistant message itself,
synchronously, unconditionally (`service/app.py:442-462`). If we add a naive publish
call there, every turn served while the Redis bus is active would append the assistant
reply to Postgres **twice**.

`_chat_impl` (Site B) avoids this by branching: on the Redis bus, it publishes and
lets `cg:session-writer` do the append (no direct append call); on the NoOp bus, it
appends directly itself. It also participates in the FRE-51 wait-for-previous-write
protocol (`events/session_write_waiter.py`) so the next turn on the same session
doesn't read stale history while the previous turn's async append is still in flight.

`_process_chat_stream_background` has none of this branching or waiting today — it
just always appends directly, which was correct until now because it never triggered
a consumer. Fixing the publish means adopting the same ownership split `_chat_impl`
already uses, in this function's own control-flow shape (`_chat_impl` uses one FastAPI-
injected `db` session across the whole request; `_process_chat_stream_background` opens
its own `async with AsyncSessionLocal()` blocks per phase — the branching has to be
adapted to that shape, not copy-pasted).

## Plan

### 1. `src/personal_agent/service/app.py` — `_process_chat_stream_background`

- Resolve `bus = get_event_bus()` and `using_redis_bus = isinstance(bus, RedisStreamBus)`
  once, early (right after the function's `_bind_request_identity` call).
- If `using_redis_bus`: `await await_previous_session_write(session_id, trace_id=trace_id)`
  **before** the `async with AsyncSessionLocal() as db:` block that reads
  `session.messages` (around current line 268) — so a prior turn's async
  consumer-append has landed before this turn hydrates history from it. This call
  doesn't exist in this function today; it's the FRE-51 protocol `_chat_impl` already
  has at the equivalent point.
- Capture the `RequestTimer` the orchestrator call already builds inline
  (`request_timer=RequestTimer(trace_id=trace_id)` at current line 411) into a local
  `timer` variable before the call, so `timer.to_trace_summary()` /
  `timer.to_breakdown()` are usable afterward. The orchestrator already spans real
  work (llm_call, tool_execution, synthesis, ... — `orchestrator/executor.py`) against
  whatever `RequestTimer` it's given, so this alone produces a genuinely non-empty
  breakdown — no new spans need to be added by hand.
- Replace the unconditional assistant-append block (current lines 442-468) with:
  - **Redis branch**: `register_session_write_waiter(session_id)`, then
    `await bus.publish(STREAM_REQUEST_COMPLETED, RequestCompletedEvent(trace_id=...,
    session_id=session_id, assistant_response=response_content,
    trace_summary=timer.to_trace_summary(), trace_breakdown=timer.to_breakdown(),
    source_component="service.app", user_id=user_id))`. On publish failure: log
    `chat_stream.request_completed_publish_failed`, call
    `release_session_write_wait(session_id)`, and **do not re-raise** — unlike
    `_chat_impl`, the response text has already been pushed to the client via SSE by
    this point (`_push_event(TextDeltaEvent(...))` at current line 440), so raising
    here would only append a spurious error delta after a real answer, not prevent
    anything from reaching the user. This is a deliberate divergence from `_chat_impl`
    and will be called out in the PR body.
  - **NoOp branch**: keep the existing direct `repo.append_message(...)` call
    (unchanged), and add the ES fallback `_chat_impl`'s NoOp branch already does:
    `if es_handler and getattr(es_handler, "_connected", False):
    asyncio.create_task(es_handler.es_logger.index_request_trace(trace_id=trace_id,
    timer=timer, session_id=session_id, user_id=user_id))`. Without this,
    non-Redis environments (e.g. local dev) still never produce a `request_trace` doc
    for this path — same gap, just NoOp-shaped.
- All new imports (`get_event_bus`, `RedisStreamBus`, `STREAM_REQUEST_COMPLETED`,
  `RequestCompletedEvent`, `await_previous_session_write`,
  `register_session_write_waiter`, `release_session_write_wait`) are local imports
  inside the function, matching this function's existing convention (it already
  local-imports `compute_expansion_budget`, `Orchestrator`, etc.).

### 2. `src/personal_agent/gateway/chat_api.py` — second defect (degraded fields)

Confirmed unreachable in production today (see above), but the ticket explicitly asks
for this fix "in the same change" and it's small and contained:

- Thread `user_id: UUID` through `_stream_to_queue`'s signature, passed from `chat()`'s
  `request_user.user_id` (available at the call site, current line ~446) at the
  `asyncio.create_task(_stream_to_queue(...))` call (current line ~612-621).
- Replace the hand-built `trace_summary={"model": ..., "steps_count": 1,
  "final_state": "COMPLETED"}` / `trace_breakdown=[]` with a real `RequestTimer`:
  create `timer = RequestTimer(trace_id=trace_id)` near the top of `_stream_to_queue`
  (next to the existing `start_time = time.time()`, left untouched — that variable
  feeds unrelated cost/latency telemetry, out of scope here), wrap the
  `client.messages.stream(...)` block in `with timer.span("llm_call:anthropic_stream"):`,
  and use `timer.to_trace_summary()` / `timer.to_breakdown()` in the published event.
  Pass `user_id=user_id`.
- No behavior change to the Redis-vs-NoOp branch structure itself — only the event's
  field values change.

### 3. ADR-0090 generalization question (ticket asks to decide explicitly, not by omission)

The ticket asks whether a surface going from thousands of docs/month to zero should
alarm on its own. Decision: **out of scope for this ticket** — it names this as
belonging to "the telemetry-surface audit thread under ADR-0090," a separate,
broader instrumentation-monitoring concern (which surface, what threshold, what
alerting channel) that isn't decidable from this ticket's own deliverable. This will
be stated plainly in the PR/ticket handoff as an explicit decision, not silently
dropped.

### 4. Tests (TDD — write failing first)

- `tests/test_service/test_chat_stream_request_completed.py` (new file, follows the
  mocking pattern already established in
  `tests/test_service/test_chat_stream_contextvars_propagation.py`): mock
  `Orchestrator`, `SessionRepository`, `AsyncSessionLocal`, `_push_event`, `emit_done`,
  `_validate_attachments` the same way; patch the event bus singleton via
  `personal_agent.events.bus.set_global_event_bus(MagicMock(spec=RedisStreamBus))`
  with `.publish = AsyncMock()` for the Redis-branch test, and
  `set_global_event_bus(NoOpBus())` for the NoOp-branch regression test (restored in a
  fixture teardown). Assertions:
  - Redis branch: `bus.publish` was called once with `STREAM_REQUEST_COMPLETED` and a
    `RequestCompletedEvent` whose `trace_breakdown` is non-empty, `user_id` equals the
    test user id, and `source_component == "service.app"`; `repo.append_message` was
    **not** called a second time for the assistant role (only the one user-message
    append from earlier in the function).
  - NoOp branch (regression): `repo.append_message` was called for the assistant
    role (existing behavior preserved), and (if `es_handler` is patched present)
    `index_request_trace` was scheduled.
  - Publish-failure branch: `bus.publish` raises → function does not propagate the
    exception (matches the "already streamed, don't crash after" decision above) and
    `release_session_write_wait` was called.
- `tests/personal_agent/gateway/test_chat_api.py` (extend existing file, same style as
  `test_gateway_emits_model_call_completed_with_identity`): assert `_stream_to_queue`'s
  published `RequestCompletedEvent` (Redis branch) carries non-empty `trace_breakdown`,
  a `trace_summary` with real `total_duration_ms`/`total_steps` keys (not the old
  `model`/`steps_count`/`final_state` shape), and `user_id` set from the passed-through
  parameter.
- This satisfies the ticket's explicit AC ("add an assertion that a completed live
  turn results in a request trace document in ES carrying a non-empty breakdown and a
  populated user identifier") at the publish-site level. The ES-write side of that
  same shape is already covered by `tests/test_telemetry/test_es_logger.py` and
  `tests/test_events/test_request_completed_handlers.py` (handler → ES write,
  pre-existing, unaffected by this change) — combined, publish-site correctness +
  existing handler coverage prove the doc that lands in ES is well-formed, without a
  new full live-Redis-to-ES integration test (which the existing consumer test suite,
  e.g. `tests/personal_agent/events/test_consumer.py`, already does with a mocked
  Redis client — no precedent in this repo for spinning up real Redis+ES together in
  a unit-tier test, and doing so here would be a new, heavier pattern for marginal
  extra coverage over what's listed above).

### 5. Docs

No doc updates identified — this is a bugfix restoring existing designed behavior, not
a new capability or architectural surface. (Re-checked against §6 after implementation
in case something surfaces.)

## Codex plan-review findings (incorporated)

Codex reviewed this plan before any code was written. Verdict: the core approach
(Redis/NoOp ownership split, `await_previous_session_write` placement) is sound —
Codex specifically confirmed the wait placement is *stronger* than `_chat_impl`'s own
(this function re-opens a fresh `AsyncSessionLocal()` after waiting, so it reads the
consumer's committed transaction; `_chat_impl` reads its ORM object loaded *before* the
wait). But it found real gaps, split into two buckets:

**New risk this diff introduces (fixed in this plan, below):**
1. `except Exception:` around the publish call does not catch `asyncio.CancelledError`
   (a `BaseException` subclass on the Python versions in play here) — a cancelled task
   could leak a registered waiter Future until its timeout.
2. "Log, release the waiter, and continue" on publish failure silently drops the
   assistant turn from session history and ES — a real durability regression versus
   today, where the synchronous append only fails on its own DB error, not on a
   separate publish call. **Fix: fall back to the direct append + ES index path (the
   NoOp branch's code) on publish failure**, so a delivered answer is never silently
   lost. Accept the small residual risk of an occasional duplicate if the publish had
   actually landed server-side but the client saw an exception (Redis XADD is a single
   atomic command; full exactly-once needs an idempotency key, which is out of scope —
   see below).
3. The original test plan's "non-empty breakdown" assertion is a false-positive risk:
   `RequestTimer.to_breakdown()` always appends a `{"phase": "total", ...}` entry even
   if the timer records zero spans (`request_timer.py:234-240`), so a fully-mocked
   orchestrator that never touches the timer would still pass. Tests must assert a
   *non-total* span is present and/or `trace_summary["total_steps"] >= 1`.
4. Test coverage gaps: no waiter-dict cleanup between tests, no genuine two-turn
   ordering test, and the original plan's claim that publish-shape tests plus the
   *existing* (already-shipped, unrelated-to-this-change) handler tests "prove the
   document lands in ES" overstates it — those existing tests call the handler
   directly, not through a live publish → `ConsumerRunner` → handler chain. Added a
   real component test for that (see Tests, below).

**Pre-existing gaps in the shared `cg:session-writer`/waiter infrastructure** (not
introduced by this change — `_chat_impl` has carried these since FRE-51/158; this
ticket just raises their blast radius from "eval-only, rare" to "100% of production
chat traffic" for the first time):
- `SessionRepository.append_message` is a non-idempotent read-modify-write
  (`session_repository.py:171-190`); combined with `ConsumerRunner` coupling handler
  execution and Redis ACK in one retry block (`consumer.py:235-248`), a commit
  followed by an ACK failure can duplicate the assistant append on retry.
- `register_session_write_waiter` holds one Future per session
  (`session_write_waiter.py:28-44`); two genuinely overlapping turns on the same
  session can have the second overwrite the first's Future, letting a follow-up turn
  proceed before the first's append actually lands.
- The waiter dict is process-local module state — under multiple service replicas
  sharing one Redis, the consumer that releases a waiter may run in a different
  process than the one holding it, so the waiter free-runs to timeout instead.

Per owner decision (2026-08-06): **fix items 1-4 above in this PR; file a separate
Needs-Approval ticket for the systemic idempotency/concurrency work** rather than
folding a redesign of shared consumer infra into a telemetry-restoration ticket. That
follow-up ticket is filed alongside this plan (see ticket FRE number in the PR/handoff
comment).

### Revised implementation for §1 (`_process_chat_stream_background`)

- Wrap the publish call's `except Exception:` and add an explicit
  `except asyncio.CancelledError:` (or restructure as `try/except Exception .../except
  BaseException:`) that still calls `release_session_write_wait(session_id)` before
  re-raising, so a cancelled task never leaks the waiter.
- On a caught `Exception` from `bus.publish(...)` (not cancellation): log
  `chat_stream.request_completed_publish_failed`, `release_session_write_wait(...)`,
  then **fall through to the same direct-append + direct-ES-index code the NoOp branch
  uses** — do not silently drop the turn.
- Orchestrator-failure path (response_content becomes the generic error string) still
  reaches this block and still publishes/appends — this matches `_chat_impl`'s existing
  behavior (an orchestrator exception there also still reaches the publish block) and
  is intentional: an errored-but-answered turn is a completed request. Pre-orchestrator
  failures (session setup, attachment validation, a `_push_event` failure) raise
  straight to the outer handler and never reach this block — also matching
  `_chat_impl`'s existing behavior (its own session-lookup failures raise
  `HTTPException` before its timing/publish section). Both are stated here explicitly,
  not left implicit, per Codex's ask.

### Tests (revised)

In addition to the original test list:
- Redis-branch test's mocked orchestrator explicitly records a span on the timer it's
  given (`ctx["request_timer"].start_span(...)`/`end_span(...)`, or simpler: assert
  post-hoc on `trace_summary["total_steps"] >= 1` and that `trace_breakdown` contains
  at least one entry with `phase != "total"`) — closes the false-positive gap.
- A fixture clears `personal_agent.events.session_write_waiter._session_write_waiters`
  before and after each test in the new test module (matching the cleanup pattern in
  `tests/test_events/test_session_write_waiter.py` if one already exists there).
- A two-turn ordering test: register a waiter for a session (simulating turn A still
  in flight), start `_process_chat_stream_background` for turn B on the same session,
  assert it blocks on `await_previous_session_write` until the waiter is released, then
  release it and confirm turn B proceeds and observes turn A's committed state.
- A publisher→handler component test using the existing mocked-Redis `ConsumerRunner`
  pattern from `tests/personal_agent/events/test_consumer.py`: publish a real
  `RequestCompletedEvent` (the same shape `_process_chat_stream_background` produces)
  through a `ConsumerRunner` wired to the real `build_session_writer_handler` and
  `build_request_trace_es_handler` (with mocked DB/ES boundaries), proving the
  publish → consume → persist/index chain is actually wired end to end — not just that
  each piece is independently correct.
- Cancellation test: cancel the task after the waiter is registered but before publish
  resolves; assert the waiter is released rather than left to leak until timeout.

## Risk tier

**Standard/Complex** — touches `src/` core service logic (the primary live chat path's
message-persistence ownership), not just a relocated call. Codex plan-review completed
(above); revised plan incorporates its must-fix findings. Ready for owner approval to
implement.

## Explicit scope decisions (per owner sign-off this session)

- Fix targets the real live path (`/chat/stream`), not the two sites the ticket names
  literally — approved.
- Gateway's degraded-fields defect included in this same PR despite being dead code in
  prod today — approved.
- The gateway-process deploy-drift finding (standalone `gateway.app:gateway_app` never
  actually deployed; `Dockerfile.gateway` has a stale comment) will **not** be filed as
  a separate Backlog ticket — owner declined. Will still be mentioned in the PR body /
  ticket handoff as context, since it's directly why the "second defect" fix has no
  live effect today.
