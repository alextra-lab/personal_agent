# FRE-1492 — ADR-0150 D6: finish_reason on every worker call

Backing ADR: `docs/architecture_decisions/ADR-0150-the-worker-returns-data-not-prose.md`, D1
(`length` row of the validity table) and D6. ADR-0150's own **AC-3** (not the Linear ticket's
AC-1..AC-5 breakdown) is the canonical acceptance text; see "Scope decision" below for why the
two differ and which one this plan follows.

## Scope decision — AC-2 as literally written vs. the ADR's own AC-3

The ticket's AC-2 ("an unknown finish reason is a ledger" — `None` or `"content_filter"` on the
landing must force `report_kind == "ledger"` even with valid content) is D1's schema-backed
validity table, which only binds a **schema-backed** worker (T3, not yet built). Applied to
today's text-reporting workers it would force every existing landing with an unset
`finish_reason` (i.e. every current test and every current live worker) to `ledger` regardless
of content — directly breaking AC-4 ("nothing else changes") and the whole existing suite.
ADR-0150's own AC-3 (line 690) only requires: `"length"` → `narration`; a `None` fixture →
`ledger` (that fixture's ledger comes from its content being empty/unparseable, not from
`finish_reason` alone). D6's body text names only the `"length"` row as T1's. This plan
implements D6 literally — the `"length"` check only — and documents the AC-2 gap explicitly in
the handoff for master.

## Files touched

1. `src/personal_agent/llm_client/types.py` — fix `LLMResponse.finish_reason` docstring (says
   cloud-only; the local adapter has populated it since FRE-1413).
2. `src/personal_agent/orchestrator/sub_agent_types.py` — add `SubAgentResult.finish_reason:
   str | None = None`; update the `SubAgentReportKind` narration comment to cover the
   completed-path cut, not only forced synthesis.
3. `src/personal_agent/captains_log/capture.py` — add `SubAgentCapture.finish_reason: str |
   None = None`.
4. `docker/elasticsearch/captains-subagents-index-template.json` — add `finish_reason` (keyword)
   at top level and inside `rounds.properties`. Additive, reversible. Extend
   `tests/scripts/test_es_templates.py::test_subagents_pins_tool_loop_fields` (codex finding
   #3) with both assertions so an omitted mapping fails a fast static test, not just runtime.
5. `src/personal_agent/orchestrator/sub_agent.py`:
   - `_extract_finish_reason(response: Any) -> str | None` — new helper, mirrors
     `_extract_call_cost`.
   - `_ToolCallRecord` — add `finish_reason: str | None = None`.
   - `_ToolLoopState.capture_rounds()` — include `"finish_reason": record.finish_reason`.
   - `_ToolLoopOutcome` — add `finish_reason: str | None = None`.
   - `_forced_synthesis` — read `finish_reason` right after the call, before
     `_parse_llm_response`/`_extract_stated_tool_gap` touch the content. `== "length"` →
     narration (partial + ledger) if the partial is non-empty after strip, else ledger; both
     branches skip `_extract_stated_tool_gap` (a cut reply's sentinel line cannot be trusted).
     Every other branch threads `finish_reason=finish_reason` onto its existing
     `_ToolLoopOutcome`.
   - `_run_tool_loop`'s round call — read `round_finish_reason` right after `raw_tool_calls =
     _extract_tool_calls(raw_response)`, **before** `_parse_llm_response`/`state.round_texts`
     are touched (codex plan-review finding #1: checking only before
     `_extract_stated_tool_gap` is too late — `response_content` is parsed and appended to
     `state.round_texts` unconditionally before the no-tool-calls branch runs today, so a cut
     round's text would already be stored and could be repeated inside `_build_ledger`'s "Model
     notes per round" section). So: `if not raw_tool_calls and round_finish_reason == "length":`
     is its own branch, parsing content only inside it and returning narration/ledger before any
     `round_texts` append; the ordinary `response_content = _parse_llm_response(...)` +
     conditional `round_texts.append(...)` then runs for every other case (both the non-cut
     no-tool-calls path and the tool-call path), unchanged from today. Every existing return
     threads `finish_reason=round_finish_reason`. In the tool-call branch, `_absorb` (already a
     closure over `state`) also closes over `round_finish_reason` and stamps it onto every
     `_ToolCallRecord` it builds this round — no new parameter, no new behavior on that path
     (item 4 of the ticket: a cut tool call's malformed arguments already fail at the existing
     `json.JSONDecodeError` handler).
   - `run_sub_agent` — thread `outcome.finish_reason` onto the success-path `SubAgentResult`.
   - `_emit_sub_agent_capture` — read `result.finish_reason` onto the `SubAgentCapture` it
     builds (no new parameter; `result` already carries it).
   - `_killed_result` — untouched. No report-writing call happened on that path, so `None` is
     correct by the field's own definition.

## Tests (`tests/personal_agent/orchestrator/test_sub_agent.py`)

- `_llm_response_with_finish_reason(content, finish_reason, tool_calls=None)` helper, mirroring
  `_llm_response_with_cost`.
- **AC-1**: forced-synthesis cap path and the tool-loop's own completed path, each with
  `finish_reason="length"` and non-empty prose content → `report_kind == "narration"`,
  `success == False`, content = partial + ledger, `result.finish_reason == "length"`, and (codex
  finding #2) the emitted `SubAgentCapture.finish_reason == "length"` too — asserted via the
  existing `write_sub_agent_capture` monkeypatch-capture pattern, per ADR-0150's own AC-3 line
  ("the capture's `finish_reason == \"length\"`").
- **AC-2** (scoped per the decision above): `finish_reason=None` with empty content on a plain
  completed landing still yields `ledger` — proves the field's addition does not disturb the
  pre-existing empty-content path.
- **AC-3**: a three-tool-round stub (each round's response carries `finish_reason="tool_calls"`)
  followed by a `finish_reason="stop"` completed reply. Capture `write_sub_agent_capture`'s
  argument (existing pattern from `TestTerminalPathsDeclareAReport`) and assert `rounds` has 3
  entries each with `finish_reason == "tool_calls"`, and `result.finish_reason == "stop"`.
- **AC-4**: no existing test in this file is modified; run the full file to confirm.
- **AC-5**: monkeypatch `_extract_finish_reason` to always return `None` and re-run AC-1's cap
  scenario — outcome must become `synthesized`, proving the check is load-bearing.

## Quality gates

`make test` (targeted file first, then full suite once) · `make mypy` · `make ruff-check` +
`make ruff-format` · `pre-commit run --all-files`. Self-review with
`feature-dev:code-reviewer` scoped to `git diff origin/main...HEAD`; no security-review trigger
(no subprocess/auth/network/secrets touched).
