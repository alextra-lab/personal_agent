# FRE-407 Implementation Plan — Per-Turn 0–3 Value Rating

**Ticket:** FRE-407 (Approved, Tier-2:Sonnet) · parent EPIC FRE-403 · ADR-0078 §D5 · spec `PROMPT_MANAGEMENT_SPEC.md` §6
**Branch:** `fre-407-per-turn-rating` (already created, off `main`)
**Architect decisions (do NOT re-decide):** below. Everything here is settled — implement exactly as written.

---

## Architect decisions (resolved from live substrate)

1. **Join key is `trace_id`.** `model_call_completed` events carry `trace_id` + `span_id` + `prompt_callsite` + the `prompt_*` identity fields — **no** `turn_id`/`message_id`. The PWA already holds `trace_id` per assistant message (`seshat-pwa/src/lib/types.ts` `Message.trace_id`, surfaced as `message.traceId`). So "turn" ≡ `trace_id` throughout (consistent with the consolidator, which sets `turn_id = capture.trace_id`).

2. **Prompt-identity selection.** A single trace has multiple `model_call_completed` rows across callsites (e.g. `orchestrator.primary×6`, `role.primary×1`, `role.sub_agent×2`). The user is rating the final assistant response → attach the **primary reasoning** identity:
   - Query `agent-logs-*`, `event_type=model_call_completed`, `trace_id=<path>`, `exists prompt_static_prefix_hash`.
   - Prefer `prompt_callsite=orchestrator.primary`; if none, `role.primary`; else the most recent of any callsite.
   - Sort `@timestamp desc`, take first. Extract `prompt_callsite`, `prompt_component_ids`, `prompt_static_prefix_hash`, `prompt_dynamic_hash`.
   - If no identity found (turn predates P1 / ES miss): still record the rating with `prompt_callsite=null` and the hashes null — never 500.

3. **New bus event — do NOT reuse `FeedbackReceivedEvent`.** That event (`stream:feedback.received`) is Linear-label-specific (`issue_id`/`label`/`fingerprint`) and has live `cg:insights`/`cg:feedback` consumers that would break. Add a distinct event.

4. **Identity stored flattened** (not a nested `prompt_identity` object) so ES queries match the `agent-logs` convention (`prompt_callsite` keyword, etc.).

5. **Ownership is enforced (security).** The rating write must resolve the CF user (`_require_request_user_id`, session_api.py:55), assert the supplied `session_id` is **owned by that user** (`SessionRepository`, the FRE-379 user-scoping pattern), and assert the `trace_id`'s `model_call_completed.session_id` **equals** that owned `session_id` (binds the trace to the owned session). Foreign `trace_id`/`session_id` → `404` (don't leak existence). A user must never be able to rate another user's turn.

6. **The write-time identity join is best-effort; the read-time join is authoritative (race-proof).** `agent-logs-*` has a 5 s `refresh_interval`, so a rating POSTed within ~5 s of turn completion can miss the `model_call_completed` doc. Therefore:
   - The rating is **always** stored keyed on `trace_id` with `{session_id, rating, rated_at}` — **never lost**, never 500.
   - Write-time identity denorm is **best-effort with one delayed retry** (lookup → on miss, `await asyncio.sleep(~2s)` → retry once) so the common case fills `prompt_*` immediately.
   - The **Insights consumer joins `user-turn-ratings-*` → `agent-logs-*` on `trace_id` at read time** and treats *that* as the source of truth for mean-per-callsite. So a write-time denorm miss never corrupts the metric. The denormed fields are a convenience/debug aid, not the aggregation key.

---

## Files & steps (TDD — write the test first for each unit)

### 1. Data model — `src/personal_agent/gateway/feedback_models.py` (new)
```python
@dataclass(frozen=True)
class UserTurnRating:
    trace_id: str          # join key — UUID of the rated turn
    session_id: str
    rating: int            # 0–3
    prompt_callsite: str | None
    prompt_static_prefix_hash: str | None
    prompt_dynamic_hash: str | None
    prompt_component_ids: tuple[str, ...]
    rated_at: datetime
```
- `to_es_doc() -> dict` (flatten; `rated_at` isoformat; `prompt_component_ids` → list).
- Google docstrings, frozen, modern type hints.

### 2. Bus event — `src/personal_agent/events/models.py`
- `STREAM_USER_TURN_RATED = "stream:user.turn_rated"` (next to the other STREAM_ constants).
- `class UserTurnRatingEvent(EventBase)` with `event_type: Literal["user.turn_rated"]`, fields: `trace_id`, `session_id`, `rating: int`, `prompt_callsite: str | None`, `prompt_static_prefix_hash: str | None`. Register in the `parse_event`/dispatch map if there is one (mirror `FeedbackReceivedEvent`).

