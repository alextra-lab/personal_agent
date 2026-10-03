# FRE-1539 — A harness turn with a denied approval tool is invalid

Ticket: FRE-1539 (Approved 2026-10-03). Backing design: ADR-0063 Amendment A, FRE-1535.
Tier: Standard (multi-file behaviour in `scripts/eval/`; no `src/` change, no schema, no cost code).

## Problem

FRE-1535 made tool approval fail closed. A harness turn on the production gateway has no PWA
WebSocket, so every `requires_approval` tool call is denied. The model continues without the tool.
The turn completes and looks valid. No harness notices.

## Design

### The evidence source

The denial must not rest on an absent Elasticsearch event (FRE-1051: `agent-logs` loses events).

| Source | What it records | Use |
|---|---|---|
| Captain's Log capture (`TaskCapture.tool_results`, one document per turn) | The denied `ToolResult` as `success: false`, `error: "Permission denied: approval_<reason>"`. The executor appends it to `ctx.tool_results` for every dispatched call. | Primary. A found capture with no denial proves the primary turn had none. |
| `agent-logs` events `approval_denied`, `approval_denied_no_session_id`, `approval_denied_no_transport`, `sub_agent_tool_approval_denied` | The denial, including the sub-agent path. | Positive evidence only. An absent event proves nothing. |

Sub-agent gap (codex plan review, finding 1): `SubAgentCapture` has no per-call denial field. A
sub-agent denial reaches only the `sub_agent_tool_approval_denied` log event. The primary capture
cannot rule it out. `SubAgentCapture.rounds[].tool` does record the name of every attempted call,
including a denied one (`_absorb` in `sub_agent.py`). So durable evidence can show that no sub-agent
tried an approval-capable tool. It cannot show the outcome of one that did.

The approval-capable set is read from `config/governance/tools.yaml`: every tool with
`requires_approval: true` or a non-empty `requires_approval_in_modes`. The set is a superset on
purpose. A mode change at the gateway then cannot hide a tool.

Known residual limits, stated in the PR and the handoff:

- The set comes from the worktree copy of `tools.yaml`, not the deployed copy.
- A lost sub-agent capture document is caught only when `sub_agent_start` log events outnumber the
  sub-agent capture documents. Lost log events can hide that gap.

### Three outcomes, not two

| Verdict | Condition |
|---|---|
| `invalid` | Any denial from either source. |
| `valid` | A capture document was read, no denial was found, and no sub-agent round used an approval-capable tool, and the sub-agent capture documents are at least as many as the `sub_agent_start` events. |
| `unverified` | No capture document after the wait, or a sub-agent used an approval-capable tool with no logged denial, or a sub-agent capture document is missing. The check cannot prove the turn clean. `unverified_reason` names which. |

`unverified` is excluded from rates and counted on its own line. This is the FRE-1051 rule applied
to the new check: an absence is not a pass.

The capture query must exclude the sub-agent index. The pattern `agent-captains-captures-*` also
matches `agent-captains-captures-subagents-*`, and a sub-agent document would fake a found capture.
The same exclusion is in `read_session_captures` (`captains_log/capture.py`).

### Shared module

New file `scripts/eval/approval_denial.py`. No import from `personal_agent`.

```python
DENIAL_ERROR_PREFIX = "Permission denied: "          # tools/executor.py ToolResult.error
APPROVAL_REASON_PREFIX = "approval_"                 # PermissionResult.reason values
DENIAL_LOG_EVENTS = frozenset({...4 names above...})

@dataclass(frozen=True)
class ApprovalDenial:   tool_name: str; reason: str; source: Literal["capture", "log"]
@dataclass(frozen=True)
class ApprovalVerdict:  validity: Validity; denials: tuple[ApprovalDenial, ...]; capture_found: bool; error: str | None

def denials_from_tool_results(tool_results: Sequence[Mapping[str, object]]) -> tuple[ApprovalDenial, ...]
def denials_from_events(events: Sequence[Mapping[str, object]]) -> tuple[ApprovalDenial, ...]
def approval_capable_tools(tools_yaml: Path = DEFAULT_TOOLS_YAML) -> frozenset[str]
def assess(capture_docs, events, sub_agent_docs=(), approval_capable=frozenset()) -> ApprovalVerdict
def merge(verdicts: Sequence[ApprovalVerdict]) -> ApprovalVerdict     # worst of: invalid > unverified > valid
def check_turn(trace_id, *, es_url, logs_index="agent-logs-*", captures_index=CAPTURES_INDEX,
               wait_s=60.0, poll_s=3.0, client: httpx.Client | None = None) -> ApprovalVerdict
```

Reason normalisation (so one denial seen in both sources collapses to one entry):

