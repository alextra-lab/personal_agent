# FRE-734 — Vision config-parity fix + thinking-reply surfacing

**Ticket:** FRE-734 (Approved, Opus, Bug) · **Backing:** ADR-0101 §5 (capability-driven routing) · **Blocks:** FRE-669
**Branch:** `fre-734-vision-config-parity-fix`

## Root cause (CONFIRMED live, not the ticket's hypothesis)

The ticket hypothesized a container/build artifact discrepancy (stale installed code or pydantic
version drift). **Live forensics in `cloud-sim-seshat-gateway` disproves that:**

- `import personal_agent` resolves to `/app/src/personal_agent` (current code, NOT stale site-packages).
- The installed `ModelDefinition.model_fields` **contains** `supports_vision` (field present).
- `settings.model_config_path` in the container = **`/app/config/models.cloud.yaml`** (set by
  `docker-compose.cloud.yml:313 AGENT_MODEL_CONFIG_PATH`), **not** `models.yaml`.
- `config/models.cloud.yaml` has **0** `supports_vision` entries; `config/models.yaml` has **4**.

**Mechanism:** FRE-665 added `supports_vision: true` to the four vision-capable models in
`config/models.yaml` only. Production loads `config/models.cloud.yaml`, which was never updated → every
model defaults to `supports_vision=False` → `_resolve_vision_routing_key` finds no vision-capable model
→ default branch raises `AttachmentUnsupportedError` on every image turn. FRE-665's CI guard
`test_deployed_vision_capable_models_flagged` reads `models.yaml`, so **CI stayed green while prod broke**.
This is **config-parity drift between two model-config files**, not a code or container-artifact bug.

## Acceptance criteria (from ADR-0101 §5 + the ticket's two defects)

1. **AC-1 (Defect 1 — prod fix):** the *deployed* config (`models.cloud.yaml`) flags the four
   vision-capable models — `load_model_config('config/models.cloud.yaml')` yields
   `supports_vision is True` for `primary`, `sub_agent`, `claude_sonnet`, `claude_haiku`.
2. **AC-2 (guard — drift caught at CI, not by a user):** the CI capability test asserts the flag across
   **every deployed config file** (`models.yaml` AND `models.cloud.yaml`), so this class of parity drift
   fails CI before deploy.
3. **AC-3 (guard — live visibility):** service startup logs the vision-capable roles from the
   *actually-loaded* config, and **warns (role-aware, non-fatal)** when the expected prod roles
   (`primary`, `sub_agent`, `claude_sonnet`, `claude_haiku`) are not flagged — the drift signature.
   Owner decision 2026-07-02: warn, not fatal (CI parity test is the real gate; vision is not
   load-bearing, so its absence must not down the gateway).
4. **AC-4 (Defect 2 — thinking-reply):** when the model returns empty `content` but a substantive
   `reasoning_trace` and no tool calls, the final reply surfaces the reasoning text, not the generic
   "Task completed" / "couldn't produce an answer" fallback.

## Plan (TDD — failing test first each step)

### Step 1 — AC-2 guard test (write first, will fail against current cloud config)
File: `tests/test_config/test_model_loader.py` → `TestSupportsVisionDeployedConfig`.
Parametrize `test_deployed_vision_capable_models_flagged` over both deployed config paths that exist:
`config/models.yaml` and `config/models.cloud.yaml`. (`docker-compose.eval.yml` references
`config/models.eval.yaml`, which does NOT exist in the repo — stale/eval-only; explicitly excluded, not
guarded.) Verify: fails on `models.cloud.yaml` (0 flags), passes on `models.yaml`. (Uses `load_model_config(Path(...))` — reads the real repo config, like the
existing FRE-696 cloud-config test precedent.)

### Step 2 — AC-1 fix: add `supports_vision: true` to `config/models.cloud.yaml`
Four edits mirroring `models.yaml` comment style, `supports_vision: true` on:
- `primary` (after `supports_function_calling: true`)
- `sub_agent` (after `supports_function_calling: true`)
- `claude_sonnet` (after `default_timeout: 180`)
- `claude_haiku` (after `default_timeout: 30`)
→ Step-1 test now passes for both files.

### Step 3 — AC-3 guard: startup vision-capability log
File: `src/personal_agent/service/app.py` (`lifespan`, near `service_starting`).
Add a small helper `_log_vision_capabilities()` that loads the active config, logs
`vision_capable_roles` (info) with the config path, and `log.warning("no_vision_capable_roles_configured", ...)`
when the set is empty. Non-fatal (best-effort try/except; a config issue must not down the service).
Threads no user data → no trace_id needed (startup, no request). Test: unit test asserting the helper
logs a warning for an all-`False` config and info for a vision-capable config (capture via `structlog`
testing or a monkeypatched `load_model_config`).

### Step 4 — AC-4 fix: surface reasoning_trace when content empty (Defect 2)
File: `src/personal_agent/orchestrator/executor.py` line ~3124 (no-tool-calls final-reply path).
Current: `ctx.final_reply = response_content or _fallback_reply_from_tool_results(ctx)`.
New priority chain: `content` → substantive `reasoning_trace` → tool-results fallback. `reasoning_trace`
is `response.get("reasoning_trace")` (already extracted by `adapters.py`, `str | None`, stripped).
"Substantive" = non-empty after strip. Scope: only this final-reply branch (no tool calls) — does not
perturb the tool-execution path. Tests in `tests/test_orchestrator/`: (a) empty content + substantive
reasoning + no tools → `final_reply == reasoning_trace`; (b) empty content + empty reasoning + no tools
→ existing fallback unchanged; (c) non-empty content → content wins (regression).

### Step 5 — quality gates
`make test-file` on the two touched test files → module tests → `make test` (full) · `make mypy` ·
`make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

## Out of scope / notes
- **No deploy** — build session stops at PR. Master redeploys `seshat-gateway` (models.cloud.yaml is
  baked into the image via `COPY config/`; a rebuild ships the corrected file). Runbook in handoff.
- Defect 2 decision: surface reasoning verbatim (option 1), not disable-thinking (option 2) — the robust,
  general fix; observable-first, don't clamp round 1 (owner posture). Reasoning text may contain
  meta-commentary but is strictly better than "Task completed".
- Parity across the *other* config files (benchmark/eval/medium/baseline) is intentionally NOT changed —
  those are experimental/eval profiles, not the deployed vision surface; AC-2 guards only deployed files.
