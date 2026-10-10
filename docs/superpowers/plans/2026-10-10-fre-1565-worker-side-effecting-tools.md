# FRE-1565 — Workers get the side-effecting tools, under per-call approval, each in its own workspace

**Ticket:** FRE-1565 (option A, step 3). **Backing ADRs:** ADR-0150 D2 (worker registry, amended
2026-10-10 by FRE-1564), ADR-0063 Amendment A (A2, A4), FRE-1461 broker, FRE-1388 grant record.

## Owner decisions (2026-10-10, this session)

1. **A new `operator` worker type** holds the side-effecting tools. `researcher` and `general` do not
   change.
2. **Approval is per call, with the exact arguments on the card.** An identical call in the same turn
   runs once. A later identical request receives the first call's result, or its refusal. Arguments
   over 4,000 characters are refused without a card.
3. **A worker's `bash` runs as `nobody` with an allowlisted environment** (the FRE-1505 `setpriv`
   drop). Its working directory and `HOME` are its own workspace. It fails closed when the gateway
   is not root.

## Facts found while scoping

- The ticket names `write_file`. No tool of that name is registered. The registered file writer is
  the FRE-261 primitive `write`, and its `allowed_paths` include `/opt/seshat/**` and `/app/**`.
- `bash` runs in the gateway container as root with the gateway's full environment. One call can
  read a credential (`env`, `/proc/1/environ`) and send it out (`curl`). So `bash` is a private read
  and an outbound channel in one tool.
- `bash` has `requires_approval: true`, so FRE-1461's resolver already gates it. The layer's
  `auto_approve_prefixes` shortcut does not bypass a worker: the broker asks before dispatch.
- Workers of one fan-out run one after another (`expansion_controller.py:1663`, an awaited loop).
  The coalescing code is still safe under concurrency (a per-key future).
- With no PWA client, a pause waits `constraint_pause_timeout_seconds` (180 s) and then denies
  (FRE-928). Per call, that wait repeats for every call. See step 3, "unreachable owner".

## Tool list per type (the ADR-0150 D2 amendment)

| Type | Tools | Approval |
|---|---|---|
| `researcher` | `web_search`, `fetch_url`, `get_library_docs` | unchanged (policy) |
| `general` | `run_python`, `search_memory`, `recall_personal_history`, `query_telemetry`, `notes_search`, `read_skill` | unchanged (policy) |
| `operator` (new) | `bash`, `write`, `run_python`, `create_linear_issue`, `create_linear_project`, `notes_write`, `artifact_write` | `bash`, both Linear writes, `notes_write`, `artifact_write`: **per call**. `write` and `run_python`: policy |

Refused, with a recorded reason: `read` (it reads `/opt/seshat/**` and `/app/**` with no approval, a
private read on a type with outbound tools), `artifact_draft` (it spawns its own sub-agent).

## The exfiltration path, part by part, for `operator`

| Part | Control |
|---|---|
| Untrusted input | The task text only. `operator` holds an outbound tool, so `carries_conversation_context` gives it no conversation messages (FRE-1564 rule, unchanged code). It holds no memory, notes or telemetry read. |
| Private read | `bash` is the only private read. It runs as `nobody` with an allowlisted environment, so it cannot read the gateway's credentials. Every `bash` call is approved per call. |
| Way out | `bash` (`curl`) and the two Linear writes. Each call is approved per call, and the card shows the full arguments. |
| Durable poisoning | `notes_write` and `artifact_write` are approved per call. `write` is confined to the worker's own workspace. |

## Steps

### Step 1 — Governance model: a per-call approval field

- `src/personal_agent/governance/models.py`: `SubAgentToolDecision.approval: Literal["policy",
  "per_call"] = "policy"`. A validator refuses `per_call` on a refused decision.
- `src/personal_agent/governance/sub_agent_tools.py`:
  - `sub_agent_tool_asks_per_call(tool_name, config) -> bool`.
  - `sub_agent_tool_requires_approval` returns `True` for a per-call tool in every mode.
