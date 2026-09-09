# FRE-1466 — Fix run_python network=True broken network name

**Issue:** `run_python` with `network=True` fails because it hardcodes `"cloud-sim"` instead of `"seshat_cloud-sim"` (Docker Compose prefixes networks with the project name).

**Scope:** Resolve network name from configuration; ensure it is not a hardcoded literal. Add tests for all four acceptance criteria.

## Acceptance Criteria

- **AC-1**: Network-enabled run reaches the internet (DNS + HTTP request)
- **AC-2**: Network-disabled run has no access (DNS fails)
- **AC-3**: Network name resolved from config, not hardcoded
- **AC-4**: Network attachment failure produces a distinguishable error

## Implementation Steps

### 1. Add network config setting
- File: `src/personal_agent/config/settings.py`
- Location: Near existing sandbox settings (line ~2750)
- Field: `sandbox_network: str` with default `"seshat_cloud-sim"`
- Reason: Makes it configurable; allows rename without re-breaking

### 2. Update sandbox.py to use config
- File: `src/personal_agent/tools/primitives/sandbox.py`
- Line 177: Replace hardcoded `"cloud-sim"` with `settings.sandbox_network`
- Add import: `from personal_agent.config import settings`
- Verify: Import `settings` at module level

### 3. Verify error messaging
- Line 177: When network attachment fails, the error should name the network
- No change needed if Docker already reports the network name in stderr

### 4. Write tests
- File: `tests/personal_agent/tools/test_run_python.py` (create if missing)
- Test AC-1: `test_run_python_network_enabled_reaches_internet` — curl/httpx to external URL
- Test AC-2: `test_run_python_network_disabled_no_access` — DNS resolution fails
- Test AC-3: `test_run_python_uses_configured_network` — verify settings.sandbox_network is used
- Test AC-4: `test_run_python_network_error_is_distinguishable` — mock Docker to fail network attachment

### 5. Run quality gates
- `make test` — ensure all tests pass
- `make mypy` — type check
- `make ruff-check` + `make ruff-format` — lint and format
- `pre-commit run --all-files` — commit hooks

### 6. Self-review and post
- Diff class: Standard (config + test changes only, no production logic change)
- No security implications
- Fold in: None anticipated

## Files to change

1. `src/personal_agent/config/settings.py` — add `sandbox_network` field
2. `src/personal_agent/tools/primitives/sandbox.py` — use `settings.sandbox_network`
3. `tests/personal_agent/tools/test_run_python.py` — add tests

## Estimated complexity

Tier-3 Haiku: config setting + one line change + four tests. Standard ticket.
