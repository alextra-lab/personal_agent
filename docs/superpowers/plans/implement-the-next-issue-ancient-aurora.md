# FRE-385: Revive Captain's Log Reflection Pipeline

## Context

The ADR-0040 self-improvement feedback loop is completely dormant because CL-*.json files stopped appearing after April 27, 2026. The designed feedback cycle (agent reflection -> CL-*.json -> promotion pipeline -> Linear ticket -> FeedbackPoller) has never produced a single promoted ticket. All 16 agent-filed tickets came from direct tool calls, not the designed loop.

**Root cause investigation found TWO bugs:**

### Bug 1: Unguarded `.strip()` on DSPy coroutine output (PRIMARY)

`src/personal_agent/captains_log/reflection_dspy.py` line 419:
```python
has_proposed_change=bool(result.proposed_change_what.strip()),  # CRASHES
```

DSPy runs via `asyncio.to_thread()` with a cloud LM backend (`openai/gpt-5.4-nano`). Intermittently, DSPy result fields are coroutine objects instead of strings. The `_ensure_str()` guard exists at line 56 for exactly this case, but it's applied AFTER the crash point (line 429).

**Evidence**: 2,689 failures across 101 distinct production minutes (May 5-26), ALL with `'coroutine' object has no attribute 'strip'`. Meanwhile, 1,556 reflections DID succeed and exist in Elasticsearch -- the error is intermittent.

### Bug 2: Missing Docker volume mount for CL-*.json directory

`docker-compose.cloud.yml` line 336 only mounts `captures/`:
```yaml
- seshat_captures_cloud:/app/telemetry/captains_log/captures
```

The parent `/app/telemetry/captains_log/` where `save_entry()` writes CL-*.json files has no volume mount. Even when reflections succeed, the files are ephemeral and wiped on container restart.

---

## Implementation

### Step 1: Fix the `.strip()` crash in `reflection_dspy.py`

**File**: `src/personal_agent/captains_log/reflection_dspy.py`

**1a. Guard the log statement (lines 417-420)**:
```python
# BEFORE:
has_rationale=bool(result.rationale),
has_proposed_change=bool(result.proposed_change_what.strip()),

# AFTER:
has_rationale=bool(_ensure_str(getattr(result, "rationale", ""))),
has_proposed_change=bool(_ensure_str(getattr(result, "proposed_change_what", "")).strip()),
```

**1b. Guard `_parse_enum` inputs (lines 430-431)** -- latent bug, same class:
```python
# BEFORE:
category = _parse_enum(ChangeCategory, getattr(result, "proposed_change_category", ""))
scope = _parse_enum(ChangeScope, getattr(result, "proposed_change_scope", ""))

# AFTER:
category = _parse_enum(ChangeCategory, _ensure_str(getattr(result, "proposed_change_category", "")))
scope = _parse_enum(ChangeScope, _ensure_str(getattr(result, "proposed_change_scope", "")))
```

**1c. Close coroutines in `_ensure_str` to suppress `RuntimeWarning`**:
```python
if inspect.iscoroutine(value):
    value.close()  # Suppress "coroutine was never awaited" warning
    log.warning(...)
    return default
```

### Step 2: Fix Docker volume mount in `docker-compose.cloud.yml`

Replace the `captures/`-only mount with a parent-level mount:

```yaml
# BEFORE (line 336):
- seshat_captures_cloud:/app/telemetry/captains_log/captures

# AFTER:
- seshat_captains_log_cloud:/app/telemetry/captains_log
```

Add the new volume definition (around line 476):
```yaml
seshat_captains_log_cloud:
  driver: local
```

Keep `seshat_captures_cloud` defined (for rollback safety) but unused.

**Note**: Existing capture data in `seshat_captures_cloud` volume will not auto-migrate. Document a one-time migration command in PR description if capture history matters.

### Step 3: Add tests

**File**: `tests/test_captains_log/test_reflection_coroutine_guard.py`

Tests:
1. `test_ensure_str_returns_string_unchanged`
2. `test_ensure_str_returns_default_for_none`
3. `test_ensure_str_returns_default_for_coroutine` -- verify `.close()` is called
4. `test_ensure_str_coerces_non_string`
5. `test_dspy_log_block_survives_coroutine_fields` -- mock DSPy result with coroutine attrs, verify the log statement (post-fix) doesn't crash

### Step 4: Run existing tests + quality checks

```bash
make test
make mypy
make ruff-check
```

---

## Verification

### Pre-deploy
- `make test` passes (including new tests)
- `make mypy` + `make ruff-check` clean

### Post-deploy (same session)
1. Send a test message to the agent: `uv run agent "What time is it?"`
2. Check for new CL file: `docker exec cloud-sim-seshat-gateway ls -lt /app/telemetry/captains_log/CL-*.json | head -3`
3. Check logs for success: `grep "captains_log_entry_created" telemetry/logs/current.jsonl | tail -3`
4. Check for coroutine warnings (non-fatal): `grep "reflection_field_was_coroutine" telemetry/logs/current.jsonl | tail -3`
5. Restart container and verify CL files persist: `make restart SERVICE=seshat-gateway` then re-check CL files

### Post-deploy gate (24h)
- New CL-*.json files accumulate in `telemetry/captains_log/`
- Zero `captains_log_reflection_failed` errors with the coroutine `.strip()` message

### Future gate (2 weeks)
- Promotion pipeline produces at least 1 promoted entry
- `promotion.issue_created` events appear in Redis Streams
