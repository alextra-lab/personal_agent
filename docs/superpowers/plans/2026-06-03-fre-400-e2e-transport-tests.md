# FRE-400 — End-to-end testing for transport / UI / error paths

## Context

The features we keep shipping on the WebSocket transport (ADR-0075) and constraint-governance
protocol (ADR-0076) — constraint pause round-trips, live `turn_status`, Stop/cancel, classified
error cards, reconnect replay — are today validated **only by hand on a phone**. The eval harness
(`tests/evaluation/harness/runner.py`) drives the synchronous `POST /chat`, so it never touches the
transport layer at all. Result: slow, manual, easy-to-regress-silently coverage of exactly the paths
most prone to breaking (FRE-388 shipped **8 hotfixes** post-merge for this reason).

FRE-400 is the Approved tracking thread to close that gap with automated coverage at three layers
(backend WS, PWA components, browser e2e) plus a CI lane to run them.

### Two decisions settled with the owner before planning

1. **No prior "unify local/VPS dev setup" ticket is needed.** The topology was already audited and
   ratified by **FRE-214 (Done, PR #27)** — VPS is the canonical dev host, laptop mirrors. FRE-400's
   tests depend on **neither** the dormant local Mac stack **nor** the live VPS: backend tests use the
   isolated `docker-compose.test.yml` substrate (FRE-375, ports 5433/9201/7688) or in-memory mocks;
   PWA tests run in jsdom; e2e mocks the backend in-process. **GitHub Actions CI is the de-facto
   unification** — a neutral, reproducible runner that both environments invoke identically.
2. **FRE-400 subsumes FRE-390.** FRE-390 (Needs Approval, Low — "eval harness skips transport") is a
   strict subset of FRE-400 improvement #1. Its only distinct nuance is the **async streaming path**
   (`POST /chat/stream` fire-and-forget + WS, not sync `POST /chat`), which this plan honors with one
   integration-marked test. FRE-390 will be closed as subsumed (Master CC does the Linear close; noted
   in the PR).

Owner also confirmed: **include Playwright.** Correction captured during planning — Playwright runs
**headless on Linux natively** (no display server); it is the standard CI target and does **not**
require the Mac. The e2e layer mocks the backend entirely via `page.routeWebSocket()` + `page.route()`.

---

## Sequencing — 3 PRs (not one)

Delivered as sequenced PRs, each independently mergeable + CI-green, FRE-400 stays In Progress until
the last lands (multi-phase thread). CI is bootstrapped in PR1 and each PR adds its own job.

| PR | Contents | Model | Notes |
|---|---|---|---|
| **PR1** | WS1 backend harness (Tier-1 + Tier-2) + `.github/workflows/ci.yml` with `backend-unit` + `backend-integration` jobs | Sonnet → **escalate WS1 to Opus** if loop/TestClient semantics stall (3 attempts) | Riskiest; do first. Note FRE-390 subsumption in PR body. |
| **PR2** | WS2 PWA Vitest suites + `make test-pwa` + `pwa-unit` CI job | Sonnet | Mechanical from plan (pattern: `TurnRating.test.tsx`). |
| **PR3** | WS3 Playwright e2e + `make test-pwa-e2e` + `pwa-e2e` CI job | Sonnet | New infra, standard pattern. |

## Scope — four workstreams (mapped to PRs above)

### WS1 — Backend WS integration harness (Python)

The transport is cleanly decoupled: events flow `emit_*() → _push_event() → SessionEventBuffer.append
(Postgres) + asyncio.Queue → _sender → socket`. The waiter registry and queue are **module-level dicts
bound to the app event loop**, so events must be produced *inside* the app loop (cross-thread injection
from a sync test is unsafe). Two tiers resolve this:

**Tier 1 — unit, runs under `make test` (no Postgres, no LLM):**
New helper `tests/personal_agent/transport/ws_harness.py`:
- `build_ws_test_app()` — minimal `FastAPI()` mounting `ws_router` (from
  `personal_agent.transport.agui.ws_endpoint`) **plus a test-only `/__test/emit` router** whose handlers
  call the real transport emit functions (`emit_turn_status`, `register_and_push_constraint`,
  `emit_classified_error`, `emit_cancelled`, `AGUITransport().send_text_delta`, …) so they execute on
  the app loop.
- Fixtures/overrides: `settings.gateway_auth_enabled = False`; monkeypatch `SessionRepository.get` to
  return a stub session; monkeypatch `SessionEventBuffer` (in both `transport.py` and `ws_endpoint.py`
  import sites) + `AsyncSessionLocal` to an in-memory `FakeSessionEventBuffer` implementing
  `append`/`replay`/`oldest_available_seq` with the real seq semantics.
- `ws_connect(client, session_id)` — context manager wrapping `TestClient.websocket_connect` that
  performs the mandatory `CONNECT` handshake and yields the socket.

Tests (`tests/personal_agent/transport/test_ws_integration.py`):
- Event delivery: inject TEXT_DELTA×N → assert order, monotonic `seq`, `DONE` sentinel.
- Constraint round-trip: trigger pause (background `register_and_push_constraint`) → receive
  `CONSTRAINT_PAUSE` → `ws.send_json({type:"CONSTRAINT_DECISION", request_id, decision, remember})` →
  assert waiter resolves + `CONSTRAINT_RESOLVED` with `resolution="user_choice"`.
- Invalid decision substituted with default option; timeout → `timeout_default`.
- Stop: `USER_CANCEL` → pending waiters resolve `user_cancel` + `CANCELLED` event.
- `turn_status` STATE_DELTA payload shape.
- `RUN_ERROR` categories incl. `budget_denied` (via `emit_classified_error`).
- Reconnect: reconnect with `last_seq=k` → replay events `> k`; stale `last_seq` → `REPLAY_GAP`.
- Hardening: second connection evicts first with code `4001`; oversized message → `1008`; rate-limit
  breach → `1008`; missing/invalid `CONNECT` → `1008`.

**Tier 2 — integration, CI substrate job only (`@pytest.mark.integration`, not in `make test`):**
`tests/integration/test_transport_stream_e2e.py` — real `docker-compose.test.yml` Postgres, real
`SessionEventBuffer`. Open WS → `POST /chat/stream` (fire-and-forget) with the **LLM client mocked**
to emit deterministic deltas/tool calls → assert the async event sequence + replay across a
disconnect. This is FRE-390's exact "done" definition (async timing/concurrency path).

### WS2 — PWA component & hook tests (Vitest + Testing Library — existing stack)

New files under `seshat-pwa/src/__tests__/` (mirror the existing `TurnRating.test.tsx` pattern; mock
`@/lib/agui-client` and `@/lib/submitTurnRating` as it already does):
- `DecisionCard.test.tsx` — title/context/options render; first option = primary; click →
  `onDecide(actionId, remember)`; decide-once guard (`decidedRef`); remember checkbox toggles.
- `TurnStatusBar.test.tsx` — `null` → renders nothing; ctx amber ≥70 / red ≥85; tools amber at
  `max−2`; `formatTokens` + cost formatting.
- `ClassifiedErrorCard.test.tsx` — category/reason/next_step render; `retry`/`switch_to_cloud`/`stop`
  actions fire the right callbacks.
- `ChatInput.test.tsx` — Send↔Stop swap on `isStreaming` (Stop `aria-label="Stop generating"` →
  `onStop`); textarea stays writable while streaming (FRE-421); Send gated on
  `pendingInterrupt`/`pendingApproval` and inference `down`.
- `useSSEStream.test.tsx` — mock `connectWebSocket`, feed crafted `AGUIEvent`s (TEXT_DELTA, STATE_DELTA
  `turn_status`, CONSTRAINT_PAUSE/RESOLVED, CANCELLED, RUN_ERROR, DONE) → assert state transitions and
  outbound `send` payloads (`CONSTRAINT_DECISION`, `USER_CANCEL`).

Add `make test-pwa` → `cd seshat-pwa && npm ci && npx vitest run`.

### WS3 — Playwright browser e2e (net-new, headless, backend fully mocked)

- Add `@playwright/test` devDep + `seshat-pwa/playwright.config.ts` with a `webServer` block that
  builds + serves the PWA (`next build && next start -p 3100`) against a dummy `NEXT_PUBLIC_SESHAT_URL`
  and empty `NEXT_PUBLIC_GATEWAY_TOKEN` (so no ticket fetch; WS URL carries no ticket param).
- `seshat-pwa/e2e/*.spec.ts` — each test stubs `/api/v1/sessions*`, `/chat/stream` via `page.route()`
  and mocks the `**/ws/**` socket via `page.routeWebSocket()`, then pushes crafted server frames:
  - Constraint pause → `DecisionCard` renders → click option → assert outbound `CONSTRAINT_DECISION`
    frame → push `CONSTRAINT_RESOLVED` → card collapses to pill.
  - Send→Stop → assert `USER_CANCEL` frame → push `CANCELLED`.
  - `turn_status` STATE_DELTA → status bar thresholds render.
  - `RUN_ERROR` → `ClassifiedErrorCard` → `retry` re-issues `/chat/stream`.
- Add `npm run test:e2e` + `make test-pwa-e2e`. (Fallback if `routeWebSocket` proves too limiting: a
  tiny mock WS server started via Playwright `webServer`.)

### WS4 — GitHub Actions CI (repo's first workflow) — improvement #4

New `.github/workflows/ci.yml`, triggers `push` + `pull_request`. Jobs:
- `backend-unit` — `uv sync` → `make test` (includes WS1 Tier-1).
- `backend-integration` — Postgres service container / `docker-compose.test.yml` →
  `pytest -m integration -k transport` (WS1 Tier-2 only, to bound runtime).
- `pwa-unit` — node → `npm ci` → `vitest run`.
- `pwa-e2e` — node → `npx playwright install --with-deps chromium` → `playwright test`.

**CI vs on-demand split (documented in the workflow + `docs/`):** unit + component + mocked-e2e run on
every PR; the **LLM eval harness stays on-demand** (`make eval`, 100+ calls); heavy substrate
integration is its own job. **Risk to flag in the PR:** this is the first time `make test` runs in CI —
it may surface latent collection failures (e.g. FRE-186 `mcp` import chain). First implementation step
is to confirm the suite is green in a clean checkout and scope the lane if not.

---

## Critical files

| Area | Path | Action |
|---|---|---|
| WS1 helper | `tests/personal_agent/transport/ws_harness.py` | new — app builder + `FakeSessionEventBuffer` + `ws_connect` |
| WS1 tests | `tests/personal_agent/transport/test_ws_integration.py` | new |
| WS1 Tier-2 | `tests/integration/test_transport_stream_e2e.py` | new (`integration` marker) |
| Reuse | `src/personal_agent/transport/agui/{ws_endpoint,transport,event_buffer,adapter}.py` | read-only — emit fns, `ws_router`, seq semantics |
| WS2 | `seshat-pwa/src/__tests__/{DecisionCard,TurnStatusBar,ClassifiedErrorCard,ChatInput,useSSEStream}.test.tsx` | new (pattern: existing `TurnRating.test.tsx`) |
| WS3 | `seshat-pwa/playwright.config.ts`, `seshat-pwa/e2e/*.spec.ts`, `seshat-pwa/package.json` | new + devDep |
| WS4 | `.github/workflows/ci.yml` | new |
| Targets | `Makefile` | add `test-pwa`, `test-pwa-e2e` |
| Plan of record | `docs/superpowers/plans/2026-06-03-fre-400-e2e-transport-tests.md` | copy this plan (project convention) |

Standards: type hints + Google docstrings on public test helpers; no `os.getenv`/`print`/bare `except`;
one pytest process at a time (lock hook).

---

## Acceptance Criteria

**Pre-merge (local, this session):**
- [ ] `make test` green incl. WS1 Tier-1 (no Postgres/LLM required).
- [ ] `make test-pwa` green (all WS2 suites).
- [ ] `make test-pwa-e2e` green locally/headless (WS3).
- [ ] `make mypy` + `make ruff-check` + `make ruff-format` clean on new Python.
- [ ] WS1 covers: delivery/seq/DONE, constraint round-trip (+invalid/timeout), USER_CANCEL,
      turn_status, RUN_ERROR(+budget_denied), reconnect replay, REPLAY_GAP, eviction 4001,
      rate-limit/oversize/CONNECT 1008.
- [ ] WS2 covers DecisionCard, TurnStatusBar, ClassifiedErrorCard, ChatInput Send↔Stop, useSSEStream
      dispatch + outbound payloads.
- [ ] WS3 covers constraint pause, Send→Stop, turn_status, RUN_ERROR→retry.

**CI gate (on the PR):**
- [ ] All four `ci.yml` jobs pass on the PR; Tier-2 substrate job green against test Postgres.
- [ ] Workflow documents CI-vs-on-demand split (eval harness excluded).

**Follow-up / owner (Master CC):**
- [ ] Close FRE-390 as subsumed by FRE-400 (noted in PR body).
- [ ] Update `MASTER_PLAN.md` (Active Design Threads row + Last updated).
- [ ] Optional follow-up ticket: browser-e2e against the *real* gateway (vs mocked) once a CI substrate
      profile is desired.

---

## Verification (end-to-end)

```bash
# Backend unit (agent-safe)
make test                          # WS1 Tier-1 included
make test-file FILE=tests/personal_agent/transport/test_ws_integration.py

# Backend integration (needs test substrate)
make test-infra-up
PERSONAL_AGENT_INTEGRATION=1 uv run pytest -m integration -k transport
make test-infra-down

# PWA
make test-pwa                      # vitest
make test-pwa-e2e                  # playwright (headless chromium)

# Quality
make mypy && make ruff-check && make ruff-format

# CI: open the PR → confirm all four ci.yml jobs green
```

Build worktree implements + pushes the PR only; Master CC reviews, merges, deploys, closes FRE-390,
and updates MASTER_PLAN (per workspace policy).
