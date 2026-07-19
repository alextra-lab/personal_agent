# ADR-0079: Server-Authoritative Session Execution Profile

**Status:** Implemented (FRE-416 + FRE-419, PRs #102–#104; deployed + on-device verified 2026-05-29) — **subject superseded by [ADR-0121](ADR-0121-model-catalog-and-selection-layer.md)**: the execution profile ("Path") this ADR governs is removed in favour of per-role model selection. **The decision below is not retired — it is inherited.** All eleven invariants (server-authoritative resolution, the new-vs-existing session asymmetry and its post-deploy correction, toggle-as-a-write with user scoping, hydration at every entry point, single-socket notification, in-flight-turn immutability, durability over delivery, offline-write failure, provenance) apply verbatim to the selection store. See ADR-0121 §4.
**Date:** 2026-05-29
**Issue:** FRE-416, FRE-419
**Amends:** ADR-0044 D2 (profile bound at conversation creation — see "Mid-conversation switching" below)
**Related:** ADR-0044 (execution profile config — local/cloud), ADR-0075 (WebSocket transport + durable channel), ADR-0074 (end-to-end traceability)

## Context

A conversation's **execution profile** (`local` → Qwen on the Mac SLM tunnel; `cloud` → Claude Sonnet) decides which model runs every turn. Today the profile is **client-only state** with no server-side source of truth:

- The PWA holds it in `localStorage['seshat_profile']`, defaulting to `'local'` at three independent layers (`StreamingChat.tsx` `useState`, `agui-client.ts` `sendChatMessage`, `useSSEStream.ts` `sendMessage`).
- It is sent per-message as a form field; `/chat/stream` has `profile: str = Form(default="local")` (`service/app.py:1659`) and silently defaults to `local` when the field is absent, with no validation (an unknown name only logs a warning and continues).

**Incident (2026-05-29, session `33a22590`).** Five turns ran `profile=cloud`; after WS connection churn and a client re-initialisation (`session_created` at 05:02:47), one turn launched `profile=local`, routed to the local SLM (which was down), and returned HTTP 530 — while the PWA still displayed the "Cloud" pill. The user did not refresh; the remount is consistent with iOS silently reloading a backgrounded PWA and/or a second device sharing the same `session_id` (the logs show repeated `ws.evicted_old_connection`). The precise client trigger is unproven, but the **structural fault is established**: profile is per-client ephemeral state that re-derives to `local` on any fresh mount, a session can be driven by multiple concurrent clients, and nothing reconciles displayed/sent/executed profile against an authority.

This produces three user-visible failures already filed separately: the wrong context-window meter (FRE-414), the path-blind error card (FRE-415), and the silent local execution itself (FRE-416/419). All three stem from the same root: **no component owns the truth of which profile a session is running.**

### Why not just patch the backend default?

Removing the `/chat/stream` default (e.g. 422 on missing) stops the *silent* fallback but leaves the client free to send a stale or re-defaulted value, and gives a second device no way to learn the current profile. The desync is a state-ownership problem, not a single-line default bug; it must be fixed at the layer that can arbitrate.

## Decision

**The session owns its execution profile. The server is the single source of truth. Clients hydrate from and reconcile to it; they never assume.**

### Mid-conversation switching (amends ADR-0044 D2)

ADR-0044 D2 and `config/profile.py:124-131` state the profile is bound at conversation creation and mid-conversation switching is unsupported. **The running system already contradicts this:** `/chat/stream` resolves `profile` per request and calls `set_current_profile` per turn — which is exactly how the incident session flipped cloud→local→cloud mid-conversation. This ADR makes the de-facto behavior explicit and supported: the profile is a mutable, per-session, server-owned value. The `ExecutionProfile` docstring and ADR-0044 D2 are amended accordingly.

### 1. Persistence

Add `execution_profile VARCHAR(50) NOT NULL DEFAULT 'local'` to the `sessions` table via migration `docker/postgres/migrations/0007_session_execution_profile.sql` (+ `init.sql`; no Alembic — house convention). The default is an *explicit stored value*, not a silent param fallback. Set at session creation; mutated only by an explicit write.

### 2. Resolution at turn time

`/chat/stream`'s `profile` field is **optional** and resolution is asymmetric by session existence (validated against `list_profiles()` → currently `local`, `cloud`; **422 on invalid**):

- **Existing session** → always use the stored `execution_profile`. A supplied value is **advisory and ignored** — a stale/reloaded client cannot overwrite it, and the only mutator is the PATCH (§3). This is what keeps the original desync fixed.
- **New session (no row yet)** → **adopt the supplied value** (the client's pill), falling back to `local` only when nothing was sent. The value is persisted when the background task creates the row.

There is no path where a missing value silently means `local` for an *existing* session. The client therefore still sends its pill on `/chat/stream` (the only way the server can learn a brand-new session's intended profile); the server decides whether to honour it.

> **Correction (post-deploy, 2026-05-29):** an earlier revision had the client stop sending the field entirely and the server fall back to `local` when the param was absent. For a **new** session (no stored row) this silently created every "Cloud" session as `local` — observed live immediately after the FRE-419 deploy. Fixed by the new-vs-existing rule above + the client resuming the pill send.

The resolved profile is **echoed** in the `/chat/stream` response so the active client can reconcile.

### 3. The toggle is a write

Changing the profile in the PWA calls `PATCH /api/v1/sessions/{id}` (new `profile` field on the gateway session router) rather than mutating local state. The server validates, persists, and emits the change to the active socket. The pill reflects the **server-confirmed** value, optimistically updated and reverted on failure.

**Ownership is an explicit invariant, not an implementation detail (Codex review):** the PATCH requires a `sessions:write` scope **and** resolves the CF Access user and scopes the repo write to that `user_id`, returning 404 on mismatch — identical to the read pattern in `gateway/session_api.py` and the write pattern in `service/repositories/session_repository.py:118` (`repo.update(..., user_id=...)`). A bearer-token holder must not mutate another user's session.

### 4. Hydration at every entry point

A client must be able to learn the current profile both when it mounts *and* when it reconnects:
- **Mount / page-load (HTTP):** the gateway `GET /api/v1/sessions/{id}` response includes `execution_profile`. The PWA initialises the pill from it (localStorage becomes a cache, not the authority). This path is required because the WebSocket only connects on first send, not on mount.
- **WS reconnect:** the connect handshake (ADR-0075 `CONNECT`) emits the current profile as a `session_profile` `STATE_DELTA` before draining the live queue.

### 5. Notification to the active client (not a broadcast)

**Correction from Codex review:** ADR-0075 enforces **one active socket per session** — `_active_connections` is keyed one-per-session (`ws_endpoint.py:81`) and a new connection *evicts* the prior one with code 4001 (and ADR-0075 tells clients not to reconnect on 4001). There is therefore no set of concurrent sockets to "broadcast" to. Live cross-device convergence is **out of scope** today (see "Future direction").

On a profile change the server emits a `session_profile` `STATE_DELTA` to the single active socket (best-effort live update of its pill). Wire shape, consistent with ADR-0075:
```json
{"type": "STATE_DELTA", "seq": N, "data": {"key": "session_profile", "value": "cloud"}, "session_id": "..."}
```
Any other device converges via **hydration** (§4) the moment it foregrounds / reconnects / sends — which is exactly the handoff pattern this system is used in.

### 6. Conflict rule, in-flight turns, and durability

- **PATCH is the canonical writer.** The toggle writes; the server validates, persists, echoes.
- **Last-write-wins is acceptable** because there is no concurrent-editor model (one socket per session). A lost update is bounded to the brief window where a device acts just before eviction and self-heals on that device's next hydration. No version token is introduced now; if collaborative sessions land (below), add an `updated_at`/version guard then.
- **In-flight turns keep the profile resolved at launch.** A PATCH affects *subsequent* turns only; it never mutates a turn already handed to `_process_chat_stream_background`.
- **Durability over delivery.** Correctness depends on the persisted `execution_profile` row + hydration, **not** on the `session_profile` event arriving. The event and the row write are independent (`_push_event` persists to `session_events` separately); if the event write fails, the next hydration still converges. The event is an optimization, never the source of truth.
- A toggle made while offline is a failed write: revert the pill and surface that it did not take (no silent optimistic divergence).

### 7. Observability (ADR-0074)

Emit profile **provenance** so a future flip is attributable: the client records where the sent/displayed value came from (`server-hydrated` / `localStorage` / `default`), and `chat_stream.launched` continues to log the resolved `profile` with `session_id` + `trace_id`. The `session_profile` event and any new `MERGE`/log site carry identity per ADR-0074.

## Future direction: collaborative sessions (north star)

The longer-term vision is **multi-participant live sessions** — several devices (and eventually several users) attached to one session at once, seeing each other's state and turns in real time. This ADR deliberately lays foundations that generalize to that future without committing to it now:

- The **session row is the authority** (not any client) — the precondition for any number of participants.
- A **typed `session_profile` `STATE_DELTA`** over a **per-session event stream** already exists; turning notification into true fan-out is a transport change, not a data-model change.
- A **PATCH write endpoint** is the participant-agnostic mutation path.

What this ADR does *not* do (the actual future work, to be its own ADR/ticket): lift one-socket-per-session to N concurrent sockets with per-connection replay cursors, and define the **round-trip ownership policy** for approvals / HITL interrupts / cancel when multiple participants are present (the coordination problem ADR-0075 closed by evicting). We avoid choices that assume a single client forever; we do not pay for multi-socket coordination today.

## Consequences

**Positive**
- Eliminates the silent-`local` class of bug at the authority layer; closes FRE-416 + FRE-419 together and removes the per-message footgun.
- Gives FRE-414 (real `context_max`) and FRE-415 (path-aware error card) a trustworthy profile to read.
- A handed-off device converges on the authoritative profile when it foregrounds/reconnects/sends; FRE-399 recovery/failover can rely on a stable, authoritative profile and must not silently switch it.
- Lays generalizable foundations for collaborative sessions without paying for them now (see Future direction).

**Negative / cost**
- New DB column + migration; new PATCH endpoint + write scope; new event type + client handler. Two PRs (backend then frontend).
- Adds one HTTP read on mount and one event on profile change (negligible).
- A profile toggle now requires connectivity (it is a write). Acceptable: the toggle is meaningless without the server anyway.
- **No live convergence between two simultaneously-open devices** (one socket per session). Accepted for the handoff usage pattern; revisited under collaborative sessions.

**Neutral**
- localStorage is retained as a fast-paint cache but is no longer authoritative; a stale cache is corrected by the mount hydration.

## Alternatives considered

1. **Backend-default removal only** (422 on missing) — rejected: stops silent fallback but not client-side desync.
2. **WS-connect event as the only hydration** — rejected: the WS connects on first send, not on mount, so the pill would be wrong until the user sends. HTTP GET hydration on mount is required; the connect event is supplementary.
3. **Profile as a per-message client field made mandatory** — rejected: keeps the client authoritative, which is the root cause.
4. **Live broadcast to all session sockets** (original draft) — rejected after Codex review: there is only ever one socket per session (ADR-0075 eviction), so there is nothing to broadcast to; convergence is achieved by hydration instead. True fan-out is deferred to collaborative sessions.
5. **Lift one-socket-per-session now** — rejected for this ticket: it reopens ADR-0075's bidirectional-coordination decision (who answers approvals/interrupts) and is a much larger change; tracked as future work.

## Rollout

- **PR1 (FRE-416):** migration `0007` + `SessionModel`/Pydantic field; `/chat/stream` optional-resolve (absent→stored, present→validated write, 422 invalid) + echo; ownership-scoped PATCH + `execution_profile` in the gateway session GET; `session_profile` emit on change; in-flight turn unaffected. Tests.
- **PR2 (FRE-419):** PWA mount hydration from GET; toggle → PATCH; consume `session_profile` STATE_DELTA; remove the three `local` defaults and stop sending the per-message field; provenance telemetry. Tests. Depends on PR1. Closes the transitional caveat in §2.
