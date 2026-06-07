# FRE-505 — Sub-agent path auditability (input context + full output + injected digest)

**Issue:** FRE-505 (Approved, Tier-2:Sonnet, project *Observability Foundation*)
**Refs:** trace `87cbd720`, ADR-0086 (discovery sub-agents), ADR-0074 (identity/joinability), FRE-501 (cost/status sibling)
**Branch:** `worktree-build` → PR (build stops at PR)

## STEP 1 — gap CONFIRMED (posted to Linear)

- `sub_agent.py:94-103` (`sub_agent_start`) and `:216-228` (`sub_agent_complete`) emit no input context and only `digest_chars` for output.
- Sub-agents call `llm_client.respond` directly (`sub_agent.py:148/345/449`), bypassing the executor's primary-only `llm_call_messages_debug` (`executor.py:2605`). ES: 0 `messages_debug` docs with `model_role=sub_agent`; trace `87cbd720` shows the 2 subs with `digest_chars=2000` only.

## Deliverable (visibility only — no behavior change)

A durable, identity-threaded **per-sub-agent capture** so a decomposition turn is reconstructable from telemetry alone. Emitted from the single chokepoint `run_sub_agent` (covers both dispatch paths: `expansion.py` and `expansion_controller.py`).

### Design decisions (for owner sign-off)

1. **Where it lives:** a *sibling* captures index `{captains_log_index_prefix}-captures-subagents-{YYYY-MM-DD}`, doc_id `{trace_id}:{task_id}`. Sibling (not the TaskCapture daily index) avoids ES dynamic-mapping pollution from a different doc shape; still inside the `agent-captains-captures-*` wildcard family. **ES-only** via `schedule_es_index` (best-effort, non-blocking) — no disk write (one-file-per-`trace_id` would collide across N sub-agents).
2. **Single emit site:** `run_sub_agent` (not the two dispatchers) — every sub-agent, every mode, one code path.
3. **Memory/KG-in-context detection:** marker scan over `spec.context` for `## Your Memory Graph`. By current design memory is injected only into the *primary* system prompt, so this is typically `False` — surfacing that explicitly is the answer the ticket asks for, not a bug to fix here.
4. **No clamp / no digest change** (owner steer 2026-06-05: observe first). Pure addition.

### Record shape (`SubAgentCapture`, frozen Pydantic, `ConfigDict(frozen=True)`)

Identity (ADR-0074): `trace_id: str`, `session_id: str | None`, `task_id: str`, `timestamp: datetime`. (Join to the parent turn is by `trace_id` — the parent `TaskCapture` shares it.)
Input context: `system_prompt_chars: int`, `skill_index_block_chars: int`, `spec_task: str`, `context_message_count: int`, `context_chars: int`, `context_messages: list[dict]` (per-message `{role, chars, content_preview}` — preview ≤200 chars, mirrors `llm_call_messages_debug`), `memory_in_context: bool`, `mode: str`, `model_role: str`, `max_tokens: int`.
Task/tools: `tools_granted: list[str]`, `tools_used: list[str]`.
Output: `full_output: str`, `full_output_chars: int`, `injected_digest: str` (= `result.summary`), `digest_chars: int`, `truncation_ratio: float` (`digest_chars / full_output_chars`, `0.0` when output empty), `success: bool`, `error: str | None`, `duration_ms: float`, `cost_usd: float`.

### Codex review — resolutions folded in

