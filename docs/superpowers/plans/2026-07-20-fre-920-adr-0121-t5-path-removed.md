# FRE-920 — ADR-0121 T5: Path removed end to end (assembled seam, AC-9)

**ADR:** `docs/architecture_decisions/ADR-0121-model-catalog-and-selection-layer.md`
**Depends on (merged):** T1 (FRE-916), T2 (FRE-917), T3 (FRE-918), T4 (FRE-919)
**Owns:** AC-9 (assembled seam) and the completion of AC-1(b) (grep-clean: `ExecutionProfile`/
`resolve_model_key` absent from active source and config).

## Linear handoff note (T3 gate, 2026-07-20)

T3 deliberately left `GET /api/inference/status` profile-keyed because it still served the live
pill. This ticket owns retiring it (see Step 3 below — chosen: **remove**, not re-key, since the
config-read endpoint already exposes provider availability).

## Scope confirmed by direct code reads (not the ADR's stale line numbers)

Full current-state map gathered via Explore agent + direct reads — see conversation. Key facts that
drive this plan:

- The selection store, config-read endpoint (`GET /api/v1/sessions/{id}/config`), and selection-write
  endpoint (`PATCH /api/v1/sessions/{id}/selection`) are **fully built** (T2/T3). This ticket is
  entirely about *removing the old path* and *wiring the picker to the endpoint that already exists*
  — no new backend selection mechanism needed.
- `config/profile.py` (`ExecutionProfile`, `resolve_model_key`, `resolve_profile_redirect`,
  `load_profile`, `set_current_profile`/`get_current_profile`) is still fully live and is consulted by:
  `llm_client/factory.py` (fallback branch), `error_classification.py` (`is_cloud`),
  `orchestrator/executor.py` (attachment routing), `service/app.py` (`/chat` and `/chat/stream`),
  `gateway/session_api.py` (`_profile_role_bridge`, `PATCH /{id}`), `ui/service_cli.py`.
- `vision` **does not exist as a Layer-3 role today**. The pinning work is not a re-point, it's new:
  add `vision` to `config/model_roles.yaml` and simplify the escalation logic in `executor.py` down to
  a single pinned-role lookup.
- No frontend selection-store or config-read client exists in the PWA at all. `ProfileSelector.tsx`
  is dead code (defined, never imported) — a second, never-wired Path chooser.
- `useSSEStream.ts` already has a `session_selection` STATE_DELTA emitted server-side that it does
  **not** consume — required for AC-9's "survives a WS reconnect via hydration".

## Codex plan-review round 1 — findings incorporated

Ran `codex:rescue` against this plan before writing any code (Standard/Complex tier, mandatory per
the build skill). Six findings came back; all six confirmed against source and folded into the plan
below (search "**[codex-N]**" markers at each fix point):

1. **[CRITICAL, verified]** The picker has no pre-session read/write path. `GET /{id}/config` and
   `PATCH /{id}/selection` both 404 when no `sessions` DB row exists yet
   (`gateway/session_api.py` — `SessionRepository.get()` → `not_found("session")` in both handlers).
   Confirmed a brand-new PWA conversation has **no DB row** until the first message is sent —
   `StreamingChat.tsx`'s `handleNewConversation` only mints a UUID and navigates; the row is created
   lazily inside `_process_chat_stream_background`/`_chat_impl` on first turn. The pill worked around
   this today via `localStorage` + a session-independent `/api/inference/status` poll. Fix: Phase B
   gains a new sessionless `GET /api/v1/config` endpoint (Step B.4a below); Phase C's picker falls
   back to it before a session row exists.
