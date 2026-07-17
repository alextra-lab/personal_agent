# ADR-0069: R2-Backed Artifact Substrate

**Status**: Implemented (FRE-227 + FRE-371 shipped 2026-05-17) — two implementation deviations recorded below
**Date**: 2026-05-15
**Deciders**: Project owner
**Related**: ADR-0064 (Inbound User Identity via Cloudflare Access), ADR-0063 (Primitive Tools / Action-Boundary Governance), ADR-0052 (Seshat Owner Identity Primitive), ADR-0070 (Output Channel Model), FRE-227, FRE-368, FRE-369, FRE-370
**Implementation Plan**: deferred — FRE-227 execution will produce one in `docs/superpowers/plans/`

---

## Context

FRE-227 originally proposed a "protected directory tree with agent write access" — a local-volume scratchpad for agent notes, searchable by NLP. In design discussion on 2026-05-15 (see `docs/superpowers/plans/i-want-to-research-bubbly-shannon.md`) the framing widened: the protected tree is not a feature, it is a **substrate** that a family of features bind onto:

- Notes (text, agent-internal) — the original FRE-227 use case
- Artifacts (HTML reports, charts, dashboards the agent produces for human consumption) — FRE-368
- User uploads (images, files, URLs dropped into chat) — FRE-369
- Future: auto-updating CLAUDE.md (FRE-226), Captain's Log human-readable briefings, generated reports, URL-ingestion via Browser Rendering

Three additional requirements emerged that ruled out a local-volume approach:

1. **Addressability from outside the PWA** — the user accesses the agent from both iPad Safari and a laptop browser. Artifacts must be reachable by URL without going through the chat surface — for bookmarking, sharing, and later reference. A local volume satisfies the agent's read/write needs but not the user-facing URL surface.
2. **Persistence outside the PWA's lifecycle** — artifacts must survive PWA cache wipes, session deletions, and device migrations. Conversation-bound storage is insufficient.
3. **A single substrate for multiple content types** — note, artifact, upload, capture share enough common shape (bytes, metadata, NLP-searchable) that splitting their physical storage costs more than it saves.

ADR-0064 already established Cloudflare Access as the canonical identity layer and the existing `users` table as the FK target for ownership. This ADR builds on that foundation rather than restating it.

---

## Decision

Adopt **Cloudflare R2 + Cloudflare Workers + Postgres metadata** as the artifact substrate. Bytes live in R2, metadata lives in Postgres, access is gated by the existing Cloudflare Access policy, and the agent talks to R2 via a native S3 SDK (no filesystem mount).

### D1 — Storage tier: Cloudflare R2 (EU jurisdiction)

R2 bucket `seshat-artifacts` created with `jurisdiction: eu`. Reasoning:

- **Zero egress cost** — uncommon among object stores and load-bearing when the user opens artifacts from mobile networks
- **Already in topology** — Cloudflare manages DNS, tunnel, WAF, Access for `example.com`; storage is a small addition to an existing IaC surface
- **S3-compatible** — agent-side client is portable; migration to MinIO-on-Synology or OVH Object Storage later is a configuration change, not a code change
- **EU jurisdiction** — bytes stay in EU data centers; GDPR-by-construction; aligns with existing VPS location (OVH, EU)

R2's free tier (10 GB storage, 1M Class A operations, 10M Class B operations per month) covers projected personal-tier usage. Overage is pay-as-you-go and negligible at projected scale.

### D2 — Compute tier: Worker on Workers Paid plan

A single Cloudflare Worker fronts R2 at `artifacts.example.com`. Responsibilities:

- Resolve `GET /{artifact_id}` → Postgres metadata lookup → R2 GET → return bytes with appropriate headers
- Generate presigned PUT URLs for user-upload flow (FRE-369)
- Set `Content-Type`, `Cache-Control`, and `Content-Disposition` headers per artifact metadata
- Enforce Cloudflare Access policy at the edge (Access sits in front of the Worker route)

Workers Paid ($5/month) is adopted over Workers Free because it provides:

- 10M requests/month bundled (vs 100k/day free tier)
- 30s CPU time per request (vs 10ms free tier — relevant for future Browser Rendering integration)
- **Browser Rendering** access — closes the future URL-ingestion loop (capture a webpage as PDF/screenshot to R2) without separate tooling
- Durable Objects, Queues, Cron Triggers — not required for this ADR but available for future work (e.g., HITL approval coordination)

