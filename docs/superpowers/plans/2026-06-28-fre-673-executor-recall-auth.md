# FRE-673 — Thread request identity into the executor entity-recall path

**Ticket:** FRE-673 (Approved, Tier-2:Sonnet) · **Backing:** FRE-229 (visibility scoping), ADR-0100 / FRE-435 (recall quality)

## Problem (confirmed in code + Neo4j)

`orchestrator/executor.py`'s memory-enrichment block (gated only on `settings.enable_memory_graph`,
so it runs on **every** turn, independent of `gateway_output`) calls two recall functions **without
identity**:

- `query_memory_broad(...)` (~line 2000) — broad recall path
- `query_memory(query, feedback_key=..., query_text=...)` (~line 2040) — entity-name path

Both default `user_id=None`, `authenticated=False`. `_build_visibility_filter` reveals
`visibility='group'` content **only when `authenticated=true`**. In prod **all** memory is `group`
(2145/2145 Turns, 7392/7392 Entities), so the filter drops 100% of memory → `candidate_set_size=0`
on every executor-path recall ("no prior discussions").

Root of the threading gap: `ExecutionContext` carries `user_id` but **no `authenticated` field**, and
`Orchestrator.handle_user_request` never receives `authenticated`. The gateway pipeline path
(`run_gateway_pipeline` → `_query_memory_for_intent`) already threads `authenticated=True` correctly —
this is why the proactive path returns memory while the executor path returns zero.

## Fix — thread identity through the chain (4 surgical edits)

1. **`src/personal_agent/orchestrator/types.py`** — add `authenticated: bool = False` to
   `ExecutionContext`, next to `user_id` (FRE-229 comment block ~line 226).
2. **`src/personal_agent/orchestrator/orchestrator.py`** — add `authenticated: bool = False` kwarg to
   `handle_user_request`; pass `authenticated=authenticated` into the `ExecutionContext(...)` ctor
   (~line 109). Docstring line for the new arg.
3. **`src/personal_agent/service/app.py`** — pass `authenticated=True` at both `handle_user_request`
   call sites (lines ~376 and ~1800). Both are CF-Access-authenticated endpoints and already pass
   `authenticated=True` to `run_gateway_pipeline` (lines 340, 1741) — this restores symmetry.
4. **`src/personal_agent/orchestrator/executor.py`** — thread identity into both recall calls:
   - `query_memory_broad(...)` (~2000): add `user_id=ctx.user_id, authenticated=ctx.authenticated`.
   - `query_memory(...)` (~2040): add `user_id=ctx.user_id, authenticated=ctx.authenticated`.

All new kwargs default-compatible: test callers of `handle_user_request` that omit `authenticated`
keep the prior unauthenticated behavior (no breakage).

## Tests (TDD — write failing first)

### Unit regression guard (catches THIS bug) — `tests/test_orchestrator/test_executor.py`
Drive `handle_user_request(user_id=uid, authenticated=True, ...)` with the global `memory_service`
mocked (pattern: `test_search_memory_tool_called_when_llm_requests_it`, patch
`personal_agent.service.app`), and `settings.enable_memory_graph=True`:
- **Entity path:** message with a capitalized >3-char token (e.g. "Tell me about Athens"),
  not a memory-recall phrase → assert `memory_service.query_memory` called with
  `user_id == uid` and `authenticated is True`. (Fails pre-fix: called without them.)
- **Broad path:** a memory-recall message (`is_memory_recall_query` true) → assert
  `memory_service.query_memory_broad` called with `user_id == uid`, `authenticated is True`.

### AC outcome proof (substrate, real Neo4j :7688) — `tests/personal_agent/memory/test_executor_recall_visibility.py` (`integration` marker)
Mirror `test_participated_in_edge.py` fixtures. Seed a `visibility='group'` Turn (unique
`turn_id`, `key_entities=["<Token>"]`) via `create_conversation(turn, user_id=uid, visibility="group")`:
- `query_memory(MemoryQuery(entity_names=["<Token>"]), user_id=uid, authenticated=True)` →
  the seeded turn is in `result.conversations`.
- same query with `authenticated=False` → the turn is **absent** (filtered — current behavior).

## Audit of other recall call sites (ticket asks)
- `service/app.py:2252` — already passes `authenticated=True`. OK.
- `request_gateway/context.py` (recall/recall_broad/suggest_relevant) — already threads identity. OK.
- `tools/memory_search.py:156,178` (`search_memory` tool) — **sibling bug, same class**: omits
  identity. But `TraceContext` carries `user_id` and **not** `authenticated`, so the fix needs its own
  design decision (add `authenticated` to `TraceContext` vs. derive from `user_id`). → **out of scope;
  file follow-up ticket** (Step 5). Keeps this PR surgical to the executor path the ticket names.
- `gateway/app.py`, `ui/memory_cli.py` — separate standalone-gateway / CLI surfaces; not the live
  agent recall path. Not in scope.

## Quality gates
`make test-file FILE=tests/test_orchestrator/test_executor.py` → module green ·
`make test` (full unit) · `make mypy` · `make ruff-check` + `make ruff-format` ·
`pre-commit run --all-files`. Integration test runs against test substrate :7688 with
`AGENT_NEO4J_PASSWORD` (per reference note) — proven, not skipped.