2. **[CRITICAL, verified]** The `config/profile.py` consumer list in round 1 was incomplete. Verified
   additional active consumers: `config/__init__.py` (re-exports `ExecutionProfile`, `DelegationConfig`,
   `load_profile`, `list_profiles`); `llm_client/client.py:23,226` (`resolve_profile_redirect` imported
   at module level, called in `LocalLLMClient.respond`); and **six more call sites in
   `orchestrator/executor.py`** beyond attachment routing: a context-length helper (~line 192), a
   `TYPE_CHECKING`-only import (~line 876, dies naturally once `_apply_default_processing_target`'s
   signature is gone), `_no_think_applies` (~line 1049), `_frozen_backend` (~line 1172, ADR-0081 cache
   scheduler — reads `profile.provider_type` for `"local"`/`"cloud"`), a skill-routing override lookup
   (~line 3500, uses `get_skill_routing_mode_override` — **not profile-related**, just co-located in
   the same module), and the LLM-call dispatch path (~line 3656). All folded into Phase B below.
3. **[HIGH, verified]** Legacy `/chat`'s selection handling was under-specified — it can't just "set
   a selection if given," it needs the **same** existing-session-wins / new-session-adopts resolution
   `_resolve_session_selection` already implements for `/chat/stream`, or an existing legacy-created
   session's next turn silently diverges from what `GET /config` just showed the picker. Fix: `_chat_impl`
   calls the *same* `_resolve_session_selection` function (Phase B.3 below), not a parallel one-off.
4. **[HIGH, verified]** `SessionResponse.execution_profile` (`service/models.py`) and
   `_session_to_dict`'s `"execution_profile"` key (`gateway/session_api.py:874`) still serialize the
   field to every client — a live AC-1(b) violation if left. Removed from both response paths (Phase
   B.5/B.4b below), not just the internal resolution logic.
5. **[MEDIUM]** Deleting (not re-keying) `/api/inference/status` is only justified once finding #1's
   sessionless-config gap is closed — otherwise there is a real window with no availability signal at
   all for a new conversation. Resolved by the same B.4a fix.
6. **[LOW]** Vision-pinning test coverage should assert the actual capability contract (image *and*
   PDF against the pinned role, fail-closed if the pinned deployment lacked a required capability) —
   folded into Phase A.5's test list, already broadly scoped there; made explicit.

## Design decisions requiring explicit sign-off (flagged for codex + owner review)

