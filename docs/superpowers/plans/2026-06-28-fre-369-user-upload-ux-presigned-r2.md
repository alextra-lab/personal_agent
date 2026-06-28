# FRE-369 Implementation Plan: User-Upload UX in PWA with Presigned PUT to R2

**Date:** 2026-06-28  
**Ticket:** FRE-369  
**ADRs:** ADR-0069 (R2 substrate), ADR-0070 (output channels), ADR-0064 (identity)  
**Branch:** `fre-369-featuploads-user-upload-ux-in-pwa-with-presigned-put-to-r2`

---

## Scope (3–5 bullets)

- New `uploads_router.py` with `POST /api/uploads/presign`, `POST /api/uploads/{id}/complete`, `DELETE /api/uploads/{id}` — issues presigned PUT URL to browser, finalizes or cancels the pending row
- DB migration 0012 adds `upload_pending BOOLEAN NOT NULL DEFAULT FALSE` to `artifacts` table to track in-flight uploads
- `/chat/stream` form endpoint gains optional `attachments` JSON field; augments the user message with structured context so the agent knows which `artifact_id`s are available to call `artifact_read()`
- PWA `ChatInput.tsx` gains drag-drop, paste-image, and file-picker upload UX with per-file progress spinner and attachment chips; `agui-client.ts` gains `presignUpload`, `completeUpload`, `cancelUpload` helpers
- Tests cover: content-type allowlist enforcement, size-cap enforcement, R2 HEAD verification on complete, pending-row cleanup on cancel, cross-user isolation

---

## Acceptance Criteria from ADR-0069 / Ticket