### D3 — Identity & authorization: inherit ADR-0064

The Worker route sits behind a Cloudflare Access application policy. Per ADR-0064, every authenticated request carries `Cf-Access-Authenticated-User-Email` and `Cf-Access-Jwt-Assertion`. The substrate inherits:

1. **Identity source** — Cloudflare Access, no second auth layer
2. **Identity store** — the existing `users` table; `artifacts.user_id` FKs into it
3. **Authorization semantics** — 404 on ownership mismatch (per ADR-0064 D3 §"Authorization model: 404, not 403")
4. **Dev/CLI fallback** — header-less requests in dev resolve to the deployment owner via `AGENT_OWNER_EMAIL` (per ADR-0064 D4); CLI artifact creation works without authentication infrastructure

Session duration on the `artifacts.example.com` Access app uses the same 720h policy as the rest of the stack (per FRE-370).

### D4 — Metadata canon: Postgres `artifacts` table

Bytes live in R2; metadata is canonical in Postgres. Schema changes go in `docker/postgres/init.sql` plus an ordered file under `docker/postgres/migrations/` (no Alembic per project convention).

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PRIMARY KEY` | The `artifact_id` exposed in URLs |
| `user_id` | `UUID NOT NULL` | FK to `users.user_id` (per ADR-0064) |
| `session_id` | `UUID NULL` | FK to `sessions.id`; NULL for session-detached artifacts (briefings, scheduled exports) |
| `type` | `TEXT NOT NULL` | Discriminator: `note` / `artifact` / `upload` / `capture` |
| `slug` | `TEXT NULL` | Optional human-readable handle within session scope |
| `title` | `TEXT NULL` | Display title |
| `summary` | `TEXT NULL` | Short summary for inline cards (per ADR-0070) |
| `content_type` | `TEXT NOT NULL` | MIME type — controls Worker `Content-Type` header |
| `size_bytes` | `BIGINT NOT NULL` | For governance limits and display |
| `r2_key` | `TEXT NOT NULL UNIQUE` | Opaque to the agent; internal R2 key |
| `tags` | `TEXT[] DEFAULT '{}'` | Free-form tags for filtering |
| `embedding` | `vector(N) NULL` | pgvector embedding for NLP search (FRE-227 requirement) |
| `created_by` | `TEXT NOT NULL` | `agent` / `user` |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | Audit |

Indexes:

- `(user_id, type, created_at DESC)` for `artifact_list` queries
- `ivfflat (embedding vector_cosine_ops)` for NLP search
- Unique constraint on `r2_key`

**Rationale for Postgres-as-canon over R2 object metadata:**

- pgvector embeddings for NLP search are a hard requirement from FRE-227
- Postgres already runs in the deployment; no new infrastructure
- Object-metadata-only filtering requires LIST + many HEAD calls; expensive at scale
- FK referential integrity to `users` / `sessions` is enforceable here; R2 object metadata cannot enforce it

### D5 — Key & URL schemes are separated

**R2 key** (internal, never exposed publicly):

```
{type}/{user_id}/{session_id_or_GLOBAL}/{artifact_id}_{slug}.{ext}
```

Human-greppable in the R2 console for ops; segregated by user and type for ACL clarity even though access is mediated by Postgres + Worker. The `slug` is optional (omitted = `{artifact_id}.{ext}`).

**Public URL** (exposed):

```
https://artifacts.example.com/{artifact_id}
```

Flat content-addressable. The session_id, user_id, and type **never appear in URLs**. Reasoning:

- URLs survive rename, tag changes, even type promotion (e.g., note → artifact) without breaking
- Session_id leakage in URLs is a sharing-surface risk; flat IDs eliminate it
- All routing decisions live in the Worker + Postgres; the URL is a stable opaque handle

A future slug-redirect form (`https://artifacts.example.com/{slug}` → `/{artifact_id}`) is conceivable but **out of scope** for this ADR.

### D6 — Agent-side client: native S3 SDK, no FUSE mount

The agent talks to R2 via `aiobotocore` (async S3-compatible client). A new module `src/personal_agent/storage/artifact_store.py` provides an `R2ArtifactStore` wrapper that encapsulates:

