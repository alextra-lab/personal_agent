# FRE-1547 — the status bar keeps tools and ctx through non-turn readings; the stored call history carries the tool calls

Ticket: [FRE-1547](https://linear.app/frenchforest/issue/FRE-1547). Follow-up of FRE-1543 AC-7. Owner rules from
FRE-1538: the tools count stays until the next send, and ctx resets only at compaction or a new session.
Design intent: ADR-0088 D4 (the projector is the sole `turn_status` emitter), ADR-0092 §D3 (session ctx
carry), ADR-0123 §2 (tool phases derive from `ToolStartEvent` / `ToolEndEvent`), ADR-0123 §5 (absent is not zero).

## Root causes (verified)

1. **Tools reset (AC-1).** A background job (session summary, batch-child trace) makes a model call for the
   user's session. The cost boundary publishes `ModelCallCompletedEvent` for that trace. The projector creates a
   fresh `TurnObservation` (`tool_iteration` 0, ceiling `None`) and emits a `turn_status` for it. That reading
   replaces the live bar and the stored snapshot (`emit_turn_status` stores before it pushes).
2. **ctx drop (AC-2).** `_report_turn_progress` sends `ctx.last_prompt_tokens or estimate_messages_tokens(...)`.
   Before the turn's first primary call resolves, the estimate (no system prompt, about 10x low) goes out. The
   projector copies it into `SessionAggregate.context_tokens`. A HYBRID/DECOMPOSE turn calls the primary only at
   synthesis, so the estimate shows for most of the turn.
3. **No tools in the stored history (AC-3).** No production code emits `ToolStartEvent` / `ToolEndEvent`.
   `AGUITransport.send_tool_event` has no caller. The production `session_events` table holds zero
   `TOOL_CALL_START` rows. The live panel (`ToolIndicator`) and the stored `turn_summary.tools` are both empty.
   FRE-1543's summary test passed because it fed hand-made `TOOL_CALL_START` envelopes.

## Steps

### 1. Projector: only a user turn emits (AC-1)

`src/personal_agent/observability/topology/projector.py`

- Add `TurnObservation.is_turn: bool = False`. Set it to `True` on `TopologyEnteredEvent`, `TurnProgressEvent`
  and `SubAgentProgressEvent`. These come only from the executor's turn (the seam wraps the executor loop).
- The shared `await self._emit(obs)` at the end of `handle` runs only when `obs.is_turn`. `TurnCompletedEvent`
  keeps its own emit (only the seam publishes it).
- `ModelCallCompletedEvent`, `TurnDegradedEvent` and the A/B/D markers still fold their state (cost, degradation,
  session counts). A real turn's gateway calls that arrive before `topology_entered` therefore still count, and
  show on the turn's first emit.

Tests (`tests/observability/topology/test_projector.py`):
- after a completed turn at 2/25 with ctx 24,000, a `ModelCallCompletedEvent` on a batch-child trace for the same
  session produces no emit. The last emitted value still reads 2/25 and 24,000.
- a `TurnDegradedEvent` on a non-turn trace produces no emit.
- a model call that arrives before `topology_entered` of a real turn produces no emit, and the turn's first emit
  carries its cost.

### 2. ctx: an estimate never reaches the session lane (AC-2)

- `src/personal_agent/events/models.py`: `TurnProgressEvent.context_tokens: int | None = None`. `None` means
  that no primary call of this turn has resolved yet.
- `src/personal_agent/orchestrator/executor.py` `_report_turn_progress`: send `ctx.last_prompt_tokens or None`.
  The estimate is no longer sent. The `estimate_messages_tokens` import stays (other users).
- projector `TurnProgressEvent` branch: set `obs.context_tokens` and `sess.context_tokens` only when
  `event.context_tokens` is a positive int.
- projector `_emit`: the ctx group is a reading only when the session holds a real reading. Emit
  `context_max = obs.context_max if session_context_tokens > 0 else None`, so the PWA `mergeTurnStatus` and the
  server `merge_turn_status` carry the stored reading over. Emit the turn lane as
  `context_tokens = obs.context_tokens or session_context_tokens`, so a valid ctx group never holds a made-up 0.

Tests:
- `tests/test_orchestrator/test_turn_progress_report.py`: replace the FRE-1326 estimate-fallback test with
  "reports `None` before the first primary call".
- projector: turn 1 reads 24,000. Turn 2 enters HYBRID and reports `context_tokens=None` with a resolved ceiling.
  Every emit keeps `session_context_tokens` 24,000. The first real reading (for example 24,481) replaces it.
- projector: a new session with no real reading emits `context_max` `None` (—/—, absent, not 0).
- server snapshot: `merge_turn_status` keeps 24,000 across those emits (test through the real merge function).

### 3. The executor streams the tool lifecycle (AC-3)

`src/personal_agent/orchestrator/executor.py` `step_tool_execution`, Phase 2:

- Wrap each `dispatch_tool_call` in `_dispatch_announced(...)`. It emits `ToolStartEvent` before the dispatch
  and `ToolEndEvent` after it, in a `finally`, through `AGUITransport().send_tool_event`. Both emits are
  best-effort (swallow and `log.debug` with `trace_id`). No emit when `ctx.session_id` is falsy.
- `ToolStartEvent.args` is `{}`: the live panel never shows arguments, and tool arguments (file content, commands)
  must not be copied into `session_events`.
- `ToolEndEvent.result_summary` is `""` on success and `"failed"` on failure or exception. The panel shows the
  summary after the name.
- Only gate-allowed, dispatched calls are announced. Blocked or malformed calls never ran. Sub-agent tool calls
  stay out of scope (the sub-agent loop shows as `sub_agent` phases).

Because the turn's recorder is a `ContextVar` and the gather tasks copy the context, the same envelopes reach the
live queue and the turn recorder. The live panel and `turn_summary` therefore read one source.

Test (`tests/personal_agent/orchestrator/test_fre1547_tool_lifecycle_events.py`):
- start a turn recorder. Install the fake persistence from `test_transport_ordering.py` (shared seq buffer, fake
  session). Run `step_tool_execution` twice: round 1 calls `web_search`, round 2 calls `fetch_url`. Read the live
  queue. Assert: the live `TOOL_CALL_START` names are `[web_search, fetch_url]`, each start has a matching
  `TOOL_CALL_END`, and `build_turn_summary(recorder.events).tools == [web_search, fetch_url]`.
- a dispatch that raises still emits the end with `"failed"`. No session id means no tool events.

### 4. PWA fold-in: the panel shows repeated calls correctly

Turning the events on makes two latent PWA defects reachable (the same tool twice in one turn is common):

- `seshat-pwa/src/components/ToolIndicator.tsx`: the row key is `${name}-${status}`, so two completed
  `web_search` rows share a key. Key by index.
- `seshat-pwa/src/hooks/useAgentStream.ts` `TOOL_CALL_END`: it marks every row of that name completed. Mark only
  the first row of that name that is still running.
- `seshat-pwa/public/sw.js`: bump `CACHE_NAME` to `seshat-v60-fre-1547`.
- Vitest: two `web_search` starts, one end → one row completed, one running.

### 5. Docs

- `TurnProgressEvent` docstring and `_report_turn_progress` docstring: `None` before the first primary call.
- `projector.py` `TurnObservation` docstring: `is_turn`.

## Gates

```bash
make test-file FILE=tests/observability/topology/test_projector.py
make test-file FILE=tests/test_orchestrator/test_turn_progress_report.py
make test-file FILE=tests/personal_agent/orchestrator/test_fre1547_tool_lifecycle_events.py
make test-file FILE=tests/personal_agent/transport/test_turn_summary.py
make test && make mypy && make ruff-check && make ruff-format && pre-commit run --all-files
cd seshat-pwa && npm run lint && npx vitest run && npx playwright test
```

## Acceptance criteria → proof

| AC | Proof |
|----|-------|
| AC-1 non-turn readings do not touch the bar | projector test: batch-child model call after 2/25 + 24,000 emits nothing (the snapshot has one writer, `emit_turn_status`) |
| AC-2 an estimate never replaces a real reading | executor test (`None`), projector HYBRID test (24,000 held, 24,481 replaces), `merge_turn_status` test |
| AC-3 stored history carries the tool calls | executor two-round test: live queue names == `turn_summary.tools` == `[web_search, fetch_url]` |
| AC-4 existing rules hold | full `make test`, PWA vitest + Playwright green |
| AC-5 live | master runbook in the handoff |

## Codex plan review (2026-10-04, task-mutrf3oh-2y38o2)

- Q2 (ctx carry through both merges), Q3 (turn-lane fallback), Q4 (recorder and lock): OK.
- Q1: a turn whose `topology_entered` is lost stays dark until its next turn event. Added a test that turn
  progress opens the turn. Sub-agent trace inheritance verified: `expansion_controller.py:1427` passes the parent
  `trace_id`, and `sub_agent.py:2382` builds the sub-agent `TraceContext` from it.
- Q5: two existing degradation tests raised a degradation on a trace with no turn. They now open the turn first,
  as a real `decompose` degradation does. A name-only `TOOL_CALL_END` cannot match out-of-order calls of the same
  tool. Accepted: rows of the same name look alike, and only a mixed success/failure pair can swap its label.

## Deploy

`seshat-gateway` rebuild plus a PWA rebuild (`CACHE_NAME` bump).
