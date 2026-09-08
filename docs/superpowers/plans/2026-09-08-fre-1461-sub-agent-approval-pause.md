# FRE-1461 — wire the constraint pause to the sub-agent tool path

Ticket: https://linear.app/frenchforest/issue/FRE-1461
Backing design: ADR-0076 (the constraint pause) · ADR-0142 D4a (the wait credited against
the turn lifetime) · ADR-0145 D1 / FRE-1444 (the worker budget belongs to the role) ·
FRE-1388 (the sub-agent tool principal).

## 1. What the code does today

Four facts establish the gap. Each is read from the current tree, not assumed.

1. **The sub-agent tool loop never asks anyone.** `orchestrator/sub_agent.py:_run_tool_loop`
   refuses an out-of-grant tool name, then calls `dispatch_tool_call` directly. No pause,
   no approval, no approver.
2. **The tool layer's own approval channel is dead.** `tools/executor.py:227-279` performs an
   approval round trip only when `settings.approval_ui_enabled` is true **and** the
   `ToolExecutionLayer` holds a `transport`. `approval_ui_enabled` defaults to `False`
   (`config/settings.py:2760`), and no `ToolExecutionLayer` in the tree is ever constructed
   with a transport (`bootstrap.py:122`, `executor.py:3171`, `tool_dispatch.py:56` — all
   three omit it). Every approval-requiring call therefore takes the
   `approval_ui_disabled_proceeding` branch and is **allowed with a warning**.
3. **The live "ask the owner" channel is the constraint pause.** `_maybe_pause_for_constraint`
   (`orchestrator/executor.py:682`) pushes a `CONSTRAINT_PAUSE` over the WS transport, waits,
   and applies a safe default on timeout. It already caps its own wait to the turn's
   remaining lifetime through `ctx` (ADR-0142 D4a).
4. **The gap is currently unreachable in production.** The only granted sub-agent tool is
   `run_python`; its `requires_approval_in_modes` is exactly `["ALERT", "DEGRADED"]`
   (`config/governance/tools.yaml:464`), and `SUB_AGENT_DENIED_MODES` removes every
   sub-agent tool in exactly those two modes. That circularity is the FRE-1388 directive
   this ticket removes the basis for. It also means this change ships inert and becomes
   load-bearing when FRE-1463 widens the grant.

## 2. Design decisions

### D1 — the fan-out answer: one prompt per distinct tool, per turn

**Policy.** The first sub-agent in a turn whose granted tool requires approval in the current
mode pauses and asks. The answer is recorded on a turn-scoped broker and applied, without a
further prompt, to every later sub-agent request for **the same tool name** in **the same
turn**. Six sub-agents that all want `run_python` produce exactly one prompt.

**Why the tool name is the key, and not the call.** Arguments differ per sub-agent by design,
so keying on arguments restores N prompts — the exact failure the ticket forbids. The owner's
decision is about the capability, not the call site.

**Why not a per-turn budget.** A budget denies the seventh identical request after the owner
already said yes to the first. That is incoherent, and the owner would switch it off.

**Why not deny-by-default-plus-information.** That is today's behaviour in a nicer wrapper. It
fails AC-1, which requires the answer to reach the sub-agent.

**Concurrency.** Dispatch is serialized today (`expansion_controller.py:660`, a `for` loop —
FRE-1397). The broker still holds an `asyncio.Lock` across the pause, so a sibling arriving
during an in-flight ask waits for that answer instead of raising a second card. The policy
then holds under a future concurrent dispatch without a second change. The lock covers the
whole sequence — cache lookup, the pause, the failure-to-denial conversion, and the cache
insertion — so no interleaving can produce a second card for a tool already being asked about.

### D2 — the safe branch is a denial, and the broker converts a failure into one

Timeout, a `user_cancel` from the Stop button, no client, a governance lookup failure, or no
broker all resolve to **deny**. A denial appends a tool-role message in the shape the loop
already handles for an out-of-grant name (`{"status": "error", "hint": ...}`) and the loop
continues.

