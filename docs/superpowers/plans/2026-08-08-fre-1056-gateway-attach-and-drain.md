# FRE-1056 — Attach ES log delivery in the standalone gateway, drain the service handler before disconnect

**Ticket:** [FRE-1056](https://linear.app/frenchforest/issue/FRE-1056) (Approved, High, Tier-2:Sonnet, stream:build1, context:keep)
**Predecessor:** FRE-1055 (built the `drain()` primitive this calls) · **Measurement:** FRE-1185 AC-7
**Backing ADR:** none. Process-coverage half of the FRE-1051 delivery gap; design intent is on the ticket.

## Scope (from the ticket, verbatim in effect)

Attach and drain in the service and in the standalone gateway. **Nothing else.** No attaching in
scripts, evals or migrations. No blanket cost-tracker guard demanding a production handler — absence of
one is *correct* in an isolated script.

## What is actually wrong, verified in-tree

1. **The standalone gateway ships no logs to Elasticsearch at all.** `gateway/app.py:145-156` constructs
   an `ElasticsearchHandler` purely to harvest `es_logger.client` for read queries and never calls
   `add_elasticsearch_handler`. Confirmed: `add_elasticsearch_handler` has exactly one call site,
   `service/app.py:754`.
2. **The gateway closes the harvested client directly** (`gateway/app.py:199-203`), bypassing the
   handler that owns it — the ticket's stated defect.
3. **A consequence I introduced in FRE-1055 and flagged in that handoff:** `connect()` now starts a
   consumer task, so the gateway's harvest-only handler leaves an idle consumer alive for the process
   lifetime. This ticket is where that gets resolved, as predicted.
4. **The service's drain already exists** — `service/app.py:1436` calls `await es_handler.disconnect()`,
   and FRE-1055 made `disconnect()` drain first. The ticket was written before that landed. What is
   missing here is the **proof**, and the detach (below).
5. **Neither process detaches the handler from the root logger.** After `disconnect()` the handler stays
   attached with `_connected = False`, so every remaining shutdown log record is counted
   `dropped_not_connected` against a dead handler.

## Design

### One helper, because the *ordering* is the invariant — and it must be exception-safe

`telemetry/logger.py` gains `detach_elasticsearch_handler(handler)`, the async counterpart to the
`add_elasticsearch_handler` already there:

```
try:
    await handler.disconnect()   # drains first (FRE-1055), then closes the client
finally:
    handler.close()              # removes itself from the root logger
```

The order is the whole point and is easy to invert. **Correction from codex review:** my first draft said
inverting it produces `dropped_shutdown`. It does not — `close()` sets `_connected = False`, so records
the consumer reaches afterwards are classified `dropped_not_connected` (`es_handler.py:605`);
`dropped_shutdown` is reserved for records abandoned when the consumer is cancelled or the queue swept
(`es_handler.py:762`). The consequence is the same, the mechanism is not, and a plan that names the
wrong counter sends the next person to the wrong place.

The `finally` is not decoration: `disconnect()` can raise — a loop error, or a client `close()` failure
out of `es_logger.disconnect` — and without it the handler stays attached to the root logger with a dead
client behind it. Detachment is guaranteed; the original exception still propagates.

### `add_elasticsearch_handler` becomes idempotent

It is a bare `root_logger.addHandler(handler)` with no duplicate check. Until now that was safe because
there was exactly one call site. **This ticket creates the second**, so a double attach becomes a
reachable way to index every log record twice. Two lines to make it a no-op on a handler already
attached. Folded in because this change is what makes the hazard reachable.

### Gateway (`gateway/app.py`)

- Startup: on a successful connect, `add_elasticsearch_handler(es_handler)`, keep the handler on
  `app.state.es_handler`, and keep `app.state.es_client` for read queries.
- **Tear down every constructed-but-unusable handler**, not just the "connected but no client" case.
  Codex caught the realistic leak I had missed: `ElasticsearchLogger.connect()` assigns `self.client`
  *before* awaiting `info()`, and on failure returns `False` **without closing or clearing it**
  (`es_logger.py:40-57`). Today the gateway just logs `gateway_elasticsearch_unavailable` and drops the
  reference, leaking an allocated client. So the failure branch runs the same teardown helper. (The
  inverse — `connect()` true with a `None` client — is not reachable through the real logger, but the
  guard stays: `ElasticsearchHandler.connect()` starts a consumer whenever the inner call returns true,
  so abandoning the handler there would leak a task.)
- Shutdown: `await detach_elasticsearch_handler(...)` replaces the direct `es_client.close()` — the
  handler owns that client, so closing it separately would double-close. **Then null both
  `app.state.es_handler` and `app.state.es_client`**, which endpoints resolve at request time
  (`observation_api.py:57`, `session_api.py:303`, `feedback_api.py:214`); leaving them pointing at a
  closed client is observably stale.
- ES teardown stays **last**, so the other teardown steps' records are still delivered.

### Service (`service/app.py`) — the ordering was wrong, not just the detach

My first draft called this "detach only". Codex is right that it is not. The ES block currently sits at
line 1427, **before** memory, sysgraph, route-trace, cost-tracker and cost-gate teardown — every one of
which logs (`neo4j_disconnected`, `sysgraph_disconnected`, `route_trace_ledger_disconnected`,
`cost_tracker_disconnected`, `cost_gate_disconnected`), as does `service_stopped` itself. Those records
are exactly what a shutdown investigation needs, and today every one of them hits an attached-but-
disconnected handler and is counted `dropped_not_connected`. Fixing the drain while leaving that
ordering would have shipped a ticket about delivery coverage that still drops the service's own
shutdown telemetry.

So: extract `_shutdown_es_delivery(handler)` in `service/app.py` — deregister the Captain's Log and
indexer producers, then `await detach_elasticsearch_handler(handler)` — and call it **last**, after
`service_stopped` is logged, inside a `finally` covering the intervening teardown so an earlier failure
cannot skip it. Codex verified none of those teardowns depends on Elasticsearch, so keeping the handler
alive across them is safe.

Deregistering the producers late rather than early (codex suggested early) is deliberate: queued work is
drained, so a late captain's-log write is delivered rather than lost, and one function means one seam to
test.

## Acceptance criteria → proof

The ticket names exactly two, both decidable from this branch. Master struck the live delivery-ratio
line at the dispatch decidability check and relocated it to FRE-1185 AC-7 — it is **not** carried,
quoted or discharged here.

| # | Criterion (ticket wording) | Test |
|---|---|---|
| AC1 | A test asserting the gateway lifespan **attaches a handler to the root logger** and **drains it on shutdown** | `test_gateway_lifespan_attaches_handler_to_root_logger` · `test_gateway_lifespan_drains_a_record_still_in_flight_at_shutdown` · `test_gateway_lifespan_detaches_handler_on_shutdown` |
| AC2 | A test asserting the **service drains before disconnect** | `test_service_shutdown_delivers_a_record_still_in_flight_before_closing_the_client` |

**Both are asserted at the outcome, not the wiring** — the lesson from FRE-1055's gate, where my first
timestamp test mocked away the seam that owned the behaviour and passed against reverted code.

- The gateway tests run the **real `_gateway_lifespan`** with a **real `ElasticsearchHandler`**, faking
  only `AsyncElasticsearch` (`info`, `index`, `close`). Minimum honest patch set, per codex: gateway
  `get_settings()` with `enable_memory_graph=False`, `init_db`, a fake route-trace ledger, and root-logger
  handler isolation so the assertion cannot be polluted by another test's handlers. Everything the AC is
  about — the lifespan, the handler, `log_event`, root attachment, the queue, drain, detach — stays real.
- **The drain proof must be deterministic.** Codex is right that emitting a record and finding it after
  exit proves nothing: the consumer may well have delivered it before shutdown ever began. So `index()`
  is blocked, and the test asserts as a **precondition** that the record is still undelivered when
  `__aexit__` starts — the pattern FRE-1055's `test_disconnect_drains_events_in_flight` already uses.
- **AC2 is proven at the service's own seam, not the helper's.** Codex's sharpest point: testing
  `detach_elasticsearch_handler` proves the helper works and says nothing about whether the service
  invokes it, once, at the right point — and it duplicates an FRE-1055 test that already covers the
  helper's behaviour. So the extracted `_shutdown_es_delivery` — the real function the service lifespan
  calls — is what the test drives, asserting an in-flight record is written *before* the client is
  closed, by recording call order on the fake client, plus that the Captain's Log producers were
  deregistered. A separate small test covers the helper's exception-safety (`disconnect()` raises →
  handler still detached).

## Steps

1. **RED** — `test_gateway_lifespan_attaches_handler_to_root_logger` against current code.
   → verify: fails (no handler attached). `make test-file FILE=tests/personal_agent/gateway/test_gateway_lifespan_es.py`
2. Add `detach_elasticsearch_handler` (exception-safe) to `telemetry/logger.py`, make
   `add_elasticsearch_handler` idempotent, export both from `telemetry/__init__.py`.
3. Rewire `gateway/app.py` startup + shutdown, including the failed-connect teardown and state nulling.
4. Extract `_shutdown_es_delivery` in `service/app.py`; move it last, inside a `finally`.
5. Remaining AC tests + the helper's exception-safety test.
   → verify: whole files green, then `make test`.
6. Quality gates + self-review (Step 8).

## Diff class (Step 8)

**Escalated** — trigger 1, production write path. This attaches the handler that issues every
`agent-logs-*` write in a production process, and changes teardown ordering in both. Self-serve review
still runs and its findings get fixed on-branch; flagged in the PR body and handoff for the owner's
`/code-review ultra`.

## Explicitly out of scope

Scripts, evals, migrations. No cost-tracker guard. The live delivery-ratio measurement (FRE-1185 AC-7).
