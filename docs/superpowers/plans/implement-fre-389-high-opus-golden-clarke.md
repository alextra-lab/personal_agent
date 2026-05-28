# FRE-389 — ADR-0076 Adaptive Constraint Governance Protocol (Phase 1)

**Linear:** FRE-389 (Approved, Tier-1:Opus) · **ADR:** `docs/architecture_decisions/ADR-0076-adaptive-constraint-governance.md`
**Branch base:** `main` (clean) · **Depends on:** FRE-388 ✅ (WS transport), FRE-392 ✅ (WS dedup) — both shipped.

## Context

Harness constraints (tool-iteration limit, context compression) currently fire **silently and unilaterally**: `force_synthesis_from_limit` injects a synthesis prompt with no user visibility, and 85% compression rewrites history without informing the user. A live run on 2026-05-27 showed the failure mode (retry loop, ~$0.13/call, degraded result, zero agency).

ADR-0076 Phase 1 replaces these with **user-visible, user-controllable pause points** delivered over the WS transport (ADR-0075): the executor emits a `CONSTRAINT_PAUSE` event, the PWA renders an inline `DecisionCard`, the user picks an action, and the executor resumes. Adds a Stop button, a persistent turn-status bar (live context/tools/cost), and a `user_constraint_preferences` table so repeat decisions are frictionless.

**Delivery decisions (this session):**
- **Two PRs, backend first.** PR1 = backend (events, transport race fix, executor wiring, migration, preferences endpoint, integration test). PR2 = frontend (DecisionCard, TurnStatusBar, Send→Stop, hook/types). Frontend depends on PR1's wire protocol being live.
- **Verification:** backend via pytest + a WS round-trip script against prod; **UI surfaces verified by the user on iPad/laptop** after deploy (VPS is headless).
- **Turn cost wired now:** `litellm_client.py:468` already returns `cost_usd` in the response dict — accumulate onto `ctx.turn_cost_usd`.

## Grounding (verified against current code)

| ADR claim | Reality | Plan impact |
|---|---|---|
| `force_synthesis_from_limit` SET `executor.py:2414`, READ `:1691` | ✅ exact | Wrap SET site in `_maybe_pause_for_constraint` |
| Hard compression `compress_in_place()` `executor.py:1429` (`needs_hard_compression`, 0.85 ratio `settings.py`) | ✅ exact | Pause before the `compress_in_place` call |
| `register_waiter` no-WS → `connection_lost` `ws_endpoint.py:101` | ✅ exact | No-WS fallback applies default |
| `_cancel_all_waiters` `ws_endpoint.py:167-179`; `_resolve_waiter` silently drops unknown `:148-164` | ✅ exact | Extend to clear `waiter_metadata` |
| Waiter race: push `transport.py:209` BEFORE register `:232` | ✅ confirmed | Fix register-before-push |
| `CONSTRAINT_DECISION` already in receiver match `ws_endpoint.py:499` | ✅ shipped (ADR-0075) | Add `USER_CANCEL`; add metadata validation |
| `_push_event(event, session_id)` module-level, persists + enqueues `transport.py:47` | ✅ | **Executor pushes by `session_id` — no transport object threading needed** |
| Executor has `ctx.session_id / trace_id / user_id` (`types.py:159,160,198`) | ✅ | All available; `user_id` non-None in prod (FRE-343 assert) |
| `tool_iteration_count` on ctx `types.py:181` | ✅ | Reuse for status bar |
| `turn_cost_usd` on ctx | ❌ missing | Add field; accumulate `response["cost_usd"]` |
| `send_state` exists `transport.py:139`; executor never emits STATE_DELTA | ⚠️ infra-only | Add module push of `StateUpdateEvent(key="turn_status")` |
| `waiter_metadata` on `_ConnectionState` | ❌ missing | Add field |
| `ConstraintPauseEvent/ResolvedEvent/CancelledEvent`, `ConstraintDecision` | ❌ missing | Add |
| migrations: highest = `0005`; `users` PK = `user_id UUID` (`init.sql`) | ✅ | New file `0006_…`; FK → `users(user_id)` |