1. **`sub_agent`/`artifact_builder` lose profile-based redirection, becoming pure binding defaults —
   owner-decided 2026-07-20: pin the binding default to `claude_haiku`, not the local Qwen deployment.**
   Today, on a session with the `cloud` profile active, both resolve via
   `profile.sub_agent_model`/`profile.artifact_builder_model` to `claude_haiku`; on `local` they
   resolve to `qwen3.6-35b-instruct`. After this ticket there is only one binding default (no
   `ExecutionProfile` consultation anywhere, per AC-1(b) and ADR §5's "sub_agent: Deployment default
   now"), and it cannot simultaneously match both prior profile values — a real behavior change is
   unavoidable for whichever profile it doesn't match. Given the choice, the owner chose to preserve
   the `cloud`-profile value: `config/model_roles.yaml`'s `sub_agent` and `artifact_builder` bindings
   change from `deployment: qwen3.6-35b-instruct` to `deployment: claude_haiku` (Phase A.1 below). This
   means a session that had the `local` pill on will see `sub_agent`/`artifact_builder` calls move from
   local Qwen instruct to cloud Claude Haiku — the inverse of the risk originally flagged — a plain
   config value change, not a code change; `resolve_role_target`'s existing binding-override semantics
   apply to whatever deployment the binding points at. `artifact_builder`'s own per-build picker
   (ADR-0122, FRE-881+, in flight separately) remains the long-term fix for real per-build choice.
2. **`vision` is pinned to `claude_sonnet`**, not the local Qwen deployment. Justification: both
   existing profiles' `escalation_model` is `claude_sonnet`, and the current code's own comment
   documents an owner-authorized live test finding local Qwen vision reads "materially worse" —
   `attachment_default_processing_target` already defaults to `"cloud"` today. Pinning to the model
   that was already the de facto default preserves current behavior for the common case and is a
   straight reading of ADR §5 ("vision is pinned... no user control").
3. **`GET /api/inference/status` is removed, not re-keyed.** **Revised after codex round 1**: this is
   only safe because Phase B.3 adds a new sessionless `GET /api/v1/config` covering the pre-session
   case (codex finding #1) — without it, a brand-new conversation would have no availability signal
   at all. With that endpoint in place, the profile-keyed status endpoint is fully redundant (the
   config-read family — session-scoped or not — already returns `providers: [{key, placement,
   available, ...}]`) and the PWA polls that instead.
4. **`sessions.execution_profile` DB column is left in place, vestigial** (NOT NULL DEFAULT
   `'local'`), not dropped by a new migration. Nothing will read or write it after this ticket; the
   repository will pass a hardcoded `"local"` on create, and — **corrected after codex round 1** — it
   is also removed from every response surface (`SessionResponse`, `_session_to_dict`), not just
   internal resolution logic, so it is truly write-only-with-a-constant and never round-trips to a
   client. Dropping the column itself is a separate, lower-risk future cleanup — this ticket's job is
   the *read path*, per its own title, and an unnecessary destructive schema change on a live table is
   exactly the kind of action to avoid absent a specific need.

## Step-by-step plan

### Phase A — `vision` role pinning + attachment routing simplification (backend)

1. `config/model_roles.yaml`:
   - Add under `bindings:`:
     ```yaml
     vision:              { deployment: claude_sonnet }
     ```
     (no `open: true` → pinned, matching the writer roles above it).
   - **Owner-decided (design decision 1):** change `sub_agent` and `artifact_builder`'s existing
     `deployment: qwen3.6-35b-instruct` to `deployment: claude_haiku`, preserving the `cloud`-profile
     value now that there is only one binding default. Pure config value change — `open: true` stays
     on `artifact_builder`, absent (pinned) on `sub_agent`, unchanged.
2. `src/personal_agent/orchestrator/executor.py`:
   - Delete `_apply_default_processing_target`.
   - Rewrite `_resolve_vision_routing_key(ctx, role_name)`: if no raster attachment, unchanged
     (delegates to `resolve_role_target` for the calling role — untouched, this is the normal
     text-turn path). If a raster attachment is present, resolve `resolve_role_target("vision")`
     directly — no profile, no `processing_target`, no local/cloud branching — and raise
     `AttachmentUnsupportedError` only if the resolved model lacks `supports_vision` (a config-drift
     guard, not a runtime routing choice).
   - Rewrite `_resolve_document_routing_key` the same way, keeping its `native_pdf`/`rasterize` mode
     decision (based on the resolved model's `supports_pdf_document`/`supports_vision`), dropping all
     target/profile logic.
3. Remove the `processing_target` field and its threading (dead once routing no longer reads it):
   - `src/personal_agent/orchestrator/types.py` — `UploadedAttachment.processing_target`,
     the mirror dataclass field (~line 231).
   - `src/personal_agent/orchestrator/document_resolution.py:337,406,428` — drop the kwarg.
   - `src/personal_agent/orchestrator/executor.py:2547,2708` — drop from ingestion.
   - `src/personal_agent/service/app.py:162-213` — drop the parsing of `processing_target` from the
     attachment payload.
4. `src/personal_agent/config/settings.py`: remove `attachment_default_processing_target`.
5. Tests: update `tests/test_orchestrator/test_executor.py`, `test_document_continuation.py`,
   `test_routing.py`, `tests/personal_agent/orchestrator/test_content_widening.py`,
   `test_attachment_cost_gate.py` — remove assertions on removed symbols; add coverage that an
   image/PDF turn always resolves to the pinned `vision` binding regardless of any legacy override
   attempt, and that a misconfigured pinned model (hypothetically lacking `supports_vision`) fails
   closed with `AttachmentUnsupportedError`.
6. `constraint_options.py`/`_maybe_confirm_attachment_cost`: **no code change** — the `attachment_cost`
   DecisionCard stays as a cost-consent gate against the (now-pinned) resolved model's cost, per ADR §5.

### Phase B — Delete `config/profile.py` and every consumer

0. **[codex-2]** Relocate the skill-routing context vars (`set_skill_routing_mode`,
   `get_skill_routing_mode_override`, `_skill_routing_mode_override`) out of `config/profile.py` into
   `config/selection.py` (same per-task-ContextVar pattern already there) **before** touching anything
   else — they are unrelated to Path/profile, just co-located, and deleting the file would otherwise
   take them out with it. Update their one call site (`executor.py` ~3500) to the new import path.
1. `src/personal_agent/llm_client/factory.py`: replace the `resolve_profile_redirect(role_name)`
   fallback branch with a direct `resolve_role_target(role_name)` call (binding default) when no
   selection context exists.
   **[codex-2]** `src/personal_agent/llm_client/client.py:23,226`: `resolve_profile_redirect(role.value)`
   → `get_current_selection(role.value)` (from `config/selection.py`), passed as `resolve_role_target`'s
   `model_key`.
2. `src/personal_agent/error_classification.py`:
   - Drop the `is_cloud`/`get_current_profile()` resolution and the `switch_to_cloud` action id.
     `classify_error(error)` becomes placement-neutral: single wording per category (no "local"/
     "cloud" branch), `retry_actions = ("retry", "stop")` always.
   - `AttachmentUnsupportedError` branch: reword `next_step` to drop "local/cloud override" language.
   - Update `tests/personal_agent/test_error_classification.py` to match (drop `is_cloud` param usage
     and `switch_to_cloud` assertions).
3. `src/personal_agent/service/app.py`:
   - `/chat/stream`: remove the `profile: str | None = Form(...)` param, `_resolve_session_profile`,
     and the `set_current_profile`/`load_profile` call in `_process_chat_stream_background`.
     Simplify `_resolve_session_selection` to drop the profile-bridge fallback entirely (order becomes:
     stored selection wins for an existing session → adopt a supplied, valid key for a new session →
     binding default). Provenance values narrow to `stored` / `adopted` / `default`.
   - **[codex-3]** Legacy `/chat` (`_chat_impl`): replace `profile: str = "local"` with
     `model: str | None = None` (a `primary` deployment key). Call the **same**
     `_resolve_session_selection(session_id, model, session, trace_id=...)` function `/chat/stream`
     uses (do not write a parallel one-off resolver) so an existing legacy-created session's next turn
     matches exactly what `GET /config` just displayed. On new-session creation, upsert a
     selection-store row the same way `/chat/stream` does, so `GET /{id}/config` sees a real row from
     turn one. Response dict's `"profile"` key becomes `"primary_selection"`.
   - **[codex-2]** Six more call sites resolve via `resolve_profile_redirect`/`get_current_profile`
     and need updating, not just the chat endpoints:
     - `_context_window_max`-style helper (~line 192) and `_no_think_applies` (~line 1051, in
       `executor.py` — see below) both do
       `resolve_role_target("primary", model_key=resolve_profile_redirect("primary"))`; replace with
       `resolve_role_target("primary", model_key=get_current_selection("primary"))`.
     - `_frozen_backend()` (`executor.py` ~1172): replace `get_current_profile().provider_type` with
       `catalog.placement_of(resolved_primary_key).value` where `resolved_primary_key` comes from
       `resolve_role_target("primary", model_key=get_current_selection("primary"))[0]` — same
       `"local"`/`"cloud"` string contract the scheduler already expects, sourced from the catalog's
       `Placement` enum instead of the profile.
     - LLM-call dispatch path (`executor.py` ~3656): `resolve_role_target(model_role.value,
       model_key=resolve_profile_redirect(model_role.value))` → same swap to `get_current_selection`.
     - `TYPE_CHECKING`-only `ExecutionProfile` import (`executor.py` ~876): delete, it only existed for
       `_apply_default_processing_target`'s type hint, which Phase A.2 already removes.
   - **[codex-1]** New sessionless read endpoint — add `GET /api/v1/config` (mounted alongside the
     existing gateway session routes, or directly on `app.py`; same auth scope as `GET /{id}/config`)
     returning `{"roles": {role: {"open": bool, "candidates"?: [...]}}, "providers": [...]}` — the
     **same** `role_candidates`/`_deployment_view`/`_provider_view`/`check_all_providers` machinery
     `GET /{id}/config` already uses, just without the per-session `resolved`/`provenance` fields
     (there is no session to resolve against). This is what makes the picker work for a brand-new
     conversation before any DB row exists — closing codex finding #1 and making the finding-#5
     `/api/inference/status` deletion actually safe.
   - Delete `inference_status` (`GET /api/inference/status`, ~2415-2476) entirely — now safe per the
     sessionless endpoint above.
   - `SessionCreate(execution_profile=profile)` call site: stop passing it (field removed in Phase B.5).
4. `src/personal_agent/gateway/session_api.py`:
   - Delete `update_session_profile` (`PATCH /{session_id}`), `_profile_role_bridge`,
     `_PROFILE_REDIRECT_ATTR`.
   - Simplify `_resolve_role_binding`: drop the `execution_profile` param and the bridge call — order
     becomes stored selection (open roles, guardrailed) → binding default. Update both call sites
     (`GET /{id}`, `GET /{id}/config`) to stop passing `session.execution_profile`.
   - **[codex-4]** `_session_to_dict` (~line 874): remove the `"execution_profile"` key from the
     serialized dict entirely — it is a live AC-1(b) violation to leave it on the wire, not just in
     internal resolution logic.
5. `src/personal_agent/service/models.py`: remove `execution_profile` from `SessionCreate`,
   `SessionUpdate`, **and `SessionResponse`** (**[codex-4]** — round 1 wrongly left this one, it's a
   real response field consumed by clients, not just internal state); delete `SessionProfileUpdate`.
   Leave `SessionModel.execution_profile` (the ORM/DB column) as-is — decision 4, vestigial, DB-defaulted,
   never read back out through any response model after this change.
6. `src/personal_agent/service/repositories/session_repository.py:75`: hardcode
   `execution_profile="local"` (the column stays NOT NULL; nothing sets it meaningfully anymore).
7. `src/personal_agent/ui/service_cli.py`: replace `--profile` with `--model` (a `primary` deployment
   key, optional); `_send_chat` sends `model` instead of `profile` to `POST /chat`; update the
   diagnostic print line (`data.get("primary_selection")` instead of `data.get("profile")`).
8. **[codex-2]** `src/personal_agent/config/__init__.py`: remove the `from personal_agent.config.profile
   import (DelegationConfig, ExecutionProfile, list_profiles, load_profile)` block and the matching
   four `__all__` entries.
9. Delete `src/personal_agent/config/profile.py`, `config/profiles/local.yaml`,
   `config/profiles/cloud.yaml`, the now-empty `config/profiles/` directory.
10. `src/personal_agent/config/settings.py`: remove `default_profile`, `profiles_dir`.
11. Grep-verify (AC-1(b)): no hits for `ExecutionProfile`, `resolve_model_key`, `config.profile`,
    `config/profiles` under `src/`, `config/`, compose files (historical ADRs/postmortems excluded).
    This grep is the actual gate for Phase B — it will immediately surface any consumer this plan (or
    codex's round 1) still missed, given how many turned up on the first pass.
12. Tests: delete `tests/test_config/test_profile.py`. Update
    `tests/personal_agent/gateway/test_session_api.py` (remove profile-PATCH tests, update
    `_resolve_role_binding` call sites/expectations and `_session_to_dict` output assertions),
    `tests/test_service/test_session_selection_resolution.py`,
    `tests/test_llm_client/test_factory_selection.py`. Broad grep of `tests/` for `config.profile`/
    `ExecutionProfile`/`load_profile` for any remaining stragglers. Add a test for the new sessionless
    `GET /api/v1/config` endpoint and for legacy `/chat` sharing `_resolve_session_selection`.

### Phase C — PWA: model picker + selection wiring

1. `seshat-pwa/src/lib/agui-client.ts`:
   - Add `getSessionConfig(sessionId)` → `GET /api/v1/sessions/{id}/config`.
   - **[codex-1]** Add `getConfig()` → the new sessionless `GET /api/v1/config` (Phase B.3) — used
     for a brand-new conversation with no DB row yet.
   - Add `setSessionSelection(sessionId, role, deploymentKey)` → `PATCH /api/v1/sessions/{id}/selection`.
   - Remove `setSessionProfile`.
   - `SendMessageOptions`: `profile?: ExecutionProfile` → `primarySelection?: string`;
     `sendChatMessage` sends `primary_selection` form param.
   - `SessionSummary`: `execution_profile: ExecutionProfile` → `primary_selection: string`,
     `selection_provenance: string`.
   - `UploadedAttachment`/`UploadState`/`completeUpload`: drop `processing_target`/`processingTarget`.
2. `seshat-pwa/src/lib/types.ts`: remove `ExecutionProfile`; add `DeploymentView`, `ProviderView`,
   `SessionConfigRole`, `SessionConfig` matching the backend response shape from §10 of the map.
3. Delete `seshat-pwa/src/hooks/useInferenceStatus.ts` and `seshat-pwa/src/components/ProfileSelector.tsx`
   (orphaned, never imported — confirmed by grep).
4. New `seshat-pwa/src/hooks/useSessionConfig.ts`: fetches `getSessionConfig` on mount + a session-id
   change + 60s interval (mirrors the removed poll cadence) + an explicit `refetch()`; returns
   `{ roles, providers, loading, refetch }`. **[codex-1]** On a 404 (no DB row yet — brand-new
   conversation), falls back to the sessionless `getConfig()` for the candidate list and provider
   table (no `resolved`/`provenance` in that case — the hook surfaces this as
   `hydrated: boolean` so the picker can show "not yet set" rather than a stale resolved value).
   Once the first message creates the session row, the next fetch (on send, or the following
   interval tick) picks up the real session-scoped payload.
5. New `seshat-pwa/src/components/ModelPicker.tsx`: renders `roles.primary.candidates` (provider,
   placement, cost/context, availability already filtered server-side per AC-5) as a compact
   dropdown in the same visual slot the pill occupied in `ChatInput.tsx`; `onSelect` calls
   `setSessionSelection(sessionId, 'primary', key)` then `refetch()`.
6. `seshat-pwa/src/components/ChatInput.tsx`: remove `profile`/`onProfileChange` props,
   `toggleProfile`, `pathUnavailable`/`activeLabel`/`otherLabel`, `useInferenceStatus` usage, and the
   attachment processing-target chip (`canOverrideProcessingTarget`, `cycleProcessingTarget`, the chip
   UI block, `processingTarget` from the submit payload). Replace the pill button with `<ModelPicker>`.
7. `seshat-pwa/src/components/StreamingChat.tsx`: remove `PROFILE_STORAGE_KEY`, `profile` state,
   `handleProfileChange`, the `serverProfile` reconciliation effect, and `execution_profile` mount
   hydration. Drive `ModelPicker` from `useSessionConfig`; `handleSend` passes the resolved selection
   key instead of a profile. `ClassifiedErrorCard`'s `onSwitchToCloud` prop removed (action retired).
8. `seshat-pwa/src/components/ClassifiedErrorCard.tsx`: remove `switch_to_cloud` handling (`ACTION_LABELS`
   entry, `onSwitchToCloud` prop) — matches the backend's Phase B.2 change.
9. `seshat-pwa/src/hooks/useSSEStream.ts`: remove `serverProfile` + `session_profile` STATE_DELTA
   handling. Add `session_selection` STATE_DELTA handling → new `serverSelection: {role, deploymentKey} | null`
   return value, so a selection change made elsewhere reconciles the picker (mirrors the removed
   profile reconciliation) — this is what makes AC-9's "survives a WS reconnect via hydration" real.
   `sendMessage` signature: `profile: ExecutionProfile` param → `primarySelection?: string`.

### Phase D — Observe view

1. New `seshat-pwa/src/app/observe/page.tsx` (mirrors `app/artifacts/page.tsx`).
2. New `seshat-pwa/src/components/ObserveView.tsx`: reads `session` from the URL query
   (`?session=<id>`, falling back to the `seshat_last_session_id` localStorage key, matching
   `StreamingChat`'s own last-session convention), calls `getSessionConfig`, renders: per-role table
   (pinned vs open badge, resolved model, provenance) and the provider table (placement, available,
   summary, max_concurrency).
3. `StreamingChat.tsx` drawer: add an "Observe" nav link next to the existing "Artifacts" link,
   `href={session ID present ? \`/observe?session=${sessionId}\` : '/observe'}`.

### Phase E — Tests (PWA) + lint

1. `seshat-pwa/src/__tests__/ChatInput.test.tsx`: remove pill/chip-gating tests; add
   `ModelPicker` presence/interaction coverage (or move that coverage to a new `ModelPicker.test.tsx`
   — prefer the latter, matching one-component-one-test-file convention already used).
2. `seshat-pwa/src/__tests__/ClassifiedErrorCard.test.tsx`: remove "Switch to Cloud" assertions.
3. New `ModelPicker.test.tsx`, `ObserveView.test.tsx`, `useSessionConfig.test.ts`.
4. `npm run lint` clean (FRE-395 gate, mandatory per project checklist).

### Phase F — Documentation

1. Add a "T5 build note" to ADR-0121 (matching T1/T4's precedent of recording in-build design
   corrections) covering the four flagged decisions above, without claiming deploy/live-verification
   (master's job at the gate).
2. Do **not** touch `docs/plans/MASTER_PLAN.md` (master's role).

## Acceptance criteria mapped to proof

- **AC-9** (assembled seam): manual + automated check on the deployed stack (master's gate) — no
  pill/chip/switch-to-cloud action anywhere; picking a model persists via `PATCH .../selection`;
  `GET /{id}` and page reload reflect it; WS reconnect delivers `session_selection` STATE_DELTA and
  the picker updates; an in-flight turn's `set_current_selection` context var (already immutable
  per-task since T2) proves the "doesn't mutate in flight" half — existing T2 test coverage, unchanged
  by this ticket, still asserts it.
- **AC-1(b)** completion: the Phase B.10 grep-clean check.

## Test commands

```bash
make test-file FILE=tests/test_orchestrator/test_executor.py
make test-file FILE=tests/test_orchestrator/test_document_continuation.py
make test-file FILE=tests/test_orchestrator/test_routing.py
make test-file FILE=tests/personal_agent/orchestrator/test_content_widening.py
make test-file FILE=tests/personal_agent/orchestrator/test_attachment_cost_gate.py
make test-file FILE=tests/personal_agent/gateway/test_session_api.py
make test-file FILE=tests/test_service/test_session_selection_resolution.py
make test-file FILE=tests/test_llm_client/test_factory_selection.py
make test-file FILE=tests/personal_agent/test_error_classification.py
make test    # full suite once module-level passes
make mypy
make ruff-check && make ruff-format
cd seshat-pwa && npm test && npm run lint && npm run typecheck  # (or project's equivalent scripts)
```