- Test: `tests/personal_agent/governance/test_sub_agent_tools.py` (extend) — per-call is true in
  NORMAL for a tool whose own policy needs no approval. A refused decision with `per_call` fails
  validation.

### Step 2 — Worker workspace

- New `src/personal_agent/tools/primitives/worker_workspace.py`:
  - A `ContextVar[Path | None]` with `set_worker_workspace`, `get_worker_workspace`,
    `reset_worker_workspace`.
  - `worker_workspace_path(trace_id, task_id) -> Path` = `settings.worker_workspace_root /
    trace_id / task_id`.
  - `ensure_worker_workspace() -> Path | None`: creates the directory on first use. As root, it
    gives the leaf directory to uid/gid 65534 so the `nobody` shell can write in it.
- `src/personal_agent/config/settings.py`: `worker_workspace_root: str =
  "/app/agent_workspace/workers"` (`AGENT_WORKER_WORKSPACE_ROOT`). It is under the durable
  `/app/agent_workspace` volume and inside `write`'s `allowed_paths`.
- `run_sub_agent` (`orchestrator/sub_agent.py`) sets the workspace path for every worker before
  its tool loop and resets it in its `finally`. The directory exists only when a tool uses it.
- `write` (`tools/primitives/write.py`): with a worker workspace set, a relative path resolves inside
  it. An absolute path or a `..` path that resolves outside it is refused (`outside_worker_workspace`).
  The existing path governance and durability checks then run. As root, a written file goes to
  65534 so the worker's `bash` can change it.
- `bash` (`tools/primitives/bash.py`): with a worker workspace set, the child runs through the
  existing `setpriv` prefix, with `eval_child_env()` plus `HOME`/`TMPDIR` = the workspace, and
  `cwd` = the workspace. A non-root gateway refuses (`credential_isolation_unavailable`). The primary
  (no workspace set) is unchanged.
- Tests (AC-4): `tests/personal_agent/orchestrator/test_worker_side_effects.py`
  - two `run_sub_agent` workers write `out.txt` through the real `write_executor`: two files, two
    directories, each with its own content.
  - an absolute path outside the workspace and a `../` path are refused.
  - `bash` with a workspace set spawns with `setpriv`, the allowlisted environment and `cwd`
    (spawn mocked, as the FRE-1505 tests do). Non-root refuses. The primary path is unchanged.

### Step 3 — The broker: per-call approval and the side-effect ledger

`src/personal_agent/orchestrator/sub_agent_approval.py`:

- `resolve_sub_agent_per_call_tools(granted_tools, *, trace_id) -> frozenset[str]`. It fails
  closed: a lookup failure returns every granted name.
- `SubAgentApprovalBroker.run_once(tool_name, arguments, *, worker_type, task,
  worker_remaining_seconds, execute) -> SideEffectOutcome`:
  - Key = `(tool_name, canonical JSON of arguments)` (sorted keys, compact separators).
  - **Ledger.** The first request for a key creates a future. A later identical request awaits it and
    returns `coalesced=True` with the same content. No second card, no second execution. A refusal
    is recorded the same way.
  - **Card.** Under the broker's lock (one card at a time): "A operator worker wants to run
    `bash` with these exact arguments: {json}. Task: {task[:120]}. Allowing covers this one call
    only." `allow_preference=False`.
  - **Too long.** Canonical arguments over 4,000 characters → refusal `arguments_too_long_for_card`,
    no card.
  - **Unreachable owner.** After a per-call ask resolves with no answer (`timeout_default`,
    `connection_lost`, `user_cancel`) or the pause fails, the broker refuses every later per-call
    ask in the turn at once (`owner_did_not_answer_this_turn`). Without this, a socket-less turn
    waits 180 s per call.
  - The worker-budget floor of `decide` applies unchanged.
  - The future always resolves. A cancelled or failed `execute` resolves it with a refusal content,
    then re-raises.
- `decide` (per tool per turn) does not change.

