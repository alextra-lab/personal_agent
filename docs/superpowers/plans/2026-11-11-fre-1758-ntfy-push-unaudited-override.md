# FRE-1758: ntfy push when a stored settings override has no audit row

Backing decision: FRE-1753 (the startup warning), FRE-1550 (owner ntfy path), FRE-1632 (IPv4).
Tier: Standard (`src/` logic, a secret setting, compose). Codex plan review required.

## Facts that shape the plan

- `Dockerfile.gateway` copies `src/` and `config/` only. `src/` cannot import `scripts/dispatch/owner_notify.py`.
  The plan copies a small sender into `src/` and leaves the dispatch module unchanged.
- `AGENT_OWNER_NTFY_URL` is not an `AppConfig` field today. The dispatch daemons parse `.env` themselves.
  The gateway needs a field so that it reads the URL through `settings`.
- The gateway service in `docker-compose.cloud.yml` already imports `/opt/seshat/.env` through `env_file`.
  AC-4 asks for an explicit `${AGENT_OWNER_NTFY_URL:-}` entry. An `environment:` entry beats `env_file`,
  so compose must resolve the key. On the VPS compose runs from `/opt/seshat`, which holds `.env`.
- The sender uses `urllib`, not `httpx`. `httpx` logs the request URL at INFO. Only a global logger
  level hides it. `urllib` never logs.
- `settings_edit.py` tests replace `settings_edit.settings` with a `SimpleNamespace`. The new sender
  module imports `settings` itself, so those tests do not break.

## Steps

1. **Setting.** `src/personal_agent/config/settings.py`: add `owner_ntfy_url: str | None`, alias
   `AGENT_OWNER_NTFY_URL`, `json_schema_extra={"secret": True}`, default `None`.
   `src/personal_agent/config/setting_classes.py`: add `"owner_ntfy_url": _SECRET` to `READ_ONLY_SETTINGS`.
   Add the key to `.env.example` (commented, no value). Regenerate `docs/reference/CONFIG_INVENTORY.md`
   with `uv run python -m scripts.audit.config_inventory generate`.
   Verify: `make test-file FILE=tests/scripts/test_config_inventory.py`, `tests/personal_agent/config/`.
2. **Sender (test first).** `tests/personal_agent/service/test_owner_ntfy.py`, then
   `src/personal_agent/service/owner_ntfy.py` with `async def push_to_owner(message, *, trace_id, opener=..., timeout_s=5.0) -> bool`.
   It reads `settings.owner_ntfy_url`. Unset: log `owner_ntfy_unconfigured`, send nothing.
   Else one plain-text POST over IPv4 in `asyncio.to_thread`, bounded by `asyncio.wait_for`.
   Any failure logs `owner_ntfy_failed` with the exception type only. It never raises.
   Tests (AC-2, AC-3): sentinel URL absent from the log on success, on each failure kind, and on a bad URL;
   unset URL logs once and sends nothing; `URLError`, `TimeoutError`, a refused connection to a closed
   port, a hung server (hard timeout) and an HTTP 500 each log the type and return `False`;
   a local HTTP server receives one POST with the message body and the headers.
3. **Wire-in (test first).** Tests in `tests/personal_agent/service/test_settings_edit_api.py` next to the
   FRE-1753 tests, with a fake sender. Then `settings_edit._warn_about_unaudited_overrides`: after the
   warning loop, if `unaudited` is not empty, `await push_to_owner(message, trace_id=trace_id)`.
   The message holds the count and the parameter names. It never holds a value.
   Tests (AC-1, AC-2): two unaudited rows give one push that names both and the count; zero
   unaudited rows give no push; a failed audit read gives no push; the override sentinel value is
   absent from the push body and the log; a failing sender does not stop `load_overrides_at_startup`.
4. **Compose.** `docker-compose.cloud.yml`, gateway `environment:`: add
   `AGENT_OWNER_NTFY_URL: ${AGENT_OWNER_NTFY_URL:-}`. Add a render test that the entry exists, uses
   `:-`, and no `:?` form of it appears. Run the compose render tests in `tests/scripts/`.
5. **Docs.** `docs/runbooks/dispatch-orchestrator.md` owner-notification section: one line that the
   gateway also reads the key, and the post-deploy test command.
6. **Gates.** `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `uv run pre-commit run --all-files`. Commit, then `feature-dev:code-reviewer` and `security-review`
   on `git diff origin/main...HEAD`.

## AC-5 live probe (for master)

`docker exec cloud-sim-seshat-gateway python -c "..."` calls `push_to_owner` through the gateway code
path with a fixed test line and prints `True` or `False`. It writes no row and reads no override.
The owner confirms the line on the phone.

## Risks

- Compose resolves the key from the project `.env`. If compose runs where that file is absent, the
  entry sets an empty string and the push stays silent. The runbook names this.
- The `urllib` sender duplicates the transport of `scripts/dispatch/owner_notify.py`. The duplicate is
  small. A shared module would make the daemons import `personal_agent`, which they avoid.