- Per-content-type prefix scoping (the `notes_write` tool cannot write to an `artifact_*` R2 prefix, and vice versa)
- Path-prefix guard against R2 key escape (security tests in FRE-227)
- Content-type metadata propagation to R2 object headers
- Presigned URL generation for user-upload flow (FRE-369)

A FUSE mount alternative (rclone, s3fs) was considered and rejected — see Alternative C below.

### D7 — Single bucket, three flows

One R2 bucket serves three flows, discriminated by the `type` column:

| Flow | Direction | Mechanism | Type |
|---|---|---|---|
| Agent writes artifact | gateway → R2 | `artifact_write` tool, S3 PUT via `R2ArtifactStore` | `artifact` / `note` |
| User uploads file | PWA → R2 | gateway issues presigned PUT URL, PWA uploads directly to R2 (gateway bandwidth not consumed) | `upload` |
| URL ingestion (future) | Worker → R2 | Browser Rendering captures page to R2 | `capture` |

Reasoning for one bucket: access pattern, retention policy, and metadata schema are uniform across flows. Segregation is at the `type` discriminator and the R2 key prefix, not the bucket level. Per-flow buckets would require parallel Worker routes, parallel terraform, parallel Access policies — overhead with no benefit.

### D8 — Terraform location: private secrets repo

All HCL — R2 bucket, Worker, Cloudflare Access application, DNS records, secrets bindings — lives in `alextra-lab/personal_agent_secrets/terraform-cloudflare/`. The public `alextra-lab/personal_agent` repo contains:

- The agent code (Python tools, gateway endpoints, storage client)
- The Postgres schema migration (SQL with no deployment-specific identifiers)
- This ADR and related documentation

The public repo contains **zero** deployment-specific identifiers (R2 account ID, bucket name, Worker URL, Access app ID). This continues the practice established by FRE-213 / ADR-0052 and is enforced by `scripts/check_no_personal_paths.py` (pre-commit hook).

---

## Consequences

### Positive

- **Substrate is shared by every future feature** needing persistent, addressable, NLP-searchable bytes. FRE-226 (auto-updating CLAUDE.md), Captain's Log human briefings, future memory exports, generated reports — all bind onto the same table and Worker without separate infrastructure.
- **Architecturally aligned with ADR-0064.** Identity, authorization, dev fallback all inherited; no parallel auth surface in the agent.
- **Zero new vendors.** Cloudflare is already in the topology; R2 + Workers extend an existing relationship rather than introduce a third-party dependency.
- **Migration path preserved.** S3-compatible API means MinIO-on-Synology or OVH Object Storage is a `R2ArtifactStore` endpoint change, not a code rewrite. The substrate decision is reversible if sovereignty constraints later shift.
- **URL stability.** Flat content-addressable URLs survive metadata changes; bookmarks and shared links don't break under reorganization.
- **Per-tool auditability.** Every write is a function call carrying `trace_id`, governance check, rate limit. A FUSE mount would have made write auditing significantly harder.
- **Closes a known UX gap.** Mobile and laptop browsers reach artifacts without requiring the PWA — bookmarkable, shareable, viewable on devices the agent doesn't run on.

### Negative / Risks

- **Workers Paid plan ($5/month) becomes a hard dependency.** Workers Free is theoretically sufficient at current scale but Workers Paid was chosen for headroom and Browser Rendering. Mitigation: cost is bounded, well below the personal-tier comfort threshold, and unlocks several optional capabilities.
- **Postgres becomes load-bearing for the Worker path.** Every artifact fetch is `Worker → Postgres → R2`. A Postgres outage = 503 on artifact reads. Mitigation: the same Postgres outage already breaks `/chat` and `/sessions`; the new failure mode is not strictly larger. A future enhancement caches the `{artifact_id} → {r2_key}` map in Workers KV with short TTL if measured fetch volume warrants it.
- **EU jurisdiction lock-in.** R2 buckets cannot change jurisdiction after creation. If the deployment ever moves outside EU, migration (read-from-eu → write-to-new-jurisdiction) is required. Acceptable: deployment intent is EU-resident long-term.
- **R2 eventual consistency on object LIST.** Not relevant to this ADR (the agent never LISTs from R2; metadata queries hit Postgres) but worth noting for ops tooling.
- **Privilege model placeholder remains.** ADR-0064's two-level visibility (group / private) applies to memory, not yet to artifacts. This ADR defaults all artifacts to creator-private; cross-user sharing is deferred to a follow-up if/when the need arises (FRE-345 placeholder).

