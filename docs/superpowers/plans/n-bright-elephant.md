# FRE-392: WebSocket Transport Duplicate Message Idempotency Guard

## Context

During ADR-0077 e2e verification (2026-05-27), a single user message produced two separate orchestrator traces (`df983a9d` and `8e39f1a8`) in the same session, 39 seconds apart. The user confirmed they didn't retry — the PWA sent the message once but the orchestrator received it twice. The second trace's sub-agent timed out due to GPU contention with the first, then fell back to direct `artifact_write`.

**Root cause**: `POST /chat/stream` has zero idempotency protection. Every POST unconditionally spawns a new `asyncio.create_task(_process_chat_stream_background(...))` (app.py:1593). No dedup key, no content hash, no in-flight task tracking. When a WS reconnect or any client-side race triggers a second POST for the same message, both tasks run concurrently — each generating a new trace_id, appending the user message to the DB, and invoking the full orchestrator pipeline.

**Architecture context**: Chat messages go via HTTP POST (not WebSocket). The WS channel is output-only (server→client events + HITL decisions). Existing seq-based dedup in the transport layer only covers outbound event replay, not inbound message processing.

---

## Approach

Two-layer idempotency guard: client-generated idempotency key (primary) + server-side content hash (fallback for CLI/older clients).

### 1. New module: `src/personal_agent/service/idempotency.py`

`MessageDeduplicator` class with an in-process `dict` (single-process Uvicorn deployment — no need for Redis/Postgres). The asyncio event loop is single-threaded, so a plain dict is safe.

```python
@dataclass(frozen=True)
class DeduplicationResult:
    is_duplicate: bool
    original_trace_id: str | None = None
```

Key methods:
- `check_and_record(session_id: str, message: str, trace_id: str, client_msg_id: str | None = None) -> DeduplicationResult`
  - Dedup key: `client_msg_id` if provided, else `sha256(session_id + message)` truncated to 16 hex chars
  - Lookup key: `(session_id, dedup_key)`
  - If found and within TTL (120s): return `DeduplicationResult(is_duplicate=True, original_trace_id=...)`
  - If not found or expired: record `(trace_id, timestamp)` and return `DeduplicationResult(is_duplicate=False)`
- `release(session_id: str, dedup_key: str) -> None` — remove entry on task completion so immediate retries work
- `cleanup_expired() -> int` — evict all entries older than TTL, return count removed

Module-level singleton: `_deduplicator = MessageDeduplicator()` with `get_deduplicator()` accessor (testable via dependency injection).

### 2. Modify `src/personal_agent/service/app.py`

**`chat_stream_endpoint` (line 1558)**:
- Add `client_msg_id: str | None = Form(None)` parameter
- Before `asyncio.create_task()`: call `deduplicator.check_and_record(session_id, message, trace_id, client_msg_id)`
- If duplicate: log `chat_stream.deduplicated` and return `{"session_id": session_id, "status": "streaming", "deduplicated": true}`
- Generate `trace_id` at the endpoint level (move from line 151 of `_process_chat_stream_background` to the endpoint) so it's available for the dedup record
- Pass `dedup_key` to `_process_chat_stream_background`

**`_process_chat_stream_background` (line 127)**:
- Accept new `dedup_key: str | None` parameter
- In the `finally` block (line 356): call `deduplicator.release(session_id, dedup_key)` if `dedup_key` is set

**`chat` endpoint (line 1233)**:
- Same dedup guard (content hash only — CLI doesn't send `client_msg_id`)
- Already generates `trace_id` at line 1262, so no move needed

**Lifespan**: Add periodic cleanup task (every 60s) to evict expired entries, same pattern as `run_event_cleanup` for session_events TTL.

### 3. PWA client changes

**`seshat-pwa/src/lib/agui-client.ts`**:
- Add `clientMsgId?: string` to `SendMessageOptions` interface (line 46-50)
- Include `client_msg_id` in `URLSearchParams` body when present (line 104-108)

**`seshat-pwa/src/hooks/useSSEStream.ts`**:
- In `sendMessage` (line 219): generate `generateUUID()` and pass as `clientMsgId` to `sendChatMessage`
- Import `generateUUID` from `../lib/uuid`

### 4. Tests: `tests/personal_agent/service/test_idempotency.py`

Unit tests for `MessageDeduplicator`:
- `test_first_message_not_duplicate` — fresh message returns `is_duplicate=False`
- `test_same_message_is_duplicate` — identical (session, message) returns `is_duplicate=True` with original trace_id
- `test_client_msg_id_takes_precedence` — same content with different `client_msg_id` is not a duplicate
- `test_same_client_msg_id_different_content` — same `client_msg_id` is duplicate regardless of content
- `test_different_session_not_duplicate` — same message in different session is not a duplicate
- `test_ttl_expiry` — entry expires after TTL, message is no longer duplicate
- `test_release_allows_resend` — after `release()`, same message is not a duplicate
- `test_cleanup_expired` — cleanup removes only expired entries
- `test_content_hash_fallback` — without `client_msg_id`, content hash dedup works

---

## Files to modify

| File | Change |
|------|--------|
| `src/personal_agent/service/idempotency.py` | **New** — `MessageDeduplicator` class |
| `src/personal_agent/service/app.py` | Add dedup guard to `chat_stream_endpoint` + `chat` + `_process_chat_stream_background` + lifespan cleanup task |
| `seshat-pwa/src/lib/agui-client.ts` | Add `clientMsgId` to `SendMessageOptions` + POST body |
| `seshat-pwa/src/hooks/useSSEStream.ts` | Generate + pass `clientMsgId` per send |
| `tests/personal_agent/service/test_idempotency.py` | **New** — unit tests for deduplicator |

---

## Verification

1. `make test` — all existing tests pass (dedup module is new, no regressions)
2. `make mypy` — type-clean
3. `make ruff-check` + `make ruff-format` — clean
4. Manual test: deploy, send same message twice quickly via PWA → second returns `deduplicated: true` in server logs
5. Verify: normal message send still works end-to-end (no false positives from hash collision)
6. Verify: after a response completes, re-sending the same text works (release clears the entry)
