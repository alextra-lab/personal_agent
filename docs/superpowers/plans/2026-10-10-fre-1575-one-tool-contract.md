# FRE-1575 — ADR-0156 T3: one tool contract

**Ticket:** FRE-1575 (umbrella FRE-1573). **ADR:** ADR-0156 D6, D5.6, D7.5. **Tier:** Complex (codex plan review).

## Goal

Every registered tool and every dispatch failure path gives the model one shape:

- success: `{"ok": true, ...payload}`
- failure: `{"ok": false, "error": {"code", "message", "retryable", "next_action"}}`

A failure is built only from the tool's typed error-code set. Telemetry logs a failure as a failure. Every result has a size bound. Every description follows S5. Eight skills fold into tool descriptions. The three MCP copies go.

## Design

### 1. The contract module — `src/personal_agent/tools/contract.py` (new)

- `ToolErrorDetail` — frozen Pydantic model: `code: str`, `message: str` (bounded at 2,000 characters, cut with a `[truncated]` mark), `retryable: bool`, `next_action: str`.
- `ToolError(ToolExecutionError, Generic[CodeT])`, `CodeT = TypeVar(bound=str)`. Constructor: `(code: CodeT, message: str, *, retryable: bool, next_action: str)`. Property `detail -> ToolErrorDetail`.
- Each tool module declares `XCode = Literal[...]` and `class XError(ToolError[XCode])`. mypy rejects an undeclared code at the call site (probe confirmed: `Argument 1 to "ReadError" has incompatible type "Literal['bogus']"`). Literals nest, so a shared helper code set (for example `PathCode`) is part of `ReadCode = Literal[PathCode, "not_a_file", ...]`.
- `error_codes(cls) -> frozenset[str]` reads the `Literal` from `__orig_bases__`. Each `ToolDefinition` sets `error_codes=error_codes(XError)`.
- `DispatchCode = Literal[...]` for failures that the layer builds, not a tool: `unknown_tool`, `missing_parameter`, `invalid_arguments`, `mode_refused`, `path_refused`, `permission_denied`, `permission_rules_unavailable`, `approval_unavailable`, `denied_by_user`, `approval_timed_out`, `timeout`, `tool_exception`, `undeclared_error_code`, `contract_violation`, `loop_blocked`, `not_available_to_worker`, `invalid_json_arguments`. `dispatch_error(code: DispatchCode, ...) -> ToolErrorDetail`.
- `legacy_failure_marker(output) -> str | None` — returns a reason when a success payload looks like an old error dict: `success is False`, `status == "error"`, a top-level `error` key, or an `ok` key. The layer turns such a payload into a `contract_violation` failure (seeded negative of AC-1).
- `render_content(tool_def | None, result) -> str` — the one place that builds the model-facing JSON. Success: `{"ok": true, **output}` (a `str` output becomes `{"ok": true, "result": output}`). Failure: `{"ok": false, "error": detail}`. Then the size bound (item 4).

### 2. Type changes — `tools/types.py`

- `ToolDefinition`: `error_codes: frozenset[str] = frozenset()`, `max_result_tokens: int = Field(25_000, ge=1, le=25_000)`, `truncation_next_action: str` (default: "Narrow the request and call again.").
- `ToolResult`: `error_detail: ToolErrorDetail | None = None`. `error: str | None` stays (the durable record), set to `detail.message`.

### 3. The execution layer — `tools/executor.py`

- Unknown tool → `unknown_tool`, `next_action` lists the available tools.
- Mode refusal → `mode_refused`. Path rule refusal in `_validate_tool_arguments` → `path_refused`. `PermissionResult.error` becomes a `ToolErrorDetail` (codes stay as T2 set them).
- Schema validation → `invalid_arguments`, retryable, all errors (bounded).
- Executor raises `ToolError` → code must be in `tool_def.error_codes`, else `undeclared_error_code` with a `tool_contract_violation` log. `TerminalToolError` becomes a `ToolError` subclass and keeps its `terminal` metadata.
- Executor raises `TimeoutError` / `asyncio.TimeoutError` / `httpx.TimeoutException` → `timeout`, retryable. No new layer-level timeout is enforced: the declared `timeout_seconds` values are not enforced today, and `bash`/`run_python`/`artifact_draft` run longer by design. Out of scope.
- Any other exception → `tool_exception`, message `"<Type>: <text>"`.
- Success payload with a legacy marker → `contract_violation`.
- **D6.3:** every executed call logs `tool_call_completed` once, with `success` equal to `ok` and `error_code` on a failure. `tool_call_failed` stays on failures (the reflection pass reads it).

### 4. Size bounds (D6.4)