### Neutral

- **Existing primitive tools** (`primitives/read.py`, `primitives/write.py` per ADR-0063) are unchanged. They operate on sandbox scratch space — a different concern from the artifact substrate. Sandbox primitives and artifact tools coexist without overlap.
- **The CLI** path inherits ADR-0064's fallback identity. CLI-originated artifacts work via the deployment owner's `user_id` resolved through `AGENT_OWNER_EMAIL` without code changes.
- **MCP tools** continue to flow through the existing `_summarize_tool_result` (executor.py:2451) markdown summarization. Any future MCP tool that wants to produce a rich artifact does so by calling `artifact_write` like a native tool would; no special MCP handling required.

---

## Alternatives Considered

### A. Local Docker volume (the original FRE-227 framing)

*Rejected.* Satisfies agent read/write needs but fails the "available outside the PWA" requirement. A second layer (rclone serve webdav, Caddy directory listing, a separate signed-URL service) would be needed to expose bytes to external readers — at which point the local volume's only advantage is "bytes don't leave the VPS," which is also true of MinIO-on-VPS or MinIO-on-Synology with strictly more capability. The local volume is also tied to the VPS lifecycle; recovery from VPS loss requires explicit backup restoration.

### B. Synology NAS via MinIO (or native WebDAV / Synology Drive)

*Rejected as initial choice; preserved as a future migration path.* The Synology hardware is owned and gives the strongest sovereignty story. Reasons not to start there:

- Operating MinIO is non-trivial (versioning, replication, monitoring, durability story)
- "Bytes never leave home" comes at the cost of LAN-dependent reachability — mobile tethered scenarios add latency or fail entirely
- R2's free egress is genuinely useful for the access-from-anywhere requirement
- Synology + Cloudflare Tunnel achieves equivalent reachability with significantly more moving parts

Migration to MinIO-on-Synology later is an `R2ArtifactStore` endpoint and credentials change. The S3-compatible API choice keeps this reversible.

### C. FUSE mount of R2 inside the gateway container (rclone, s3fs, goofys)

*Rejected.* The mount option's only advantage is "agent reuses existing filesystem primitives." Costs:

- R2 credentials live on disk as rclone config — broader blast radius if container is compromised
- POSIX semantics lose access to S3-native features (content-type metadata, object tags, multipart upload, lifecycle rules, presigned URLs)
- Mount lifecycle adds failure modes (stale mounts, FUSE kernel issues) inside a container
- Per-tool auditing is harder — filesystem writes don't carry tool-call context
- The agent doesn't need a mount: it queries `artifact_list` (Postgres) and writes via `artifact_write` (tool boundary). Filesystem semantics buy nothing the SDK lacks.

Native SDK is more secure, more flexible, and only marginally more code.

### D. OVH Object Storage

*Rejected.* Technically viable — OVH offers an S3-compatible Public Cloud Storage product. Rejected because:

- Adds a third vendor (Cloudflare for edge + OVH for compute + OVH for storage); diversification has cost
- Not integrated with the existing Cloudflare terraform; Worker → OVH cross-vendor latency is real
- Egress is non-zero (vs R2's zero); marginal but consistently paid
- No data-sovereignty win over R2 EU jurisdiction

### E. R2 with object metadata as canon (no Postgres table)

*Rejected.* R2 object metadata is sufficient for trivial use (filename, content-type, custom headers) but cannot support:

- pgvector embeddings for NLP search (hard requirement from FRE-227)
- FK referential integrity to `users` / `sessions`
- Efficient filtering by type, date, tag (requires LIST + many HEAD requests on R2)
- Audit trails of metadata changes

Postgres-as-canon costs one table; the alternative costs every query.

### F. Hierarchical public URLs (e.g., `/sessions/{session_id}/{artifact_id}`)

*Rejected.* Hierarchical URLs:

- Leak session_id (sharing-surface risk; bookmarks reveal session structure)
- Don't survive content reorganization (an artifact promoted from session-scoped to global breaks its URL)
- Force the user to remember structural conventions; flat IDs require no memory

Flat content-addressable URLs are the de-facto pattern at every hosted agent (Claude artifacts, ChatGPT files, v0, Replit, Vercel build outputs) for exactly these reasons.

### G. Separate buckets per flow

*Rejected.* Three buckets, three Worker routes, three Access policies, three terraform stanzas — for no benefit beyond "type discrimination at the storage layer instead of the metadata layer." Single bucket with `type` discriminator is simpler, cheaper, and equally auditable.

---

## Implementation Deviations (FRE-371, 2026-05-17)

### Dev-1 — EU jurisdiction dropped from R2 bucket (D1)

ADR prescribed `jurisdiction: eu`. In practice, Cloudflare's Workers binding API (error 10085) cannot locate EU-jurisdiction buckets — they live in a separate API namespace (`/jurisdictions/eu/accounts/…/r2/buckets`) that the script deployment endpoint does not query. The bucket was created with `location = "EEUR"` (Eastern Europe datacenter) and no jurisdiction flag. Physical data residency is unchanged; the CF contractual GDPR guarantee is not present. Acceptable for a personal project with no regulatory obligation.

### Dev-2 — Separate artifacts Access app replaced with self_hosted_domains (D3) — **REVERTED 2026-05-17**

**Original deviation (no longer in force):** ADR implied a separate `cloudflare_zero_trust_access_application` for `artifacts.example.com`. To avoid a per-visit OTP prompt (separate per-app cookies), `artifacts.example.com` was originally added as a secondary entry under the `destinations` attribute of the existing `agent` Access application.

**Why the revert (H1 finding, 2026-05-17 07:54 UTC):** during FRE-227 smoke testing the Worker fronting `artifacts.example.com` returned 404 on every legitimate request even though CF Access was authenticating the user. Wrangler tail showed the request arriving at the Worker **without `Cf-Access-Jwt-Assertion`**. The cause is a documented-but-subtle CF Access behavior: **the JWT header is injected only on the application's primary `domain`, not on secondary `destinations` entries.** Destinations get policy enforcement (auth gate, allowlist) but not auth-context propagation. The Worker had no JWT to verify, the gateway had no JWT to receive — the entire identity chain collapsed silently.

**Current state:** the dedicated `cloudflare_zero_trust_access_application.artifacts` is restored, with the same policy as the agent app (`personal_only`) and `session_duration = "720h"` + `auto_redirect_to_identity = true` per FRE-370 convention. Cross-app SSO works at the CF Access team-domain level (one global session covers both `agent.example.com` and `artifacts.example.com`), so the original UX motivation for Dev-2 (no per-visit re-auth) is still satisfied without breaking JWT propagation.

**Lesson for future ADRs:** when a substrate depends on `Cf-Access-Jwt-Assertion` injection, each hostname that needs the JWT must be the **primary `domain`** of its own Access application. The `destinations` attribute is for policy reuse, not auth-context reuse.

### Dev-3 — JWT verification required end-to-end (security hardening, 2026-05-17)

**Background:** the original ADR D3 inherited authorization from ADR-0064 (header-based identity via `Cf-Access-Authenticated-User-Email`). During FRE-227 smoke testing on 2026-05-17, an off-allowlist email materialized a `users` row at 04:45:18 UTC despite the CF Access policy include list containing only four allowlisted emails. Root cause: the `Cf-Access-Authenticated-User-Email` header is a plaintext string that can be forged by any caller that reaches the Worker (e.g., via the workers.dev default URL) or the gateway internal endpoint (with the shared `X-Internal-Token`). Trusting it without cryptographic verification was a spoofing vector.

**Hardening (PR #65 + FRE-371 Phase B):** every layer that consumes a CF Access identity now requires a cryptographically-verified `Cf-Access-Jwt-Assertion`:

1. **Worker** validates the incoming JWT against the team JWKS + app `aud` before any further processing. Returns 404 to the caller if verification fails.
2. **Worker forwards the validated JWT** to the gateway as `X-Cf-Access-Jwt-Assertion` — it does *not* forward `X-Authenticated-User-Email`.
3. **Gateway re-verifies the JWT** independently (`service/cf_access_jwt.py`) against the same JWKS + `aud`. The verified `email` claim is the only trusted identity source. Plaintext email headers are explicitly ignored.
4. **`X-Internal-Token`** remains as a defense-in-depth filter that rejects callers that aren't our Worker — but it is no longer the sole gate.
5. **`workers_dev = false`** on the artifacts Worker resource eliminates the bypass URL, leaving `artifacts.example.com` (Access-gated) as the only public ingress.

The gateway requires `cf_access_team_domain` + `cf_access_aud` to be populated; missing config returns 503 (fail-closed) rather than degrading to header-trust. Settings live in `personal_agent.config.settings` (already defined since FRE-213; verification code is new).

Verified end-to-end at 08:08 + 08:11 UTC on 2026-05-17 with fresh writes and reads.

---

## Implementation Pointers

Substrate implementation is FRE-227. Consumers (FRE-368 artifacts, FRE-369 uploads) build on it sequentially.

Files touched in FRE-227:

- `src/personal_agent/storage/artifact_store.py` — new (`R2ArtifactStore` wrapper class)
- `src/personal_agent/tools/notes_tools.py` — new (`notes_write`, `notes_search` — the first consumer)
- `src/personal_agent/tools/registry.py` — register notes tools
- `config/governance/tools.yaml` — governance entries for notes tools
- `docker/postgres/init.sql` + new ordered file under `docker/postgres/migrations/` — `artifacts` table schema
- `src/personal_agent/config/settings.py` — R2 endpoint URL, bucket name, Worker URL, credentials (env-injected)
- `personal_agent_secrets/terraform-cloudflare/` — R2 bucket, Worker, Access application, DNS record (private repo)

A detailed implementation plan will be written in `docs/superpowers/plans/YYYY-MM-DD-fre-227-*.md` when FRE-227 is taken up.

---

## Verification

1. **Round-trip**: agent calls `notes_write("test", "hello")`, retrieves via `notes_search("hello")`, verifies content matches and `r2_key` resolves to the same bytes via Worker (via the Access-gated URL). ✅ verified 2026-05-17 08:08 + 08:11 UTC.
2. **Access policy**: an unauthenticated browser session attempting `GET https://artifacts.example.com/{any-id}` is blocked at the Cloudflare Access edge before reaching the Worker. ✅ verified — returns 302 to `team.cloudflareaccess.com/cdn-cgi/access/login/...`.
3. **Prefix escape**: `notes_write` cannot write to an `artifact_*` R2 prefix; governance + storage layer both reject. Negative tests in FRE-227.
4. **Cross-user ownership**: user A writes an artifact; user B's `artifact_list` does not return it; user B's `GET /{artifact_id}` returns 404 per ADR-0064 D3 semantics.
5. **Sovereignty**: R2 bucket location confirmed `EEUR` (physical EU residency, no CF contractual EU jurisdiction — see Dev-1).
6. **CLI fallback**: a CLI-originated `notes_write` resolves to the deployment owner's `user_id` and is visible to the same user through the PWA (no disjoint identity across entry points).
7. **Cost monitoring**: monthly R2 + Workers spend visible via Cloudflare dashboard; alert at 80% of bundled limits (alerting itself deferred to ops follow-up).
8. **JWT spoofing rejected (Dev-3 hardening)**: a request to `/internal/artifacts/{id}` with `X-Internal-Token` but no `X-Cf-Access-Jwt-Assertion` returns 401 (verified on VPS); a request with a forged `X-Authenticated-User-Email` header but no JWT returns 401 (regression test pinned in `tests/personal_agent/service/test_artifacts_router.py`).
9. **`workers_dev = false`** (Phase B1): the `<script>.workers.dev` bypass URL is unreachable; the Worker only accepts traffic via `artifacts.example.com` behind CF Access.

---

## Related

- **ADR-0064** — Inbound User Identity via Cloudflare Access (foundation; this ADR inherits auth model directly)
- **ADR-0063** — Primitive Tools / Action-Boundary Governance (tool/governance entry pattern this ADR's tools follow)
- **ADR-0052** — Seshat Owner Identity Primitive (CLI/dev fallback identity)
- **ADR-0070** — Output Channel Model (companion ADR; uses this substrate as the persistence layer for the rich-human channel)
- **FRE-227** — substrate implementation
- **FRE-368** — agent-side artifact tools
- **FRE-369** — user upload UX
- **FRE-370** — Cloudflare Access 720h session duration (related operational fix)
- **FRE-226** — auto-updating CLAUDE.md (future consumer)
- **FRE-345** — admin vs non-admin privilege model (placeholder; ADR-0064 + this ADR's defaults are sufficient for now)
- **FRE-315** — PWA Mermaid block rendering (related: the consumer-side rendering precedent ADR-0070 builds on)
- Discussion record: `docs/superpowers/plans/i-want-to-research-bubbly-shannon.md`
