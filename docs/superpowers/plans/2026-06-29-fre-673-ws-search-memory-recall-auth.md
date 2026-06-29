# FRE-673 (continuation) — thread identity into the live agent-recall path (search_memory tool)

**Ticket:** FRE-673 (reopened by master, In Progress). PR #275 merged but live recall still
returned `candidate_set_size=0`. Backing: FRE-229 (visibility scoping), ADR-0100 / FRE-435.

## Why #275 didn't fix it (corrected diagnosis from live telemetry)

#275 fixed the executor *inline* recall (`step_init`, runs only when `gateway_output is None`) — not
the live path. Live trace `88164bbf` (prod, after #275 deploy) shows the real flow:

1. PWA sends over HTTP `/chat/stream` (ADR-0075 hybrid transport) → `_process_chat_stream_background`.
2. `run_gateway_pipeline(authenticated=True)` runs the proactive path (authenticated, works).
3. Orchestrator runs; the LLM then **calls the `search_memory` tool**.
4. `search_memory_executor` → `query_memory` → **`memory_recall` candidate_set_size=0** (×3 in the trace).

The three empty `memory_recall` events are emitted by the **`search_memory` tool**, not the gateway
`context.py` recall. The tool called `query_memory`/`query_memory_broad` **without identity**: its
`ctx` is a `TraceContext`, which carried `user_id` but had **no `authenticated` field**, so the
FRE-229 filter dropped 100% of the (all-`group`) memory despite strong vector matches
(top_vector_score 0.76–0.89, vector_entity_count 20). Neo4j confirms: visibility clause with
`authenticated=true` → 1849 turns, `false` → 0.

This is the site originally filed as FRE-676; master's reopen makes it the FRE-673 fix.

## Fix (thread the real `authenticated` to the tool via TraceContext)

1. `telemetry/trace.py` — add `authenticated: bool = False` to `TraceContext`; propagate through
   `new_span` (tool dispatch spans) and `new_trace`.
2. `orchestrator/executor.py` (`execute_task`, ~1436) — thread `authenticated=ctx.authenticated` into
   the `TraceContext` it builds (the one passed to tool executors). `ctx.authenticated` exists from #275
   and is fed `True` from both `service/app.py` call sites.
3. `tools/memory_search.py` — pass `user_id=getattr(ctx,"user_id",None)` and
   `authenticated=getattr(ctx,"authenticated",False)` into both `query_memory` and `query_memory_broad`.

Chain end to end: `/chat/stream` → `handle_user_request(authenticated=True)` [#275] →
`ExecutionContext.authenticated` → `execute_task` `TraceContext.authenticated` → tool dispatch span →
`search_memory` reads `ctx.authenticated` → `query_memory(authenticated=True)`.

## Tests
- `tests/test_tools/test_memory_search.py` — entity + broad paths thread `ctx.user_id` +
  `ctx.authenticated` into `query_memory`/`query_memory_broad` (fail before, pass after).
- `tests/test_telemetry/test_trace.py` — `authenticated` defaults False and propagates through `new_span`.

## Scope notes
- FRE-676 narrows to the remaining sites (`memory_cli`, `get_related_conversations`) — `search_memory`
  is fixed here.
- The fail-closed `RequestContext` + single-chokepoint rewrite remains FRE-678 (ADR). This is the point fix.

## Live re-check (master): after deploy, a `search_memory` `memory_recall` event on an authenticated
turn shows `candidate_set_size > 0` when relevant entities exist.
