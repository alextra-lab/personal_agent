# FRE-1750 Embedder fallback: send a credential and record its cost

**Goal:** The same-model fallback embedder path (`memory/embeddings.py`, `_generate_vectors`) sends an optional bearer token from a new secret setting, and records the cost of a billed fallback call. With no token, the fallback path behaves as before.

**Tier:** Standard (touches `src/` logic and cost recording). Codex plan review required.

**Out of scope (master's deploy step, AC-2):** the `.env` values (`AGENT_LOCAL_FALLBACK_EMBEDDING_ENDPOINT`, `..._MODEL`, `..._TOKEN`). The gateway egress allowlist already names `api.scaleway.ai` (`docker-compose.cloud.yml:661`), so no compose change is needed.

**Known conflict, not changed here:** `config_guard.check_embedding_fallback_identity` (`config/config_guard.py:673`) compares model ids with an exact, case-sensitive match. The managed default is `Qwen3-Embedding-8B`. Scaleway's id is `qwen3-embedding-8b`. If master sets the lowercase id, `run_all_checks` reports `embedding_fallback_identity_mismatch`. The owner must choose the fix. This plan leaves the guard alone.

## Steps

1. **Write failing tests** in `tests/personal_agent/memory/test_embeddings_managed.py`, next to `test_managed_failure_falls_back_to_local`:
   - `test_fallback_sends_configured_token`: set `local_fallback_embedding_token="scw-test-token"`, mock `_call_embeddings_api`, assert `api_key` kwarg is `"scw-test-token"`. → verify: fails (kwarg absent).
   - `test_fallback_without_token_sends_unused_key`: token unset, assert `api_key == "unused"`. → verify: passes today (regression guard).
   - `test_fallback_with_token_records_scaleway_cost`: token set, fallback response has `usage.total_tokens=10` and 4096-wide vectors, patch `record_vendor_cost` with `AsyncMock`, assert it is awaited once with `provider="scaleway"`, `tokens=10`, `purpose="embedding"`, and the trace and session ids, and the returned vector has 1024 unit components. → verify: fails.
   - `test_fallback_without_token_records_no_cost`: token unset, assert `record_vendor_cost` is not awaited. → verify: passes today.
   - Run: `uv run pytest tests/personal_agent/memory/test_embeddings_managed.py -k fallback -q` → expect 2 failures.

2. **Add the setting** in `src/personal_agent/config/settings.py`, after `local_fallback_embedding_model`:
   `local_fallback_embedding_token: str | None = Field(default=None, description="Bearer token for the local fallback embedder endpoint (FRE-1750). Set only when the fallback is a billed provider such as Scaleway. Unset by default.", json_schema_extra={"secret": True})`

3. **Classify the secret** in `src/personal_agent/config/setting_classes.py`: add `"local_fallback_embedding_token": _SECRET,` between `linear_api_key` and `managed_database_url`.

4. **Use the token and share the cost code** in `src/personal_agent/memory/embeddings.py`:
   - Move the body of the `if total_tokens ...` block from `_embed_managed_batch` into a new helper `_record_embedding_cost(*, provider, model, total_tokens, latency_ms, trace_id, session_id) -> None`. Keep the logic unchanged, including the debug log when usage is missing. The `provider` argument replaces the literal `"ovh"`.
   - `_embed_managed_batch` calls the helper with `provider="ovh"`.
   - In `_generate_vectors`, the fallback call passes `api_key=settings.local_fallback_embedding_token or "unused"` and times the call.
   - A module constant `_BILLED_FALLBACK_PROVIDERS = {"https://api.scaleway.ai/v1": "scaleway"}` decides whether a cost row is written. The provider comes from the endpoint, not from the token, because the token only sets the auth header. An unlisted endpoint gets no cost row, even with a token.
   - Price: `_record_embedding_cost` uses the "embedding" role's rate (OVH). Scaleway lists the same EUR 0.10 per million tokens, so the number is correct. If the prices diverge, add a per-provider rate.
   - A missing or malformed `usage` skips the cost row and keeps the vectors.
   - → verify: the step 1 tests pass.

5. **Document the setting**: add `# AGENT_LOCAL_FALLBACK_EMBEDDING_TOKEN=<fallback-bearer-token>` under the `AGENT_LOCAL_FALLBACK_EMBEDDING_MODEL` line in `.env.example`. Add the matching row to `docs/reference/CONFIG_INVENTORY.md` (secret, redacted).
   - → verify: `uv run pytest tests/scripts/test_config_inventory.py tests/scripts/test_config_usage_audit.py -q` passes. If a test asserts a fixed secret list or row count, update that test's list in the same change.

6. **Run the wider gates:**
   - `uv run pytest tests/personal_agent/memory/test_embeddings_managed.py tests/personal_agent/config -q`
   - `make test` (full unit suite, one run)
   - `make mypy`
   - `make ruff-check` and `make ruff-format`
   - `pre-commit run --all-files`

7. **Self-review** the diff with `git diff origin/main...HEAD` (commit first). Check: no `print`, no `os.getenv`, `trace_id` on the new log call, Google docstring on the new helper.

## Acceptance criteria mapping

| AC | Covered by |
|----|-----------|
| AC-1 token from a secret setting; empty keeps the unauthenticated case | Step 1 tests 1–2; step 2–3 |
| AC-2 master sets `.env` values | Out of scope (deploy). Egress allowlist already present. Identity conflict flagged. |
| AC-3 fallback cost recorded (`api_costs`, trace, session) | Step 1 test 3–4; step 4 |
| AC-4 forced-fallback test returns 1024 unit vectors | Step 1 test 3 (4096 → 1024, norm checked) |
| AC-5 gates as CI runs them | Step 6 |