**Note on ADR text:** ADR's example schema says `REFERENCES users(id)` and PK `user_id, constraint_name`. Real PK column is `users(user_id)` — use that. `tool_budget_warning_injected` is at `:1725` not `~1711` (cosmetic).

---

## PR1 — Backend

Branch: `starry-plaza-1s/fre-389-adr-0076-adaptive-constraint-governance-protocol` (Linear-suggested).

### Step 1 — New event types (`transport/events.py`)
Add three `@dataclass(frozen=True)` events mirroring `ToolApprovalRequestEvent` (carry `session_id` + `trace_id`):
- `ConstraintPauseEvent(request_id, session_id, trace_id, constraint, context, options: Sequence[str], default_option, expires_at)`
- `ConstraintResolvedEvent(request_id, session_id, constraint, action_id, resolution)`
- `CancelledEvent(session_id, trace_id, reason)`
Add all three to the `InternalEvent` union.

### Step 2 — Adapter mappings (`transport/agui/adapter.py`)
Add `match` cases producing wire envelopes exactly per ADR §"Wire protocol additions":
- `CONSTRAINT_PAUSE` → `{type, request_id, data:{constraint, context, options, default_option, expires_at}, session_id}`
- `CONSTRAINT_RESOLVED` → `{type, request_id, data:{constraint, action_id, resolution}, session_id}`
- `CANCELLED` → `{type, data:{reason}, session_id}`

### Step 3 — Action-ID registry (new `orchestrator/constraint_options.py`)
`ConstraintOption(action_id, label)` frozen dataclass + `CONSTRAINT_OPTIONS: dict[str, list[ConstraintOption]]` exactly per ADR §"Action ID registry" (`tool_iteration_limit`: `continue_10`/`finish_now`; `context_compression`: `compress_continue`/`stop_here`). **Last option = safe default.**

### Step 4 — WS endpoint: metadata + ConstraintDecision + USER_CANCEL (`transport/agui/ws_endpoint.py`)
- Add `WaiterMetadata(constraint, options, default_option, created_at)` dataclass.
- Add `waiter_metadata: dict[str, WaiterMetadata]` field to `_ConnectionState`.
- Add `ConstraintDecision(decision: str, remember: bool, request_id: str)` frozen dataclass (separate from `ApprovalDecision`).
- In `_cancel_all_waiters` and per-resolution cleanup: clear `waiter_metadata[request_id]`.
- `USER_CANCEL`: add to receiver match (`:449`+); set `conn.cancel_requested = True` (new bool field on `_ConnectionState`).
- On `CONSTRAINT_DECISION`: validate `msg["decision"]` against `waiter_metadata[request_id].options`; unknown → substitute `default_option` + `log.warning`. (Match arm at `:499` already routes to `_resolve_waiter` — add the validation before resolving.)

### Step 5 — Transport: race fix + constraint helper (`transport/agui/transport.py`)
- **Fix race in `request_tool_approval`:** register waiter **before** `_push_event` (currently push `:209` then register `:232`). Use a `register_waiter`-then-`_push_event` ordering.
- Add module-level `async def register_and_push_constraint(*, session_id, request_id, event, metadata, timeout_seconds) -> dict`: registers waiter + stores `metadata` on the connection (register-before-push), `_push_event(event, session_id)`, awaits resolution, returns the decision payload dict (`{decision, resolution, remember}`). When no active WS connection: returns `{resolution: "connection_lost", decision: default}` immediately **and does not persist the pause event** (per ADR §"No-active-WS fallback").