`src/personal_agent/orchestrator/sub_agent.py` tool loop: a tool in the per-call set goes through
`broker.run_once`, with `execute` = `dispatch_tool_call(..., approved_upstream=True)`. No broker →
refusal (`no_approver_in_scope`). A coalesced result is absorbed as "A sibling worker already made
this identical call in this turn. Its result: …". Other tools keep the FRE-1461 path.

Tests (same new file):
- **AC-1** — a worker `bash` call drives the REAL `_maybe_pause_for_constraint` with the transport
  push faked: the card carries the exact command. Approve → dispatched once with
  `approved_upstream=True`. Deny → not dispatched, the worker reaches its final answer, `success`.
- **AC-2** — six workers (FRE-1461 AC-4 shape), each with two distinct `bash` calls → 12 cards, 12
  dispatches. Contrast: six workers with `run_python` (policy) → 1 card.
- **AC-5** — two workers request the same `create_linear_issue` → one dispatch. The second worker's
  tool message names the sibling and carries the result. A concurrent variant (`asyncio.gather`)
  gives the same count. Seeded negative: different titles → two cards.
- **AC-6** — no broker in scope → refused, not dispatched. A pause resolving `connection_lost` →
  refused, and a second per-call ask in that turn raises no card.
- Too long → no card, refused. Cancellation still propagates.

### Step 4 — The `operator` type and the grants

- `src/personal_agent/orchestrator/worker_types.py`: `WorkerType.OPERATOR`, its spec (description,
  prompt block, tools above, `report_schema=None`, `default_thoroughness="quick"`).
  `OUTBOUND_TOOLS` gains `bash`, `create_linear_issue`, `create_linear_project`.
- `config/governance/tools.yaml` `sub_agent_tools`: entries for `bash`, `write`,
  `create_linear_issue`, `create_linear_project`, `notes_write`, `artifact_write` (granted, with
  reasons, `approval: per_call` where the table says), and refusals for `read`, `artifact_draft`.
  The header comment notes the change. `run_python`'s reason notes that `operator` holds it too.
- **AC-3** — `tests/personal_agent/orchestrator/test_worker_tool_split.py` (rewritten rule):
  - Classes: OUTBOUND (`web_search`, `fetch_url`, `get_library_docs`, `bash`, both Linear writes),
    PRIVATE (`search_memory`, `recall_personal_history`, `notes_search`, `query_telemetry`, `bash`),
    DURABLE_WRITE (`notes_write`, `artifact_write`), NEUTRAL (`run_python`, `read_skill`, `write`).
  - The rule: for each type, if it holds a PRIVATE tool and an OUTBOUND tool, every OUTBOUND tool it
    holds is `per_call` in the real config and resolves as approval-required in NORMAL. Every
    DURABLE_WRITE tool held is `per_call`.
  - Seeded negatives: `bash` set to `policy` in a copied config fails the rule. `web_search` added
    to `operator` fails the rule. The old two seeded negatives stay.
  - The existing "no side-effecting tool" test becomes: side-effecting tools reach only `operator`.
- Planner prompt: rendered from `WORKER_TYPES`, so the new line appears with no code change. A test
  checks the `operator` line names its tools and `researcher`/`general` lines do not.

### Step 5 — Documents

- `docs/architecture_decisions/ADR-0150-...md`: a dated section "2026-10-10 — Amendment to D2:
  side-effecting tools for an `operator` worker (FRE-1565)": owner decisions, the table, the
  exfiltration table, the approval scope with its justification, the workspace, the ledger, costs.
- `docs/architecture_decisions/ADR-0063-...md`: a dated status line. Row A4's "once per tool per
  turn" holds for policy tools. A `per_call` tool asks per call (ADR-0150 D2, FRE-1565).
- Module docstrings of `sub_agent_approval.py`, `sub_agent_tools.py`, `worker_types.py`.

### Step 6 — Gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
Then the code reviewer and `security-review` on `git diff origin/main...HEAD`.
Diff class: **escalated** (governance and approval code).

## Costs and risks (recorded, not fixed here)

- More cards: one per side-effecting call. The planner sends side effects to one `operator`, and
  identical calls coalesce.