- **ES mapping:** `agent-captains-captures-subagents-*` matches the shared captains template, where `full_output`/`injected_digest` would hit `default_string_keyword` (`ignore_above:1024`). `_source` is preserved regardless (retrieval-by-`trace_id` works), but Step 0 adds explicit `text` properties (`full_output`, `injected_digest`, `spec_task`) + a nested `context_messages` mapping to the template — additive, harmless to TaskCapture/reflection docs. (Template change affects only newly-created indices; needs ES re-apply on deploy — master's step.)
- **Cancellation:** the global dispatch timeout (`expansion_controller.py:427`) cancels gathered `run_sub_agent` coroutines with `CancelledError` (a `BaseException`) — not caught by `except Exception`/`except asyncio.TimeoutError`. Add an explicit `except asyncio.CancelledError` that emits a minimal "cancelled" capture then `raise` (preserves cancel semantics; `schedule_es_index` is non-blocking + never raises).
- **Unbound vars:** lift `_system_content`/`tools_used`/`call_cost_usd` above the `try`; the context breakdown is built from `spec` (always available); helper uses `msg.get("role")`/`msg.get("content")`.

## Atomic steps (TDD)

### Step 0 — ES template properties (`docker/elasticsearch/captains-index-template.json`)
Full per-field audit of every `SubAgentCapture` field vs the template `dynamic_templates` (ms→ids→enums→free_text→default_keyword). Fields needing explicit `properties` (the rest map correctly by rule — `*_chars`/`max_tokens`→long, `task_id`/`*_id`→keyword, `model_role`→keyword via enums, `memory_in_context`/`success`→boolean, `tools_*`→keyword, `duration_ms`→float via ms-rule):
- `"full_output": {"type":"text"}` — else default keyword `ignore_above:1024` (drops indexing of long output).
- `"injected_digest": {"type":"text"}` — same.
- `"spec_task": {"type":"text"}` — same; tasks can exceed 1024.
- `"error": {"type":"text"}` — tracebacks exceed 1024.
- `"truncation_ratio": {"type":"float"}` — **first value `0.0` → ES detects `long` → later `0.4` fails**.
- `"cost_usd": {"type":"float"}` — **same 0.0→long trap**.
- `"context_messages": {"type":"nested","properties":{"role":{"type":"keyword"},"chars":{"type":"integer"},"content_preview":{"type":"text"}}}`.
Additive only — no existing TaskCapture/reflection field collides.
- Verify: `python -c "import json; json.load(open('docker/elasticsearch/captains-index-template.json'))"` (valid JSON).

### Step 1 — capture model + writer (`src/personal_agent/captains_log/capture.py`)
- Add `SUBAGENT_CAPTURES_INDEX_PREFIX = f"{CAPTURES_INDEX_PREFIX}-subagents"`.
- Add `class SubAgentCapture(BaseModel)` with `model_config = ConfigDict(frozen=True)` and the fields above (Google docstring).
- Add `write_sub_agent_capture(capture: SubAgentCapture, es_handler=None) -> None`: `schedule_es_index(f"{SUBAGENT_CAPTURES_INDEX_PREFIX}-{date}", capture.model_dump(mode="json"), es_handler=handler, doc_id=f"{capture.trace_id}:{capture.task_id}")`. Best-effort; no disk write.
- **Test first** `tests/personal_agent/captains_log/test_sub_agent_capture.py`:
  - `test_truncation_ratio_computed` / `test_truncation_ratio_zero_when_empty`.
  - `test_write_schedules_es_index_with_composite_doc_id` (monkeypatch `schedule_es_index`, assert index name + doc_id `trace:task`).
  - `test_frozen` (mutation raises).
- Verify: `make test-file FILE=tests/personal_agent/captains_log/test_sub_agent_capture.py`.

### Step 2 — context breakdown helper (`src/personal_agent/orchestrator/sub_agent.py`)
- Add pure helper `_summarize_input_context(system_content: str, spec: SubAgentSpec) -> dict` returning `system_prompt_chars`, `skill_index_block_chars`, `context_message_count`, `context_chars`, `context_messages` (`[{role, chars}]`), `memory_in_context` (any `spec.context` content contains `"## Your Memory Graph"`).
- **Test first** in `test_sub_agent.py`: `test_input_context_summary_detects_memory` (context with the marker → `True`; without → `False`) and char/count math.

### Step 3 — emit the capture from `run_sub_agent` (`sub_agent.py`)
- After the existing `sub_agent_complete` log (`:216-228`), build a `SubAgentCapture` from `spec`, `result`, `_system_content`, and the Step-2 breakdown, then call `write_sub_agent_capture(...)` inside a `try/except Exception` that logs `sub_agent_capture_failed` (never fail the sub-agent on a capture error — mirror executor.py:1523 `capture_write_failed` guard).
- `_system_content` is built inside the `try`; lift it (or recompute the same way) so it is available on the timeout/exception paths too — on those paths `full_output=""`, `truncation_ratio=0.0`.
- Enrich `sub_agent_complete` with `full_output_chars` and `truncation_ratio` (cheap breadcrumb in `agent-logs-*`; full payload stays in the capture).
- **Test first** in `test_sub_agent.py`: `test_run_sub_agent_writes_capture` (monkeypatch `write_sub_agent_capture`, assert called once with identity fields + `injected_digest == result.summary` + `truncation_ratio`), and assert it still fires on the error path.
- Verify: `make test-file FILE=tests/personal_agent/orchestrator/test_sub_agent.py`.

### Step 4 — quality gates
`make test` (the two modules, then full) · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

### Step 5 — docs + follow-ups
- Note the new index in the capture module docstring; no MASTER_PLAN/CLAUDE.md edits (master's role).
- File a follow-up (Needs Approval, Observability Foundation): **REST read surface for sub-agent captures** — relates to FRE-514 (route-trace ledger read surface) so the decomp turn is reconstructable via the gateway observations API, not just raw ES.

## Out of scope
- No behavior/digest/cap changes. No new gateway endpoint (follow-up ticket). No disk persistence. No change to dispatchers.

## Verify (ticket STEP 3)
A new decomposition turn produces, per sub-agent, a capture answering without forensics: what it was fed (sizes + memory presence), task/mode/tools granted vs used, full output length, the injected digest, and the truncation ratio.