**`_maybe_pause_for_constraint` is not itself exception-free**, so the broker cannot simply
assume it. Its awaits are unguarded, and the waiter contract re-raises a registration-callback
exception after cleanup (`ws_endpoint.py:340`). The broker therefore wraps the whole pause in
`except Exception`, converts it to a denial, and caches that denial like any other answer.

It catches `Exception`, never `BaseException`. `asyncio.CancelledError` must keep propagating:
`run_sub_agent` deliberately re-raises it (`sub_agent.py:848-869`) to preserve an external
dispatch cancellation, and swallowing it here would defeat that. The Stop button does not
take this path — it resolves the waiter with `user_cancel` (`ws_endpoint.py:492-522`), which
is a denial, not a cancellation.

**What AC-2 does and does not claim.** The claim is that a denial does not raise and the
parent turn completes. It is *not* that the sub-agent always reports success: a model that
retries the denied tool until `sub_agent_max_tool_iterations` trips still ends at the
iteration cap's own explicit failure (`sub_agent.py:568`), which is correct existing
behaviour and not something this ticket should mask.

### D3 — the wait is bounded, and sits inside the worker's own budget

Three bounds already exist and this design uses all three rather than adding a fourth:

- `settings.constraint_pause_timeout_seconds` — the pause's nominal ceiling.
- The turn lifetime — `_maybe_pause_for_constraint` caps its own wait to
  `_turn_lifetime_remaining(ctx)` when given `ctx`, so passing the real `ctx` gets the
  ADR-0142 D4a cap and the pause accounting.
- The worker's own `hard_deadline` — the `asyncio.wait_for` already wrapping `_run_tool_loop`,
  sized from the role's budget (ADR-0145 D1).

**The budget rule that makes AC-3's "proceeds on the safe branch" true rather than a race.**
A pause is opened only when the worker's remaining budget exceeds the nominal pause ceiling
**plus an explicit finalization reserve**. A bare strict comparison against the ceiling alone
is not enough: option resolution, waiter registration, the `phase_span` entry and exit, pause
accounting, the `CONSTRAINT_RESOLVED` emission and the refusal-message append all run around
the wait, so a remaining budget of "ceiling plus epsilon" can clear the guard and still let
the outer deadline fire before the refusal is recorded. The reserve is a named module
constant covering that work, which is entirely local and in-process — no network call sits on
the refusal path. Otherwise the request is denied immediately, with no card.

**Accepted consequence, stated rather than fixed.** `_maybe_pause_for_constraint` credits the
wait to `ctx.credited_pause_seconds`, but the expansion dispatch deadline is an absolute value
fixed before the planner runs (`executor.py:5012-5018`, FRE-1397), so that credit cannot flow
back into it. A long approval wait therefore consumes fan-out budget and can cause later tasks
to be skipped — recorded in `result.skipped_tasks`, never fabricated. That is bounded and safe:
the turn cannot outlive its deadline. Changing it would mean reopening FRE-1397's absolute-
deadline contract, which is outside this ticket.

### D4 — no stored preference for this constraint

`allow_preference=False`, following the `attachment_cost` precedent (ADR-0101 §8b / FRE-691):
a remembered "always allow" would silently hand an unattended sub-agent a governed tool for
every future turn. The owner is asked once per turn, never once forever.

### D5 — the seam is the sub-agent principal's own layer, not `tools/executor.py`

The check goes in `_run_tool_loop`, beside the existing out-of-grant refusal — the layer that
already owns "what may this principal do". Wiring a transport into `ToolExecutionLayer`
instead would change the primary's behaviour too, which is outside this ticket, and would
revive a second, separate approval channel (`request_tool_approval`) that nothing constructs.

**This gate is additional, not exclusive.** An approved call still passes through
`ToolExecutionLayer.execute_tool`, which independently enforces the tool's own
`allowed_in_modes` / `forbidden_in_modes` (`tools/executor.py:181-195`) and then reaches its
own approval check. The sub-agent broker can never grant what the tool's base policy forbids.
That second check cannot raise a second card: the sub-agent's layer is
`get_shared_tool_execution_layer()`, which is constructed without a `transport`
(`tool_dispatch.py:56`) and has no wiring path that supplies one, so the branch it reaches is
the warn-and-proceed log. The duplicate pass is therefore a duplicate log line, not a
duplicate prompt. Worth naming, because it becomes material if a transport is ever wired.

