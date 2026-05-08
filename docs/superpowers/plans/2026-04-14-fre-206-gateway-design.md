# FRE-206: Seshat API Gateway — Design Document

> **Linear**: [FRE-206](https://linear.app/frenchforest/issue/FRE-206)
> **ADR**: ADR-0045 (Phases 3–4)
> **Tier**: Tier-1:Opus (design) → Tier-2:Sonnet (implementation)
> **Date**: 2026-04-14

---

## Design Questions Resolved

### Q1: Execution → Gateway — Direct DB or HTTP API?

**Decision: Protocol-based dispatch with two implementations.**

The `KnowledgeGraphProtocol` (FRE-201) abstracts the boundary. Two concrete implementations:

| Implementation | Used when | Access path |
|---------------|-----------|-------------|
| `Neo4jKnowledgeGraph` (existing `MemoryServiceAdapter`) | Local profile, or gateway process itself | Direct Neo4j driver |
| `GatewayKnowledgeGraphClient` (new) | Remote execution (cloud profile, external agent) | HTTP → Gateway → Neo4j |

**In local single-process mode**, the execution layer uses `MemoryServiceAdapter` directly — zero HTTP overhead. The gateway endpoints also use the same adapter internally. No performance penalty.

**In cloud/split mode**, a cloud execution service uses `GatewayKnowledgeGraphClient`, which wraps `httpx` calls to the gateway. The gateway deserializes and delegates to `MemoryServiceAdapter`.

**Why this is correct**: The protocol abstraction already exists (FRE-201). Adding a second implementation is the intended use. No code in the orchestrator or request gateway changes — only the bootstrap wiring differs per profile.

### Q2: Local dev mode — Same process or separate?

**Decision: Single process, dual-mount with feature flag.**

In local development, the gateway routes mount on the **same FastAPI app** as the execution routes (port 9000). In production, a separate `gateway/app.py` runs as its own uvicorn process (port 9001 or behind reverse proxy).

```python
# service/app.py — local dev mode
if settings.gateway_mount_local:
    from personal_agent.gateway.app import create_gateway_router
    app.include_router(create_gateway_router(), prefix="/api/v1")
```

**Why**: Separate processes add dev complexity (two terminals, port management, health checks). The gateway is stateless and lightweight — mounting it on the same app is transparent. The `create_gateway_router()` factory returns a plain `APIRouter`, usable in either mode.

**Production separation**: `gateway/app.py` creates its own `FastAPI` instance with dedicated lifespan (connects to Neo4j, PostgreSQL, Elasticsearch, Redis — but NOT the LLM client, orchestrator, or brainstem). This is a strict subset of `service/app.py` lifespan.

### Q3: Migration path

**Decision: Additive + parallel operation.**

1. New gateway routes are added under `/api/v1/knowledge/*`, `/api/v1/sessions/*`, `/api/v1/observations/*`
2. Existing routes (`/memory/*`, `/sessions/*`) continue working unchanged
3. Mobile PWA and external agents use the new `/api/v1/*` routes
4. After validation, existing routes are deprecated (but not removed — they serve the CLI)

**No breaking changes.** The gateway is purely additive to `service/app.py` in local mode.

---

## Module Structure

```
src/personal_agent/gateway/
  __init__.py
  app.py                    # FastAPI app (production) + create_gateway_router() factory
  knowledge_api.py          # /knowledge/* endpoints
  session_api.py            # /sessions/* endpoints
  observation_api.py        # /observations/* endpoints
  auth.py                   # Bearer token + scope validation
  rate_limiting.py          # Per-token rate limits
  errors.py                 # Gateway-specific error responses
  client.py                 # GatewayKnowledgeGraphClient (HTTP client for remote access)
```

---

## API Specification

### Authentication

All gateway endpoints require a Bearer token:

```
Authorization: Bearer <token>
```

Tokens are defined in `config/gateway_access.yaml`:

```yaml
tokens:
  - name: claude-code-local
    secret: "${GATEWAY_TOKEN_CLAUDE_CODE}"  # env var reference
    scopes: [knowledge:read, knowledge:write, sessions:read, observations:read]
    rate_limit: 100/hour

  - name: codex-cloud
    secret: "${GATEWAY_TOKEN_CODEX}"
    scopes: [knowledge:read, sessions:read]
    rate_limit: 50/hour

  - name: pwa-client
    secret: "${GATEWAY_TOKEN_PWA}"
    scopes: [knowledge:read, sessions:read, observations:read]
    rate_limit: 200/hour

  - name: execution-service
    secret: "${GATEWAY_TOKEN_EXECUTION}"
    scopes: [knowledge:read, knowledge:write, sessions:read, sessions:write, observations:write]
    rate_limit: 1000/hour
```

**In local dev mode** (gateway mounted on same process), auth is disabled by default via `settings.gateway_auth_enabled = False`. The gateway routes work without tokens locally.

### Endpoints

#### Knowledge API

```
GET  /api/v1/knowledge/search?q={query}&limit={n}
     Scope: knowledge:read
     → Sequence[EntityResult]

GET  /api/v1/knowledge/entities/{entity_id}
     Scope: knowledge:read
     → Entity | 404

POST /api/v1/knowledge/entities
     Scope: knowledge:write
     Body: { entity: str, entity_type: str, metadata: dict }
     → { id: str, created: bool }

GET  /api/v1/knowledge/entities/{entity_id}/relationships
     Scope: knowledge:read
     → Sequence[Relationship]
```

#### Session API

```
GET  /api/v1/sessions?limit={n}
     Scope: sessions:read
     → Sequence[SessionSummary]

GET  /api/v1/sessions/{session_id}
     Scope: sessions:read
     → Session | 404

GET  /api/v1/sessions/{session_id}/messages?limit={n}
     Scope: sessions:read
     → Sequence[Message]
```

#### Observation API

```
GET  /api/v1/observations/recent?limit={n}
     Scope: observations:read
     → Sequence[TraceEvent]

GET  /api/v1/observations/{trace_id}
     Scope: observations:read
     → TraceDetail | 404

POST /api/v1/observations/query
     Scope: observations:read
     Body: { filters: dict, time_range: str, limit: int }
     → Sequence[TraceEvent]
```

#### Health

```
GET  /api/v1/health
     No auth required
     → { status: str, components: dict }
```

### Error Responses

All errors follow a consistent format:

```json
{
  "error": "unauthorized",
  "message": "Invalid or missing bearer token",
  "status": 401
}
```

| Status | Error | When |
|--------|-------|------|
| 401 | `unauthorized` | Missing/invalid token |
| 403 | `forbidden` | Token lacks required scope |
| 404 | `not_found` | Entity/session/trace not found |
| 429 | `rate_limited` | Per-token rate limit exceeded |
| 503 | `service_unavailable` | Backend (Neo4j, PG, ES) unreachable |

---

## Rate Limiting

Simple sliding window per token:

```python
@dataclass
class RateLimitState:
    token_name: str
    window_start: float
    request_count: int
    max_per_hour: int
```

Stored in Redis (or in-memory dict for local mode). Middleware checks before routing.

---

## Implementation Plan (for Sonnet subagent)

### Step 1: Auth module — `gateway/auth.py`

```python
@dataclass(frozen=True)
class TokenInfo:
    name: str
    scopes: frozenset[str]
    rate_limit: int  # requests per hour

def load_token_config(path: Path) -> dict[str, TokenInfo]: ...

async def verify_token(authorization: str | None, required_scope: str) -> TokenInfo:
    """FastAPI dependency. Raises HTTPException(401/403)."""
```

- Read `config/gateway_access.yaml`
- Hash comparison for token validation (no plaintext storage)
- Feature flag: `settings.gateway_auth_enabled` — when False, returns a permissive `TokenInfo`

### Step 2: Rate limiter — `gateway/rate_limiting.py`

```python
class RateLimiter:
    def __init__(self) -> None:
        self._windows: dict[str, RateLimitState] = {}

    def check(self, token: TokenInfo) -> bool:
        """Returns True if request is allowed. Raises HTTPException(429) if not."""
```

In-memory for now. Redis-backed in production (Phase 2).

### Step 3: Knowledge API — `gateway/knowledge_api.py`

```python
router = APIRouter(prefix="/knowledge", tags=["knowledge"])

@router.get("/search")
async def search_knowledge(
    q: str,
    limit: int = 10,
    token: TokenInfo = Depends(require_scope("knowledge:read")),
) -> list[dict[str, Any]]:
    """Search the knowledge graph."""
```

Delegates to `KnowledgeGraphProtocol` (injected via app state or dependency).

### Step 4: Session API — `gateway/session_api.py`

Similar pattern. Wraps `SessionStoreProtocol` / `SessionRepository`.

### Step 5: Observation API — `gateway/observation_api.py`

Wraps Elasticsearch queries for traces. Read-only.

### Step 6: Gateway app — `gateway/app.py`

```python
def create_gateway_router() -> APIRouter:
    """Factory for gateway routes. Usable in both local mount and standalone."""
    router = APIRouter(prefix="/api/v1")
    router.include_router(knowledge_router)
    router.include_router(session_router)
    router.include_router(observation_router)
    router.include_router(health_router)
    return router

def create_gateway_app() -> FastAPI:
    """Standalone gateway for production deployment."""
    app = FastAPI(title="Seshat API Gateway", lifespan=gateway_lifespan)
    app.include_router(create_gateway_router())
    return app
```

### Step 7: Gateway client — `gateway/client.py`

```python
class GatewayKnowledgeGraphClient:
    """KnowledgeGraphProtocol implementation that calls the Gateway HTTP API.
    
    Used by remote execution profiles (cloud, external agents).
    """
    def __init__(self, base_url: str, token: str) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, headers={"Authorization": f"Bearer {token}"})

    async def search(self, query: str, limit: int, ctx: TraceContext) -> Sequence[EntityNode]:
        resp = await self._client.get("/api/v1/knowledge/search", params={"q": query, "limit": limit})
        resp.raise_for_status()
        return [EntityNode(**e) for e in resp.json()]
```

### Step 8: Wire into service/app.py

Add feature flag + local mount:

```python
# In service/app.py, after app creation:
if settings.gateway_mount_local:
    from personal_agent.gateway.app import create_gateway_router
    app.include_router(create_gateway_router())
```

### Step 9: Config additions

Add to `settings.py`:
```python
gateway_mount_local: bool = True       # Mount gateway routes on execution service
gateway_auth_enabled: bool = False     # Disable auth in local dev
gateway_access_config: str = "config/gateway_access.yaml"
```

### Step 10: Tests

```
tests/personal_agent/gateway/
  __init__.py
  test_auth.py              # Token validation, scope checking, auth disabled mode
  test_rate_limiting.py     # Window-based limiting
  test_knowledge_api.py     # Search, get entity, store fact (mocked backend)
  test_session_api.py       # List sessions, get messages (mocked backend)
  test_observation_api.py   # Query traces (mocked backend)
```

### Step 11: Create gateway_access.yaml

```yaml
tokens:
  - name: dev-local
    secret: "dev-token-not-for-production"
    scopes: [knowledge:read, knowledge:write, sessions:read, observations:read]
    rate_limit: 1000
```

---

## Verification

| Check | Command |
|-------|---------|
| Auth works | `curl -H "Authorization: Bearer dev-token" http://localhost:9000/api/v1/health` → 200 |
| Auth rejects | `curl http://localhost:9000/api/v1/knowledge/search?q=test` → 401 (when auth enabled) |
| Knowledge search | `curl -H "Auth..." "http://localhost:9000/api/v1/knowledge/search?q=memory+protocol&limit=5"` |
| Rate limit | 1001st request in 1 hour → 429 |
| mypy | `uv run mypy src/personal_agent/gateway/` |
| Tests | `uv run pytest tests/personal_agent/gateway/ -v` |

---

## What's NOT in this issue

- VPS provisioning (ADR-0045 Phases 1–2) — separate issue after Docker Compose simulation
- Reverse proxy (Caddy/Traefik) config — infrastructure, not application code
- Event bus proxy — deferred to Phase 2
- `GatewaySessionStoreClient` / `GatewaySearchIndexClient` — only `GatewayKnowledgeGraphClient` for now (most critical path)