### 3. ES index template — `docker/elasticsearch/user-turn-ratings-index-template.json` (new)
- `index_patterns: ["user-turn-ratings-*"]`, priority 100, 1 shard / 0 replicas, best_compression.
- Properties: `trace_id` keyword, `session_id` keyword, `rating` **integer** (must aggregate for mean), `prompt_callsite` keyword, `prompt_static_prefix_hash` keyword, `prompt_dynamic_hash` keyword, `prompt_component_ids` **keyword** (array — never let it fall to dynamic `text` or mean-by-component breaks), `rated_at` date. Set `"dynamic": false` so nothing drifts.
- Wire a `put_resource` block into `scripts/setup-elasticsearch.sh` (mirror the `slm-requests-template` block).
- **ILM/retention (codex):** every other ES family has lifecycle wiring — do not skip it. Add a `user-turn-ratings-*` entry to the DataLifecycleManager prefix list (`telemetry/lifecycle_manager.py` ~:372) with **90-day** retention (ground-truth labels are worth keeping longer than logs). If an ILM policy is the house mechanism in `setup-elasticsearch.sh`, add it there too.
- Apply live once: `curl -X PUT localhost:9200/_index_template/user-turn-ratings-template -d @<file>`.

### 4. Endpoint — `src/personal_agent/gateway/feedback_api.py` (new router)
- `router = APIRouter(prefix="/turns", tags=["feedback"])`.
- `POST /turns/{trace_id}/rating`, body `RatingRequest{rating: int, session_id: str}`.
- Scope: `require_scope("feedback:write")` — **add `feedback:write` to the `pwa-client` grant** following the FRE-416 precedent that added `sessions:write` (PR #102; find the scope-grant config the same way `sessions:write` is granted).
- **Validate** `0 <= rating <= 3` → `HTTPException(400)`.
- **Ownership (decision #5, binding):** resolve CF user → assert `session_id` owned by that user → assert the `trace_id`'s `model_call_completed.session_id` == `session_id`. Any mismatch → `404`. Do these **before** any write.
- **Resolve prompt identity (decision #2/#6):** ES helper — prefer `prompt_callsite=orchestrator.primary`, else `role.primary`, else `gateway.chat`, else most-recent any callsite, for `trace_id` (scoped to the owned `session_id`); on miss, one `asyncio.sleep(2)` retry, then proceed with nulls. **Sub-agent-only / cloud `gateway.chat` traces may legitimately yield null at write time** — that's fine, the read-time join (step 5) recovers it.
- Build `UserTurnRating`, persist:
  - **ES (source of truth):** `schedule_es_index("user-turn-ratings-{YYYY.MM.DD}", doc, doc_id=trace_id)` — idempotent; a re-rate **overwrites** the single doc per `trace_id`.
  - **NDJSON (audit log only, append):** `telemetry/user_feedback/{YYYY-MM-DD}.ndjson` (file write first, ADR-0054 D4). **Document in code that this file is an append-only audit trail, NOT an aggregation source** — re-rates intentionally leave multiple lines; nothing reads it for means.
  - **Bus:** publish `UserTurnRatingEvent` **only when the rating value changed** (re-rate to the same score → no event), to avoid double-counting downstream. Best-effort; swallow on Redis down.
- Return `{"status": "received"}`.
- Mount in `src/personal_agent/gateway/app.py` (`create_gateway_router`, next to `session_router`) **and** confirm it reaches `/api/v1/*`.

### 5. Insights consumer — `src/personal_agent/insights/engine.py`
- New `async def detect_low_rating_sessions(self, days: int = 7) -> list[Insight]` modelled on `detect_delegation_patterns`.
- **Read-time join (decision #6):** aggregate mean rating per `prompt_callsite` by joining `user-turn-ratings-*` to `agent-logs-*` on `trace_id` for any rating whose denormed `prompt_callsite` is null, so late/missed write-time denorm is recovered. Ratings still null after the join (genuinely identity-less turns) are bucketed `callsite=unknown` and excluded from per-callsite flags.
- **Min-count floor (codex):** never flag a callsite with fewer than **5** ratings in the window (matches the existing `count >= 10`-style floors at engine.py:157/191; 5 is the rating-specific floor). Evidence payload carries **both** mean and count.
- Flag `prompt_callsite` whose mean < 1.5 **and** count >= 5 → `Insight` (→ Captain's Log review). Wire into `analyze_patterns`.

### 6. Tests — `tests/personal_agent/gateway/test_feedback_api.py` + `tests/.../test_feedback_models.py` + insights test
- Model: round-trip `to_es_doc`.
- Endpoint happy/invalid: rating 2 → ES doc with non-null `prompt_callsite` (mock ES join) + bus event published; rating 4 / -1 → 400.
- **Ownership (codex):** foreign `session_id` (not owned by caller) → 404; `trace_id` whose `model_call_completed.session_id` ≠ supplied `session_id` → 404. **No write occurs on rejection.**
- **Refresh-miss (codex):** first ES identity lookup returns empty, retry returns the doc → rating stored with identity; both lookups empty → rating stored with null identity (still 200, never lost).
- **Re-rate (codex):** same `trace_id` rated 1 then 3 → single ES doc with rating 3 (overwrite); re-rate to the **same** value → no second bus event.
- **Fallback identity (codex):** trace with only `gateway.chat` / only `role.sub_agent` rows → correct fallback selection (or documented null).
- **Insights (codex):** mean < 1.5 with count 4 → **not** flagged (below floor); count 6 → flagged; rating with null denorm identity → read-time join recovers `prompt_callsite`.
- **PWA:** `TurnRating` renders only when `complete && traceId`; submit calls the helper without blocking; optimistic select reverts on fetch failure.
- House rules: mocked httpx/ES, no real substrate (FRE-375), `trace_id` on every log.

---

## Verification (must pass before PR)
```
make test        # all green incl. new tests
make mypy        # clean
make ruff-check  # clean
make ruff-format
```
Endpoint smoke (after deploy, in-container): `POST /api/v1/turns/<trace_id>/rating {"rating":2,"session_id":"<sid>"}` → 200 → ES `user-turn-ratings-*` doc with `prompt_callsite`.

---

## Chunk B — PWA rating control (second subagent; buildable here, on-device verify is the user's)

**Aesthetic mandate (cohesion over novelty).** This embeds in the existing dark-slate PWA. Match its established language exactly — do **not** invent a new theme:
- **Base:** slate palette (`text-slate-500` idle → `text-slate-300` hover), dark surfaces with `/40` bg + `/50` border, `text-xs`, `rounded-full`.
- **Accent vocabulary already in use:** amber = caution, emerald = good/confirmed. Reuse it for the rating scale's meaning.
- **Hover-reveal idiom:** follow `CopyButton` precedent exactly — `opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity`, lives in the assistant message footer **beside** the copy button. Never visible mid-stream; renders only after the message is complete.
- **No stars, no thumbs** (generic). Instead: a compact **4-segment value meter** — four small rounded segments `[0][1][2][3]` that fill left-to-right up to the selected score, the fill color keyed to meaning: `0` slate-600, `1` amber-400, `2` teal/sky-400, `3` emerald-400. Each segment is a `<button>` with `aria-label` = the anchor word (No value / Low value / Meets expectation / Wow). Anchor word shows as a tooltip/title on hover.
- **Confirmation micro-interaction:** on submit, the chosen fill does a single subtle pulse (reuse `animate-pulse` vocabulary) then settles to the persisted state; re-rating is allowed (re-click re-POSTs, overwrites). Mirror `CopyButton`'s confirmed-state-with-timeout feel.
- **Non-blocking:** submit async; never block message rendering; optimistic select with revert-on-failure (the `setSessionProfile` PATCH pattern from FRE-419 is the precedent).

**BLOCKER to fix first — thread trace_id + completion + sessionId (codex):** the control cannot render today because the message never carries what it needs. The streaming path (`useSSEStream.ts`) creates the assistant message on `TEXT_DELTA` (~:144) **without** `traceId`, and `DONE` (~:303) only clears streaming state; `ChatMessage` gets no `sessionId` (`ChatMessage.tsx:8`, `StreamingChat.tsx:292`). So:
- On `DONE`, **stamp `event.trace_id` onto the in-flight assistant message** and set an explicit `complete: true` flag on it (extend the message type in `lib/types.ts`).
- Pass `sessionId` down into `ChatMessage` (from `StreamingChat`) and on into `TurnRating`.
- Gate render on `role === 'assistant' && message.complete === true && message.traceId` — never during stream.

**Files:**
- `seshat-pwa/src/lib/types.ts` — add `complete?: boolean` to the message type (and ensure `trace_id`/`traceId` is settable post-hoc).
- `seshat-pwa/src/hooks/useSSEStream.ts` — on `DONE`, set `traceId` + `complete` on the last assistant message.
- `seshat-pwa/src/components/StreamingChat.tsx` — pass `sessionId` to `ChatMessage`.
- `seshat-pwa/src/components/TurnRating.tsx` (new) — the 4-segment control; props `{ traceId: string; sessionId: string }`.
- `seshat-pwa/src/components/ChatMessage.tsx` — accept `sessionId`; render `<TurnRating>` in the assistant-message footer group per the gate above.
- `seshat-pwa/src/lib/` — a `submitTurnRating(traceId, sessionId, rating)` fetch helper → `POST /api/v1/turns/{traceId}/rating`; bump SW `CACHE_NAME` (shell change).
- ESLint/`tsc`/`npm run build` clean; rebuild `seshat-pwa` container.

**Post-deploy AC (user's device):** submit 5 ratings in a real session → query ES `user-turn-ratings-*` → all 5 carry `session_id`, `trace_id`, `prompt_callsite`.

---

## Sequencing
1. **Subagent 1 (Sonnet):** backend — steps 1–6 above. Branch `fre-407-per-turn-rating`. Architect (Opus) reviews → PR → deploy.
2. **Subagent 2 (Sonnet):** PWA — Chunk B. Same branch. Architect reviews → deploy → user device-verifies.
3. **FRE-422 (enchained, after 407 deploys):** ADR-0081 D1 layout reorder — separate branch, lands behind 407 so the FRE-407 baseline exists first.
