# FRE-368 — Agent artifact tools + PWA inline cards

**Linear**: [FRE-368](https://linear.app/frenchforest/issue/FRE-368) (Approved · Tier-2:Sonnet · Medium)
**Specs**: ADR-0069 (substrate, Implemented), ADR-0070 (output channel model, Proposed — FRE-368 is the experimental rig)
**Substrate prereq**: FRE-227 + FRE-371 (shipped 2026-05-17, PRs #62/#63/#64/#65) — R2 + Worker + CF Access JWT verification live end-to-end
**Branch base**: `main` at `1996f68`
**Working directory**: `/opt/seshat` (primary repo clone, branch `main`, clean)

---

## Context

The R2-backed artifact substrate ([ADR-0069](../../architecture_decisions/ADR-0069-r2-backed-artifact-substrate.md)) shipped 2026-05-17 with `notes_*` as its first consumer. FRE-368 adds the **second consumer — agent-produced artifacts** (HTML reports, charts, comparison tables, dashboards) that benefit from URL-addressability for human consumption rather than agent re-ingestion.

Per [ADR-0070](../../architecture_decisions/ADR-0070-output-channel-model-markdown-and-rich.md), this ticket is also the **build-to-learn rig**: it produces the measurement data (D8) that determines whether the channel model holds or whether a dual-representation (`reply_markdown` + `reply_html`) refactor is later justified. The chat reply itself stays markdown; artifacts are referenced inline as cards, expanded in a sandboxed viewer, and reachable at a stable `artifacts.example.com/{id}` URL.

The substrate (Postgres `artifacts` table, `R2ArtifactStore`, `/internal/artifacts/{id}` resolver) is already complete — **no schema, no Worker, no Access changes** are needed. The `type` column's CHECK constraint already permits `'artifact'`; the JWT-verified internal resolver already serves any type. FRE-368 layers three new tools, a public-facing listing endpoint, a card-click telemetry endpoint, and PWA components on top.

---

## Acceptance Criteria — Definition of Done for FRE-368

FRE-368 is **not Done** in Linear until every item in this table is checked. Items are labelled by when they can be completed; post-deploy items are not optional — they are required for closure.

| # | Criterion | When | How to verify |
|---|---|---|---|
| AC-1 | `make test` passes (2400+ tests, 0 failed) | Pre-merge | CI / local run |
| AC-2 | `make mypy` clean | Pre-merge | 0 errors |
| AC-3 | `make ruff-check` + `make ruff-format` clean | Pre-merge | 0 errors |
| AC-4 | `artifact_write`, `artifact_list`, `artifact_read` visible to LLM in NORMAL mode | Post-deploy (PR #A) | `make shell SERVICE=seshat-gateway` → `tool_registry.list_tools(mode='NORMAL')` shows all three |
| AC-5 | CLI write → list → read round-trip succeeds | Post-deploy (PR #A) | `uv run agent "Use artifact_write to save HTML titled 'Round-trip smoke', then artifact_list, then artifact_read"` — public_url in output matches artifact_id |
| AC-6 | Worker serves `type='artifact'` rows — no Worker changes needed | Post-deploy (PR #A) | Open `https://artifacts.example.com/{artifact_id}` in iPad Safari → CF Access gate + bytes render |
| AC-7 | Inline artifact card renders in chat when assistant reply contains an artifact URL | Post-deploy (PR #B) | Ask agent to write an HTML artifact → URL in reply → card appears with title/summary/open button |
| AC-8 | Sandboxed viewer: `sandbox=""` enforced, script blocked | Post-deploy (PR #B) | Write artifact with `<script>document.title='PWNED'</script>` → expand → title stays "Artifact" |
| AC-9 | WKWebView → Safari handoff on iPad PWA | Post-deploy (PR #B) | "Open standalone ↗" on installed home-screen PWA → opens in Safari, CF Access SSO covers it |
| AC-10 | `/artifacts` route shows list of user's artifacts | Post-deploy (PR #B) | Navigate from session drawer → list renders; each entry opens viewer |
| AC-11 | `artifact_card_click` telemetry emits to ES on Expand | Post-deploy (PR #B) | DevTools Network → 204 on POST; `curl es.example.com/seshat-logs-*/_search?q=event_type:artifact_card_click` returns hits |
| AC-12 | ADR-0070 status updated from Proposed → Accepted or amended | After two-week review ≥ 2026-06-04 | ADR file Status line updated; ES query confirms `artifact_write` + `artifact_card_click` counts |

**PR #A closes** when AC-1 through AC-6 are done.
**FRE-368 closes** when AC-1 through AC-11 are done (AC-12 is the follow-up D8 review, tracked in MASTER_PLAN).

---

## Shipping shape: two PRs

| | PR #A — Backend | PR #B — PWA |
|--|--|--|
| **Branch** | `fre-368-artifact-tools-backend` | `fre-368-artifact-pwa-cards` |
| **Scope** | 3 tools, governance, public listing endpoint, card-click telemetry endpoint, pytest coverage | Inline card component, sandboxed viewer, `/artifacts` route, link-handler extension in `MarkdownContent`, telemetry caller, SW cache bump |
| **Tests** | `tests/personal_agent/{tools,service}/test_*.py` (pytest, mocked R2 + Postgres) | Manual test plan in component-top docstrings (project convention — see `ApprovalModal.tsx` lines 8-37) |
| **Ships independently** | Yes — tools usable from CLI; URLs already work via existing Worker | Yes — once PR #A is on `main`, the PWA can light up cards over real artifact data |

PR #B depends on PR #A landing first (so the listing + telemetry endpoints exist). Each PR is independently mergeable.

---

## PR #A — Backend

### A1. New file: `src/personal_agent/tools/artifact_tools.py`

Mirror the structure of `src/personal_agent/tools/notes_tools.py` (the canonical FRE-227 template). Three tools, all gated on `settings.r2_endpoint_url + r2_access_key_id + r2_secret_access_key` being populated (same gate that already wraps `notes_write`/`notes_search` registration).

**Module-level constants:**
```python
_ARTIFACT_TYPE = "artifact"
_MAX_CONTENT_BYTES = 5 * 1024 * 1024   # 5 MB — matches ticket size cap
_MAX_INLINE_READ_BYTES = 256 * 1024    # 256 KB — artifact_read returns bytes inline below this; URL-only above
# Permitted content_types — keep tight; expanded via ADR amendment per ADR-0070 D7.
_ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset({
    "text/html; charset=utf-8",
    "text/markdown; charset=utf-8",
    "text/csv; charset=utf-8",
    "application/json",
    "image/png",
    "image/svg+xml",
})
# Map content_type → extension used in r2_key (re-used by build_r2_key)
_EXT_BY_CONTENT_TYPE: dict[str, str] = {
    "text/html; charset=utf-8":     "html",
    "text/markdown; charset=utf-8": "md",
    "text/csv; charset=utf-8":      "csv",
    "application/json":             "json",
    "image/png":                    "png",
    "image/svg+xml":                "svg",
}
```

Reuse from `notes_tools.py` verbatim (copy or import — prefer extraction to `tools/_artifact_common.py` only if a third consumer arrives; for two consumers, duplicate is cheaper than abstraction): `_resolve_user_id`, `_resolve_session_id`, `_public_url`, `_pgvector_literal`. If duplication, add a tiny module-level comment pointing back at the canonical implementation in `notes_tools.py`.

**Tool definitions** (mirror `notes_write_tool` shape at `notes_tools.py:67-128`):

```python
artifact_write_tool = ToolDefinition(
    name="artifact_write",
    description=(
        "Persist a human-facing artifact (HTML report, chart, comparison "
        "table, generated document) to the R2 substrate. Returns a stable "
        "public URL the user can open in a browser. Use for content the "
        "user will revisit, share, or bookmark; use `notes_write` for "
        "agent-internal durable notes."
    ),
    category="artifact_write",
    parameters=[
        ToolParameter(name="slug",         type="string", description="Human-readable handle (e.g. 'q3-spend-summary')", required=True),
        ToolParameter(name="content_type", type="string", description=f"MIME type. One of: {sorted(_ALLOWED_CONTENT_TYPES)}", required=True),
        ToolParameter(name="content",      type="string", description="Body. UTF-8 text for text/* and json; base64 for image/png.", required=True),
        ToolParameter(name="title",        type="string", required=False, default=None),
        ToolParameter(name="summary",      type="string", required=False, default=None, description="One-sentence summary used in inline cards"),
        ToolParameter(name="tags",         type="array",  required=False, default=None, json_schema={"type": "array", "items": {"type": "string"}}),
    ],
    risk_level="medium",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    rate_limit_per_hour=30,
)

artifact_list_tool = ToolDefinition(
    name="artifact_list",
    description=(
        "List recent artifacts owned by the current user. Returns metadata "
        "and public URLs only; call `artifact_read` to ingest content."
    ),
    category="memory_read",
    parameters=[
        ToolParameter(name="prefix", type="string", required=False, default=None, description="Optional slug prefix filter"),
        ToolParameter(name="k",      type="integer", required=False, default=10),
        ToolParameter(name="since",  type="string", required=False, default=None, description="ISO-8601 timestamp; only artifacts created after"),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "RECOVERY"],
    requires_approval=False,
    timeout_seconds=10,
    rate_limit_per_hour=200,
)

artifact_read_tool = ToolDefinition(
    name="artifact_read",
    description=(
        "Fetch an artifact's metadata and (for textual artifacts under "
        "256 KB) its content, so the agent can revise or build upon a "
        "prior artifact. For larger or binary artifacts, returns the "
        "public URL only."
    ),
    category="memory_read",
    parameters=[
        ToolParameter(name="artifact_id", type="string", required=True),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "RECOVERY"],
    requires_approval=False,
    timeout_seconds=10,
    rate_limit_per_hour=60,
)
```

**Executors** — three async functions following the same pattern as `notes_write_executor` (`notes_tools.py:232-378`):

`artifact_write_executor` body sketch:
1. `store = get_artifact_store()`; raise `ToolExecutionError` if None.
2. Validate: `content_type in _ALLOWED_CONTENT_TYPES`; raise `ToolExecutionError("unsupported content_type …")` otherwise.
3. Decode content: for `image/png` decode base64; for text types encode to UTF-8 bytes. Reject empty content.
4. Size guard: `len(payload) <= _MAX_CONTENT_BYTES` else `ToolExecutionError("artifact exceeds 5 MB cap")`.
5. `user_id = _resolve_user_id(ctx)`; `session_id = _resolve_session_id(ctx)`; `artifact_id = uuid4()`.
6. `r2_key = build_r2_key(type="artifact", user_id=…, session_id=…, artifact_id=…, slug=slug, ext=_EXT_BY_CONTENT_TYPE[content_type])` — wrap `ArtifactKeyError → ToolExecutionError` exactly as `notes_tools.py:300-303`.
7. Generate embedding from `f"{title or ''}\n{summary or ''}\n{' '.join(tags or [])}\n{slug}"` via `generate_embedding(...)` (skip if all empty → embedding = None).
8. `await store.put(r2_key=r2_key, content=payload, content_type=content_type, metadata={"artifact_id": str(artifact_id)})`.
9. INSERT row into `artifacts` (Postgres) — copy the SQL from `notes_tools.py:329-360` but pass `type='artifact'`, `created_by='agent'`, and include `title`, `summary`, `tags` (and `embedding` cast `[:vector]`).
10. Return:
```python
return {
    "artifact_id": str(artifact_id),
    "public_url":  _public_url(artifact_id),
    "slug":        slug,
    "content_type": content_type,
    "size_bytes":  len(payload),
    "title":       title,
    "summary":     summary,
}
```

`artifact_list_executor` body sketch — pure Postgres read, filter to `user_id == ctx.user_id AND type='artifact'`:
```sql
SELECT id, slug, title, summary, content_type, tags, created_at
FROM artifacts
WHERE user_id = :user_id
  AND type    = 'artifact'
  AND (:prefix IS NULL OR slug LIKE :prefix || '%')
  AND (:since  IS NULL OR created_at > :since)
ORDER BY created_at DESC
LIMIT :k
```
Return shape: `{"results": [{"artifact_id": str(row.id), "public_url": _public_url(row.id), "slug": ..., "title": ..., "summary": ..., "content_type": ..., "tags": list(row.tags), "created_at": row.created_at.isoformat()}]}`.

`artifact_read_executor` body sketch:
1. Resolve `user_id`; parse `artifact_id` (raise `ToolExecutionError` on UUID parse failure).
2. SELECT row `WHERE id = :artifact_id AND user_id = :user_id` — if not found, raise `ToolExecutionError("artifact not found")` (existence-hiding per ADR-0064 D3 — the executor's caller is the agent, but the same 404-style semantics apply for cross-user safety).
3. If `row.size_bytes > _MAX_INLINE_READ_BYTES` OR `row.content_type` not in `{"text/html; charset=utf-8", "text/markdown; charset=utf-8", "text/csv; charset=utf-8", "application/json"}`: skip the R2 fetch and return URL-only.
4. Otherwise `bytes_ = await store.get(row.r2_key)`; decode UTF-8 (text types).
5. Return:
```python
return {
    "artifact_id": str(row.id),
    "public_url":  _public_url(row.id),
    "slug":        row.slug,
    "title":       row.title,
    "summary":     row.summary,
    "content_type": row.content_type,
    "size_bytes":  row.size_bytes,
    "tags":        list(row.tags or []),
    "created_at":  row.created_at.isoformat(),
    "content":     text_or_none,    # only populated for small textual artifacts
}
```

### A2. Modify `src/personal_agent/tools/__init__.py`

Inside the existing `if settings.r2_endpoint_url and settings.r2_access_key_id and settings.r2_secret_access_key:` gate at lines 103-112, register all three new tools immediately after `notes_search_tool`:
```python
registry.register(artifact_write_tool, artifact_write_executor)
registry.register(artifact_list_tool,  artifact_list_executor)
registry.register(artifact_read_tool,  artifact_read_executor)
log.info("artifact_tools_registered", bucket=settings.r2_bucket_name)
```
The existing `notes_tools_skipped_unconfigured` warning already covers the unregistered branch. Add a top-of-file import for the new module.

### A3. Modify `config/governance/tools.yaml`

Insert immediately after the `notes_search:` block at lines 1192-1213:

```yaml
  # FRE-368: write a human-facing artifact (HTML/chart/table/JSON) into the
  # R2-backed artifact substrate. Size capped at 5 MB in tool code; per-mode
  # approval mirrors the `write` precedent (no approval in NORMAL; approval
  # required in ALERT/DEGRADED so unhealthy gateways can't accidentally fan
  # out artifacts). Loop guards match notes_write.
  artifact_write:
    category: "artifact_write"
    allowed_in_modes: ["NORMAL", "ALERT", "DEGRADED"]
    risk_level: "medium"
    requires_approval: false
    requires_approval_in_modes: ["ALERT", "DEGRADED"]
    timeout_seconds: 30
    rate_limit_per_hour: 30
    loop_max_per_signature: 3
    loop_max_consecutive: 5

  # FRE-368: read-only listing of the calling user's artifacts.
  artifact_list:
    category: "memory_read"
    allowed_in_modes: ["NORMAL", "ALERT", "DEGRADED", "RECOVERY"]
    risk_level: "low"
    requires_approval: false
    timeout_seconds: 10
    rate_limit_per_hour: 200

  # FRE-368: fetch a single artifact's metadata + (small textual) content.
  artifact_read:
    category: "memory_read"
    allowed_in_modes: ["NORMAL", "ALERT", "DEGRADED", "RECOVERY"]
    risk_level: "low"
    requires_approval: false
    timeout_seconds: 10
    rate_limit_per_hour: 60
```

Categories `artifact_write` / `memory_read` are intentionally not declared in `tool_categories:` (same posture as `notes_write` — verified in tools.yaml today); the per-tool `allowed_in_modes` controls visibility via `ToolRegistry.list_tools(mode=…)`.

### A4. Modify `src/personal_agent/service/artifacts_router.py`

Extend the existing router with two **public, CF-Access-gated** endpoints that the PWA will call (the existing internal `/internal/artifacts/{id}` resolver stays untouched — it's behind the shared `X-Internal-Token` and used by the Worker, not the browser).

Add to `artifacts_router.py`:

```python
@router.get("/api/v1/artifacts", response_model=ArtifactListResponse)
async def list_artifacts(
    request: Request,
    type: str = Query("artifact", regex=r"^(artifact|note|upload|capture)$"),
    prefix: str | None = Query(None, max_length=64),
    k: int = Query(20, ge=1, le=100),
    since: datetime | None = Query(None),
) -> ArtifactListResponse:
    """List the authenticated user's artifacts. CF-Access JWT required."""
    user_id = await _resolve_user_via_cf_access(request)  # 401 if no/invalid JWT
    rows = await _query_user_artifacts(user_id, type=type, prefix=prefix, k=k, since=since)
    return ArtifactListResponse(items=[_row_to_summary(r) for r in rows])


@router.get("/api/v1/artifacts/{artifact_id}", response_model=ArtifactSummary)
async def get_artifact_metadata(
    artifact_id: UUID,
    request: Request,
) -> ArtifactSummary:
    """Metadata-only fetch for a single artifact. Bytes flow through the
    Worker at artifacts.example.com — this endpoint never returns bytes."""
    user_id = await _resolve_user_via_cf_access(request)
    row = await _load_user_artifact(user_id, artifact_id)
    if row is None:
        raise HTTPException(status_code=404)  # existence-hiding per ADR-0064 D3
    return _row_to_summary(row)
```

`_resolve_user_via_cf_access` is a thin async helper that calls the same `service/cf_access_jwt.py:get_verifier().verify(...)` path used by `_verify_internal_token`-adjacent code in PR #65, then maps the verified `email` claim to a user via `get_or_create_user_by_email`. **Do not duplicate** — extract from the existing `artifacts_router.py` JWT block into a private helper (or import an existing helper if one was added in PR #65).

`ArtifactListResponse`, `ArtifactSummary` are new Pydantic models (frozen) covering the columns from §A1's `artifact_list_executor` return shape (no embedding, no r2_key — those stay server-side).

### A5. New file: `src/personal_agent/service/telemetry_router.py`

A small router for ADR-0070 D8 card-click measurement. Keep it minimal — this is structlog-emission only, no Postgres.

```python
"""FRE-368 — client-side telemetry endpoint for ADR-0070 D8 measurement.

The PWA POSTs a card_click event each time the user expands an artifact
inline card. The event flows to Elasticsearch via the structlog handler
(same path as tool-call telemetry) so the two-week post-deploy review can
join click-rates against artifact_write rates per ADR-0070 D8.
"""
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID

from personal_agent.telemetry import get_logger

log = get_logger(__name__)
router = APIRouter()


class CardClickEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    artifact_id: UUID
    session_id: UUID | None = None
    kind: str = Field(default="card_click", pattern=r"^card_click$")
    surface: str = Field(default="inline", pattern=r"^(inline|drawer|standalone)$")


@router.post("/api/v1/telemetry/card_click", status_code=status.HTTP_204_NO_CONTENT)
async def post_card_click(event: CardClickEvent, request: Request) -> None:
    user_id = await _resolve_user_via_cf_access(request)  # 401 if missing
    log.info(
        "artifact_card_click",
        artifact_id=str(event.artifact_id),
        session_id=str(event.session_id) if event.session_id else None,
        user_id=str(user_id),
        surface=event.surface,
    )
```

Best-effort posture: any error after auth still returns 204 (telemetry must not break the click). The `artifact_card_click` event flows to ES via the structlog handler — confirm field names align with the index template by following the precedent at `executor.py` where tool-call events are emitted.

### A6. Modify `src/personal_agent/service/app.py`

Register the new telemetry router immediately after the existing `artifacts_router` registration at lines 1027-1030:
```python
from .telemetry_router import router as telemetry_router
app.include_router(telemetry_router)
```
The two new endpoints added in §A4 land via the existing `artifacts_router` include (just additional routes on the same prefix-less router).

### A7. Tests

**`tests/personal_agent/tools/test_artifact_tools.py`** — mirror `tests/personal_agent/tools/test_notes_tools.py` patterns verbatim:
- Reuse `_FakeStore`, `_FakeSession`, `_ctx` factory (copy from the notes test, or factor a small `tests/personal_agent/tools/_artifact_test_helpers.py` if both tests are touched in this PR — defer to "duplicate is cheaper" rule).
- Cases per executor:
  - `artifact_write`:
    - happy path text/html: returns dict, `_FakeStore.put_calls` has the right `r2_key` + `content_type` + bytes, Postgres INSERT executed with `type='artifact'` + `created_by='agent'`.
    - happy path image/png: base64-decoded into bytes; `put_calls[0]["content"]` is raw decoded bytes; `r2_key` ends in `.png`.
    - rejects `content_type` outside the allowlist.
    - rejects content larger than 5 MB.
    - rejects bad slug (`../etc/passwd`, slash-containing, etc.) via `build_r2_key`'s guard — same shape as notes test line 170-182.
    - empty content rejected.
  - `artifact_list`:
    - returns ordered+limited list; prefix filter applied; `since` filter applied; `type='artifact'` always filtered.
  - `artifact_read`:
    - small textual artifact: `store.get` invoked, `content` populated.
    - large artifact: `store.get` NOT invoked, `content` absent, `public_url` returned.
    - binary content_type (e.g. image/png): `content` absent regardless of size.
    - row owned by another user: `ToolExecutionError("artifact not found")`.

**`tests/personal_agent/service/test_artifacts_router.py`** — extend the existing file with cases for the two new public endpoints:
- `GET /api/v1/artifacts` with no JWT → 401.
- `GET /api/v1/artifacts` with valid JWT → returns only the calling user's `type='artifact'` rows.
- `GET /api/v1/artifacts/{id}` with a row owned by a different user → 404.
- Type-coverage: `type=upload` and `type=note` queries return the matching rows; default is `type=artifact`.

**`tests/personal_agent/service/test_telemetry_router.py`** — new file:
- POST with valid JWT + valid body → 204; structlog event was emitted with the right fields (mock the logger or use `structlog.testing.capture_logs`).
- POST with no JWT → 401.
- POST with malformed body (bad UUID, bad surface enum) → 422.

### A8. PR #A quality gates & verification

Run from `/opt/seshat` on the feature branch:

```bash
# Type check + lint
make mypy
make ruff-check
make ruff-format

# Unit tests (must be the only pytest process — pre-commit hook enforces)
make test
# Targeted runs while iterating:
make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py
make test-file FILE=tests/personal_agent/service/test_artifacts_router.py
make test-file FILE=tests/personal_agent/service/test_telemetry_router.py
```

End-to-end smoke (after merging or with a feature-branch build):

```bash
# Rebuild gateway image with the new tools, then verify visibility via tool registry
make rebuild SERVICE=seshat-gateway   # local; or ENV=cloud make build for VPS

# From the gateway shell:
make shell SERVICE=seshat-gateway
python -c "from personal_agent.tools import tool_registry; print([t.name for t in tool_registry.list_tools(mode='NORMAL') if t.name.startswith('artifact_')])"
# Expect: ['artifact_write', 'artifact_list', 'artifact_read']

# CLI write → list → read round-trip
uv run agent "Use artifact_write to save an HTML artifact with title 'Round-trip smoke' summarising today's plan, then call artifact_list, then artifact_read it back and confirm the title round-trips."
```

Then on iPad Safari, open `https://artifacts.example.com/{artifact_id from output}` and confirm CF Access prompts (or shows the bytes if SSO is active). This verifies the existing Worker path still serves the new `type='artifact'` rows correctly with no Worker changes needed.

### A9. PR #A commit + Linear

- One commit per logical unit (tools / governance / endpoints / tests) is fine — Sonnet judgement.
- Commit message: `feat(artifacts): agent-side artifact_write/list/read tools (FRE-368 backend)` with body linking ADR-0069 + ADR-0070 + FRE-368.
- PR title: `feat(artifacts): agent artifact tools (FRE-368 backend)` — explicitly call out "PWA in #B".
- After merge: bump MASTER_PLAN.md "Last updated" line + add a Recently-Completed entry (per the [feedback memory](https://example) note about updating MASTER_PLAN after every shipped issue).

---

## PR #B — PWA

PR #B requires PR #A on `main` (so `/api/v1/artifacts` and `/api/v1/telemetry/card_click` exist). All changes live under `seshat-pwa/`.

### B1. New file: `seshat-pwa/src/components/ArtifactCard.tsx`

Inline card rendered when `MarkdownContent`'s link handler detects an `artifacts.example.com/{uuid}` URL. Visual shape: title + 1-line summary + content-type chip + "Expand" button (opens viewer) + "Open standalone ↗" anchor (target=_blank, fires telemetry).

Match FRE-315 chrome conventions captured by Explore: `bg-slate-800/80` chrome, `border border-slate-800/60`, button class `flex items-center gap-1 text-xs px-1.5 py-0.5 rounded hover:text-seshat-accent hover:bg-slate-700/40 transition-colors`, `FEEDBACK_MS = 1400` checkmark flash on copy. Use `animate-pulse-dot` for the loading skeleton while metadata is in flight.

Top-of-file docstring carries the **manual test plan** in the established convention (modelled on `ApprovalModal.tsx:8-37`). Tests as numbered scenarios + expected outcomes. Include at minimum:
1. Card renders title + summary fetched from `/api/v1/artifacts/{id}` for a recognised URL.
2. Card collapses gracefully (renders the URL as a plain link) if metadata fetch returns 404 or fails.
3. "Expand" opens the viewer; ESC closes; backdrop tap closes.
4. "Open standalone" opens in a new tab via `target="_blank" rel="noopener noreferrer"` (verify WKWebView→Safari handoff on iPad — the explicit fix flagged in the master plan close-out for FRE-227).
5. Telemetry `card_click` POSTed on Expand (network tab shows 204, no error if endpoint fails).

Data flow: card receives `artifactId: string` + `fallbackHref: string` props; on mount, `useEffect` calls `getArtifactMetadata(artifactId)` from `agui-client.ts`; on success populates title/summary/content_type; on failure renders `<a>{fallbackHref}</a>` (so a broken artifact never breaks the chat). Cancellation: track an `AbortController` to abort the fetch on unmount.

### B2. New file: `seshat-pwa/src/components/ArtifactViewer.tsx`

Sandboxed iframe overlay. Mirrors `ApprovalModal.tsx` chrome scaffolding (`role="dialog"`, `aria-modal="true"`, ESC handler, focus trap on first dismissible action). Responsive variant per ADR-0070 D6:

```tsx
// Desktop ≥ md: right-side drawer
className="fixed inset-y-0 right-0 z-50 w-full md:max-w-3xl bg-slate-900 border-l border-slate-700
           shadow-2xl flex flex-col
           // Mobile: bottom sheet with rounded top
           md:rounded-none rounded-t-2xl
           md:inset-x-auto inset-x-0 md:inset-y-0 inset-y-auto bottom-0 max-h-[90vh]"
```

Iframe shape (per ADR-0070 D7, default "documents not apps"):
```tsx
<iframe
  src={publicUrl}                  // browser hits Worker via artifacts.example.com, CF Access SSO covers
  sandbox=""                       // empty string = strictest: no scripts, no same-origin, no nav
  referrerPolicy="no-referrer"
  className="flex-1 w-full bg-white rounded-b-2xl md:rounded-none"
  title={title ?? "Artifact"}
/>
```

Backdrop element separate (`fixed inset-0 z-40 bg-black/60`) with `onClick` dismiss — match `StreamingChat.tsx:154-185` session-drawer pattern.

Header has: title, content-type chip, "Open standalone ↗" (target=_blank, fires `card_click` with `surface='standalone'`), close button. No JS-execution affordance; per ADR-0070 D7, any future interactive artifact path is an ADR amendment.

Top-of-file docstring: manual test plan with at least the ADR-0070 verification cases: (a) `<script>alert(1)</script>` inside an HTML artifact does NOT execute; (b) iframe cannot reach `parent.location`; (c) ESC closes the viewer; (d) backdrop tap closes; (e) right-drawer on md viewport, bottom-sheet on narrow viewport; (f) telemetry surface logged correctly per open path.

### B3. Modify `seshat-pwa/src/components/MarkdownContent.tsx`

Extend the existing `a({ href, children })` handler at lines 114-125 with artifact URL detection. The host comes from a new env var or hard-coded constant; recommend env-driven so dev/prod separation stays clean:

```tsx
// At module top:
const ARTIFACTS_HOST = process.env.NEXT_PUBLIC_ARTIFACTS_HOST ?? 'artifacts.example.com';
const ARTIFACT_PATH_RE = /^\/([0-9a-f-]{36})\/?$/i;

function tryParseArtifactUrl(href: string | undefined): string | null {
  if (!href) return null;
  try {
    const u = new URL(href);
    if (u.host !== ARTIFACTS_HOST) return null;
    const m = u.pathname.match(ARTIFACT_PATH_RE);
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

// In the components map:
a({ href, children }) {
  const artifactId = tryParseArtifactUrl(href);
  if (artifactId) {
    return <ArtifactCard artifactId={artifactId} fallbackHref={href!} />;
  }
  // existing fallback: plain external link with target=_blank rel=noopener noreferrer
  return <a href={href} target="_blank" rel="noopener noreferrer"
            className="text-blue-400 underline underline-offset-2 hover:text-blue-300">
           {children}
         </a>;
},
```

Add `NEXT_PUBLIC_ARTIFACTS_HOST` to whatever environment-template file is canonical for the PWA (check `seshat-pwa/.env.example` or document in README if no template exists); leave the runtime fallback string so dev builds work without explicit env setup.

### B4. New file: `seshat-pwa/src/app/artifacts/page.tsx`

App-Router page modelled on `src/app/c/[sessionId]/page.tsx`. Default export an async server component that renders an `<ArtifactsIndex />` client component (new — also under `components/`). The client component:
- Calls `listArtifacts({ type: 'artifact', k: 50 })` on mount.
- Renders a responsive grid (`grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4`) of compact cards: title, summary, content-type chip, relative timestamp, "Open" link.
- Empty-state message if zero artifacts: "No artifacts yet — ask the agent to make you one."
- Skeleton loader during fetch (animate-pulse).
- Standalone open uses the same `target="_blank" rel="noopener noreferrer"` convention.

Top-of-file manual test plan: (1) empty state; (2) populated list ordered newest-first; (3) per-card open standalone works; (4) loading skeleton visible during fetch.

### B5. Modify `seshat-pwa/src/lib/agui-client.ts`

Add three helpers using the existing `SESHAT_API` + `authHeaders()` patterns established by `postApprovalDecision` (lines 239-268):

```tsx
export interface ArtifactSummary {
  artifact_id: string;
  public_url: string;
  slug: string | null;
  title: string | null;
  summary: string | null;
  content_type: string;
  size_bytes: number;
  tags: string[];
  created_at: string;
}

export async function listArtifacts(opts: {
  type?: 'artifact' | 'note' | 'upload' | 'capture';
  prefix?: string;
  k?: number;
  since?: string;
} = {}): Promise<ArtifactSummary[]> {
  const params = new URLSearchParams();
  if (opts.type)   params.set('type', opts.type);
  if (opts.prefix) params.set('prefix', opts.prefix);
  if (opts.k)      params.set('k', String(opts.k));
  if (opts.since)  params.set('since', opts.since);
  const resp = await fetch(`${SESHAT_API}/api/v1/artifacts?${params}`, { headers: authHeaders() });
  if (!resp.ok) throw new Error(`listArtifacts ${resp.status}`);
  return (await resp.json()).items;
}

export async function getArtifactMetadata(id: string): Promise<ArtifactSummary> {
  const resp = await fetch(`${SESHAT_API}/api/v1/artifacts/${encodeURIComponent(id)}`,
                           { headers: authHeaders() });
  if (!resp.ok) throw new Error(`getArtifactMetadata ${resp.status}`);
  return await resp.json();
}

export function postCardClick(artifactId: string,
                              surface: 'inline' | 'drawer' | 'standalone',
                              sessionId?: string): void {
  // Best-effort fire-and-forget; failures must never break the click.
  const body = JSON.stringify({ artifact_id: artifactId, surface, session_id: sessionId });
  try {
    if (typeof navigator !== 'undefined' && 'sendBeacon' in navigator) {
      const blob = new Blob([body], { type: 'application/json' });
      navigator.sendBeacon(`${SESHAT_API}/api/v1/telemetry/card_click`, blob);
      return;
    }
  } catch { /* swallow */ }
  void fetch(`${SESHAT_API}/api/v1/telemetry/card_click`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body,
    keepalive: true,
  }).catch(() => { /* swallow */ });
}
```

Note: `sendBeacon` does not carry custom headers — falls back to `fetch` with `keepalive:true` when `Authorization`-header injection is required by your auth setup. Verify against the deployed Worker / gateway whether bearer headers are needed for `/api/v1/telemetry/*` or whether CF Access SSO cookies suffice; if cookies suffice, `sendBeacon` is the fast path.

### B6. Modify `seshat-pwa/src/components/StreamingChat.tsx`

Inside the session-drawer panel at lines 154-185, add an "Artifacts" navigation row under the "New conversation" / above the session list:
```tsx
<Link href="/artifacts"
      onClick={() => setIsDrawerOpen(false)}
      className="px-4 py-2 hover:bg-slate-800 flex items-center gap-2 text-sm text-slate-300">
  📎 Artifacts
</Link>
```
(Use whatever icon convention already exists in the drawer; emoji is the lazy choice — replace with the existing icon component if `SessionList.tsx` shows a heroicon import or similar.)

### B7. Modify `seshat-pwa/public/sw.js`

Bump `CACHE_NAME` from `'seshat-v4-fre-315-image-actions'` to `'seshat-v5-fre-368-artifact-cards'`. Per the [SW cache convention memory](https://example), this is mandatory on every shell-changing PWA deploy.

**Verification side-quest (worth doing in this PR or as a separate Tier-3:Haiku issue):** Explore agent flagged that `navigator.serviceWorker.register(...)` is not present anywhere under `src/`, so the SW may not be running. Before relying on the cache-bump for this PR, check the iPad PWA's DevTools (Safari Web Inspector → Service Workers) on the deployed `agent.example.com` and confirm `sw.js` is in fact registered & controlling the page. If not, either (a) add a tiny `useEffect` registration in `src/app/layout.tsx` (`if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js')`), or (b) file a Haiku issue and ship FRE-368 without the SW bump. The FRE-315 deploy worked, so registration is likely happening — but worth a 30-second DevTools check.

### B8. PR #B quality gates & verification

```bash
cd /opt/seshat/seshat-pwa
npm install      # if any new dependencies, none expected
npm run lint     # next lint must be clean
npm run build    # next build — verify no TS errors, all routes compile
```

End-to-end smoke (run from a real browser against the deployed gateway after the PR is up on a deploy preview or on a feature branch deployed via the existing pipeline):

1. **Inline card** — open a fresh chat session and ask: "Use artifact_write to create a small HTML report titled 'Test card' with summary 'demo for FRE-368'". The assistant response renders the URL → card with `Test card` / `demo for FRE-368` / `text/html` chip / Expand button.
2. **Sandboxed viewer** — click Expand → drawer slides in (right on laptop, bottom-sheet on iPad). View page-source of the iframe to confirm `sandbox=""` is present.
3. **No-script sandbox** — `artifact_write` an HTML payload containing `<script>document.title='PWNED'</script>`. Expand → iframe renders the rest of the HTML but document.title stays "Artifact" (script suppressed by sandbox).
4. **WKWebView → Safari** — on iPad PWA installed home-screen app, click "Open standalone ↗" → opens in Safari (not in-PWA tab), CF Access SSO carries over, page renders.
5. **/artifacts route** — open `/artifacts` from the session drawer link → list view shows the test card; clicking it opens the same viewer.
6. **Telemetry** — DevTools Network tab shows `POST /api/v1/telemetry/card_click` 204 on Expand. ES query confirms:
   ```bash
   curl -s 'https://es.example.com/seshat-logs-*/_search?q=event_type:artifact_card_click&size=5' | jq '.hits.hits[]._source | {artifact_id, surface, ts:.["@timestamp"]}'
   ```
7. **Replay cost (ADR-0070 verification §4)** — open a session with 3 artifact references; in DevTools Performance → DOM Nodes, confirm <1MB DOM weight (no inline HTML payloads in transcript).
8. **CACHE_NAME bump effective** — DevTools → Application → Cache Storage shows the new `seshat-v5-fre-368-artifact-cards` cache; old cache evicted (only if §B7 verification confirmed the SW is registered — else this step is N/A).

### B9. PR #B commit + Linear

- PR title: `feat(artifacts): PWA inline cards, sandboxed viewer, /artifacts route (FRE-368 PWA)`
- Body: link FRE-368, ADR-0070, list the 8 manual test scenarios from §B8, screenshots if practical.
- After merge:
  - Bump `MASTER_PLAN.md` "Last updated" + Recently-Completed entry (per memory).
  - Update FRE-368 Linear ticket → mark Done + comment summarising both PRs.
  - Move on to FRE-369 (user uploads — the sibling consumer of the substrate).
  - Set a calendar reminder for the ADR-0070 D8 two-week review (gate ≥ 2026-06-04 if FRE-368 ships 2026-05-21).

---

## Critical files reference

**PR #A — Backend:**
- `src/personal_agent/tools/artifact_tools.py` (new)
- `src/personal_agent/tools/notes_tools.py` (template — read, do not modify)
- `src/personal_agent/tools/__init__.py:103-112` (extend R2-gated registration block)
- `src/personal_agent/storage/artifact_store.py` (reuse — `build_r2_key`, `R2ArtifactStore.put/get`, `get_artifact_store`)
- `src/personal_agent/service/artifacts_router.py` (extend with public list + metadata endpoints)
- `src/personal_agent/service/cf_access_jwt.py` (reuse — `get_verifier`, JWT verification)
- `src/personal_agent/service/telemetry_router.py` (new)
- `src/personal_agent/service/app.py:1027-1030` (register telemetry router)
- `config/governance/tools.yaml:1192-1213` (insert 3 new entries after notes_search)
- `docker/postgres/init.sql:224-254` (schema — already complete, **no changes**)
- `tests/personal_agent/tools/test_artifact_tools.py` (new — mirror test_notes_tools.py)
- `tests/personal_agent/service/test_artifacts_router.py` (extend)
- `tests/personal_agent/service/test_telemetry_router.py` (new)

**PR #B — PWA:**
- `seshat-pwa/src/components/ArtifactCard.tsx` (new)
- `seshat-pwa/src/components/ArtifactViewer.tsx` (new)
- `seshat-pwa/src/components/ArtifactsIndex.tsx` (new — client component for `/artifacts` page)
- `seshat-pwa/src/components/MarkdownContent.tsx:90-125` (extend `a()` handler)
- `seshat-pwa/src/components/MermaidBlock.tsx` (template chrome — read, do not modify)
- `seshat-pwa/src/components/ApprovalModal.tsx:8-37` (manual test plan docstring template; chrome scaffolding template)
- `seshat-pwa/src/components/StreamingChat.tsx:154-185` (insert /artifacts link in session drawer)
- `seshat-pwa/src/app/artifacts/page.tsx` (new — App Router page)
- `seshat-pwa/src/lib/agui-client.ts:239-268` (extend with 3 new helpers, mirroring postApprovalDecision)
- `seshat-pwa/public/sw.js:14` (bump CACHE_NAME)

---

## What is explicitly out of scope

- **Schema changes** — none needed; `artifacts` table covers everything ([ADR-0069 D4](../../architecture_decisions/ADR-0069-r2-backed-artifact-substrate.md#d4--metadata-canon-postgres-artifacts-table)).
- **Worker code** — none needed; existing Worker serves any `type` row from the resolve endpoint.
- **CF Access changes** — none needed; the dedicated `artifacts` app + JWT verification already cover the substrate.
- **Interactive artifacts** (`sandbox="allow-scripts"` etc.) — explicitly deferred per [ADR-0070 D7](../../architecture_decisions/ADR-0070-output-channel-model-markdown-and-rich.md#d7--sandboxing-posture-documents-not-apps-default). Future requirement → ADR amendment.
- **Dual-representation `reply_html`** — explicitly deferred per [ADR-0070 D4](../../architecture_decisions/ADR-0070-output-channel-model-markdown-and-rich.md#d4--dual-representation-is-deferred-until-measured). FRE-368 produces the measurement data that decides whether this becomes a follow-up.
- **Cross-user sharing** — placeholder per ADR-0069 §"Privilege model placeholder remains" (FRE-345).
- **FRE-369 user-upload UX** — sibling consumer of the substrate; ships next.
- **PWA test infrastructure bootstrap** — per the project convention (`ApprovalModal.tsx` docstring), manual test plans in component docstrings are the established norm. Vitest bootstrap is a separate-ticket decision.

---

## Verification summary

| Layer | Verified by |
|--|--|
| Tools registered + LLM-visible | `tool_registry.list_tools(mode='NORMAL')` from gateway shell shows `artifact_*` |
| Round-trip (write → list → read) | CLI `uv run agent` smoke session in §A8 |
| Cross-user 404 | `test_artifact_tools.py` test asserts `ToolExecutionError("artifact not found")` |
| Size cap + content_type allowlist | Unit tests in `test_artifact_tools.py` |
| Public URL serves bytes | iPad Safari open of `artifacts.example.com/{id}` returns rendered HTML (existing Worker, unchanged) |
| CF-Access auth on public endpoints | `test_artifacts_router.py` extension covers 401-without-JWT |
| Sandbox posture | §B8.3 — `<script>` payload does not execute in viewer iframe |
| WKWebView → Safari handoff | §B8.4 — iPad home-screen PWA opens standalone link in Safari |
| Telemetry instrumentation | §B8.6 — ES query for `event_type:artifact_card_click` returns clicks |
| Replay cost | §B8.7 — 3-artifact session DOM stays <1MB |
| /artifacts route | §B8.5 — list renders + each link opens viewer |
| ADR-0070 D8 measurement data | Two-week post-deploy review gate (≥ 2026-06-04) consumes ES `artifact_write` + `artifact_card_click` counts |

---

## Open question (carry forward to D8 review, not blocking)

The cf-access JWT helper used by the PWA-facing `/api/v1/artifacts*` and `/api/v1/telemetry/card_click` endpoints is the same `service/cf_access_jwt.py:get_verifier()` already in production for the internal `/internal/artifacts/{id}` resolver (PR #65). If PR #65's helper isn't already factored as a FastAPI dependency, factor it in §A4 — don't duplicate the JWT-verification body. Worth confirming the exact import path when writing the code.
