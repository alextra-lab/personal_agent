# FRE-376 — End-to-end traceability (ADR-0074), Phase 1

**Ticket:** [FRE-376](https://linear.app/frenchforest/issue/FRE-376) (Urgent, Tier-1:Opus)
**ADR:** [ADR-0074 — End-to-End Traceability and Observability Joinability](/opt/seshat/docs/architecture_decisions/ADR-0074-end-to-end-traceability.md)
**Status note:** Linear shows `Done` — incorrectly closed by PR #68 auto-attach (PR #68 is the ship-ticket skill, unrelated). Re-open to **In Progress** before starting.
**Scope:** **Phase 1 only** — schema migrations, write-time enforcement on `api_costs`, and per-message model attribution on `sessions.messages[]`. Phases 2–5 ship as separate PRs.

---

## Context

ADR-0074 was filed on 2026-05-22 after the FRE-374 backfill replay surfaced that the observability stack has rich instrumentation but **no joinable foreign keys**:

- `api_costs.trace_id` is NULL on every row (4,077 / 4,077). The schema has the column, but `LiteLLMClient.completion()` doesn't pass it (litellm_client.py:390-397).
- `api_costs` has no `session_id` column at all.
- `sessions.messages[]` records `{"source": "service.app"}` in `metadata` — no model, model_role, model_config_path on assistant messages (app.py:282-287).
- `sessions` has no row-level model attribution (no `primary_model_at_creation`, no `model_config_path`).

Phase 1 closes the **cost** and **per-message attribution** gaps by adding columns, making the cost tracker raise on missing identity, and validating assistant-message writes at the service layer. Convention has already failed (the trace_id column was present, the code just didn't fill it) — Phase 1 fails loudly when identity is missing.

After Phase 1: every new cost row is attributable to a session + trace, and every new assistant message carries the model that produced it. Historical rows remain unattributable; the ADR explicitly puts backfill out of scope.

---

## Files to create / modify

### New

| Path | Purpose |
|---|---|
| `docker/postgres/migrations/0004_traceability_identity.sql` | Schema migration: add `api_costs.session_id`; backfill-purge NULL rows; `NOT NULL` on identity; add `sessions.primary_model_at_creation`, `sessions.model_config_path`. |
| `src/personal_agent/exceptions.py` | Central error module promised by CLAUDE.md. Exports `MissingIdentityError(ValueError)` and `InvalidMessageError(ValueError)`. (Module does not yet exist — `grep -rn personal_agent.exceptions src/` returns nothing.) |
| `tests/llm_client/test_cost_tracker_identity.py` | Unit tests: raise on missing trace_id / session_id; happy-path insert; verifies column values land. |
| `tests/service/test_session_message_validation.py` | Unit test: `SessionRepository.append_message` raises `InvalidMessageError` when an assistant message lacks `model` / `model_role` / `model_config_path`; user/tool/system messages unaffected. |
| `tests/migrations/test_0004_identity_migration.py` | Applies `0004_*.sql` against the test-stack Postgres (port 5433); asserts NULL rows purged, columns present, `NOT NULL` enforced. Mirrors the pattern in any existing `tests/migrations/` (or `tests/service/` if absent — confirm at implementation time). |

### Modified

| Path | Change |
|---|---|
| `docker/postgres/init.sql` (lines 5-13, 61-72) | Mirror the migration into fresh-install schema: add `sessions.primary_model_at_creation TEXT`, `sessions.model_config_path TEXT`; add `api_costs.session_id UUID NOT NULL`; flip `api_costs.trace_id` to `NOT NULL`. |
| `src/personal_agent/llm_client/cost_tracker.py` (record_api_call, lines 46-111) | Tighten signature: `trace_id: UUID` (drop `\| None`), add required `session_id: UUID`. Raise `MissingIdentityError` from `personal_agent.exceptions` when caller would have passed None. Add the new column to the INSERT. Update Google docstring. |
| `src/personal_agent/llm_client/litellm_client.py` (call at lines 388-399) | Pass `trace_id=UUID(trace_id)`, `session_id=trace_ctx.session_id`, `purpose=self.budget_role` to `cost_tracker.record_api_call`. The `except Exception: pass` swallow on lines 398-399 stays in place so a malformed call doesn't break the chat turn — but Phase 1 also adds a `log.error("cost_record_missing_identity", ...)` inside the except so the failure is visible. (When `trace_ctx.session_id` is None — boot-time embeddings probe, scheduler ticks — we still skip recording rather than raising at the call site; Phase 4 fixes the upstream by making `TraceContext` non-optional and introducing `SystemTraceContext`.) |
| `src/personal_agent/service/models.py` (SessionModel, lines 157-174) | Add SQLAlchemy `Column`s: `primary_model_at_creation = Column(String(120), nullable=True)`, `model_config_path = Column(String(255), nullable=True)`. Add matching fields on `SessionResponse` (lines 72-86) so the API surface stays honest. |
| `src/personal_agent/service/repositories/session_repository.py` | `create()` (lines 33-56): accept new optional kwargs `primary_model_at_creation: str \| None`, `model_config_path: str \| None`; populate the SessionModel. `append_message()` (lines 107-132): when `message["role"] == "assistant"`, require `model`, `model_role`, `model_config_path` in either the top-level dict or `message["metadata"]`; raise `InvalidMessageError` otherwise. Non-assistant messages (user/tool/system) bypass the check. |
| `src/personal_agent/service/app.py` | (a) Session-creation site (`POST /sessions` handler — confirm path during implementation): on `repo.create(...)`, pass `primary_model_at_creation=settings.<resolved>` and `model_config_path=str(settings.model_config_path)`. Use the same `load_model_config()` helper already in `src/personal_agent/config/model_loader.py` to resolve the primary model name. (b) Assistant-message append (app.py:279-288 and the second site near line 1220): augment the `metadata` dict with `{"model": <model_id>, "model_role": "primary", "model_config_path": str(settings.model_config_path)}`. Source the `model_id` from the orchestrator return value — add it to whichever LLMResponse / orchestrator-output type currently flows out (likely needs a small field added to the orchestrator's response payload; trace it at implementation time and either reuse `LLMResponse.usage` metadata or add `LLMResponse.model_id`). |
| `tests/llm_client/test_litellm_client.py` (if it exists) or a new sibling test | Verify the new kwargs reach `cost_tracker.record_api_call` (mock the tracker). |

### Reused (do not re-create)

- `TraceContext` already carries `session_id: str \| None` since `dc8fac0` (telemetry/trace.py:40-43). No change to `TraceContext` in Phase 1; Phase 4 tightens it.
- `settings.model_config_path` (config/settings.py:477) is the canonical config-path source.
- `personal_agent/config/model_loader.py:load_model_config()` already returns the resolved primary model.

---

## Migration: `0004_traceability_identity.sql`

Single transactional file, matches the simple-numeric-prefix convention of `0001_…` / `0002_…` / `0003_…`:

```sql
BEGIN;

-- ── api_costs: add session_id, enforce NOT NULL on identity ──
-- Pre-cutoff rows are unattributable (FRE-376 / ADR-0074). Drop, do not backfill.
DELETE FROM api_costs WHERE trace_id IS NULL;
ALTER TABLE api_costs ADD COLUMN session_id UUID;
ALTER TABLE api_costs ALTER COLUMN trace_id SET NOT NULL;
-- session_id stays NULL-able only until the *next* deploy cycle has populated
-- it for any in-flight rows; the contract test (Phase 5) will catch leakage.
-- For Phase 1 we add the column NULL-able and rely on application-layer raise.
CREATE INDEX idx_api_costs_session_id ON api_costs(session_id);
CREATE INDEX idx_api_costs_trace_id ON api_costs(trace_id);

-- ── sessions: row-level model attribution ──
ALTER TABLE sessions ADD COLUMN primary_model_at_creation VARCHAR(120);
ALTER TABLE sessions ADD COLUMN model_config_path VARCHAR(255);

COMMIT;
```

Verification snippet for the migration test:

```sql
SELECT count(*) FROM api_costs WHERE trace_id IS NULL;  -- expect 0
\d api_costs  -- expect: session_id present, trace_id NOT NULL
\d sessions   -- expect: primary_model_at_creation, model_config_path
```

---

## CostTracker contract (the load-bearing change)

Before:

```python
async def record_api_call(
    self, provider, model, input_tokens, output_tokens, cost_usd,
    trace_id: UUID | None = None, purpose=None, latency_ms=None,
) -> int | None:
```

After:

```python
async def record_api_call(
    self, provider, model, input_tokens, output_tokens, cost_usd,
    trace_id: UUID, session_id: UUID,
    purpose: str | None = None, latency_ms: int | None = None,
) -> int | None:
    """...
    Raises:
        MissingIdentityError: If trace_id or session_id is None.
    """
    if trace_id is None or session_id is None:
        raise MissingIdentityError(
            f"trace_id={trace_id} session_id={session_id} — both required"
        )
    # ... INSERT with session_id added.
```

Why required positional rather than required kwarg: clearer signature, mypy catches every caller. Once `LiteLLMClient` is updated, the only other caller is the eventual `LocalLLMClient` (Phase 2 work).

---

## Acceptance Criteria (Phase 1 only)

| AC | Pre-merge / post-deploy | Verification |
|---|---|---|
| AC-1 | pre-merge | `make test` passes including the three new test files. |
| AC-2 | pre-merge | `make mypy` clean — `CostTracker.record_api_call`'s tightened signature compiles at every call site. |
| AC-3 | pre-merge | `make ruff-check` + `make ruff-format` clean. |
| AC-4 | pre-merge | Manual: applying `0004_*.sql` against the test-stack Postgres (port 5433) succeeds and the assertions in `test_0004_identity_migration.py` pass. |
| AC-5 | post-deploy | After deploy, every new row in `api_costs` has non-NULL `trace_id` **and** non-NULL `session_id`. Probe: `SELECT count(*) FROM api_costs WHERE timestamp > <deploy_ts> AND (trace_id IS NULL OR session_id IS NULL);` returns 0 after one chat turn. |
| AC-6 | post-deploy | After deploy, a fresh session created via `POST /sessions` has both `primary_model_at_creation` and `model_config_path` populated. Probe: `SELECT primary_model_at_creation, model_config_path FROM sessions ORDER BY created_at DESC LIMIT 1;`. |
| AC-7 | post-deploy | After deploy, every new assistant message persisted carries `model`, `model_role`, `model_config_path` in its `metadata` dict. Probe: `SELECT messages -> -1 FROM sessions ORDER BY last_active_at DESC LIMIT 1;` and inspect. |
| AC-8 | future-gate | Phases 2–5 tickets filed under FRE-376 (or as siblings) before closing this issue. ADR-0074 status remains **Proposed** until all phases ship; do not flip to **Accepted** at Phase 1. |
| AC-9 | post-deploy | MASTER_PLAN.md updated: Phase 1 entry under Recently Completed; Wave H FRE-376 line annotated `Phase 1 shipped, Phase 2 next`. |

Per memory `feedback_plans_acceptance_criteria.md`: AC-5/6/7 must run in the same session as deploy — do not defer to "next time."

---

## Verification (end-to-end)

All steps run on the VPS at `/opt/seshat` — the canonical dev host (per memory `project_dev_environment_is_vps.md`). Local-machine work is only for Terraform; nothing in this plan touches that.

1. **Test-stack migration apply.** Bring up the isolated test stack (Postgres on :5433 per FRE-375) and apply the new migration:
   ```bash
   make test-infra-up
   docker compose -f docker-compose.test.yml exec -T postgres-test \
     psql -U agent -d personal_agent_test \
     -f /docker-entrypoint-initdb.d/migrations/0004_traceability_identity.sql
   make test-file FILE=tests/migrations/test_0004_identity_migration.py
   ```
   (Confirm exact mount path / compose-file name at implementation time — `docker-compose.test.yml` vs the `make` target.)
2. **Unit tests.** `make test` — full unit suite (one pytest at a time; the `check-pytest-lock.sh` hook will enforce).
3. **Quality gates.** `make mypy && make ruff-check && make ruff-format`.
4. **Rebuild gateway image on VPS and smoke against it.**
   ```bash
   make rebuild SERVICE=seshat-gateway   # local build + restart on this VPS
   make health
   uv run agent chat "hello" --new
   # Then against the VPS prod Postgres (port 5432):
   docker compose exec -T postgres psql -U agent -d personal_agent -c \
     "SELECT trace_id, session_id, model, cost_usd FROM api_costs ORDER BY id DESC LIMIT 3;"
   docker compose exec -T postgres psql -U agent -d personal_agent -c \
     "SELECT primary_model_at_creation, model_config_path FROM sessions ORDER BY created_at DESC LIMIT 1;"
   docker compose exec -T postgres psql -U agent -d personal_agent -c \
     "SELECT messages -> -1 FROM sessions ORDER BY last_active_at DESC LIMIT 1;"
   ```
   All three should show populated identity / attribution. The cost row should match the turn's `trace_id` and `session_id`. (`make rebuild` already runs against this VPS — no separate `make deploy` step needed for VPS-resident dev; the public/private split for deploys is only relevant when work originates on the laptop.)
5. **Post-deploy probes.** Run AC-5 / AC-6 / AC-7 SQL probes against the same VPS Postgres after one real chat turn through the PWA (so we exercise the real session-creation path, not just the CLI). Same `docker compose exec` pattern as step 4.
6. **Roll-back path.** Revert the PR, then run the inverse migration against the VPS Postgres:
   ```bash
   docker compose exec -T postgres psql -U agent -d personal_agent -c \
     "ALTER TABLE api_costs ALTER COLUMN trace_id DROP NOT NULL;
      ALTER TABLE api_costs DROP COLUMN session_id;
      ALTER TABLE sessions DROP COLUMN primary_model_at_creation;
      ALTER TABLE sessions DROP COLUMN model_config_path;"
   ```

---

## Out of scope (explicitly deferred to later phases)

- **Phase 2:** `LocalLLMClient` event-shape equalization, shared `ModelClientTelemetry` mixin.
- **Phase 3:** Audit of every `log.*` / `bus.publish` / Cypher write to thread `(session_id, trace_id, span_id, parent_span_id)`. I5 properties on `:Turn` / `:Entity` / `:Relationship`.
- **Phase 4:** `TraceContext` non-optional on internal APIs; `SystemTraceContext` factory.
- **Phase 5:** `scripts/check_identity_threaded.py` pre-commit, `tests/contracts/test_identity_threaded.py` AST audit, `scripts/monitors/joinability_probe.py` + 7-day green requirement.
- **Backfilling history:** explicitly out of scope per ADR-0074 §Consequences.

---

## Risk notes

- **Existing `LiteLLMClient.completion()` swallows the cost-record failure** (lines 398-399). With Phase 1, a missing identity tuple raises *inside* `record_api_call`, hits that `except`, and the chat turn keeps working but no cost row is written. The new `log.error("cost_record_missing_identity", …)` makes this visible without blowing up the request. Phase 4 closes the upstream so the missing-identity case becomes unreachable.
- **`session_id` is `NULL`-able on `api_costs` in this phase** — by design. Flipping to `NOT NULL` requires all in-flight callers to have shipped; that's a follow-up after Phase 2 confirms the local-LLM path also threads identity. Marked clearly in the migration's inline comment.
- **`exceptions.py` is a new module.** CLAUDE.md already tells contributors to use `from personal_agent.exceptions import …`, but the module doesn't actually exist; Phase 1 creates it. Keep it minimal: two errors, no base class hierarchy. (Existing scattered `*Error` classes in `tools/executor.py`, `brainstem/mode_manager.py`, `llm_client/types.py`, `config/loader.py` are not migrated in Phase 1 — separate cleanup ticket.)
- **`sessions.model_config_path` and `primary_model_at_creation` are NULL-able for historical rows** — required behavior to avoid breaking reads of the existing ~thousands of sessions. New sessions populate them; backfill out of scope.
