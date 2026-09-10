# FRE-1399 — a capped sub-agent can report an empty digest

Ticket: https://linear.app/frenchforest/issue/FRE-1399
Backing tickets (design intent): FRE-1389 (built the tool loop), FRE-1379 (killed-worker
partial-progress pattern), FRE-1387 (digest-cap clip visibility, `_warn_if_clipped` pattern).

## Root cause (AC-2)

`orchestrator/sub_agent.py::_run_tool_loop`, the cap check:

```python
if state.tool_iterations >= settings.sub_agent_max_tool_iterations:
    raise _ToolIterationLimitReached(response_content)
```

`response_content` is only the CURRENT round's text. A model that requests a tool call
frequently emits no accompanying assistant text (`content: ""`) — the round is tool-calls-only.
When the round that trips the cap happens to be tool-calls-only, `response_content` is `""` and
the entire report is empty, even though `state.tool_result_chars_absorbed` shows real work
happened (1,688 chars, trace f9cd43a0). When the capping round happens to carry assistant text
alongside its tool call, the report is non-empty (1,677 chars, trace c4df3857). Same cap, same
setting — the difference is only whether that one round's completion carried text, which is
model-behavior variance, not something the loop controls today.

`tool_result_chars_absorbed` (raw tool RESULT content fed back into the sub-agent) is a
different pool of text than assistant-authored content and by design never crosses into
`summary`/`full_output` (FRE-1389 AC-4's isolation guarantee) — so the fix cannot be "read from
there instead."

## Fix

1. Track every round's own assistant text (not just the capping round's) in
   `_ToolLoopState.round_texts`. On cap, join all of it — this recovers c4df3857-shaped cases
   unconditionally, and recovers f9cd43a0-shaped cases whenever ANY earlier round carried text.
   A round's text counts only when `response_content.strip()` is non-empty — codex plan-review
   flagged that a bare `if response_content` truthiness check would treat whitespace-only
   content (`" \n"`) as real narrative and still produce an effectively empty digest.
2. When literally no round ever produced assistant text (all-tool-call rounds throughout),
   synthesize a deterministic, non-generated description of what happened (round count + chars
   absorbed) — never a second LLM call (FRE-1387 ruled that out for the digest cap; same
   reasoning applies: no summarization call on the terminal path).
3. Thread a `narrative_synthesized: bool` field through `SubAgentResult` and `SubAgentCapture`
   so "found nothing" (real, if terse, model text) is distinguishable from "did work, could not
   report it" (synthesized fallback) — both in the capture record (AC-4) and via a WARNING log
   event mirroring `_warn_if_clipped`/`sub_agent_output_clipped` (AC-3).

## Steps

1. `src/personal_agent/orchestrator/sub_agent.py`
   - `_ToolLoopState`: add `round_texts: list[str] = field(default_factory=list)`.
   - `_run_tool_loop`: right after `response_content = _parse_llm_response(raw_response)`,
     append it to `state.round_texts` when `response_content.strip()` is non-empty — before the
     no-tool-calls return and before the cap check, so the capping round's own text (if any) is
     included.
   - `_ToolIterationLimitReached.__init__`: add `narrative_synthesized: bool` parameter,
     stored as `self.narrative_synthesized`.
   - New helper `_build_capped_partial_content(state: _ToolLoopState) -> tuple[str, bool]`:
     returns `("\n\n".join(state.round_texts), False)` when `round_texts` is non-empty, else a
     deterministic fallback string (mentions `tool_iterations` and
     `tool_result_chars_absorbed`) and `True`.
   - Cap-check branch: call the helper, pass both values into the raised exception.
   - `run_sub_agent`'s `except _ToolIterationLimitReached as exc:` block: pass
     `narrative_synthesized=exc.narrative_synthesized` into the constructed `SubAgentResult`.
   - New helper `_warn_if_narrative_synthesized(result, trace_id, session_id)`, mirroring
     `_warn_if_clipped`'s shape, logging `sub_agent_iteration_cap_narrative_synthesized` at
     WARNING when `result.narrative_synthesized` is `True`. Call it next to the existing
     `_warn_if_clipped(...)` call near the end of `run_sub_agent`.
   - `_emit_sub_agent_capture`: pass `narrative_synthesized=result.narrative_synthesized` into
     the `SubAgentCapture(...)` construction.