- `render_content` counts tokens with `llm_client.token_counter.estimate_tokens`. Over `max_result_tokens` (25,000 ceiling when the tool is unknown) → `{"ok": true, "truncated": true, "result_tokens": n, "max_result_tokens": b, "next_action": tool_def.truncation_next_action, "partial": <prefix of the JSON>}`, shrunk until under the bound.
- Per-tool bounds sit above each tool's own truncation, so the layer cut is a backstop: `bash` and `run_python` 16,000 (50 KiB cap); `read` 25,000 (`max_bytes` can be raised); `query_telemetry` 8,000 (20,000-character cap); others set from their own caps when converted.

### 5. Dispatch — `orchestrator/tool_dispatch.py`

- All content via `render_content`. Remove the 150-character clip and the `mark_truncated` import.
- Missing parameter → `missing_parameter`. `read_skill` dedup → `{"ok": true, "skill_name": …, "body": "<already loaded>"}`. Exception path → `tool_exception`.
- The hybrid hint goes into the JSON as a `skill_hint` key, so content stays valid JSON (T5 deletes the hint loop).
- `orchestrator/executor.py`: `_gate_blocked_result` → `loop_blocked` (with the hint as `next_action`); the unexpected-exception branch → `tool_exception`.
- `orchestrator/sub_agent.py`: tool not in the worker spec → `not_available_to_worker`; invalid JSON arguments → `invalid_json_arguments`.
- `orchestrator/tool_result_digest.py:_digest_bash`: `success` → `ok`.

### 6. Each tool (24)

Each module: `XCode`, `XError`, every `raise ToolExecutionError(...)` and every returned error dict becomes `raise XError(code, message, retryable=…, next_action=…)`. Success payloads lose `success`/`status` keys.

| Module | Tools | Notes |
|---|---|---|
| `primitives/_governance.py` | (helper) | Returns a `PathRefusal(code: PathCode, message)` instead of a dict |
| `primitives/bash.py` | `bash` | Non-zero exit stays `ok: true` with `exit_code` (the tool ran the command; `grep` exits 1 on no match). Guard failures (`empty_command`, `timeout`, `os_error`, `credential_isolation_unavailable`) raise |
| `primitives/read.py`, `write.py` | `read`, `write` | Path refusals, `not_a_file`, `permission_denied`, `io_error`, `invalid_mode`, `not_durable` raise |
| `primitives/run_python.py` | `run_python` | `sandbox_unavailable`, `timeout` (retryable, partial output tail in message), `out_of_memory` raise. Non-zero exit stays `ok: true` |
| `telemetry_query.py` | `query_telemetry` | `_refuse` returns `TelemetryQueryError("refused", …)`; field refusal keeps the full allowed list (AC-2). Store errors get `store_unavailable` / `store_timeout` / `store_rejected` / `bad_reply` |
| `read_skill.py` | `read_skill` | Unknown/empty name raise `unknown_skill` |
| `tool_result_expand.py` | `expand_tool_result` | Raise on store/fetch/hash failures |
| `fetch.py`, `web.py`, `memory_search.py`, `personal_history.py`, `location.py`, `context7.py`, `perplexity.py`, `linear.py` (4 tools), `notes_tools.py` (2), `artifact_tools.py` (4) | | Typed raises. `location` unresolved results (`resolved: false, reason`) stay success data: they are answers, not failures |
| `mcp/client.py`, `mcp/gateway.py`, `mcp/types.py` | dynamic MCP tools | `isError: true` → `MCPToolError("mcp_tool_error", text)`; other failures → `mcp_unavailable` / `mcp_call_failed`. `mcp_tool_to_definition` sets `error_codes` (D6.6) |

### 7. Descriptions (D6.5) and the 8 folds (D5.6)

Each of the 24 descriptions: at least 3 sentences, what it does, when to use, when not, limits, 1–3 example calls written as `tool(arg=…)`. Parameter descriptions stay. Folds (only what the model does not know):

- `bash` ← `bash.md` (where commands run, absolute paths, plain-command rule, ask/deny, 50 KiB cap, SIGPIPE note) + `list-directory.md` (`find … | wc -l` to count, not `ls -R`).
- `fetch_url` ← `fetch-url.md` (fetch_url is citable, bash curl is not; curl only for non-GET or headers).
- `get_location` ← `get_location.md` (call first, never ask; `consent_not_given` → PWA toggle; `session_notes` for "I'm in X").
- `recall_personal_history` and `search_memory` ← `personal-history-recall.md` (we/I/my → recall_personal_history, else search_memory; days_ago table in short).
- `read`, `write` ← `read-write.md` (durable paths, scratch paths, tail_lines for logs, append mode).
- `run_python` ← `run-python.md` (no state between calls, app source not importable, network is an ask, /sandbox scratch).
- `web_search` ← `web-search.md` (real-world facts here, not search_memory; brand/product claims need a result; what `categories` returns).

The PR body holds the AC-3 table: skill → lines kept → description.