| # | Criterion | Proof |
|---|-----------|-------|
| AC1 | Drag-drop, paste, and file-picker uploads work from PWA on laptop and iPad | Manual test + PWA lint pass |
| AC2 | Uploads go directly to R2 (gateway bandwidth doesn't spike) | Presign endpoint never proxies bytes; R2 PUT is to the presigned URL |
| AC3 | Agent can reference and read uploaded content in the same turn | `artifact_read(artifact_id)` already shipped in FRE-368; attachments context prepended to user message |
| AC4 | Uploads persist across sessions (visible in `/artifacts` index) | `upload_pending=False` rows appear in `GET /api/v1/artifacts?type=upload` |
| AC5 | Cross-user ownership enforced (user A's upload not visible to B) | DB query filters by `user_id` + test coverage |
| AC6 | Oversized and disallowed-type uploads rejected pre-presign | 422 validation in presign endpoint + test coverage |

---

## Codex Plan-Review Findings (2026-06-28)

Five blocking issues + two advisory; incorporated below.

**Blocking:**
1. Do not prepend attachment text BEFORE `run_gateway_pipeline` — corrupts TaskType routing. Augment AFTER classification, just before `orchestrator.handle_user_request`.
2. Pending rows must be invisible in list/metadata/resolve queries — add `AND upload_pending = FALSE` filter to existing artifacts_router queries.
3. `artifact_read` has `AND type = 'artifact'` (line 639 of artifact_tools.py) — uploads unreadable. Must extend to `AND type IN ('artifact', 'upload')`.
4. Validate ownership + `upload_pending = FALSE` for each attachment_id before injecting into agent context.
5. `/complete` must verify actual ContentLength from R2 HEAD (not client-supplied `size_hint`).

**Advisory (incorporated):**
- Replace DELETE cancellation endpoint with background expiry task (pending rows older than 30min).
- Add bounded retry (2 attempts, 200ms sleep) around R2 HEAD in `/complete`.
- Fix paste guard order: check `clipboardData.files` BEFORE calling `e.preventDefault()`.

---

## File Map

| File | Action |
|------|--------|
| `docker/postgres/migrations/0012_upload_pending_column.sql` | NEW — add `upload_pending` column |
| `src/personal_agent/storage/artifact_store.py` | EDIT — add `head()` method |
| `src/personal_agent/tools/artifact_tools.py` | EDIT — extend `artifact_read` to `type IN ('artifact', 'upload')` |
| `src/personal_agent/service/artifacts_router.py` | EDIT — exclude `upload_pending=TRUE` rows from list/get/resolve |
| `src/personal_agent/service/uploads_router.py` | NEW — presign + complete endpoints + expiry cleanup |
| `src/personal_agent/service/app.py` | EDIT — include uploads_router; add `attachments` to `/chat/stream`; augment AFTER gateway pipeline |
| `src/personal_agent/config/settings.py` | EDIT — add `upload_max_size_bytes` setting |
| `tests/personal_agent/service/test_uploads_router.py` | NEW — TDD tests |
| `seshat-pwa/src/lib/types.ts` | EDIT — add `UploadedAttachment`, `UploadState` types |
| `seshat-pwa/src/lib/agui-client.ts` | EDIT — add `presignUpload`, `completeUpload`; extend `SendMessageOptions` |
| `seshat-pwa/src/components/ChatInput.tsx` | EDIT — drag-drop, paste, file picker, attachment chips |
| `seshat-pwa/src/components/StreamingChat.tsx` | EDIT — attachment state; pass to sendChatMessage |

---

## Step-by-Step Plan

### Step 1 — DB migration 0012  
**File:** `docker/postgres/migrations/0012_upload_pending_column.sql`

```sql
ALTER TABLE artifacts
  ADD COLUMN IF NOT EXISTS upload_pending BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN artifacts.upload_pending IS
  'TRUE while the browser is uploading to R2; FALSE once /complete is called. '
  'Pending rows are excluded from all public artifact queries.';
```

**Verify:** `make test-infra-up && psql ... -c "SELECT upload_pending FROM artifacts LIMIT 0"`

---

### Step 1b — Exclude pending rows from existing artifact queries (Blocking #2)
**File:** `src/personal_agent/service/artifacts_router.py`

Add `AND upload_pending = FALSE` to:
1. `list_artifacts` SQL (~line 266)
2. `get_artifact_metadata` SQL (~line 318)
3. `resolve_artifact` SQLAlchemy query (~line 205) — add `.where(ArtifactModel.upload_pending == False)`

Also add `upload_pending` column to `ArtifactModel` in `models.py`.

---

### Step 1c — Fix artifact_read to read uploads (Blocking #3)
**File:** `src/personal_agent/tools/artifact_tools.py`

Change line 639:
```sql
  AND type = 'artifact'
```
to:
```sql
  AND type IN ('artifact', 'upload')
```

Also update the similar filter at line 554 (if it's for `artifact_list` — check if uploads should appear in the agent's list too, they should).

---

### Step 1d — Add `head()` to R2ArtifactStore
**File:** `src/personal_agent/storage/artifact_store.py`

```python
async def head(self, r2_key: str, *, trace_id: str | None = None) -> dict[str, Any]:
    """Return HEAD metadata for an R2 object.

    Returns a dict with at minimum ``content_length: int``.
    Raises ArtifactStoreError if the object does not exist or on network error.
    """
    client = await self._get_client()
    try:
        response = await client.head_object(Bucket=self._bucket, Key=r2_key)
        return {
            "content_length": response.get("ContentLength", 0),
            "content_type": response.get("ContentType", ""),
        }
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        log.info(
            "artifact_store_head_failed",
            bucket=self._bucket,
            r2_key=r2_key,
            error_code=error_code,
            trace_id=trace_id,
        )
        raise ArtifactStoreError(f"R2 head failed for {r2_key}: {exc}") from exc
    except BotoCoreError as exc:
        raise ArtifactStoreError(f"R2 head failed for {r2_key}: {exc}") from exc
```

---

### Step 2 — Settings addition  
**File:** `src/personal_agent/config/settings.py` — add after existing R2 settings (~line 570):

```python
upload_max_size_bytes: int = Field(
    default=52_428_800,  # 50 MiB
    description="Maximum upload size the presign endpoint accepts (bytes).",
)
```

---

### Step 3 — Write failing tests (TDD)  
**File:** `tests/personal_agent/service/test_uploads_router.py`

Tests (all unit-level, mocking DB + R2):

1. `test_presign_returns_upload_url_and_artifact_id` — valid image, 200
2. `test_presign_rejects_disallowed_content_type` — `application/x-executable` → 422
3. `test_presign_rejects_oversized` — size_hint > 50MB → 422
4. `test_presign_inserts_pending_row` — DB insert called with `upload_pending=True`, `size_bytes=0`
5. `test_complete_verifies_r2_head` — mock R2 HEAD → updates row with actual ContentLength, upload_pending=False
6. `test_complete_returns_404_when_no_pending_row` — non-existent or already complete → 404
7. `test_complete_returns_502_when_r2_object_missing` — R2 HEAD raises ArtifactStoreError → 502
8. `test_complete_rejects_if_head_size_exceeds_max` — HEAD ContentLength > max → 422
9. `test_cross_user_cannot_complete_other_users_upload` — user B → 404
10. `test_expire_pending_uploads_deletes_old_rows` — rows older than expiry window get deleted
11. `test_augment_message_with_attachments` — unit test of the helper function
12. `test_attachment_validation_rejects_wrong_user` — validate_attachments returns only owned rows

**Run:** `make test-k test_uploads_router` → should fail before implementation

---

### Step 4 — uploads_router.py  
**File:** `src/personal_agent/service/uploads_router.py`

```
Module-level constants:
  ALLOWED_UPLOAD_CONTENT_TYPES: frozenset[str] = frozenset({
      "image/jpeg", "image/png", "image/gif", "image/webp",
      "text/plain", "application/pdf", "text/csv",
      "application/json", "text/markdown",
  })
  PRESIGN_EXPIRY_SECONDS = 300
  _UPLOAD_EXPIRY_MINUTES = 30
  _MIME_TO_EXT: dict[str, str] = {
      "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
      "image/webp": "webp", "text/plain": "txt", "application/pdf": "pdf",
      "text/csv": "csv", "application/json": "json", "text/markdown": "md",
  }

Pydantic models (all frozen=True):
  class PresignRequest(BaseModel):
      filename: str  # title only; not used in R2 key
      content_type: str
      size_hint: int

  class PresignResponse(BaseModel):
      upload_url: str
      artifact_id: UUID

Endpoints:
  POST /api/uploads/presign
    - Validate content_type in ALLOWED_UPLOAD_CONTENT_TYPES → 422
    - Validate 0 < size_hint <= settings.upload_max_size_bytes → 422
    - artifact_id = uuid4()
    - ext = _MIME_TO_EXT[content_type]
    - r2_key = build_r2_key(type='upload', user_id=request_user.user_id,
                            session_id=None, artifact_id=artifact_id, slug=None, ext=ext)
    - INSERT INTO artifacts (id, user_id, type, title, content_type,
                            size_bytes, r2_key, created_by, created_at, upload_pending)
          VALUES (..., size_bytes=0, created_by='user', created_at=now(), upload_pending=TRUE)
    - upload_url = await store.generate_presigned_put_url(r2_key=r2_key,
                       content_type=content_type, max_size=size_hint,
                       expires_in=PRESIGN_EXPIRY_SECONDS, trace_id=ctx.trace_id)
    - Return PresignResponse(upload_url=upload_url, artifact_id=artifact_id)

  POST /api/uploads/{artifact_id}/complete
    - SELECT id, r2_key, upload_pending FROM artifacts
      WHERE id=artifact_id AND user_id=request_user.user_id
    - Not found OR upload_pending=FALSE → 404
    - With bounded retry (2 attempts, 200ms sleep): store.head(r2_key, trace_id=...)
    - ArtifactStoreError (object missing or transient) → 502
    - actual_size = head_meta["content_length"]
    - Validate actual_size <= settings.upload_max_size_bytes → 422 (defense-in-depth)
    - UPDATE artifacts SET size_bytes=actual_size, upload_pending=FALSE,
                           created_at=NOW() WHERE id=artifact_id AND user_id=...
    - Return ArtifactSummary via text() SELECT (matches _row_to_summary fields)

  (No DELETE endpoint — pending row cleanup handled by background expiry)

async def expire_pending_uploads(db_factory: Callable) -> int:
    """Delete artifact rows with upload_pending=TRUE older than _UPLOAD_EXPIRY_MINUTES."""
    Async function exported for app.py lifespan loop.
    SELECT id, r2_key FROM artifacts WHERE upload_pending=TRUE
      AND created_at < NOW() - INTERVAL '_UPLOAD_EXPIRY_MINUTES minutes'
    For each: try store.delete(r2_key) ignoring ArtifactStoreError
    DELETE FROM artifacts WHERE id IN (ids)
    Return row count
```

All endpoints require `request_user: RequestUser = Depends(get_request_user)`.
All DB ops use `db: AsyncSession = Depends(get_db_session)`.
All R2 ops go through `get_artifact_store()` — if None → 503.
All log calls include `trace_id`.

---

### Step 5 — Wire router in app.py  
**File:** `src/personal_agent/service/app.py`

After the existing `app.include_router(artifacts_router)` line, add:

```python
from personal_agent.service.uploads_router import router as uploads_router
app.include_router(uploads_router)
```

Add `attachments` to `/chat/stream` form signature:

```python
@app.post("/chat/stream")
async def chat_stream_endpoint(
    message: str = Form(...),
    session_id: str = Form(...),
    profile: str | None = Form(default=None),
    client_msg_id: str | None = Form(default=None),
    attachments: str | None = Form(default=None),  # JSON: [{artifact_id, content_type, title}]
    request_user: RequestUser = Depends(get_request_user),
) -> dict[str, str]:
```

In the body, **do NOT augment before `run_gateway_pipeline`** (Blocking #1 — corrupts TaskType routing).
Instead, pass `attachments` to the background task and augment AFTER classification:

```python
asyncio.create_task(_process_chat_stream_background(
    session_id=session_id,
    message=message,         # ← original for DB storage + gateway classification
    attachments_json=attachments,  # ← raw JSON string, augmented inside task
    ...
))
```

Update `_process_chat_stream_background` signature to accept `attachments_json: str | None = None`.

Inside `_process_chat_stream_background`, after `run_gateway_pipeline` and BEFORE `orchestrator.handle_user_request`:

```python
# Validate attachment ownership + completion (Blocking #4)
validated_attachments = await _validate_attachments(attachments_json, user_id, session_id_str)
# Augment orchestrator message only (not the DB-stored message)
orchestrator_message = _augment_message_with_attachments(message, validated_attachments)

result = await orchestrator.handle_user_request(
    user_message=orchestrator_message,  # ← augmented for agent
    ...
)
```

Note: `message` (original) is still used for DB storage (`repo.append_message`) and `run_gateway_pipeline`.

Add helpers:

```python
import json as _json
import asyncio as _asyncio

async def _validate_attachments(
    attachments_json: str | None, user_id: UUID, trace_id: str
) -> list[dict[str, str]]:
    """Parse, ownership-check, and completeness-check each attachment.
    Returns only attachments that exist, belong to user_id, and upload_pending=FALSE."""
    if not attachments_json:
        return []
    try:
        items = _json.loads(attachments_json)
    except (ValueError, TypeError):
        return []
    if not items:
        return []
    valid = []
    async with AsyncSessionLocal() as db:
        for att in items:
            aid = att.get("artifact_id", "")
            if not aid:
                continue
            result = await db.execute(
                text("SELECT id, content_type, title FROM artifacts "
                     "WHERE id = :id AND user_id = :uid AND upload_pending = FALSE"),
                {"id": aid, "uid": user_id},
            )
            row = result.first()
            if row:
                valid.append({
                    "artifact_id": str(row.id),
                    "content_type": row.content_type or "",
                    "title": row.title or str(row.id),
                })
    return valid

def _augment_message_with_attachments(message: str, attachments: list[dict[str, str]]) -> str:
    if not attachments:
        return message
    lines = ["[Attachments — call artifact_read(artifact_id) to read content:]"]
    for att in attachments:
        lines.append(
            f"  - artifact_id: {att['artifact_id']}, "
            f"content_type: {att['content_type']}, filename: {att['title']}"
        )
    return "\n".join(lines) + "\n\n" + message
```

Also wire expiry cleanup in the lifespan loop (add alongside `_dedup_cleanup_loop`):
```python
from personal_agent.service.uploads_router import expire_pending_uploads

async def _upload_expiry_loop() -> None:
    while True:
        await asyncio.sleep(1800)  # every 30 min
        try:
            n = await expire_pending_uploads(AsyncSessionLocal)
            if n:
                log.info("upload_expiry_cleaned", count=n)
        except Exception:
            log.exception("upload_expiry_failed")

upload_expiry_task = asyncio.create_task(_upload_expiry_loop())
```
(cancel in shutdown alongside `ws_cleanup_task`)

---

### Step 6 — PWA types  
**File:** `seshat-pwa/src/lib/types.ts` — add:

```typescript
/** An attachment that has been uploaded to R2 and is ready to send. */
export interface UploadedAttachment {
  artifact_id: string;
  content_type: string;
  title: string;  // filename
}

/** Per-file upload state tracked while the upload is in-progress. */
export interface UploadState {
  id: string;           // local random ID for React keying
  file: File;
  status: 'uploading' | 'complete' | 'error';
  artifact_id?: string; // set when presign succeeds
  error?: string;
}
```

Extend `SendMessageOptions`:
```typescript
export interface SendMessageOptions {
  message: string;
  sessionId: string;
  profile?: ExecutionProfile;
  clientMsgId?: string;
  attachments?: UploadedAttachment[];  // NEW
}
```

---

### Step 7 — agui-client.ts upload helpers  
**File:** `seshat-pwa/src/lib/agui-client.ts`

Add:
```typescript
export interface PresignResponse {
  upload_url: string;
  artifact_id: string;
}

/** Step 1: get a presigned PUT URL from the gateway. */
export async function presignUpload(
  file: File,
): Promise<PresignResponse> {
  const resp = await fetch(`${SESHAT_API}/api/uploads/presign`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      filename: file.name,
      content_type: file.type,
      size_hint: file.size,
    }),
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail ?? `presign failed: ${resp.status}`);
  }
  return resp.json();
}

/** Step 2: PUT the file bytes directly to R2 via the presigned URL. */
export async function uploadToR2(uploadUrl: string, file: File): Promise<void> {
  const resp = await fetch(uploadUrl, {
    method: 'PUT',
    headers: { 'Content-Type': file.type },
    body: file,
  });
  if (!resp.ok) throw new Error(`R2 PUT failed: ${resp.status}`);
}

/** Step 3: tell the gateway the upload is complete. */
export async function completeUpload(artifactId: string): Promise<UploadedAttachment> {
  const resp = await fetch(`${SESHAT_API}/api/uploads/${artifactId}/complete`, {
    method: 'POST',
    headers: { ...authHeaders() },
  });
  if (!resp.ok) throw new Error(`complete failed: ${resp.status}`);
  const data = await resp.json();
  return {
    artifact_id: String(data.artifact_id),
    content_type: data.content_type,
    title: data.title ?? data.slug ?? artifactId,
  };
}

```
// Note: no cancelUpload — cancellation is client-side only (remove chip).
// Pending rows are cleaned up server-side by the expiry background task.

Update `sendChatMessage` to include `attachments` in the form params:
```typescript
if (opts.attachments?.length) {
  params['attachments'] = JSON.stringify(opts.attachments);
}
```

---

### Step 8 — ChatInput.tsx  
**File:** `seshat-pwa/src/components/ChatInput.tsx`

Changes:
- Props: add `onSend: (text: string, attachments: UploadedAttachment[]) => void` (replace `onSend: (text: string) => void`)
- Add prop `onUploadFile: (file: File) => void` — or handle uploads internally + expose completed attachments via `onAttachmentsChange`

**Cleaner design**: handle upload state inside `ChatInput`:
- Internal state: `uploads: UploadState[]`
- Prop addition: none needed; `onSend` receives completed attachments
- On send: filter to `status === 'complete'`, pass as attachments, clear list

```
Drag-drop: wrap the <form> in a drop zone with onDragOver/onDrop
Paste (CRITICAL — fix guard order per Blocking #5):
  In handlePaste: check e.clipboardData.files FIRST.
  If image files found → call e.preventDefault(), trigger upload, return.
  Otherwise → fall through to the existing text normalization path unchanged.
  Do NOT call e.preventDefault() before the files check.
File picker: <input type="file" accept="image/*,.pdf,.txt,.md,.csv,.json" multiple hidden ref>
             triggered by a paperclip button

Upload flow per file:
  1. call presignUpload(file) → {uploadUrl, artifact_id}
  2. add to uploads state: {status:'uploading', artifact_id}
  3. call uploadToR2(uploadUrl, file)
  4. call completeUpload(artifact_id) → UploadedAttachment
  5. set status:'complete'
  On error at any step: status:'error'
  On × button: remove from uploads state (no server call — expiry handles cleanup)

UI below textarea (when uploads non-empty):
  A row of chips, each showing:
    - File type icon (image/doc)
    - Truncated filename
    - Status: spinner (uploading) | ✓ (complete) | ✗ (error)
    - × button to cancel/remove
```

**Send button guard**: `canSend` only when all uploads are `complete` or empty (no mid-upload send).

---

### Step 9 — StreamingChat.tsx  
**File:** `seshat-pwa/src/components/StreamingChat.tsx`

The `ChatInput` now calls `onSend(text, attachments)`. Update the `handleSend` callback:

```typescript
const handleSend = useCallback((text: string, attachments: UploadedAttachment[]) => {
  const msgId = generateUUID();
  sendChatMessage({ message: text, sessionId, profile, clientMsgId: msgId, attachments });
  // ... existing message append to local state
}, [sessionId, profile, ...]);
```

No further changes needed in StreamingChat — the agent receives context via the augmented message.

---

### Step 10 — Quality gates

```bash
make test-k test_uploads_router   # new tests pass
make test                         # full suite
make mypy
make ruff-check && make ruff-format
cd seshat-pwa && npm run lint
```

---

## Acceptance Criteria Proof Plan

| AC | Test / Probe |
|----|-------------|
| AC1 | PWA lint passes; manual verify on deploy |
| AC2 | `test_presign_returns_upload_url_and_artifact_id` verifies no bytes in response |
| AC3 | `test_augment_message_with_attachments` (unit) + orchestrator sees the artifact_id context |
| AC4 | `test_complete_updates_row` — `upload_pending=False` row appears in list query |
| AC5 | `test_cross_user_cannot_complete_other_users_upload` |
| AC6 | `test_presign_rejects_disallowed_content_type`, `test_presign_rejects_oversized` |

---

## Safety Constraints / Gotchas

1. **`upload_pending` default False** — existing rows are unaffected; the migration is additive.
2. **Presigned URL is single-use / expiring** — the browser must PUT within 5 min. No retry path needed at this tier.
3. **R2 HEAD in `/complete`** — use `client.head_object(Bucket=..., Key=...)`, not `get_object`. The `R2ArtifactStore` does not currently expose `head_object`; I'll add a `head` method to it.
4. **Content-Length from R2 HEAD** — Cloudflare R2 returns `ContentLength` in the HEAD response metadata. Use that to update `size_bytes`.
5. **WAF token risk in Linear comment** — any SQL/CLI tokens in the Linear comment body will be stripped per the WAF memory (`reference_linear_mcp_waf_blocks_cli_tokens.md`). The handoff comment is plain prose.
6. **`ALLOWED_UPLOAD_CONTENT_TYPES` constant** — kept in the router file (not settings) to avoid Pydantic frozenset serialization complexity.
7. **PWA paste handler** — current `handlePaste` in `ChatInput.tsx` calls `e.preventDefault()` and normalizes to plain text. The new handler must check `e.clipboardData.files` BEFORE the current text normalization, so image pastes are captured and plain-text paste still works.
8. **Test substrate isolation** — `test_uploads_router.py` is pure unit (mock DB + mock R2), no `make test-infra-up` required.