2. `src/personal_agent/orchestrator/sub_agent_types.py`
   - `SubAgentResult`: add `narrative_synthesized: bool = False`, with a docstring line
     explaining it distinguishes a synthesized fallback from the model's own (possibly terse)
     text (FRE-1399).

3. `src/personal_agent/captains_log/capture.py`
   - `SubAgentCapture`: add `narrative_synthesized: bool = False` next to the other
     FRE-1389 tool-loop fields. No index-mapping change needed (dynamic ES mapping).

4. `src/personal_agent/telemetry/error_monitor.py`
   - Add `"sub_agent_iteration_cap_narrative_synthesized"` to `WARNING_EVENT_ALLOWLIST`, with a
     one-line comment referencing FRE-1399, mirroring the existing `sub_agent_output_clipped`
     comment.

## Tests (TDD — write failing first)

In `tests/personal_agent/orchestrator/test_sub_agent.py`, new class `TestIterationCapNarrative`:

- `test_capped_worker_recovers_earlier_round_text_even_when_capping_round_is_empty` — cap=2,
  round 1 has real text + a tool call, rounds 2 and 3 are tool-calls-only with empty text.
  Assert `result.summary` contains round 1's text and `result.narrative_synthesized is False`.
  This is the c4df3857/f9cd43a0-reconciling case: AC-1 (non-empty) and AC-2 (explains why one
  case differs from the other) in one assertion.
- `test_capped_worker_with_no_assistant_text_anywhere_gets_synthesized_fallback` — cap=1, every
  round's response is tool-calls-only with empty text. Assert `result.summary` is non-empty,
  `result.narrative_synthesized is True`, and the fallback text mentions the absorbed char count.
- `test_whitespace_only_round_text_is_not_treated_as_narrative` — cap=1, every round's response
  is tool-calls-only with content `" \n"` (whitespace, not empty). Assert
  `result.narrative_synthesized is True` and the fallback (not the whitespace) is what
  `result.summary` holds — codex plan-review's required change.
- `test_narrative_synthesized_emits_its_own_warning` — same seed as above, with
  `structlog.testing.capture_logs()`; assert one `sub_agent_iteration_cap_narrative_synthesized`
  WARNING carrying `trace_id`/`tool_iterations`/`tool_result_chars_absorbed`.
- `test_real_narrative_emits_no_synthesized_warning` — the recovered-earlier-round-text case
  emits no such warning (mirrors `test_output_under_new_cap_emits_no_clip_warning`).
- `test_narrative_synthesized_warning_is_allowlisted` —
  `"sub_agent_iteration_cap_narrative_synthesized" in WARNING_EVENT_ALLOWLIST`.
- `test_capture_carries_narrative_synthesized` — extends the existing
  `test_capture_carries_tool_loop_activity` pattern (monkeypatch `write_sub_agent_capture`) to
  assert the field reaches `SubAgentCapture`.
- Regression: `test_iteration_cap_stops_the_loop_with_explicit_failure` (existing, line ~538)
  must keep passing unchanged.

## Test commands

```bash
make test-file FILE=tests/personal_agent/orchestrator/test_sub_agent.py
make mypy
make ruff-check
make ruff-format
```

## AC-2 explanation to record in the handoff comment

State plainly: the two traces differ only in whether the round that tripped the cap happened to
carry assistant text alongside its tool call — a property of that one model completion, not of
the cap or the loop. The fix removes the dependence on that one round by keeping every round's
text, and removes the possibility of zero content by synthesizing a deterministic description
when no round ever produced text.

## Out of scope

- The `_killed_result` path (timeout/cancellation) — already fixed by FRE-1379, not defective
  here, not touched.
- Any second LLM call to summarize — ruled out by FRE-1387's precedent for the same shape.
- Changing `sub_agent_max_tool_iterations` itself.
