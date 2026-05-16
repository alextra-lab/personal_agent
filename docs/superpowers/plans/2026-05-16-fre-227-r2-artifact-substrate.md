# FRE-227 — R2-Backed Artifact Substrate (public-repo half)

**Linear**: [FRE-227](https://linear.app/frenchforest/issue/FRE-227) (Approved · Tier-2:Sonnet)
**Specs**: [ADR-0069](../../architecture_decisions/ADR-0069-r2-backed-artifact-substrate.md) (substrate) · [ADR-0070](../../architecture_decisions/ADR-0070-output-channel-model-markdown-and-rich.md) (output model) · [ADR-0064](../../architecture_decisions/ADR-0064-inbound-user-identity-cloudflare-access.md) (identity)
**Branch**: `fre-227-r2-artifact-substrate`
**Date**: 2026-05-16

---

## Context

ADR-0069 reframes the original FRE-227 ("protected directory tree with agent write access") as a shared **substrate** for a family of features — notes, artifacts (FRE-368), user uploads (FRE-369), URL captures, future auto-updating CLAUDE.md (FRE-226). The substrate is R2-backed (not a local volume) so artifacts are URL-addressable from iPad and laptop browsers without going through the chat surface.

This plan ships the **public-repo half** of FRE-227:

- Postgres `artifacts` table + migration
- `R2ArtifactStore` async S3 client (`aiobotocore`)
- First consumer: `notes_write` / `notes_search` tools with pgvector NN search on Qwen3-Embedding-0.6B vectors
- A gateway internal endpoint that the Worker calls to resolve `artifact_id → r2_key`
- Governance, settings, env, tests

The **private-repo half** (R2 bucket, Worker JS, Cloudflare Access app, DNS — all terraform) ships as a separate Linear ticket targeted at the user's laptop, drafted at the end of this plan and filed after approval. Terraform identifiers do not enter the public repo (per the existing convention).

After this lands, FRE-368 and FRE-369 build on the same substrate without modifying it.

---

## Pre-flight

- New dependency `aiobotocore` is not currently in `pyproject.toml`.
- `src/personal_agent/storage/` does not exist yet — created in this plan.
- Embedding dim is **1024** (`settings.embedding_dimensions` for Qwen3-Embedding-0.6B; see `src/personal_agent/config/settings.py:409`). The existing `embeddings` table uses `vector(1536)` with a stale "OpenAI ada-002" comment — that's a separate inconsistency; the new `artifacts` table uses the project-correct 1024.
- Existing migrations: `0001_cost_gate_schema.sql`, `0002_cost_gate_null_uniqueness.sql`. Next is **0003**.
- Existing CF Access identity layer (`get_request_user` in `src/personal_agent/service/auth.py`) is reused; the substrate adds no new auth surface.

---

## Implementation Steps

### 1. Dependency: `aiobotocore`

**File**: `pyproject.toml`

- Add `aiobotocore>=2.13` to `[project] dependencies`. Pin a minor floor that is compatible with the existing `aiohttp` version pulled in by other deps.
- `uv lock && uv sync`.
- Verify: `python -c "import aiobotocore; print(aiobotocore.__version__)"`.

### 2. Postgres migration `0003_artifacts_schema.sql`

**File**: `docker/postgres/migrations/0003_artifacts_schema.sql`

Mirrors the existing migration style (`BEGIN; ... COMMIT;`, idempotent `IF NOT EXISTS`, header block citing ADR / ticket). Schema per ADR-0069 D4:

```sql
CREATE TABLE IF NOT EXISTS artifacts (
    id              UUID PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES users(user_id),
    session_id      UUID NULL REFERENCES sessions(session_id),
    type            TEXT NOT NULL CHECK (type IN ('note','artifact','upload','capture')),
    slug            TEXT NULL,
    title           TEXT NULL,
    summary         TEXT NULL,
    content_type    TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL CHECK (size_bytes >= 0),
    r2_key          TEXT NOT NULL UNIQUE,
    tags            TEXT[] NOT NULL DEFAULT '{}',
    embedding       vector(1024) NULL,
    created_by      TEXT NOT NULL CHECK (created_by IN ('agent','user')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_artifacts_owner_type_created
    ON artifacts (user_id, type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_artifacts_embedding
    ON artifacts USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_artifacts_tags
    ON artifacts USING gin (tags);

CREATE INDEX IF NOT EXISTS idx_artifacts_session
    ON artifacts (session_id)
    WHERE session_id IS NOT NULL;
```

HNSW parameters match the existing `idx_embeddings_vector` pattern at `init.sql:90-92`.

Verify idempotency: `psql $AGENT_DATABASE_URL -f docker/postgres/migrations/0003_artifacts_schema.sql` runs cleanly **twice** with no error.

### 3. Mirror into `init.sql`

**File**: `docker/postgres/init.sql`

Append the same DDL (without `BEGIN`/`COMMIT`) at the end of the file under a `-- ===== Artifact substrate (ADR-0069 / FRE-227) =====` header so fresh installs receive the table. (`init.sql` only runs on empty Postgres volumes; the migration covers existing dev/prod DBs.)

### 4. Settings — R2 + internal-resolve config

**File**: `src/personal_agent/config/settings.py`

Add a new section after `# --- Embedding & Reranker configuration ---`:

```python
# --- Artifact substrate (ADR-0069 / FRE-227) ---
r2_endpoint_url: str | None = Field(
    default=None,
    description="R2 S3-compatible endpoint URL (e.g. https://<account>.r2.cloudflarestorage.com)",
)
r2_bucket_name: str = Field(
    default="seshat-artifacts",
    description="R2 bucket name for the artifact substrate",
)
r2_access_key_id: str | None = Field(default=None, description="R2 access key id")
r2_secret_access_key: str | None = Field(default=None, description="R2 secret access key")
r2_region: str = Field(default="auto", description="R2 region (S3 SDK requires a value; 'auto' for R2)")
artifacts_public_base_url: str | None = Field(
    default=None,
    description="Public Worker URL prefix, e.g. https://artifacts.frenchforet.com",
)
artifact_resolve_internal_token: str | None = Field(
    default=None,
    description="Shared secret the Worker presents to /internal/artifacts/{id} on the gateway",
)
```

All seven `AGENT_R2_*` / `AGENT_ARTIFACTS_*` env vars are wired via the existing `AGENT_` prefix.

### 5. `.env.example` additions

**File**: `.env.example`

Append a block referencing ADR-0069 and the sibling Linear ticket; placeholders only (no real values).

```bash
# --- Artifact substrate (ADR-0069 / FRE-227) ---
# Populate after laptop-side terraform applies the R2 bucket + Worker.
AGENT_R2_ENDPOINT_URL=
AGENT_R2_BUCKET_NAME=seshat-artifacts
AGENT_R2_ACCESS_KEY_ID=
AGENT_R2_SECRET_ACCESS_KEY=
AGENT_R2_REGION=auto
AGENT_ARTIFACTS_PUBLIC_BASE_URL=
AGENT_ARTIFACT_RESOLVE_INTERNAL_TOKEN=
```

### 6. `R2ArtifactStore` — async S3 client wrapper

**Files**:
- `src/personal_agent/storage/__init__.py` (new)
- `src/personal_agent/storage/artifact_store.py` (new)

**Class shape**:

```python
class R2ArtifactStore:
    """Async R2/S3 wrapper. One instance per process, reused across requests."""

    ALLOWED_TYPES: ClassVar[frozenset[str]] = frozenset({"note", "artifact", "upload", "capture"})

    def __init__(self, *, endpoint_url: str, bucket: str,
                 access_key_id: str, secret_access_key: str, region: str = "auto") -> None: ...

    async def __aenter__(self) -> "R2ArtifactStore": ...
    async def __aexit__(self, *exc: object) -> None: ...

    @staticmethod
    def build_r2_key(*, type: str, user_id: UUID, session_id: UUID | None,
                     artifact_id: UUID, slug: str | None, ext: str) -> str:
        """{type}/{user_id}/{session_id|GLOBAL}/{artifact_id}_{slug}.{ext} (ADR-0069 D5).

        Raises ArtifactKeyError if type not in ALLOWED_TYPES, slug contains '/' / '..'
        / control chars / leading '-', or ext contains '/'.
        """

    async def put(self, *, r2_key: str, content: bytes, content_type: str,
                  metadata: Mapping[str, str] | None = None) -> None: ...

    async def get(self, r2_key: str) -> bytes: ...  # used by tests + internal-resolve admin path

    async def delete(self, r2_key: str) -> None: ...

    async def generate_presigned_put_url(self, *, r2_key: str, content_type: str,
                                         max_size: int, expires_in: int = 900) -> str:
        """Presigned PUT URL for FRE-369 user-upload flow. Implemented now per ADR-0069 D7."""
```

Notes:
- Uses `aiobotocore.session.AioSession().create_client('s3', endpoint_url=..., aws_access_key_id=..., aws_secret_access_key=..., region_name=...)`.
- Client lifecycle: the class is an async context manager **and** also caches a long-lived client for in-process reuse (lazy `_get_client()` that opens-on-first-call and closes on shutdown via FastAPI lifespan hook).
- `build_r2_key` slug regex: `^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$` — rejects empty slugs, leading symbols, traversal, and slashes.
- Typed exceptions in `src/personal_agent/exceptions.py`: `ArtifactKeyError(ValueError)`, `ArtifactStoreError(Exception)` (wraps `botocore.exceptions.ClientError`).
- All operations log via `structlog` with `trace_id` extracted from caller.

A module-level `get_artifact_store()` returns the singleton, constructed from `settings` on first call; returns `None` if R2 env vars are unset (callers must handle the "substrate not configured" case gracefully).

### 7. Notes tools

**File**: `src/personal_agent/tools/notes_tools.py` (new)

Two tools registered following the `web.py` pattern (`ToolDefinition` + async executor in `tools/types.py`).

**`notes_write`** — `ToolDefinition`:
- Params: `slug: str` (required, validated), `content: str` (required), `title: str | None`, `mode: Literal['append','overwrite'] = 'append'`, `tags: list[str] | None`
- Description directs the model to use this for "thought trails, durable notes, knowledge to carry across sessions"
- `risk_level="medium"`, `requires_approval=False`, `timeout_seconds=20`, `rate_limit_per_hour=60`

**Executor `notes_write_executor`**:

1. Resolve `user_id` from `ctx`. The orchestrator already populates `TaskCapture.user_id`; access via `ctx.user_id` (matches the FRE-343 pattern). Fall back to `settings.owner_user_id` resolution if absent (ADR-0064 D4 dev path).
2. Validate `slug` via `R2ArtifactStore.build_r2_key`'s slug regex (call the static `_validate_slug` helper before generating IDs).
3. In `mode='append'`: SELECT the most-recent `(user_id, slug, type='note')` row. If found, fetch its content from R2 and concatenate `existing + "\n\n" + content`. If absent, treat as fresh write.
4. Generate embedding: `await generate_embedding(final_content, mode='document')` — reuses `src/personal_agent/memory/embeddings.py`. Returns 1024-dim list.
5. Allocate new `artifact_id = uuid4()`. Build `r2_key` (ext `md`, content_type `text/markdown; charset=utf-8`).
6. `R2ArtifactStore.put(r2_key, content=final_content.encode("utf-8"), content_type="text/markdown; charset=utf-8")`.
7. INSERT a new `artifacts` row (we never UPDATE — each `append` is a new revision sharing the same slug; latest-by-`created_at` wins for reads).
8. Return `{"artifact_id": str(id), "public_url": f"{settings.artifacts_public_base_url}/{id}", "slug": slug, "mode_applied": mode, "size_bytes": len(...)}`.

**`notes_search`** — `ToolDefinition`:
- Params: `query: str` (required), `k: int = 5`, `tags: list[str] | None`
- `risk_level="low"`, `timeout_seconds=10`, `rate_limit_per_hour=200`

**Executor `notes_search_executor`**:

1. Resolve `user_id` as above.
2. `query_emb = await generate_embedding(query, mode='query')` (the embeddings module already prepends the Qwen instruction prefix in query mode).
3. SQL:
   ```sql
   SELECT id, slug, title, summary, created_at,
          1 - (embedding <=> $1::vector) AS similarity
   FROM artifacts
   WHERE user_id = $2
     AND type = 'note'
     AND embedding IS NOT NULL
     AND ($3::text[] IS NULL OR tags && $3::text[])
   ORDER BY embedding <=> $1::vector
   LIMIT $4;
   ```
4. Return `{"results": [{artifact_id, slug, title, summary, similarity, public_url, created_at}, ...]}`.
5. No content body in the response — keeps token cost predictable. The agent dereferences via the public URL or via a follow-up `notes_read(slug)` (out of scope for this PR; logged as `# TODO(FRE-368)`).

DB access via the existing async SQLAlchemy session pattern in `src/personal_agent/service/db.py` — pass `AsyncSession` in or grab one from the session factory.

### 8. Register tools

**File**: `src/personal_agent/tools/__init__.py`

In `register_mvp_tools(registry)`, after the existing memory/web registrations:

```python
if settings.r2_bucket_name and settings.r2_access_key_id and settings.r2_endpoint_url:
    registry.register(notes_write_tool, notes_write_executor)
    registry.register(notes_search_tool, notes_search_executor)
    log.info("notes_tools_registered", bucket=settings.r2_bucket_name)
else:
    log.warning("notes_tools_skipped_unconfigured")
```

This gate ensures the tools are never advertised to the LLM in environments where R2 is unwired (unit-test runs without R2 env, eval profiles, etc.).

### 9. Governance entries

**File**: `config/governance/tools.yaml`

Two new entries patterned after existing `write` / `web_search` blocks:

```yaml
notes_write:
  category: "artifact_write"
  allowed_in_modes: ["NORMAL", "ALERT", "DEGRADED"]
  forbidden_in_modes: ["LOCKDOWN", "RECOVERY"]
  risk_level: "medium"
  requires_approval: false
  timeout_seconds: 20
  rate_limit_per_hour: 60
  loop_max_per_signature: 3   # discourage tight-loop overwrites of the same slug
  loop_max_consecutive: 5

notes_search:
  category: "memory_read"
  allowed_in_modes: ["NORMAL", "ALERT", "DEGRADED", "RECOVERY"]
  forbidden_in_modes: ["LOCKDOWN"]
  risk_level: "low"
  requires_approval: false
  timeout_seconds: 10
  rate_limit_per_hour: 200
```

`artifact_write` is a new category — namespaced cleanly so the broader `system_write` rules around sandbox paths don't bleed in. The category name will be reused by FRE-368.

### 10. Gateway internal artifact-resolve endpoint (for the Worker)

**File**: `src/personal_agent/service/artifacts_router.py` (new), mounted in `src/personal_agent/service/app.py`

Purpose: the Worker calls back here to translate `{artifact_id}` → `{r2_key, content_type, size_bytes}` because the Worker doesn't talk to Postgres directly (chosen pattern this session — see AskUserQuestion response).

```python
@router.get("/internal/artifacts/{artifact_id}")
async def resolve_artifact(
    artifact_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ArtifactResolveResponse:
    # 1. Validate shared-secret header from Worker.
    token = request.headers.get("X-Internal-Token")
    if not token or not secrets.compare_digest(token, settings.artifact_resolve_internal_token or ""):
        raise HTTPException(401)

    # 2. The Worker validated CF Access already and forwards the verified email.
    email = request.headers.get("X-Authenticated-User-Email")
    if not email:
        raise HTTPException(404)  # ADR-0064 D3 — 404 on auth-shape mismatch.

    # 3. Resolve email -> user_id via existing helper.
    user_id = await resolve_user_id_by_email(db, email)
    if not user_id:
        raise HTTPException(404)

    # 4. SELECT artifact; 404 if absent OR user_id mismatch (existence-hiding per ADR-0064 D3).
    row = await db.execute(
        select(ArtifactModel).where(ArtifactModel.id == artifact_id,
                                    ArtifactModel.user_id == user_id))
    art = row.scalar_one_or_none()
    if not art:
        raise HTTPException(404)

    return ArtifactResolveResponse(
        r2_key=art.r2_key,
        content_type=art.content_type,
        size_bytes=art.size_bytes,
        created_at=art.created_at,
    )
```

- Not behind CF Access — only the Worker can reach it (Cloudflare Tunnel ingress for `api.frenchforet.com` already exists; the Worker has the internal token).
- Constant-time token compare via `secrets.compare_digest`.
- New SQLAlchemy ORM model `ArtifactModel` lands in `src/personal_agent/service/models.py` alongside `UserModel` / `SessionModel`.

### 11. Tests

**Files**:
- `tests/personal_agent/storage/test_artifact_store.py` (new)
- `tests/personal_agent/tools/test_notes_tools.py` (new)
- `tests/personal_agent/service/test_artifacts_router.py` (new)
- `conftest.py` fixtures as needed; mirrors `src/` per project convention.

**Coverage** (all unit, no markers — fast tier):

| Test | What it asserts |
|---|---|
| `test_build_r2_key_happy_path` | Output matches `{type}/{user_id}/{session_id_or_GLOBAL}/{artifact_id}_{slug}.{ext}` exactly |
| `test_build_r2_key_session_null_renders_GLOBAL` | session_id=None → `GLOBAL` segment in key |
| `test_build_r2_key_rejects_traversal_slug` | slug=`../etc/passwd` → `ArtifactKeyError` |
| `test_build_r2_key_rejects_slash_in_slug` | slug=`foo/bar` → `ArtifactKeyError` |
| `test_build_r2_key_rejects_disallowed_type` | type=`system` → `ArtifactKeyError` |
| `test_build_r2_key_rejects_control_chars` | slug with `\x00`, `\n` → `ArtifactKeyError` |
| `test_put_get_roundtrip_mocked` | aiobotocore client mocked via `patch("aiobotocore.session.AioSession.create_client")`; assert correct args, ContentType propagated |
| `test_presigned_put_url_includes_content_length_cap` | mock-asserts `Conditions` includes `[content-length-range, 0, max_size]` |
| `test_notes_write_append_concatenates_existing` | DB pre-seeded with one note; new write produces a new row whose R2 content = `prior + "\n\n" + new` |
| `test_notes_write_overwrite_ignores_prior_content` | mode='overwrite' produces a new row whose content = new only |
| `test_notes_write_invalid_slug_rejected_pre_id` | slug fails validation before any UUID/embedding/R2 work occurs |
| `test_notes_write_embedding_dim_matches_settings` | inserted row has `len(embedding) == 1024` |
| `test_notes_search_cross_user_isolation` | seed 3 notes (user A, user B); search by user A returns only A's |
| `test_notes_search_orders_by_similarity` | seed notes with controlled embeddings; verify top-k order |
| `test_notes_search_respects_tags_filter` | tags=['proj-x'] returns only proj-x-tagged notes |
| `test_resolve_endpoint_happy_path` | valid token + matching email → 200 with metadata |
| `test_resolve_endpoint_bad_token_401` | wrong token → 401 (token mismatch is the only 401; everything else is 404) |
| `test_resolve_endpoint_missing_email_404` | header absent → 404 |
| `test_resolve_endpoint_cross_user_404` | artifact exists but owned by another user → 404 (ADR-0064 D3) |
| `test_resolve_endpoint_unknown_id_404` | id doesn't exist → 404 |

Mock strategy:
- aiobotocore S3 client: `patch("personal_agent.storage.artifact_store.AioSession")` with a fake that records `put_object` / `get_object` calls.
- Embeddings: `patch("personal_agent.tools.notes_tools.generate_embedding", return_value=[0.0]*1024)` for default, deterministic vectors for similarity-order test.
- DB: existing `async_session_fixture` from `conftest.py` against a test Postgres.

Pre-tool-use hook `.claude/hooks/check-pytest-lock.sh` blocks parallel runs — single `make test` at a time.

### 12. Quality gates

Run in this order:

```bash
uv sync                                                # picks up aiobotocore
psql $AGENT_DATABASE_URL -f docker/postgres/migrations/0003_artifacts_schema.sql
psql $AGENT_DATABASE_URL -f docker/postgres/migrations/0003_artifacts_schema.sql   # idempotency
make test-file FILE=tests/personal_agent/storage/test_artifact_store.py
make test-file FILE=tests/personal_agent/tools/test_notes_tools.py
make test-file FILE=tests/personal_agent/service/test_artifacts_router.py
make test                                              # full unit suite
make mypy
make ruff-check
make ruff-format
```

All must pass before PR. Quality-gates checklist mirrors the project `Final Checklist` in `.claude/CLAUDE.md`.

### 13. Branch + commit + PR

- Branch off main: `git checkout -b fre-227-r2-artifact-substrate`
- Stage explicit paths (no `-A`)
- PR title: `feat(fre-227): R2-backed artifact substrate + notes tools`
- PR body: links ADR-0069 + ADR-0070; cross-links the sibling Linear ticket (terraform half) so the reviewer knows the substrate is half-deployed until that ticket lands; calls out that the gateway logs `notes_tools_skipped_unconfigured` until R2 env vars arrive.

After PR merges, deploy and verify the empty-table happy path (gateway boots, tools register conditionally, no errors).

---

## Critical Files

### New
- `src/personal_agent/storage/__init__.py`
- `src/personal_agent/storage/artifact_store.py`
- `src/personal_agent/tools/notes_tools.py`
- `src/personal_agent/service/artifacts_router.py`
- `docker/postgres/migrations/0003_artifacts_schema.sql`
- `tests/personal_agent/storage/test_artifact_store.py`
- `tests/personal_agent/tools/test_notes_tools.py`
- `tests/personal_agent/service/test_artifacts_router.py`

### Modified
- `pyproject.toml` (add `aiobotocore`)
- `docker/postgres/init.sql` (mirror `artifacts` DDL at end)
- `src/personal_agent/config/settings.py` (R2 + resolve-token fields)
- `.env.example` (R2 placeholder block)
- `src/personal_agent/tools/__init__.py` (conditional registration of notes tools)
- `src/personal_agent/service/models.py` (`ArtifactModel`)
- `src/personal_agent/service/app.py` (mount `artifacts_router`)
- `src/personal_agent/exceptions.py` (`ArtifactKeyError`, `ArtifactStoreError`)
- `config/governance/tools.yaml` (`notes_write`, `notes_search`)

### Reused (read-only)
- `src/personal_agent/memory/embeddings.py:generate_embedding` — embedding pipeline (Qwen3-Embedding-0.6B, 1024-dim)
- `src/personal_agent/service/auth.py:get_request_user`, `get_or_create_user_by_email` — identity resolution
- `src/personal_agent/service/db.py` — async session factory
- `src/personal_agent/telemetry/trace.py:TraceContext` — trace plumbing

---

## Verification (end-to-end, post-deploy)

Once both halves are deployed (this PR + the sibling terraform ticket below):

1. **Round-trip** — `uv run agent "Save a note slug='fre227-test' with content 'hello substrate'"` → tool calls `notes_write` → row appears in `artifacts` table with non-null `embedding` → R2 object exists → `https://artifacts.frenchforet.com/{artifact_id}` opens the bytes via Worker.
2. **NLP search** — `uv run agent "Find my notes about the substrate"` → `notes_search` returns the previous note with `similarity > 0.6`.
3. **Cross-session persistence** — `uv run agent chat "..." --new` → `notes_search` still finds the note.
4. **Prefix-escape** — direct unit-test call `notes_write(slug="../evil", ...)` raises `ArtifactKeyError` before any R2 or DB activity.
5. **Cross-user 404** — direct GET against the public Worker URL for another user's artifact returns 404 (ADR-0064 D3 semantics; tested via the resolve-endpoint unit test as a proxy).
6. **Sovereignty** — `aws s3api get-bucket-location --bucket seshat-artifacts --endpoint-url $AGENT_R2_ENDPOINT_URL` confirms EU. (Manual verification step in the sibling ticket; not part of CI.)
7. **CLI fallback** — `uv run agent "save a note"` in a shell without CF Access headers resolves to `AGENT_OWNER_EMAIL` and the note becomes visible to the same user in the PWA.
8. **Substrate-not-configured graceful degradation** — start gateway with R2 env vars unset → logs `notes_tools_skipped_unconfigured` → `notes_*` tools not advertised → other tools work normally.

---

## Sibling Linear ticket to file post-approval

After this plan is approved I will file the following new Linear issue. It is **not** part of this plan's edits — listing it here so you can review the body before I open it.

**Title**: `Terraform: R2 + Worker + Access app for FRE-227 substrate`
**Team**: FrenchForest
**State**: Approved
**Labels**: `Tier-3:Haiku`, `PersonalAgent`
**Block / relation**: blocks FRE-227 closing (substrate cannot serve public URLs until this lands)

**Body** (draft — targeted at the user's laptop Claude Code working against `~/Dev/personal_agent_secrets/infrastructure/terraform-cloudflare/`):

> Adds the cloud-side half of FRE-227 (R2 bucket, Worker, Cloudflare Access app, DNS) to the private secrets repo. Public-repo half ships in FRE-227 PR.
>
> ### Resources to add
>
> 1. **R2 bucket** — `cloudflare_r2_bucket "seshat_artifacts"`, jurisdiction `eu`, public access disabled (Worker-fronted only).
> 2. **R2 API token** — generated out-of-band in CF dashboard (or via `cloudflare_api_token` + custom scope); access key + secret captured into local `.tfvars` and copied into the VPS `.env` as `AGENT_R2_ACCESS_KEY_ID` / `AGENT_R2_SECRET_ACCESS_KEY` post-apply.
> 3. **Worker script** — `cloudflare_workers_script "artifacts_substrate"` with bindings:
>    - `R2_BUCKET` → the `seshat_artifacts` bucket
>    - `GATEWAY_INTERNAL_URL` → `https://api.frenchforet.com/internal/artifacts` (existing tunnel ingress)
>    - `INTERNAL_TOKEN` → secret_text binding (the value also lands in VPS `.env` as `AGENT_ARTIFACT_RESOLVE_INTERNAL_TOKEN`)
> 4. **Worker source** (`worker/artifacts.js` next to the HCL):
>    - Parse `artifact_id` from path; validate UUID shape; 404 on malformed.
>    - GET `${GATEWAY_INTERNAL_URL}/${artifact_id}` with headers `X-Internal-Token: ${INTERNAL_TOKEN}`, `X-Authenticated-User-Email: ${request.cf.access.user.email}` (CF Access populates this on validated requests).
>    - On 200 → `R2.get(r2_key)` → stream bytes back with `Content-Type` from metadata.
>    - On 401/404 from gateway → mirror to caller.
> 5. **Worker custom domain** — `cloudflare_workers_custom_domain "artifacts"` binding the script to `artifacts.frenchforet.com`.
> 6. **DNS record** — proxied CNAME `artifacts.frenchforet.com` → Worker (or whatever the v5 provider expects for Workers custom domains).
> 7. **Cloudflare Access application** — `cloudflare_zero_trust_access_application "artifacts"` on `artifacts.frenchforet.com`, session duration `720h` (per FRE-370 convention), `auto_redirect_to_identity = true`.
> 8. **Access policy** — `cloudflare_zero_trust_access_policy "personal_only"` referencing the same allowlist used by the agent app (the four CF Access users from FRE-344).
>
> ### Apply steps
>
> ```bash
> cd ~/Dev/personal_agent_secrets/infrastructure/terraform-cloudflare
> terraform init -upgrade
> terraform plan -out=fre-227.plan
> # review the plan
> terraform apply fre-227.plan
> ```
>
> ### Post-apply
>
> 1. Capture R2 access key + secret + endpoint URL from the apply output (or CF dashboard).
> 2. SSH to VPS, edit `/opt/seshat/.env`:
>    - `AGENT_R2_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com`
>    - `AGENT_R2_ACCESS_KEY_ID=<from output>`
>    - `AGENT_R2_SECRET_ACCESS_KEY=<from output>`
>    - `AGENT_ARTIFACTS_PUBLIC_BASE_URL=https://artifacts.frenchforet.com`
>    - `AGENT_ARTIFACT_RESOLVE_INTERNAL_TOKEN=<generated; must match Worker binding>`
> 3. `ENV=cloud make restart SERVICE=seshat-gateway`
> 4. Verify the gateway log line `notes_tools_registered` appears (replaces the `…_skipped_unconfigured` warning).
> 5. From iPad Safari: log in via CF Access; `GET https://artifacts.frenchforet.com/<known-id>` returns the bytes.
> 6. Negative: a logged-out browser tab on the same URL is bounced to Access; an unauthorized email is also bounced (Access stops it before the Worker).
>
> ### Verification
>
> Both halves green = FRE-227 closes.

---

## Out of scope (deferred)

- **`notes_read(slug)`** as an explicit tool — defer to FRE-368 (artifact read is a fuller surface).
- **Embedding backfill** — no historical notes exist yet; no backfill needed.
- **Workers KV mapping cache** — ADR-0069 calls this out as a follow-up "if measured fetch volume warrants it"; defer.
- **pgvector ivfflat vs hnsw** — using hnsw to match existing precedent; no benchmarking exercise.
- **Per-tool R2 prefix governance** beyond `type` discrimination — FRE-368 introduces `artifact` writes; this PR's prefix guard is at the `build_r2_key` type check, not yet at the governance layer.
- **Slug uniqueness across revisions** — intentional: every append is a new row with the same slug; latest-by-`created_at` wins. The unique constraint is on `r2_key` (which embeds `artifact_id`), not on slug.