### Step 6 — Executor: pause/cancel/turn-status (`orchestrator/executor.py`)
- Add `turn_cost_usd: float = 0.0` to `ExecutionContext` (`types.py`) + `cancel_requested` checked via connection (read through a small helper keyed by `session_id`).
- **`_maybe_pause_for_constraint(...)` method** per ADR §"`_maybe_pause_for_constraint()`": load preference → if set & ≠ `always_pause` apply silently (structlog `constraint_preference_applied`, no event); else `register_and_push_constraint`; emit `ConstraintResolvedEvent`; upsert preference if `remember`. Returns `action_id`.
- **`_load_constraint_preference` / `_save_constraint_preference`** async helpers (new `service/repositories/constraint_preferences.py` repo, reused from the endpoint — see Step 8).
- **Wire tool-iteration site (`:2414`):** replace unconditional `ctx.force_synthesis_from_limit = True` with the pause; `continue_10` → extend limit by 10 (add `ctx.tool_iteration_bonus`), else force synthesis.
- **Wire compression site (`:1429`):** before `compress_in_place`, pause with `context_compression`; `stop_here` → force synthesis + return.
- **USER_CANCEL checkpoint:** between tool iterations (same point as `:2414` check) — if cancel flag set: resolve pending waiters with `user_cancel`, emit `ConstraintResolvedEvent` per waiter, force synthesis, emit `CancelledEvent`.
- **turn_status emission:** after each LLM call (near `:1906` where `usage` is read) and after each tool execution, push `StateUpdateEvent(key="turn_status", value={context_tokens, context_max, tool_iteration, tool_iteration_max, turn_cost_usd}, session_id)`. Accumulate `ctx.turn_cost_usd += response.get("cost_usd") or 0.0` at `:1906`.
- **Telemetry:** all structlog events from ADR §Telemetry table, each with `trace_id` + `session_id` (identity-threading rule).

### Step 7 — Migration (`docker/postgres/migrations/0006_constraint_preferences.sql`)
```sql
CREATE TABLE user_constraint_preferences (
    user_id           UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    constraint_name   TEXT NOT NULL,
    preferred_action  TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_session_id UUID,
    PRIMARY KEY (user_id, constraint_name)
);
```
(No Alembic — ordered SQL only, per project policy.) Add matching SQLAlchemy model in `service/models.py`.

### Step 8 — Preferences endpoint (`service/app.py`)
`PUT /api/v1/preferences/constraint` mirroring `POST /sessions` (`:1172`): `Depends(get_request_user)` + `Depends(get_db_session)`. Body `{constraint_name, preferred_action}`. Validate `preferred_action ∈ {"always_pause"} ∪ action_ids(CONSTRAINT_OPTIONS[constraint_name])` else 422. Upsert via `ConstraintPreferencesRepository.upsert(... source_session_id=None)` (`on_conflict_do_update` on PK).

### Step 9 — Tests (TDD, write first)
`tests/personal_agent/transport/test_constraint_governance.py` and `tests/personal_agent/orchestrator/test_constraint_pause.py`:
- **Race fixed (AC-14):** register+push then resolve within same loop tick → decision captured, not dropped.
- **Idempotent decision (AC-15):** duplicate `CONSTRAINT_DECISION` → resolved once.
- **Action-ID validation (AC-17):** invalid decision → default applied + warning.
- **No-WS fallback (AC-13):** no connection → default, no pause persisted, `constraint_no_ws_default_applied`.
- **Preference applied (AC-7):** stored `continue_10` → no pause emitted.
- **Timeout default (AC-5):** waiter times out → `resolution=timeout_default`, default action.
- One integration test (AC-1/3/4): register waiter → push pause → send `CONSTRAINT_DECISION` → assert executor branch (continue extends limit / finish forces synthesis).
- Substrate: use test ports (conftest redirects 7688/9201/5433); `make test-infra-up` if DB-touching.

### PR1 quality gates
`make test-file FILE=tests/personal_agent/transport/test_constraint_governance.py` → full `make test` → `make mypy` → `make ruff-check` + `make ruff-format` → `pre-commit run --all-files`.

---

## PR2 — Frontend (`seshat-pwa/`, after PR1 merged + deployed)

Next.js 15 + React 18 + Tailwind. Types in `src/lib/types.ts`; WS client `src/lib/agui-client.ts`; stream hook `src/hooks/useSSEStream.ts`.

### Step A — Types (`src/lib/types.ts`)
- Add `'USER_CANCEL'` to `ClientMessageType` (`CONSTRAINT_DECISION` already present).
- Add `'CONSTRAINT_PAUSE'`, `'CONSTRAINT_RESOLVED'`, `'CANCELLED'` to `AGUIEventType`.
- New `src/lib/constraint-options.ts`: `CONSTRAINT_ACTION_LABELS` (`action_id` → display label), mirroring backend registry.

