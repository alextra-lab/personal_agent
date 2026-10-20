# FRE-1623 — Seshat effort selector in the PWA — implementation plan

**Ticket:** FRE-1623 (blocked by FRE-1622, merged as PR #1309). **ADR:** ADR-0157 D2 and D5, with ADR-0121 §4.
**Tier:** Standard (touches `src/` gateway code and the PWA). Codex plan review required.

## Scope decision (owner, this session)

The ticket assumes that `GET /{session_id}/config` returns the mode for each level. It does not.
FRE-1622 added only `seshat_effort` and `effort_provenance`. The owner chose to fold a small
additive backend field into this PR. The deploy class becomes a `seshat-gateway` rebuild plus a
PWA rebuild. Master decides that class at the gate.

## Design

### Backend (additive, two response fields)

- `_deployment_view` gains `modes: list[str]`, the names of the modes that the deployment declares.
- Both config responses (`GET /{session_id}/config` and `GET /config`) gain
  `effort_levels: {fast, standard, deep}`. Each value is the mode name that the level requests
  (`EffortSpec.primary_mode`) or `null`. It comes from `get_seshat_effort()`.
- The PWA computes the label for the selected model: if the level requests mode `m` and the
  selected deployment's `modes` contains `m`, the label is `m`. Otherwise it is `model default`.
- Why two fields and not one `effort_modes` map for the resolved model: a new session can hold a
  picker choice that is not stored yet. The PWA then knows the model, and the server does not.

### PWA

- `lib/types.ts`: `SeshatEffort` type; optional `modes` on `DeploymentView`; optional
  `effort_levels`, `seshat_effort`, `effort_provenance` on `SessionConfig`; optional
  `seshat_effort` on `SessionSummary` (in `agui-client.ts`).
- `lib/agui-client.ts`: `setSessionEffort(sessionId, level)` sends
  `PATCH /api/v1/sessions/{id}/effort` with `{seshat_effort}`. `SendMessageOptions.seshatEffort`
  adds the form field `seshat_effort`.
- `lib/effort.ts` (new, pure): `EFFORT_LEVELS`, `effortModeLabel(level, effortLevels, deployment)`.
- `hooks/useSeshatEffort.ts` (new): holds the level that the selector shows.
  - `level` is `null` until the session GET answers. `sendLevel = level ?? 'standard'`.
  - `hydrate(level)`: the server's level (session GET, `STATE_DELTA`). Sets shown and stored.
  - `change(level)`: sets the shown level, sends one `PATCH`. On success, stored = level.
    On 404 (no session row yet), keep the level: the first chat request carries it and the server
    stores it (ADR-0157 D2 rule 2). On any other failure, return to the stored level and set `error`.
  - A write counter makes a late answer from an older write a no-op.
  - A session change resets the hook (the component instance is reused across sessions).
- `useAgentStream`: a `session_effort` `STATE_DELTA` sets `serverEffort` (same pattern as
  `serverSelection`). `sendMessage` takes `seshatEffort` and passes it to `sendChatMessage`.
- `useSessionConfig`: also returns `effortLevels`.
- `components/EffortSelector.tsx` (new): a button and a listbox like `ModelPicker`. Each option
  shows the level and its mode label. An error shows in a `role="alert"` line.
- `ChatInput`: renders `EffortSelector` next to `ModelPicker`, through new props.
- `StreamingChat`: wires the hook, the `STATE_DELTA`, the session GET hydration and the send.

## Acceptance criteria → tests

| AC | Test |
|---|---|
| AC-1 hydration | `useSeshatEffort.test.tsx`: session GET `deep` → shows `deep`. `STATE_DELTA` `fast` → shows `fast`. A localStorage value never wins. |
| AC-2 write/revert | `useSeshatEffort.test.tsx`: one `PATCH` per change. A rejected `PATCH` → shows the stored level and sets `error`. `EffortSelector.test.tsx`: the error shows. |
| AC-3 model default | `effort.test.ts` and `EffortSelector.test.tsx`: model declares `fast`, no `deep` → Fast shows `fast`, Deep shows `model default`. |
| AC-4 sending | `StreamingChat.effort.test.tsx`: new session → first request carries `standard`. Existing session at `deep` → request carries `deep`. `agui-client.effort.test.ts`: the form field. |
| AC-5 lint | `cd seshat-pwa && npm run lint` exits 0. |
| Backend | `tests/personal_agent/gateway/test_session_effort_api.py`: both config responses carry `effort_levels`; the views carry `modes`. |

## Steps

1. Backend test first (fails), then `_deployment_view` and the two responses. Run
   `make test-file FILE=tests/personal_agent/gateway/test_session_effort_api.py` and
   `tests/personal_agent/gateway/test_session_api.py`.
2. PWA: types, client functions, `effort.ts` with its test.
3. `useSeshatEffort` with its test (fail first).
4. `EffortSelector` with its test.
5. Wire `useAgentStream`, `useSessionConfig`, `ChatInput`, `StreamingChat`. Add the send test.
6. Gates: `cd seshat-pwa && npm test && npm run lint && npm run typecheck`, then `make test`,
   `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
7. Commit, then the self-review agents on `git diff origin/main...HEAD`.

## Codex plan review (3 blocking, 2 non-blocking) — all applied

1. **Send before hydration** carried `standard` for a Deep session. The hook now has `ready`
   (false while the session GET is pending). `StreamingChat` holds Send until it is true.
   A failed GET also sets `ready`, so a network fault never blocks Send for good.
   Test: `StreamingChat.effort.test.tsx` "holds Send until the session GET answers".
2. **PATCH 404 is ambiguous** (no row, or a row of another user). The hook keeps the chosen level
   on a 404 only when the session GET found no row. Any other 404 is a failed write.
3. **Races.** Two counters: one for session change and server notice (a stale GET or write answer
   is dropped), one for choices (a stale GET is dropped, and only the latest choice may revert).
   Writes go out one at a time, in order, so the last choice is the last commit.
   Tests: `useSeshatEffort.test.ts` (rapid changes, late failure, notice during a write,
   GET answering after a change, session switch).
4. **STATE_DELTA parse path.** Test in `useAgentStream.test.tsx` feeds a `session_effort` event.
   The chat-screen test delivers it through the socket.
5. **Config-to-selector wiring.** `StreamingChat.effort.test.tsx` renders the real screen with a
   config response in which the model declares `fast` and no `deep` (AC-3).

## Risks

- A new required field on `DeploymentView` would break typed test fixtures. The new fields are optional.
- A `vi.mock` of `@/lib/agui-client` in older tests lacks `setSessionEffort`. The tests only break if the code calls it.
- The backend dict shape may be pinned by an existing test. Step 1 finds it.