- A worker's deadline does not stop during a card. Each per-call ask needs 185 s of worker budget
  left, or it is refused without a card (FRE-1461 floor). A long owner wait spends worker budget.
- Coalescing covers identical arguments only. Two near-duplicate Linear issues raise two cards; the
  owner sees both.
- Workspaces are not cleaned up. They are small and under the durable volume.
- The planner request changes (one more type line), so ADR-0154 D7 applies.
- `nobody` can read world-readable files in the container. The per-call card is the control there.

## Post-deploy (AC-7, master)

One owner PWA turn that needs a shell command from a worker, for example "Use an operator worker to
run `uname -a` and report it." Expected: a card naming `operator`, `bash` and `{"command": "uname -a"}`
reaches the PWA. Approve → the worker reports the output. Logs: `sub_agent_side_effect_decided`
`approved=true`, then `bash_started` with `credential_isolation=true`.

## Codex plan review (2026-10-10) — disposition

| Finding | Severity | Disposition |
|---|---|---|
| A relative `write` path fails `_validate_tool_arguments` before `write` can move it into the workspace (`executor.py:157`) | High | **Fixed in the plan.** `dispatch_tool_call` (sub-agent principal) turns a `write` path into the absolute workspace path, or refuses it, before `execute_tool`. `write_executor` checks again. One function in `worker_workspace.py` does both. |
| A follower that awaits the ledger future directly can cancel it | High | **Fixed.** Followers `await asyncio.shield(future)`. Only the leader resolves it. |
| A leader cancelled during the lock wait or the card leaves the future open | High | **Fixed.** The whole leader path (lock, card, execution) is in one `try/finally` that resolves the future on every exit. A cancelled leader resolves it with a refusal, then re-raises. |
| The worker budget is read before the card lock and goes stale | Medium | **Fixed.** `run_once` takes the absolute worker deadline and reads the remaining time after it takes the lock. |
| The card shows arguments before the sub-agent clamp | Medium | **Fixed.** The loop applies `clamp_sub_agent_tool_params` first. The card, the key and the dispatch use the clamped arguments. (No per-call tool has a clamp today.) |
| AC-2's `run_python` contrast cannot ask in NORMAL | High | **Fixed.** The contrast patches the resolver, as the FRE-1461 tests do, with a policy tool that needs approval. |
| `bash` children can outlive the call | Medium | **Fixed for workers.** A worker's `bash` starts a new session, and its process group is killed when the call ends or times out. The primary is unchanged. |
| More cancellation tests; one approve/deny test per per-call tool | Medium | **Added.** Leader cancellation during the card, follower cancellation, and a test parameterized over every `per_call` grant. |
| `write` in NORMAL is not approved | High | **Kept: owner decision 2** ("`write` inside the worker's own workspace asks only where its policy asks"). Recorded in the ADR. |
| The workspace is a working directory, not a security boundary: every worker shell is uid 65534, and `bash` can reach a sibling's directory | High | **Kept and stated.** Every `bash` call is approved per call with its exact command. The workspace stops workers from overwriting each other by accident (F9, AC-4). The ADR says it is not a boundary. |
| A symlink swap between the `write` check and the write | High | **Kept, residual.** It needs an approved `bash` call that leaves a process behind, and the process-group kill above removes that process. Recorded in the ADR. |
| A detached task keeps a copy of the workspace ContextVar | Low | **Kept.** No tool spawns one. A test checks the variable is `None` after `run_sub_agent`, so the primary never sees a workspace. |
| A real container and uvloop test for isolation | Medium | **Post-deploy.** CI runs unprivileged. The `setpriv` drop is the FRE-1505 code path, proven by `scripts/eval/probe_bash_credential_isolation.py`. `cwd` under uvloop was probed in this session. |
| Move per-call approval into `ToolExecutionLayer`; run the operator shell in a sandbox container | Medium | **Declined.** The first is a refactor of the FRE-1461 boundary, not this ticket. The second removes what `bash` is for: the system's own state (`docker ps`, logs, `psql`). |