| Source field | Normalised reason |
|---|---|
| capture error `Permission denied: approval_connection_lost` | `approval_connection_lost` |
| capture error `Permission denied: approval_no_transport` | `approval_no_transport` |
| log `approval_denied` with `decision=connection_lost` | `approval_connection_lost` |
| log `approval_denied_no_transport` | `approval_no_transport` |
| log `approval_denied_no_session_id` | `approval_no_session_id` |
| log `sub_agent_tool_approval_denied` with `reason=R` | `sub_agent_R` |

Not a denial: `approval_ui_disabled_proceeding` (the eval opt-out; the tool ran), a capture error that
does not start with `Permission denied: approval_` (for example `Tool not allowed in mode`).

`check_turn` is synchronous. Async harnesses call `await asyncio.to_thread(check_turn, ...)`.
It polls the capture index until a document appears or `wait_s` ends, then reads the events once.
An `httpx.HTTPError` yields `unverified` with `error` set. It never raises.

### Harness wiring

| Harness | Decision |
|---|---|
| `fre1517/prod_session_runner.py` | Wire. Per turn: `check_turn`, pass the verdict to `turn_outcome`, add `approval` to the row, print `validity`. End of session: `session_summary(rows)` prints invalid and unverified counts and the delivered rate over valid turns only. Exit 11 when any turn is invalid. |
| `fre1517/session_runner.py` | `turn_outcome` gains optional `approval`. With it, the outcome adds `validity` and `approval_denials`. Without it, the outcome is unchanged (existing exact-dict tests stay green). Eval-stack callers do not pass it. |
| `fre453_canonical_evalset/harness.py` | Wire. `run_case` checks the setup traces and the scored trace, and merges. `render_markdown` prints a validity count line, and prints an invalid or unverified case as a notice with no MATCH/MISMATCH table. `amain` returns 1 when any case is invalid (the existing instrument-health code). |
| `fre433_cache_ab/harness.py` | Wire. `TurnMetric` gains `validity` and `approval_denials`. The cross-turn reuse rate excludes turns that are not `valid` or `unassessed`. The report prints the invalid count. `reextract` is not changed (old traces) and leaves `validity="unassessed"`. |
| `fre475_compression_ab/run_ab.py` | Wire. `extract` calls `check_turn`. When the turn is not `valid`, the reduction percentage prints `n/a` and the verdict line prints `INVALID` or `UNVERIFIED` in place of PASS or FAIL (codex finding 3). |
| `fre481_decomposition_ab/harness.py` | Wire (codex finding 2). The arm flag `artifact_decomposition_enabled` is gone, but `--arm` is only a metadata tag. The script still POSTs to `:9001/chat` and writes comparative reports. It is not dead. |
| `fre1337_intent_probe/substrate.py` | Leave. `assert_eval_chat_url` allows only the eval gateways `:9002` and `:9003`. The `9001` in the file is the container port. |
| `gateway_freshness.py` | Leave. It sends `GET /health` only. It sends no turn. |
| `recovery_harness.py` | Leave. Default is the dev service `:9000`. No change since 2026-05-28. A user can pass `--chat-url` to reach `:9001`. The PR says so. |
| `fre1517/planner_probe/phase_b_report.py`, `blind_export.py` | Leave. Hard-wired to three finished runs. Their rows predate the check. |

## Tasks

Run every pytest command alone. Do not start a second pytest process.

### T1 — Shared check, pure part (TDD)

Files: `tests/evaluation/test_approval_denial.py` (new), `scripts/eval/approval_denial.py` (new).

1. Write tests. Expected: they fail with `ModuleNotFoundError`.
   - AC-1: capture with `bash` denied `Permission denied: approval_connection_lost` → `invalid`, one denial `(bash, approval_connection_lost, capture)`.
   - AC-1: capture with `write` denied `Permission denied: approval_no_transport` → `invalid`, reason `approval_no_transport`.
   - AC-2: capture with no tool results → `valid`.
   - AC-2: capture with `bash` success plus log event `approval_ui_disabled_proceeding` → `valid`.
   - Capture with a non-approval permission error → `valid`.
   - Log events only, one `approval_denied_no_session_id` → `invalid`, even with no capture.
   - Same denial in capture and log → one entry.
   - Sub-agent log event → reason `sub_agent_<reason>`.
   - No capture and no event → `unverified`, `capture_found` false.
   - `merge`: invalid beats unverified beats valid.
   - Sub-agent doc whose rounds used `bash`, no logged denial → `unverified`, reason `sub_agent_approval_capable_tool`.
   - Same, plus a `sub_agent_tool_approval_denied` log event → `invalid`.
   - Sub-agent doc whose rounds used only `read` → `valid`.
   - Two `sub_agent_start` events, one sub-agent doc → `unverified`, reason `sub_agent_capture_missing`.
   - `approval_capable_tools` over the real `config/governance/tools.yaml` contains `bash`, `write` and `run_python`, and does not contain `web_search`.