### D6 — the broker reaches the sub-agent through a `ContextVar`

`dispatch_tool_call` deliberately takes primitives, never an `ExecutionContext`
(its module docstring states this). The established way across that boundary is an async-safe
`ContextVar`, already used for the artifact-builder resolution
(`constraint_options.py:355-415`), per-turn model selection, and the topology label. The
broker holds the real `ctx`, so `_maybe_pause_for_constraint` still receives it and the
ADR-0142 accounting is unchanged. `execute_task` sets the carrier and resets it in the same
`finally` that already resets the artifact carrier, so no broker outlives its turn.

### D7 — what this ticket does **not** change

`SUB_AGENT_DENIED_MODES` stays exactly as it is. The ticket is explicit: this removes the
*reason* that forced the ALERT/DEGRADED revocation; revisiting the revocation itself is a
later decision on the merits. The module comment is updated to say the premise is now false
and to name this ticket, without touching the constant.

## 3. Files

| File | Change |
|------|--------|
| `src/personal_agent/governance/sub_agent_tools.py` | Add `sub_agent_tool_requires_approval()`; update the FRE-1388 comment (constant unchanged) |
| `src/personal_agent/governance/__init__.py` | Export the new predicate |
| `src/personal_agent/transport/events.py` | Widen the `ConstraintName` literal with `sub_agent_tool_approval` |
| `src/personal_agent/orchestrator/constraint_options.py` | Add the `sub_agent_tool_approval` constraint and its two options |
| `src/personal_agent/orchestrator/sub_agent_approval.py` | **New** — `ApprovalOutcome`, `SubAgentApprovalBroker`, the `ContextVar` carrier, `resolve_sub_agent_approval_requirements()` |
| `src/personal_agent/orchestrator/sub_agent.py` | Resolve the approval-required subset once per call; check it before dispatch |
| `src/personal_agent/orchestrator/executor.py` | Set and reset the broker carrier in `execute_task` |
| `tests/personal_agent/orchestrator/test_sub_agent_approval.py` | **New** — AC-1 to AC-5 |
| `tests/personal_agent/governance/test_sub_agent_tools.py` | Cases for the new predicate |

## 4. Steps

Each step names its verification.

1. **Governance predicate.** Add `sub_agent_tool_requires_approval(tool_name, mode, config)`
   to `governance/sub_agent_tools.py`, mirroring `tools/executor.py:227-229` exactly
   (`policy.requires_approval or mode.value in policy.requires_approval_in_modes`; a tool
   with no policy entry requires no approval). Export it.
   *Verify:* `make test-file FILE=tests/personal_agent/governance/test_sub_agent_tools.py`
   passes, including a case for each of: no policy entry, `requires_approval: true`,
   mode in `requires_approval_in_modes`, mode outside it.

2. **The constraint name and its options.** Widen `ConstraintName`
   (`transport/events.py:52`) with `"sub_agent_tool_approval"` — the literal is closed today
   and `_maybe_pause_for_constraint` is typed against it, so omitting this would need a
   `type: ignore`, which is the exact drift FRE-881 closed for `attachment_cost`. Then add to
   `CONSTRAINT_OPTIONS`: `approve_sub_agent_tool` ("Allow for this turn") then
   `deny_sub_agent_tool` ("Deny"). The last entry is the safe default, per the module's
   stated convention. The label states the turn scope, so the card itself tells the owner
   what their answer covers.
   *Verify:* `default_action_id("sub_agent_tool_approval") == "deny_sub_agent_tool"`, and
   `make mypy` passes with no new ignore.