### 8. MCP removal (D7.5)

- `docker/mcp/run-gateway.sh`: `DEFAULT_SERVERS='[]'`; an empty list exits 1 with "no MCP server is enabled; set AGENT_MCP_GATEWAY_ENABLED=false".
- `mcp_gateway_enabled` default stays `False` (already). `.env.example` example line updated.
- `config/governance/tools.yaml` and `permissions.yaml`: remove the `mcp_resolve-library-id`, `mcp_sequentialthinking`, `mcp_query-docs` entries.
- `tests/scripts/test_run_gateway.py` updated.

### 9. Failure-line coverage (AC-1)

- `scripts/check_tool_failure_coverage.py <coverage.json>`: imports each registered tool's executor module (registry with every flag on, plus `primitives/_governance.py`, `mcp/client.py`, `mcp/gateway.py`), finds every AST `Call` whose callee resolves to a `ToolError` subclass or a function annotated to return one, and fails listing each such line that coverage did not execute.
- CI (`.github/workflows/ci.yml`): the unit-test step adds `--cov=src/personal_agent/tools --cov=src/personal_agent/mcp --cov-report=json:coverage-tools.json`; a next step runs the checker. `make tool-failure-coverage` runs both locally.
- `tests/scripts/test_check_tool_failure_coverage.py`: the checker fails on a seeded uncovered failure line, passes when covered.

### 10. Contract tests — `tests/personal_agent/tools/test_tool_contract.py`

Registry with every flag on (`primitive_tools_enabled`, `location_enabled`, R2 settings, `tool_result_compression_enabled`):

- registry names == the 24 rows (no `mcp_*`; `get_library_docs` present) — AC-5.
- each tool: `error_codes` non-empty and equal to `error_codes(<module error class>)`; module AST has no `ToolExecutionError(` construction and no returned dict with a top-level `error`/`success: False`.
- description lint: ≥3 sentences, ≥1 `name(` example, every parameter described.
- size bound: the real definition with a fake executor that returns 200,000 characters → content ≤ bound tokens, has `next_action`; no bound above 25,000.
- shape: fake executor success → `ok: true`; seeded negative: a fake tool returning `{"success": False, "error": "x"}` → `contract_violation`, and the contract assertion helper fails on it.
- dispatch: unknown tool, missing parameter, mode refusal, permission deny, timeout, executor exception, undeclared code → D6.1, `tool_call_completed success=false` where executed, full message (AC-8).
- AC-2: `query_telemetry` with a disallowed field → message holds the full allowed list, unclipped, `success: false` logged.
- Per-tool failure tests to drive every typed failure line (coverage step).

## Steps

1. Contract module + types + layer + dispatch + their tests (TDD: contract tests first, fail, implement). `make test-file FILE=tests/personal_agent/tools/test_tool_contract.py`.
2. Convert tools in groups, each with its tests updated and new failure tests: primitives → telemetry/read_skill/expand → network tools → linear/notes/artifacts → MCP.
3. Descriptions + folds; description lint passes.
4. MCP removal + config.
5. Coverage checker + CI + Makefile; run `make tool-failure-coverage` until no uncovered line.
6. Docs: `docs/reference/TOOL_INTEGRATION_GUIDE.md` (the contract), `src/personal_agent/tools/AGENTS.md`.
7. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`; self-review.

## Out of scope

- A layer-enforced timeout (see 3). Deleting skill files (T5). `edit` and `query_telemetry` for the primary (T4).
- The context-window error heuristics (`'"error"' in content`) still match the new failure shape; unchanged.

## Codex plan review — dispositions

1. Primary-loop malformed-JSON path still legacy → **fixed**: `invalid_json_arguments` via `undispatched_failure`.
2. Pre-execution failures did not log `tool_call_completed` → **fixed**: every failure, run or not, logs it once with `success: false` and `executed`.
3. Checker missed layer/dispatch builders → **fixed**: `dispatch_error`, `_governance_refusal`, `_permission_refusal` and `undispatched_failure` calls count, over the layer, dispatch, orchestrator and MCP modules.
4. One code set per multi-tool module is not closed per tool → **fixed**: one error class per tool; shared helpers raise a shared class whose `Literal` nests into each caller's.
5. Terminal error underspecified → **fixed**: `TerminalToolError(ToolError[CodeT])`; `error_codes` walks the MRO; `artifact_draft` uses a typed terminal subclass.
6. `cl100k_base` count is only an approximation → **fixed**: `result_tokens` never counts below one token per 3 characters.
7. Unbounded `next_action` → **fixed**: bounded at 2,000 characters; a failure never goes through the success truncation envelope.
8. Production still sets `AGENT_MCP_GATEWAY_ENABLED=true` → **master's deploy step** (ticket item 9); repeated in the handoff.