2. Implement the module pure functions.
3. Run `make test-file FILE=tests/evaluation/test_approval_denial.py`. Expected: all pass.

### T2 — Shared check, ES part (TDD)

Same files. Use `httpx.MockTransport`.

1. Tests, expected to fail first:
   - The capture request goes to an index string that contains `-agent-captains-captures-subagents-*`.
   - Capture found on the second poll: the function returns after two requests.
   - Capture never found: returns `unverified` after `wait_s` (use `wait_s=0`).
   - The transport raises `httpx.ConnectError`: returns `unverified`, `error` set, no raise.
   - End to end with `connection_lost` in the mocked capture: `invalid`.
2. Implement `check_turn`. Use `time.sleep` for the poll.
3. Run the test file. Expected: pass.

### T3 — `turn_outcome` and `prod_session_runner` (TDD)

Files: `tests/evaluation/test_fre1517_session_runner.py`, `scripts/eval/fre1517/session_runner.py`, `scripts/eval/fre1517/prod_session_runner.py`.

1. Tests, expected to fail first:
   - `turn_outcome` without `approval` returns the existing four keys only.
   - `turn_outcome` with an invalid verdict adds `validity="invalid"` and the denials; `delivered` is unchanged.
   - AC-3: `session_summary` over five rows (three valid, one invalid, one unverified) reports `invalid=1`, `unverified=1`, and a delivered rate computed over the three valid rows only. A denied turn that was `delivered` does not enter the rate.
   - `session_summary` over zero valid rows prints `n/a`.
2. Implement. Wire `check_turn` after `_trace_reads` in `run`. Print the summary after the loop. Return 11 if any turn is invalid.
3. Run the file. Expected: pass.

### T4 — fre453 (TDD)

Files: `tests/evaluation/test_fre453_canonical_evalset.py`, `scripts/eval/fre453_canonical_evalset/harness.py`.

1. Tests, expected to fail first:
   - AC-3: `render_markdown` with one valid and one invalid result prints the invalid count, prints the notice for the invalid case, and prints no MATCH/MISMATCH table for it.
   - `render_markdown` with results that have no `approval` key renders as before.
2. Implement. Add `approval` to the `run_case` result and to `_result_to_json`.
3. Run the file. Expected: pass.

### T5 — fre433, fre475 and fre481 (TDD)

Files: `tests/evaluation/test_harness_approval_validity.py` (new), `scripts/eval/fre433_cache_ab/harness.py`, `scripts/eval/fre475_compression_ab/run_ab.py`, `scripts/eval/fre481_decomposition_ab/harness.py`.

1. Tests, expected to fail first:
   - fre433 `render_markdown`: an invalid turn is not in the reuse rate denominator, and the report prints the invalid count.
   - fre475: a pure helper `verdict_lines(total_fresh, artifact, failed, validity)` returns reduction `n/a` and `INVALID` for an invalid turn even when the numbers pass.
   - fre481: its report renderer excludes an invalid turn from every aggregate and prints the invalid count (read the harness first, then write the exact assertion).
2. Implement.
3. Run the file. Expected: pass.

### T6 — Gates

- `make test` (once, alone) · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
- Commit. Then `feature-dev:code-reviewer` on `git diff origin/main...HEAD`. Fix confirmed findings.
- `security-review`: the diff adds no input, subprocess or file code, so it is not required. State this.

## Acceptance criteria

| AC | Proof |
|---|---|
| AC-1 | `test_approval_denial.py`: `connection_lost` and `no_transport` cases, tool name and reason asserted |
| AC-2 | `test_approval_denial.py`: no-tool turn and `approval_ui_disabled_proceeding` turn are `valid` |
| AC-3 | `test_fre1517_session_runner.py` (summary) and `test_fre453_canonical_evalset.py` (report) |
| AC-4 | Master. Handoff gives the command: `uv run python -m scripts.eval.approval_denial <trace_id> --es-url http://localhost:9200` |

AC-4 needs a CLI entry. Add `main()` to the module: it prints the verdict as JSON and exits 0 for
`valid`, 1 for `invalid`, 2 for `unverified`. One small function, tested.

## Out of scope

- No bypass flag on production (owner decision, FRE-1535).
- No change to the three finished fre1517 report scripts.
- No stop-on-first-invalid policy in the runner. The runner marks, counts and exits 11. Master decides
  if a session must stop at the first invalid turn.
