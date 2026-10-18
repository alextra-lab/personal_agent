# FRE-1614 — Prompt debug capture

**Ticket:** [FRE-1614](https://linear.app/frenchforest/issue/FRE-1614) · **Tier:** Standard (touches `src/` logic and the owner's private content) · **Backing ADR:** none (owner instruction 2026-10-18).

## Goal

When `AGENT_PROMPT_DEBUG_CAPTURE_ENABLED=true`, every model call writes one JSON file with the full request that left the client. A viewer inside the gateway prints the files in a readable form.

## Facts that shape the design

- Only two `litellm.acompletion` sites exist in `src/` (verified with ast-grep): the cloud path `respond()` (`litellm_client.py:1442`, non-streaming) and the local path `_respond_local()` (`litellm_client.py:2023`, streaming). Every role goes through one of them.
- The gateway image copies `src/`, `config/`, `skills/` and `docker/mcp/` only. `scripts/` is not in the image. So the viewer must be a module under `src/`, run with `python -m`.
- `/app/telemetry` is already the named volume `seshat_telemetry_cloud` (FRE-910 parent mount). A writer under `telemetry/prompt_debug/` is durable with no compose change.
- Existing client tests patch `get_settings()` with a `MagicMock`. A `MagicMock` attribute is truthy. So the capture flag is read from the module singleton `personal_agent.config.settings`, not from the patched `get_settings()`. Otherwise every existing test would write files.
- The compose project for `docker-compose.cloud.yml` has no `name:`. `docker compose exec` from a worktree resolves a different project. The Make target uses `docker exec cloud-sim-seshat-gateway` (the fixed `container_name`).

## Design

### 1. Setting

`src/personal_agent/config/settings.py`: `prompt_debug_capture_enabled: bool = Field(default=False, ...)`. Add `AGENT_PROMPT_DEBUG_CAPTURE_ENABLED=false` to `.env.example`. Regenerate the inventory: `uv run python scripts/audit/config_inventory.py generate`.

### 2. Writer — `src/personal_agent/telemetry/prompt_debug.py`

- `ALLOWED_PARAMS: frozenset[str]` — the request keys that get written: `model, max_tokens, temperature, top_p, top_k, min_p, presence_penalty, frequency_penalty, repetition_penalty, seed, stop, tool_choice, response_format, parallel_tool_calls, reasoning_effort, thinking, stream, stream_options, num_retries, timeout, extra_body, allowed_openai_params`. Every other key is dropped. `messages` and `tools` are written in their own fields. `api_key`, `api_base`, `client` and `extra_headers` are never written, because they are not in the list.
- `capture_enabled() -> bool` — reads `settings.prompt_debug_capture_enabled` from the singleton.
- `prompt_debug_root() -> Path` — `<project root>/telemetry/prompt_debug` (same pattern as `feedback_history_dir()`).
- `build_record(...) -> dict[str, object]` — pure. Fields: `trace_id, session_id, role, model, provider, placement, started_at (UTC ISO), duration_ms, usage, params, messages, tools`. The `seq` field is added at write time.
- `async capture_model_call(...) -> None` — builds the record and runs the write in `asyncio.to_thread`. Any `Exception` is caught and logged as `prompt_debug_capture_failed` with `trace_id`. A capture failure never fails the model call.
- Layout: `<root>/<session_id or "no-session">/<trace_id>/<seq:03d>-<role>.json`. The ticket's example layout uses `<date>/`. I use `<session>/` instead, because it makes `--session` a directory lookup, and a trace that crosses midnight does not split. Retention uses file mtime, so it does not need a date directory.
- Path components (session, trace, role) are reduced to `[A-Za-z0-9_-]`. This blocks path traversal from an odd session id.
- Sequence number: the count of `*.json` in the trace directory plus one. The file is opened with `O_CREAT | O_EXCL` and mode `0o600`. On `FileExistsError` the number goes up by one and the open runs again. This needs no in-memory state, and concurrent sub-agent writes cannot overwrite each other. Directories are created with mode `0o700`.
- JSON: `json.dumps(record, default=str, ensure_ascii=False, indent=1)`. `default=str` covers the local path's `httpx.Timeout`.
- Retention: `prune_old_captures(root, *, now, max_age_days=7) -> int` deletes every `*.json` with mtime older than the limit, then removes an empty directory only when its own mtime is also older than the limit (a new trace directory is never removed under a concurrent write). The writer calls it at most once per hour per process (module-level monotonic stamp), inside the same worker thread.

### 3. Call sites — `src/personal_agent/llm_client/litellm_client.py`

- Cloud `respond()`: take `started_at = datetime.now(timezone.utc)` with the existing `start_time`. After `emit_model_call_completed(...)`, add:
  `if prompt_debug.capture_enabled(): await prompt_debug.capture_model_call(..., request=litellm_kwargs, usage=usage, duration_ms=latency_ms, placement="cloud")`.
  `litellm_kwargs["messages"]` is the wire copy (with Anthropic `cache_control` marks), which is what left the client.
- Local `_respond_local()`: take `started_at` and a monotonic start before the `asyncio.timeout` block. After the `async with request_slot(...)` block exits (so the write never holds the GPU slot), and before `return llm_response`, add the same guarded call with `placement="local"` and `usage=llm_response["usage"]`.
- With the flag off, the added work on the request path is one attribute read per call.
- A call that raises writes no file. This keeps AC-5 exact: one file per `model_call_completed`.

### 4. Viewer — `src/personal_agent/telemetry/prompt_debug_viewer.py`

`python -m personal_agent.telemetry.prompt_debug_viewer (--trace ID | --session ID | --last) [--full] [--root PATH]`.

- `--trace`: glob `*/<trace>/*.json`. `--session`: `<session>/*/*.json`. `--last`: the trace of the newest file by mtime.
- Order: by `started_at`, then `seq`.
- Per call: a header line (seq, role, model, placement, started_at, duration, actual usage tokens), then each message with its role and an approximate token count (`estimate_tokens`, cl100k), the full message text, then the tool block: tool names, each tool's token estimate and the block total. `--full` adds the complete tool JSON. A footer gives the estimate total (messages + tools) next to the actual `prompt_tokens`.
- Output via `sys.stdout.write` (no `print()`).

### 5. Make target and docs

- `Makefile`: `prompt-show: ## Show captured model-call prompts inside the gateway (FRE-1614): ARGS="--last"` → `docker exec cloud-sim-seshat-gateway /app/.venv/bin/python -m personal_agent.telemetry.prompt_debug_viewer $(ARGS)`.
- `docs/reference/PROMPT_DEBUG_CAPTURE.md`: "How to see a prompt" — enable, run a turn, run the viewer, retention, privacy note.

## Codex plan review — dispositions

1. A local retry or a failed call writes no extra file. **Kept:** a retry resends the same request, and a failed call emits no `model_call_completed`, so AC-5 stays exact. Documented.
2. Nested credentials and `default=str`. **Partly accepted:** `extra_body` and `thinking` come only from the typed catalog `ModeSpec` (no credential fields). An unknown object is now written as its type name only, never its `repr`. `httpx.Timeout` is written with `as_dict()`.
3. The record is the litellm arguments, not the wire JSON. **Accepted:** the docs say so.
4. A partial file is visible, and the prune can race a writer. **Accepted:** the writer publishes a hidden temporary file with `os.link` (atomic, exclusive). A `FileNotFoundError` after a prune makes the writer create the directory again.
5. Retention needs traffic. **Accepted:** one prune at service start (`prune_captures_at_startup`, in the `app.py` lifespan).
6. A real `.env` with the flag on makes the suite write files. **Accepted:** `tests/conftest.py` forces `AGENT_PROMPT_DEBUG_CAPTURE_ENABLED=false`.
7. Drop the sequence number. **Rejected:** the ticket asks for it.

## Steps and verification

1. Tests first: `tests/personal_agent/telemetry/test_prompt_debug.py` (writer, allow-list, retention, path sanitising, seq collision) and `tests/personal_agent/llm_client/test_prompt_debug_capture_sites.py` (both call sites, flag off) → run, confirm they fail on missing module.
2. Implement settings + writer → `make test-file FILE=tests/personal_agent/telemetry/test_prompt_debug.py`.
3. Wire the two call sites → `make test-file FILE=tests/personal_agent/llm_client/test_prompt_debug_capture_sites.py`.
4. Viewer + tests (`tests/personal_agent/telemetry/test_prompt_debug_viewer.py`) → `make test-file ...`.
5. Make target, `.env.example`, inventory regenerate, docs → `uv run python scripts/audit/config_inventory.py verify`.
6. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Acceptance criteria → proof

| AC | Proof |
|----|-------|
| AC-1 flag off → no file | Call-site tests for both paths with the flag false: the capture root stays empty, and `capture_model_call` is not called. |
| AC-2 seeded secret | Cloud-site test with a real `api_key` value, plus a request with `extra_headers={"Authorization": "Bearer <seeded>"}` into `build_record`: neither seeded value appears anywhere in the written file bytes. |
| AC-3 both sites | One test per site, flag on: one file, holding the messages, tools and usage of that call. |
| AC-4 retention | A file with mtime 8 days old is deleted, a 1-day-old file is kept. |
| AC-5 / AC-6 | Live, after deploy (master + owner turn). Runbook in the handoff. |
| AC-7 docs | `docs/reference/PROMPT_DEBUG_CAPTURE.md`. |
