# FRE-1535 — Tool approval fails closed

> Tier: Standard (touches `src/` logic, security). Codex plan-review required.
> Governing ADR: ADR-0063 (D3 action-boundary governance). Consumer: ADR-0153 D7 (FRE-1536).

## Problem

`_check_permissions` (`src/personal_agent/tools/executor.py`) allows a tool that requires approval,
with only a warning, in three cases: no session id, `approval_ui_enabled` false, and no transport.
No `ToolExecutionLayer` is built with a transport, so on the live gateway
(`AGENT_APPROVAL_UI_ENABLED=true`) every approval-required call runs without a prompt.

## Owner decisions (relayed by cc-master, 2026-10-03 07:18 UTC)

| # | Question | Decision |
|---|----------|----------|
| 1 | A turn with no PWA client (CLI HTTP `/chat`, harness, probe) calls an approval-required tool | **Deny.** |
| 2 | Meaning of `AGENT_APPROVAL_UI_ENABLED=false` | **Explicit opt-out.** Tools run without a prompt. The log event names the real cause. |
| 3 | Sub-agents | **Keep the FRE-1461 broker.** The broker asks once per tool per turn. The generic gate does not ask again. |
| 4 | Which of the eight tools keep the prompt | **All eight.** `tools.yaml` does not change. |

Question 1 needs an ADR-0063 amendment. It is included in this PR (Amendment A).

## Design

Policy table for an approval-required tool (after the mode check and the bash allowlist shortcut):

| UI flag | session id | transport | Outcome | Log event |
|---------|-----------|-----------|---------|-----------|
| false | any | any | allow | `approval_ui_disabled_proceeding` (warning) |
| true | none | any | deny | `approval_denied_no_session_id` |
| true | yes | none | deny | `approval_denied_no_transport` |
| true | yes | yes | round trip; only `approve` allows | `approval_denied` with the decision on any other answer |
| sub-agent call that the broker approved (`approved_upstream=True`) | any | any | allow (the broker was the gate) | none |
| sub-agent call that the broker did not gate | any | none | deny, by the rows above | `approval_denied_no_transport` |

"No PWA client" is `connection_lost`: the transport exists, but the session has no WebSocket.
`AGUITransport.request_tool_approval` already returns `connection_lost` without waiting. The gate
denies it as `approval_connection_lost`.

Changes:

1. `tools/executor.py`: one helper, `_request_approval`, runs the round trip. The generic gate and
   the new `approve` callable both use it.
2. `tools/executor.py`: reorder the gate to the table above.
3. `tools/executor.py`: `execute_tool` builds an `approve` callable and passes it to executors that
   declare an `approve` parameter, the way it passes `ctx`. `approve` is a reserved name. The layer
   removes any model-supplied `approve` from the arguments, then sets the real callable. The
   callable returns `deny` with no transport, no session id, or the UI flag off (ADR-0153 D7). The
   request carries the exact `session_id` and `trace_id` of the invocation. A sync executor that
   declares `approve` is refused with a failed `ToolResult`, because it cannot await the callable.
4. `tools/executor.py`: `execute_tool(..., approved_upstream: bool = False)`. When true, the generic
   approval step is skipped. The flag means "the broker approved this call". It does not follow from
   the principal. `sub_agent.py` passes it through `dispatch_tool_call` only when the tool is in the
   broker's approval set (`approval_required_tools`), because only then the broker has run for this
   call. A tool outside that set keeps the generic gate. The set is frozen at spawn and the generic
   gate reads the mode at call time. A NORMAL→ALERT change can make a tool newly approval-required.
   That call reaches the generic gate with no transport and is denied (codex plan-review, finding 2).
5. `orchestrator/executor.py` `_get_tool_execution_layer`: build the primary layer with
   `AGUITransport()`. The class holds no state. It uses the module-level session registries.
6. ADR-0063 Amendment A, the `approval_ui_enabled` field description, `.env.example`.

Not changed: `tools.yaml`; the default of `approval_ui_enabled` (false); `bootstrap.py` (its layer
has no transport and now fails closed); the shared layer for sub-agents (no transport by design).

## Tests (TDD, written first)

New file `tests/test_tools/test_approval_fail_closed.py`.

| Test | Proves |
|------|--------|
| flag on, no session → denied, event `approval_denied_no_session_id` | AC-2 |
| flag on, session, no transport → denied, event `approval_denied_no_transport` | AC-2 (CLI, harness) |
| flag off → allowed, event `approval_ui_disabled_proceeding`; flag on never logs it | AC-2 |
| `connection_lost` → denied `approval_connection_lost` | AC-2 (no PWA client) |
| `deny`, `timeout` → denied; `approve` → allowed | AC-1 |
| layer: `approved_upstream=True`, no transport → runs; default → denied | AC-2 (sub-agent) |
| layer: mode changes NORMAL→ALERT, `run_python` not broker-gated, `approved_upstream=False`, no transport → denied | AC-2 (mode drift) |
| `dispatch_tool_call` forwards `approved_upstream` to `execute_tool` | AC-2 (sub-agent) |
| sub-agent loop: a tool in the broker set reaches dispatch with `approved_upstream=True` after approval; a tool outside the set reaches it with `False`; a broker denial never dispatches | AC-2 (sub-agent) |
| `approve` request carries the exact `session_id` and `trace_id` of the invocation | AC-3 |
| tool definition that exposes a parameter named `approve`: a model-supplied value never reaches the executor | AC-3 (spoof) |
| sync executor that declares `approve` → failed `ToolResult`, executor does not run | AC-3 |
| primary layer has an `AGUITransport`; the shared layer has none | wiring |
| real `AGUITransport` + fake connection: `bash` outside the allowlist pushes one request, and the command does not run until the decision; deny leaves no marker file; approve creates it | AC-1 |
| `approve` with transport calls `request_tool_approval` once and returns its decision | AC-3 |
| `approve` with no transport, no session, flag off → `deny` | AC-3 |
| executor without `approve` gets none; an LLM-supplied `approve` argument is dropped | AC-3 |

Existing tests that assume the old fail-open path are updated to the table above.

Commands:

```bash
make test-file FILE=tests/test_tools/test_approval_fail_closed.py
make test-file FILE=tests/test_tools/test_executor.py
make test-file FILE=tests/test_tools/test_bash_auto_approve_wire.py
make test
make mypy
make ruff-check && make ruff-format
pre-commit run --all-files
```

## Acceptance criteria

| AC | Evidence | Where |
|----|----------|-------|
| AC-1 | Real-transport test above | PR |
| AC-2 | One test per path in the policy table | PR |
| AC-3 | Three `approve` tests | PR |
| AC-4 | One live PWA turn that calls `bash` shows the prompt | After deploy, needs the owner's OK. Master fires it. |

## Risks and gotchas

- Once deployed, `bash` outside the allowlist prompts in the PWA. Parallel calls in one assistant
  message each send a card. The owner accepts this side effect.
- A prompt waits `approval_timeout_seconds` (60 s), then denies.
- A PWA that lost its socket gets `connection_lost`, so the call is denied.
- The eval gateway sets the flag to false on purpose (FRE-1505), so it is unchanged.
- The code default of the flag is false. The live `.env` sets true. A deploy without that line stays
  open. This PR does not change the default.
- `AGUITransport.request_tool_approval` persists the request event even with no connection. A CLI
  session can hold a stale approval event. It cannot resolve, because no waiter exists.