### Step B — Stream hook (`src/hooks/useSSEStream.ts`)
- Extend `STATE_DELTA` handler (`:146`) to also handle `key === 'turn_status'` (object payload) → new `turnStatus` state.
- Add `CONSTRAINT_PAUSE` → set `pendingConstraint`; `CONSTRAINT_RESOLVED` → collapse matching card; `CANCELLED` → "Stopped by user" pill.
- Add `sendConstraintDecision(request_id, action_id, remember)` and `sendUserCancel()` using existing `streamRef.current.send(...)` (precedent: `handleApprovalDecision` `:305`). Seq dedup (`:88`) already handles reconnect replay.

### Step C — `DecisionCard.tsx` (new)
Inline bubble (precedent: `BudgetDeniedCard.tsx`; countdown precedent: `ApprovalModal.tsx:105`). Option buttons from `options` mapped via `CONSTRAINT_ACTION_LABELS`; countdown bar from `expires_at`; "Remember this choice" toggle (default off). On select → `sendConstraintDecision`; collapse to pill. On `CONSTRAINT_RESOLVED` (replay/timeout/cancel) → render collapsed. **Non-blocking** (unlike `ApprovalModal`).

### Step D — `TurnStatusBar.tsx` (new, replaces `ContextBudgetMeter.tsx`)
Visible during streaming. `ctx: 34K/128K …%` (amber 70%, red 85%), `tools: 12/25` (amber at max−2), `$0.42`. Fed by `turnStatus`. Delete `ContextBudgetMeter.tsx`; swap its render site `StreamingChat.tsx:224`.

### Step E — Send→Stop (`StreamingChat.tsx` + `ChatInput.tsx`)
While `isStreaming`: render Stop (square) → `sendUserCancel()`. Render `<DecisionCard>` inline in stream (`:237-261`); render "Stopped by user" pill on `CANCELLED`.

### PR2 quality gates
`npm run lint` + `npm run build` in `seshat-pwa/`. Service-worker convention: bump `CACHE_NAME` suffix (shell change) — network-first stays.

---

## Verification

**PR1 (automated + prod WS script):**
1. `make test` green; `make mypy`/`ruff` clean.
2. Deploy: `make build SERVICE=seshat-gateway` then `make deploy`. Confirm migration `0006` applied (`make shell SERVICE=postgres` → `\d user_constraint_preferences`).
3. `make health` (ENV=cloud).
4. WS round-trip script (no browser): open authed WS to prod session, send a query forcing 25+ tool calls, assert `CONSTRAINT_PAUSE` envelope arrives, send `CONSTRAINT_DECISION{continue_10}`, assert continuation; repeat with `finish_now`. Verify `session_events` rows (pause + resolved) and structlog events.
5. **Joinability probe** (touches new emit sites + new table): `scripts/monitors/joinability_probe.py` against prod — paste output (ADR-0074 §3.4).
6. `PUT /api/v1/preferences/constraint` curl → 200 + row present; invalid action → 422.

**PR2 (user-driven, iPad/laptop):** after deploy, user walks AC-1/2/3/4/8/9/10/11/12/16 in browser (DecisionCard render, Continue/Finish, status-bar colors, Stop, reconnect-collapsed). I provide the trigger prompts + a checklist.

## Acceptance criteria mapping
- Automated PR1: AC-5, 7, 13, 14, 15, 17 (+ backend half of 1/3/4 via integration test).
- Prod script PR1: AC-1, 3, 4, 16 (event arrival + executor branch).
- User browser PR2: AC-2, 8, 9, 10, 11, 12 (and visual confirm of 1/3/4/16).

## Out of scope (do not bundle — Phase 2 / other tickets)
- `timeout_expiring` constraint + heartbeat (Phase 2 ADR).
- Adaptive `max_tokens` (FRE-391, related).
- Settings UI for preferences; eval transport coverage (FRE-390).

## Post-merge (same session as each merge)
Deploy → live-verify → joinability probe (PR1) → update `docs/plans/MASTER_PLAN.md` on `main` → comment FRE-389 with PR links + deploy timestamp + evidence. **FRE-389 stays In Progress until PR2 ships** (multi-PR ticket — never mark Done after PR1 alone).
