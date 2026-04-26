# ADR-0064: Inbound User Identity via Cloudflare Access

**Status**: Accepted — FRE-268 (session scoping) shipped 2026-04-26; FRE-229 (memory visibility layer) shipped 2026-04-26
**Date**: 2026-04-26
**Deciders**: Project owner
**Related**: ADR-0052 (Seshat Owner Identity Primitive / FRE-213), FRE-228 (`:User` node + me/I/my resolution — parked), FRE-229 (memory visibility model — redirected by this ADR), FRE-235 (session list drawer — the live leak)
**Implementation Plan**: `plans/i-think-fre-213-258-linear-mitten.md`

---

## Context

The personal-agent FastAPI service runs behind Cloudflare Access with per-human email policies. The active user population is 2–5 trusted testers. On 2026-04-25 the PWA shipped a session-list drawer (FRE-235) whose `GET /sessions` handler calls `repo.list_recent(limit)` with no user filter. As a result:

- Every tester's drawer renders every other tester's conversation list.
- Any session_id can be opened via `GET /sessions/{id}`, patched via `PATCH /sessions/{id}`, streamed via SSE `/stream/{session_id}`, and continued via `POST /chat` / `POST /chat/stream` by anyone holding the URL.

This is a live cross-user data leak, not a hypothetical. The trusted small group makes cross-user *memory* sharing acceptable (and arguably desirable — testers benefit from each other's discoveries), but session-level leakage is not.

A separate but adjacent project, FRE-229, was previously specified to deliver a three-level visibility model (`private` / `group` / `public`) with `:Group` nodes, `:MEMBER_OF` edges, and a `MULTI_USER_ENABLED` flag. That scope is heavier than the actual user need: there is no group/team/family use case in the foreseeable horizon. The FRE-235 leak therefore demands a smaller, faster decision than FRE-229's full surface area.

ADR-0052 already established a single-operator identity primitive (`:Agent -[:OPERATED_BY]-> :Person`) seeded from `AGENT_OWNER_NAME`. That ADR remains correct; this one extends the model to multi-user *inbound* identity without disturbing the operator/owner concept it defined.

---

## Decision

Scope session and stream endpoints by an authenticated `user_id` resolved from Cloudflare Access headers, backed by a small Postgres `users` table. Leave the memory graph global. Redirect FRE-229's target end-state from three levels to two.

### D1 — Identity provider: Cloudflare Access

CF Access is the canonical identity source. Every request to the service is expected to carry:

| Header | Purpose |
|---|---|
| `Cf-Access-Authenticated-User-Email` | Verified email of the human |
| `Cf-Access-Jwt-Assertion` | Signed JWT, verifiable against the CF Access team JWKS |

The service consumes the email as identity and verifies the JWT against the cached team JWKS as defense-in-depth. JWKS is fetched once and cached with periodic refresh. JWT verification is cheap with PyJWT, and protects against any future direct-path exposure (misconfigured tunnel, accidental port forward, dev environment skew).

This decision avoids building a second authentication layer in FastAPI: CF Access already authenticates each tester at the edge with verified email policies. The service trusts CF as the identity source, while still verifying signatures.

### D2 — Identity store: a Postgres `users` table

A new table is added:

| Column | Type | Notes |
|---|---|---|
| `user_id` | `UUID PRIMARY KEY` | Durable foreign key |
| `email` | `TEXT UNIQUE NOT NULL` | Identity, may churn |
| `display_name` | `TEXT NULL` | Optional friendly name |
| `created_at` | `TIMESTAMPTZ NOT NULL` | Audit |

Records auto-create on the first authenticated request via `get_or_create_user_by_email`. UUIDs (not emails) are the foreign key for ownership; emails can change while the underlying identity remains stable.

### D3 — Session ownership

`sessions.user_id UUID NOT NULL` is added (FK to `users.user_id`, indexed). An alembic migration backfills existing rows to the deployment owner's UUID (the service was effectively single-user before this change). The following endpoints become user-scoped:

- `GET /sessions` — filtered by `user_id = request_user.user_id`
- `GET /sessions/{id}` — 404 on ownership mismatch
- `PATCH /sessions/{id}` — 404 on ownership mismatch (and rejects ownership transfer attempts)
- `POST /sessions` — writes `user_id = request_user.user_id`
- `POST /chat`, `POST /chat/stream` — ownership check before processing
- SSE `/stream/{session_id}` — ownership check before subscription

**Authorization model: 404, not 403.** A 403 confirms a session exists but the caller cannot access it. A 404 reveals nothing. For a small trusted-tester deployment leakage of mere existence is low-risk, but the cost of returning 404 is zero and the principle is worth establishing now.

### D4 — Dev / CLI fallback

When `gateway_auth_enabled=False` and no `Cf-Access-Authenticated-User-Email` header is present, identity falls back to the deployment owner. A new setting `AGENT_OWNER_EMAIL` provides this email; the owner UUID is derived by passing this email through the same `get_or_create_user_by_email` path used for CF requests. This is load-bearing: the CLI path and the production CF path **must resolve to the same `user_id`**, otherwise the deployment owner sees two disjoint session histories depending on entry point.

The dev fallback is also the path used by `service_cli.py` (the `uv run agent` entrypoint), preserving existing local development and CLI workflows without code changes.

### D5 — Memory remains global in this slice

Memory partitioning is **explicitly out of scope** for this ADR — not deferred-by-omission, but deliberately decided against. Justification:

- The 2–5 trusted testers benefit from a shared knowledge graph; cross-pollination is desired.
- Partitioning every Cypher call site at once is a high-blast-radius change for marginal benefit in this group size.
- The chokepoint can be added later without rework if and when `MemoryQuery.user_id` is introduced.

ADR-0052's single-operator stanza (FRE-213) likewise stays as-is for this slice: every authenticated user sees the deployment owner's profile in the system prompt. Acceptable in a trusted group; revisit when memory partitioning lands.

### D6 — Target end-state for memory partitioning (informs follow-ups, not this ADR's slice)

When memory partitioning is desired, the model is **three levels with a single unnamed group**:

| Level | Meaning | Who sees it |
|---|---|---|
| `public` | World/shared knowledge — public figures, project facts, general concepts | All users, including unauthenticated fallback (CLI) |
| `group` | Household/family knowledge — facts about the family unit, shared context, routines | All CF Access authenticated users |
| `private` | Individual knowledge — personal facts about the specific user | Only the owning user |

**The group is unnamed and has no membership management.** Group = "is authenticated via CF Access." Adding a person to the family = adding them to the Cloudflare Access email policy. This eliminates the need for an admin console inside the agent entirely.

A single chokepoint Cypher filter applies to all reads in `memory/service.py`:

```cypher
WHERE m.visibility = 'public'
   OR (m.visibility = 'group' AND $authenticated = true)
   OR m.visibility = 'private:' + $user_id
```

When `authenticated = false` (CLI / dev fallback, `user_id = NULL`), the filter returns only `public` nodes — the shared world-knowledge graph.

**Default visibility at extraction time**: `group`. A new fact extracted from an authenticated session is household-visible by default; the agent or user can promote to `public` or demote to `private` based on classification heuristics (see FRE-229).

### D7 — Simplified redirect of FRE-229

FRE-229 originally specified a three-level scope (`private` / `group` / `public`) with `:Group` nodes, `:MEMBER_OF` edges, and a `MULTI_USER_ENABLED` flag. **This ADR simplifies FRE-229's model** — the three levels are retained, but the group infrastructure is removed:

- ~~`:Group` node with `type` (family/friends/team/custom)~~
- ~~`(:User)-[:MEMBER_OF {role, since}]->(:Group)` relation~~
- ~~`MULTI_USER_ENABLED` feature flag~~
- ~~Admin console for group management~~

Justification: the deployment is a family assistant. The family unit is exactly the set of people in the CF Access policy, which the operator already manages. Building group-management infrastructure inside the agent would duplicate what CF Access already provides. The three-level visibility model is correct; the group management layer is unnecessary.

### D8 — No PWA changes

CF Access authenticates every PWA request at the edge. The PWA's existing `NEXT_PUBLIC_GATEWAY_TOKEN` continues to authorize the *client*; the new `Cf-Access-Authenticated-User-Email` header authenticates the *human*. The PWA's session drawer code is unchanged — it already calls `GET /sessions`, which now returns only the caller's sessions.

---

## Consequences

### Positive

- **Closes the FRE-235 cross-user leak** with the smallest possible change to the service.
- **Reuses CF Access infrastructure** already deployed for the trusted-tester group; no second auth layer in FastAPI.
- **PWA and CLI unchanged.** No client work; existing CLI flows continue via the dev fallback.
- **Preserves shared knowledge graph** — testers continue to benefit from each other's memory contributions during the small-group phase.
- **Establishes the multi-user identity primitive** (`users` table, UUID FK on sessions) that future work (FRE-228 self-referential extraction, eventual memory partitioning) builds on without rework.
- **Drops FRE-229's group complexity** before any of it is implemented — saves design/code cost on a feature the user does not need.

### Negative / Risks

- **New runtime dependency on CF Access JWKS**: a JWKS fetch is required on cold start and on rotation. Mitigated by caching with periodic refresh and the dev-mode fallback, which means service degradation in CF outages remains localized to JWT-verification, not to identity itself (header email is still consumable).
- **One Postgres table + one column**: low-cost migration; backfill is mechanical (all existing rows assigned to the deployment owner UUID). Risk is bounded.
- **Owner-identity misconfiguration**: the deployment owner's CF Access email **must equal** `AGENT_OWNER_EMAIL`, otherwise the owner resolves to two distinct user_ids depending on entry point (CLI vs CF). Mitigations: documented in `.env.example`, verified by an integration test that boots the service, hits both paths, and asserts identity equality.
- **Per-user rate limits, per-user telemetry attribution, per-user operator stanza** are deferred. Acceptable in a 2–5 user trusted group; revisit when memory partitioning lands.
- **Header-trust fallback**: if `gateway_auth_enabled=False` is set in production by mistake, the service would fall back to deploying-owner identity for any header-less request. Mitigation: production deployment sets `gateway_auth_enabled=True`; CI asserts this in the deployed config.

### Neutral

- The session leak is closed without changing memory semantics. Cross-user knowledge sharing remains possible (and is preserved by design).
- ADR-0052's owner stanza behaviour is unchanged.

---

## Alternatives Considered

### A. OAuth/OIDC implemented inside the FastAPI service

*Rejected.* CF Access already provides verified email identity at the edge with a signed JWT. Building a second auth layer duplicates effort, adds attack surface (CSRF, session fixation, refresh-token handling), and yields no capability beyond what the edge already provides for this deployment.

### B. Cookie / session auth in FastAPI

*Rejected* for the same reasons as A. Adds CSRF surface and a parallel identity story.

### C. Trust the tunnel — read the email header without JWT verification

*Rejected as the primary path; accepted as the dev-mode fallback.* In the production deployment the only path to the service is via CF Access, so reading the header is "good enough" for correctness today. Verifying the JWT is cheap with PyJWT + cached JWKS and protects against any future direct path (misconfigured tunnel, dev port forwarding, accidental exposure). The asymmetry between cost and protection makes verification the right default.

### D. FRE-229's original group infrastructure (`:Group` nodes, `:MEMBER_OF`, `MULTI_USER_ENABLED` flag)

*Rejected.* The three-level scope (public / group / private) is retained in D6/D7. What is rejected is the group *management* infrastructure: named groups, membership edges, and the admin console they require. The family/household group is not a managed entity — it is the CF Access policy. Group membership = "has a CF Access login." This eliminates the entire group lifecycle problem (creation, membership transfer, scope inheritance) without giving up the family-assistant use case.

### E. Memory partitioning in the same slice as session ownership

*Rejected as scope creep.* Every Cypher call site in `memory/` would need editing; blast radius is high; the marginal privacy benefit in a trusted group is low. The chokepoint filter design in D6 means partitioning can be added later without architectural rework — `MemoryQuery.user_id` slots in cleanly when the user actually wants per-user memory.

### F. Per-user operator stanza now (extend ADR-0052)

*Rejected for this slice.* Per-user stanzas require memory partitioning to source per-user `:User` nodes; without partitioning, every guest tester would see the deployment owner's profile anyway. Defer to the slice that lands FRE-228's `:User` node.

---

## Implementation Pointers

Full plan in `plans/i-think-fre-213-258-linear-mitten.md`. Summary: ~300–500 lines including migration and tests, touching `service/auth.py` (new), `service/app.py` (six ownership checks), `service/models.py` (`SessionModel.user_id`, new `UserModel`), `service/sessions.py` (filter parameters), `transport/agui/endpoint.py` (SSE check), `config/settings.py`, `.env.example`, and one alembic migration.

---

## Verification

1. **End-to-end manual**: tester A and tester B (different CF emails) load the PWA. A creates a session, B's drawer does not show it; B's direct `GET /sessions/{A's id}` returns 404; B's SSE subscribe to A's session_id is rejected.
2. **Unit tests**: header parsing, JWKS verification (mocked), dev-mode fallback identity, session ownership filter on `list_recent` / `get_session`, ownership-transfer rejection on `PATCH`.
3. **Integration test**: boot service with both CF header and CLI fallback paths; assert both resolve to the same `user_id` when `AGENT_OWNER_EMAIL` matches the CF identity.
4. **Migration**: `alembic upgrade head` on a copy of the production database; assert all existing rows backfilled, no NOT NULL violations.
5. **Backwards compatibility**: `make test`, `make mypy`, `make ruff-check` all green; `uv run agent "..."` continues to work without CF headers.

---

## Related

- ADR-0052 — Seshat Owner Identity Primitive (single-operator stanza; remains as-is)
- FRE-213 — Owner identity bootstrap (Approved; unchanged by this ADR)
- FRE-228 — `:User` node + me/I/my resolution (parked; ships when memory partitioning is desired)
- FRE-229 — memory visibility model (this ADR redirects to two-level; FRE-229 should be edited accordingly)
- FRE-235 — session list drawer (the live leak)
- Implementation plan: `plans/i-think-fre-213-258-linear-mitten.md`