3. **The broker (failing test first).** Write
   `tests/personal_agent/orchestrator/test_sub_agent_approval.py` with the AC-4 test — six
   sub-agents, one prompt — and confirm it fails. Then write
   `orchestrator/sub_agent_approval.py`:
   - `ApprovalOutcome` — frozen dataclass, `approved: bool`, `reason: str`.
   - `resolve_sub_agent_approval_requirements(tools, *, trace_id) -> frozenset[str]` —
     returns empty for an empty grant without any lookup; on a governance or mode lookup
     failure returns **every** requested name (fails closed: the machinery then asks, and
     denies when there is no approver).
   - `SubAgentApprovalBroker` — holds `ctx`, a `dict[str, ApprovalOutcome]` cache and an
     `asyncio.Lock`. `decide(tool_name, *, task, worker_remaining_seconds)` returns the
     cached outcome, else denies without a card when
     `worker_remaining_seconds <= settings.constraint_pause_timeout_seconds` (D3), else
     pauses through `_maybe_pause_for_constraint` (lazy import) with `allow_preference=False`
     and `ctx=self._ctx`, caches the outcome and returns it.
   - `set_/get_/reset_sub_agent_approval_broker` — the `ContextVar` trio, in the shape
     `constraint_options.py` already uses.
   *Verify:* the AC-4 test passes and `mock_pause.call_count == 1`.

4. **The call-site check.** In `sub_agent.py`, resolve the approval-required subset once
   inside `run_sub_agent`'s `try` (beside `effective_timeout`) and pass it into
   `_run_tool_loop`. In the loop, after the out-of-grant refusal and after the JSON argument
   parse — so a malformed call is refused without troubling the owner — deny or dispatch on
   the broker's outcome. Log `sub_agent_tool_approval_granted` / `_denied` with `trace_id`
   and `session_id`.
   *Verify:* AC-1 and AC-2 tests pass; the existing
   `tests/personal_agent/orchestrator/test_sub_agent.py` is unchanged and still passes.

5. **The turn lifecycle.** In `executor.py:execute_task`, set the broker carrier beside
   `_builder_carrier_token` (line 3957) and reset it in the same `finally` (line 4272).
   *Verify:* AC-5's full-turn negative passes; `make test` is green.

6. **Gates.** `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files`.

## 5. Acceptance criteria

| AC | Criterion | Test | Observable outcome |
|----|-----------|------|--------------------|
| AC-1 | A request reaches the owner and the answer reaches the sub-agent | `test_approval_request_reaches_the_owner_surface`, `test_approve_lets_the_tool_run`, `test_deny_stops_the_tool` | A `CONSTRAINT_PAUSE` is pushed for `sub_agent_tool_approval` (not merely logged). On approve, `dispatch_tool_call` runs and `result.tools_used == ["run_python"]`. On deny, `dispatch_tool_call` is never called and `result.tools_used == []` |
| AC-2 | A denial is a refusal, not an error | `test_denial_is_a_refusal_not_an_exception`, `test_pause_failure_becomes_a_denial` | The denied round appends a tool message and the loop continues to a final answer: `result.success is True`, `result.error is None`, nothing raised. An exception raised inside the pause is converted to a denial rather than propagating |
| AC-3 | No answer is not an indefinite wait, and the wait sits inside the worker's budget | `test_timeout_denies_and_the_worker_survives`, `test_pause_is_skipped_when_the_worker_budget_is_too_small` | On timeout the pause resolves to `deny_sub_agent_tool` and the sub-agent still returns a result. With a worker budget below the ceiling plus the finalization reserve, no card is raised at all and the call is denied |
| AC-4 | The fan-out answer is stated and tested at fan-out scale | `test_six_sub_agents_produce_one_prompt` | Six sub-agents in one turn each wanting `run_python`: `_maybe_pause_for_constraint` is called exactly **once**, and all six honour that one answer |
| AC-5 | The seeded negative | `test_no_approval_needed_raises_no_prompt`, `test_grantless_sub_agent_never_looks_up_governance` | A sub-agent whose granted tool needs no approval in the current mode completes a full turn with zero pauses; a grant-less sub-agent performs no governance lookup at all |

## 6. Risk

- **Diff class.** Escalate. The change sits in the turn path and touches governance code.
- **Blast radius.** Inert under today's production config (§1 fact 4). The only behaviour
  change reachable today is on a seeded/expanded grant.
- **The failure mode to watch.** A pause that fires per call instead of per turn. AC-4 is the
  guard, and the `ContextVar` reset in `execute_task`'s `finally` is what keeps a stale
  answer from crossing into the next turn.
