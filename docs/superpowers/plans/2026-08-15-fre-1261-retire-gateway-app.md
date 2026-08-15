# FRE-1261 — Retire the standalone gateway app and gateway/chat_api.py

**Ticket:** https://linear.app/frenchforest/issue/FRE-1261
**Backing context:** FRE-1260 (scope-correction investigation), FRE-1262 (SDK-confinement guard widening, merged ahead of this ticket — commit f6d762b7)

## Scope recap

Delete the standalone gateway app (`create_gateway_app()`, module-level `gateway_app`) and its
only-mounted-there chat module (`gateway/chat_api.py`), which called the Anthropic SDK directly.
The router factory (`create_gateway_router()`) and everything it composes — mounted on the main
service app in local mode, serving live `/api/v1/*` paths — is explicitly NOT in scope. The
WebSocket transport router (`transport/agui/ws_endpoint.py`) is NOT in scope; it survives via the
identical mount in `service/app.py`.

## Investigation findings (this session)

- `_gateway_lifespan()` and its imports (`asyncio`, `asynccontextmanager`, `AsyncGenerator`,
  `FastAPI` [as the lifespan's own type — `Request`/`APIRouter` stay], `get_settings`,
  `RequestRootSpanMiddleware`) are used **only** by `create_gateway_app()`. Once that function is
  deleted, `_gateway_lifespan` has zero callers anywhere in `src/` — it becomes exactly the kind of
  orphan CLAUDE.md says to remove. Confirmed via grep: its only non-test references are inside
  `gateway/app.py` itself.
- `_KnowledgeGraphAdapter` is **not** an orphan — `service/app.py:1342` imports it directly for the
  local-mount wiring path, and `tests/personal_agent/gateway/test_knowledge_api.py` /
  `test_session_api.py` exercise it independently of chat/gateway_app. Stays untouched.
- Five test files are wholly and only about the code being deleted (verified by reading each in
  full — no shared coverage of surviving code):
  - `tests/personal_agent/gateway/test_chat_api.py`
  - `tests/personal_agent/gateway/test_chat_api_records_cost.py`
  - `tests/personal_agent/gateway/test_chat_trace_identity.py` (the **gateway** one — do NOT touch
    `tests/personal_agent/service/test_chat_trace_identity.py`, a different file covering the
    service app's own `/chat` endpoint, FRE-1215, unrelated to this deletion)
  - `tests/personal_agent/gateway/test_gateway_otel_bootstrap.py` (entirely drives
    `_gateway_lifespan`)
  - `tests/personal_agent/gateway/test_gateway_lifespan_es.py` (entirely drives
    `_gateway_lifespan`)
- The three ticket-named stale comments:
  1. `Dockerfile.gateway:1-8` header — says it depends on `create_gateway_app`; its own `CMD` runs
     `service.app:app`. Fix the header.
  2. `service/app.py` comment above the `gateway_mount_local` block — "In production, run
     personal_agent.gateway.app:gateway_app on its own port" — false since ADR-0044/FRE-207. Fix.
  3. The `/stream/* — AG-UI SSE` comment on `create_gateway_app()`'s
     `app.include_router(transport_router)` line — resolved automatically by deleting the whole
     function (no separate edit needed; noting it in the PR per the ticket's own instruction that
     it "survives this ticket, since the router does" — the *router* survives via
     `service/app.py`'s own correctly-worded WebSocket comment, already correct, untouched).
- `gateway/app.py`'s own module docstring (lines 1-18) and `gateway/__init__.py`'s docstring both
  describe the "standalone" deployment mode via `create_gateway_app()` — both need updating since
  they're live module docs, not historical prose.
- AC-4 explicitly scopes the "no live reference" search to `docs/` too, carving out only ADRs and
  closed-ticket prose as exempt. Grepped every `chat_api`/`gateway_app`/`create_gateway_app` hit
  under `docs/`: `docs/architecture_decisions/*`, `docs/superpowers/plans/*`,
  `docs/plans/sessions/*`, `docs/research/*` are historical — exempt. Three are NOT exempt and
  reference the file as live:
  - `docs/specs/PROMPT_MANAGEMENT_SPEC.md` — `gateway_persona` row in the leaf-prompt taxonomy
    (§1.1), `gateway.chat` callsite (§2.1), an entire "Gateway Telemetry Coverage" design section
    (§5) proposing to instrument the file being deleted, and two AC rows in §10 (P1, P2) that name
    `gateway.chat`/`gateway/chat_api.py`.
  - `docs/specs/CONVERSATION_CONTINUITY_SPEC.md` — one status-line clause describing the "cloud
    gateway path" as implemented via `gateway/chat_api.py`.
  - `docs/reference/PROMPT_CORPUS.md` — **generated** by `scripts/render_prompt_corpus.py`, which
    has a `ConstantEntry("gateway_persona", SRC_ROOT / "gateway" / "chat_api.py", "_SYSTEM_PROMPT", ...)`
    that would crash the renderer (FileNotFoundError) once the file is gone. This is a file "built
    from" per AC-4's own text — fix the generator, regenerate the doc.
  - `docs/guides/SCHEMA_REFERENCE.md` mentions `"gateway.chat_api"` as one *historical value* of a
    persisted `metadata.source` JSONB field — this documents real data already written to
    production Postgres rows, not a code reference. Left alone; not a "live reference to the
    removed symbol."
- The SDK-confinement guard (`tests/observability/topology/test_ci_teeth.py`) holds
  `_ALLOWED_SDK_IMPORTERS` keyed by `(path, sdk)`. Only
  `("gateway/chat_api.py", "anthropic")` is removed; `("memory/embeddings.py", "openai")` stays —
  confirmed it waives a real, unrelated, live openai import (managed-embedder path). Removing
  chat_api.py's entry does not add a new offender because the file itself is deleted.
- `anthropic>=0.18.0` in `pyproject.toml` (with its explanatory comment) is the only production
  dependency to drop; grepped `src/` for any other `import anthropic` — zero hits. `uv.lock` needs
  regenerating after the `pyproject.toml` edit.
- AC-3 route census (captured now, as the BEFORE baseline — re-run identically after the change,
  expect a byte-identical result since `create_gateway_router()` itself is never touched):
  - `create_gateway_router()` alone: **21** distinct (path, method) combinations across the 8
    sub-routers (knowledge, session, config, observation, route_trace, sub_agent_capture,
    feedback, health).
  - Full `service.app.app` (gateway router mounted + everything else): **51** total route combos,
    of which **28** start with `/api/v1/`, plus the one WebSocket route `/ws/{session_id}`.

## Implementation steps

1. **RED anchor for AC-5.** In `tests/observability/topology/test_ci_teeth.py`, remove the
   `("gateway/chat_api.py", "anthropic")` entry (and its docstring reason) from
   `_ALLOWED_SDK_IMPORTERS`, leaving the `("memory/embeddings.py", "openai")` entry untouched. Run
   `make test-file FILE=tests/observability/topology/test_ci_teeth.py -k test_model_sdk_confined_to_llm_client`
   → confirm it now **fails** (chat_api.py still imports anthropic, now unlisted).

2. **GREEN — delete the module and its dedicated tests.**
   - `rm src/personal_agent/gateway/chat_api.py`
   - `rm tests/personal_agent/gateway/test_chat_api.py tests/personal_agent/gateway/test_chat_api_records_cost.py tests/personal_agent/gateway/test_chat_trace_identity.py tests/personal_agent/gateway/test_gateway_otel_bootstrap.py tests/personal_agent/gateway/test_gateway_lifespan_es.py`
   - Re-run the guard test from step 1 → confirm it now **passes**.

3. **Delete the standalone app factory in `src/personal_agent/gateway/app.py`.**
   - Remove `create_gateway_app()` (the function body) and the trailing
     `gateway_app = create_gateway_app()` module-level call + its banner comment.
   - Remove `_gateway_lifespan()` in full (now a zero-caller orphan).
   - Remove now-unused imports: `asyncio`, `asynccontextmanager`, `AsyncGenerator` (keep `Any`),
     `FastAPI`, `get_settings`, `RequestRootSpanMiddleware`, the `chat_api` import, the
     `transport.agui.ws_endpoint` import. Keep `APIRouter`, `Request`, `Sequence`, `Any`,
     `SessionDigestView`, `get_logger` — all still used by `create_gateway_router`,
     `_health_router`, and `_KnowledgeGraphAdapter`.
   - Rewrite the module docstring (lines 1-18) to describe only the router-factory /
     local-mount deployment mode; drop the standalone-uvicorn example.

4. **Fix the two remaining stale comments + one stale docstring.**
   - `Dockerfile.gateway` lines 1-8: rewrite the header note to state the image runs the full
     service app (matching its own `CMD`), not the retired standalone factory.
   - `src/personal_agent/service/app.py`: fix the comment above the `gateway_mount_local` include
     block — remove the false "In production, run ...gateway_app on its own port" claim.
   - `src/personal_agent/gateway/__init__.py`: rewrite the docstring's "Standalone" bullet, which
     describes `create_gateway_app()` as a live second deployment mode.

5. **`pyproject.toml`**: remove the `anthropic>=0.18.0` line and its preceding explanatory comment
   from `dependencies`. Run `uv lock` to regenerate `uv.lock` (dependency-only change, no source
   edits needed there).

6. **Docs.**
   - `scripts/render_prompt_corpus.py`: remove the `ConstantEntry("gateway_persona", ...)` list
     item pointing at `gateway/chat_api.py`.
   - Regenerate: `uv run python scripts/render_prompt_corpus.py` (or `make render-prompt-corpus`)
     to refresh `docs/reference/PROMPT_CORPUS.md` without the gateway_persona entry.
   - `docs/specs/PROMPT_MANAGEMENT_SPEC.md`: remove the `gateway_persona` row from §1.1 (13 → 12
     leaf prompts; update the §10 P0 AC row's count accordingly), remove the `gateway.chat` row
     from §2.1, replace §5's body with a short "moot — module retired by FRE-1261" note (keep the
     section heading/number so nothing downstream renumbers), and fix the two §10 AC rows (P1
     pre-merge "routed through canonical telemetry emit", P1/P2 post-deploy rows naming
     `gateway.chat`) to no longer assert work against a deleted file.
   - `docs/specs/CONVERSATION_CONTINUITY_SPEC.md`: trim the status line's "cloud gateway path"
     clause to note it was retired by FRE-1261, keeping the still-accurate orchestrator-path
     description intact.

7. **Quality gates.** `make test` (module: `tests/personal_agent/gateway/`,
   `tests/observability/topology/test_ci_teeth.py`, then full suite) · `make mypy` ·
   `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

8. **AC-3 proof — re-run the census script from the investigation** against the post-change tree;
   confirm both counts (21 router-only combos, 51 total / 28 `/api/v1/*` on the full app, plus the
   one WS route) are byte-identical to the BEFORE baseline above.

9. **AC-1/AC-2/AC-4 proof** — grep-based, recorded in the ticket handoff:
   - `grep -rn "^import anthropic\|^from anthropic" src/` → zero hits.
   - `grep -n anthropic pyproject.toml` → zero hits in `dependencies`.
   - `python -c "from personal_agent.gateway import app; app.create_gateway_app"` → `AttributeError`.
   - `grep -rn "create_gateway_app\|gateway_app\b\|gateway\.chat_api" src/ tests/ docs/ Dockerfile* docker-compose*.yml` → only historical docs remain (ADRs, `docs/superpowers/plans/`, `docs/plans/sessions/`, `docs/research/`), plus the one legitimate historical-data mention in `SCHEMA_REFERENCE.md`.

## Codex plan review — findings incorporated

Codex reviewed this plan read-only and found real gaps, all verified against source before
accepting:

- **`get_logger`/`log` are also orphaned** — every `log.*` call in `gateway/app.py` (15 of them,
  grepped) lives inside `_gateway_lifespan`. `create_gateway_router`, `_health_router`, and
  `_KnowledgeGraphAdapter` never log. Add `get_logger` and the module-level `log = get_logger(__name__)`
  to the removed-imports list.
- **`PRE_BOOTSTRAP_LOGGERS` (`telemetry/otel_bootstrap.py:37-42`) carries a `"personal_agent.gateway.app"`
  entry whose own comment explains it exists *because* `_gateway_lifespan` logs before its own
  `configure_tracing()` call.** Once that lifespan is gone, the module never logs pre-bootstrap at
  all (confirmed: no `log.*` calls survive outside it). Remove the entry and its explanatory bullet.
  Verified safe: `tests/personal_agent/service/test_pre_bootstrap_loggers.py` never asserts this
  specific entry's membership (only `personal_agent.config.settings` and `uvicorn.error`), so the
  removal breaks nothing there.
- **Two more stale-but-live spots found by direct inspection, not just the ticket's named three:**
  - `gateway/app.py:79` section banner — "Router factory (shared between local-mount and standalone
    modes)" — fix to drop "standalone".
  - Root `CLAUDE.md:131` (checked-in project doc) module-map row: "`gateway/` | Seshat API Gateway —
    standalone FastAPI app over storage only (Neo4j, Postgres, ES); mountable as a router in local
    mode or run standalone on :9001" — the ":9001 standalone" clause is now false. Fix.
- **PROMPT_MANAGEMENT_SPEC.md §10 has three affected AC rows, not two** — line 371 (P1 pre-merge:
  "`gateway/chat_api.py` routed through canonical telemetry emit"), line 375 (P1 post-deploy:
  `prompt_callsite = "gateway.chat"` events), line 385 (P2 post-deploy: breakdown including
  `gateway.chat`). All three get fixed, not two.
- **Explicitly judged out of scope, with reasoning** (Codex flagged these as candidates; verified
  each and declined):
  - `telemetry/logger.py:300` and `observability/route_trace/ledger.py:117` — both are historical
    rationale comments explaining why an idempotency guard exists ("FRE-1056 adds a second call
    site, the standalone gateway lifespan"), attached to guard code that stays correct and useful
    regardless of whether that second caller still exists. Neither names the deleted symbols or
    asserts a current architectural fact that becomes false. Left alone — editing them would be
    unrequested cleanup of adjacent code (CLAUDE.md § Surgical Changes).
  - `telemetry/AGENTS.md`'s FRE-1051 section — a dated (2026-07-23..28) incident writeup that was
    *already* stale before this ticket (it describes the pre-FRE-1056 gap, when the gateway lifespan
    "never attached" its ES handler — FRE-1056 fixed that months before this ticket). This ticket's
    deletion doesn't newly break anything here; the doc was already describing a superseded state.
    Out of scope — flagged in the ticket handoff as a pre-existing doc-drift observation, not fixed
    here.
  - ADR-0078's `gateway/chat_api.py:39` mention — an ADR, exempt as a historical record per the
    ticket's own carve-out regardless of `Status: Proposed`.
- Codex's challenge to the plan's "CLAUDE.md orphan-cleanup convention" citation: that wording lives
  in the user's global `~/.claude/CLAUDE.md` (§ Surgical Changes: "Remove imports/variables/functions
  that YOUR changes made unused"), not the project's checked-in CLAUDE.md files — Codex only checked
  the latter. The convention is real; the deletion is justified primarily on its own merits anyway
  (zero remaining callers, confirmed by grep), not by citation.
- Confirmed no dynamic/string-based factory reference anywhere (Makefile, docker-compose, CI,
  scripts) to `gateway.app:gateway_app` or `create_gateway_app` — the only such reference was
  `Dockerfile.gateway`'s own comment (already in scope as stale comment #1), not its `CMD`.

## Risk tier

**Standard** — touches `src/` production code (deleting a mounted-nowhere-but-still-live-code-path
app factory), removes a top-level dependency, and edits a security/governance-adjacent guard
(`test_ci_teeth.py`'s SDK-confinement allowlist). Codex plan-review required before implementation
per the build skill.
