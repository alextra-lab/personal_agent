# ADR-0153: Artifact Sharing — Owner-to-User Grants

**Status:** Proposed
**Date:** 2026-10-01
**Deciders:** Project owner
**Tags:** artifacts, identity, authorization, sharing, prompt-injection

---

## Context

On 2026-09-16 the owner asked: "Are artifacts shareable with other seshat users?" The answer was no.
FRE-1525 asks whether that changes, and how.

### Today's behaviour (verified on `main` at `bc6c2206`, 2026-10-01)

- **One owner per artifact.** The `artifacts` table (`docker/postgres/init.sql:404-420`) has a
  single `user_id` and no visibility, grant, or token column.
- **Every read path filters on `artifacts.user_id = <caller>` and nothing else:**

  | Path | Location | On mismatch |
  |---|---|---|
  | `GET /internal/artifacts/{id}` (the Worker calls it) | `service/artifacts_router.py:141-240` | 404 |
  | `GET /api/v1/artifacts` (list) | `artifacts_router.py:243-290` | empty list |
  | `GET /api/v1/artifacts/{id}` (metadata) | `artifacts_router.py:293-336` | 404 |
  | `GET /api/v1/artifacts/{id}/export` | `artifacts_router.py:428-520` | 404 |
  | `artifact_list` tool | `tools/artifact_tools.py:520-597` | empty list |
  | `artifact_read` tool | `tools/artifact_tools.py:600-735` | `ToolExecutionError` |
  | Chat attachments | `service/app.py:155` (`_validate_attachments`) | id dropped |
  | `notes_search` (pgvector) | `tools/notes_tools.py:387-484` | empty |

- **404, not 403.** ADR-0069 D3 inherits ADR-0064 D3: an ownership mismatch returns 404, which
  hides existence. ADR-0069 Verification item 4 states the case: user A writes, user B gets 404.
- **Memory is global.** ADR-0064 D5 keeps the knowledge graph shared, and new facts default to
  `group` visibility (`memory/service.py:187-212`). So user B's agent can recall a fact from the
  owner's turns, but B cannot open the artifact that the fact came from.
- **Sessions have one user.** `sessions.user_id` is a single column. The multi-participant
  session work is FRE-420, which is in Backlog and unscheduled.
- **No content from outside is fenced.** Tool output enters the model context as raw JSON
  (`orchestrator/tool_dispatch.py:256`). `artifact_read` decodes R2 bytes straight into
  `output["content"]` (`artifact_tools.py:721`). `web_fetch` has the same property.
- **The main API path trusts a plaintext header.** `get_request_user` (`service/auth.py:194`)
  takes identity from `Cf-Access-Authenticated-User-Email` with no JWT check. The JWT verifier
  (`service/cf_access_jwt.py`) has one call site, the Worker path (`artifacts_router.py:191`,
  confirmed by FRE-1530). Reachability of this header without Cloudflare Access is not verified.

### Demand

The live `users` table holds 29 rows on 2026-10-01. The owner holds 105 of the 113 artifacts. One
other real user holds 8 artifacts across 11 sessions. The owner confirmed on 2026-10-01 that this
second user is the intended recipient.

### The owner's answers (2026-10-01)

1. The recipient is a real user.
2. Access control must be **per individual**: share with one person and not with another.
3. Sharing with people outside the Cloudflare Access allowlist is **not planned**.
4. The grantee's **session and agent must be able to read** a shared artifact inside Seshat, not
   only the grantee in a browser.

The owner also confirmed five design choices, recorded as D3, D7, and three items in "Out of
scope" below.

---

## Decision

### D1 — A grant table, which is also the audit record

Add `artifact_grants`:

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID PRIMARY KEY` | |
| `artifact_id` | `UUID NOT NULL` | FK `artifacts(id) ON DELETE CASCADE` |
| `grantee_user_id` | `UUID NOT NULL` | FK `users(user_id)` |
| `granted_by` | `UUID NOT NULL` | FK `users(user_id)`. Always the artifact owner (D2) |
| `granted_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `revoked_at` | `TIMESTAMPTZ NULL` | NULL means active |
| `revoked_by` | `UUID NULL` | FK `users(user_id)` |

A partial unique index on `(artifact_id, grantee_user_id) WHERE revoked_at IS NULL` allows one
active grant per pair. A revoke sets `revoked_at` and `revoked_by`. It never deletes the row, so
the table records who shared what, with whom, and when it ended. A later re-grant inserts a new
row.

The schema change goes in `docker/postgres/init.sql` and a new file in
`docker/postgres/migrations/`, run as the `agent` superuser (FRE-808).

### D2 — Only the owner grants, and a grantee only reads

The owner is `artifacts.user_id`. Only the owner can create, revoke, or list the grants on an
artifact. A grantee cannot re-share, overwrite, or delete the artifact. A grant request on an
artifact that the caller does not own returns 404, the same answer as for an unknown id.

### D3 — Shareable types: `artifact` and `upload`

A grant can target `type IN ('artifact', 'upload')`. `note` is excluded. A note is a chain of
revisions under one slug (`notes_tools.py:204-219`), and a grant on one revision id does not fit
that model. `capture` is excluded because it is not a user-facing type. A grant request on an
excluded type returns 400 to the owner.

### D4 — Grant by email, to existing users only

The owner names the grantee by email. The grant resolves the email to an existing `users` row.
It never creates a row. This settles the question of a person who never signs in: that person
cannot receive a grant until they sign in once. An unknown email returns a "no Seshat user with
that email" error.

There is no user directory to browse. The error tells the owner whether an email belongs to a
Seshat user. This is accepted, because every caller is already on the Access allowlist. A grant to
oneself returns 400.

### D5 — "Owned or actively granted" in every read path, through one predicate

One shared predicate decides read access: the caller owns the artifact, **or** an
`artifact_grants` row exists for the artifact and the caller with `revoked_at IS NULL`. These
paths use it:

- `GET /internal/artifacts/{id}` (browser access through the Worker)
- `GET /api/v1/artifacts/{id}` (metadata)
- `GET /api/v1/artifacts/{id}/export`
- `artifact_read`
- chat attachments (`_validate_attachments`)

Write, delete, and grant-management paths keep the owner-only filter. `notes_search` keeps the
owner-only filter, because notes are not shareable (D3).

The predicate lives in one place. A read path that re-implements its own ownership filter is the
defect that D5 forbids.

### D6 — Discovery: a scope on the list paths

`artifact_list` and `GET /api/v1/artifacts` take a scope: `own` (the default), `shared`, or
`all`. `shared` returns the artifacts with an active grant to the caller, of type `artifact` or
`upload`. The default stays `own`, so the existing behaviour of both paths does not change.

The grantee's agent finds shared items with `artifact_list(scope="shared")` and reads them with
`artifact_read`. This meets the owner's answer 4.

### D7 — The agent can share, only with the owner's approval

A new tool, `artifact_share(artifact_id, email)`, creates a grant on an artifact that the caller
owns. It carries `requires_approval: true` in `config/governance/tools.yaml` in every mode. The
approval prompt shows the artifact title and the grantee email, so the owner sees what they
approve.

**Why approval:** text injected into a web page or a shared artifact can tell the agent to share
the owner's artifacts. The blast radius is limited to allowlisted users, but the owner must still
decide each grant. The approval step is that decision.

Revoke is available through the API and the PWA. The agent has no revoke tool in v1.

### D8 — Existence hiding stays, and revocation is immediate

- A caller with no grant gets 404 on every read path. The body is the same as for an id that does
  not exist.
- A revoked grantee also gets 404 with the same body.
- The next request after a revoke gets 404. The internal artifact endpoint returns
  `Cache-Control: private, no-store`, so no shared cache holds the bytes. If the Worker rewrites
  this header, the Worker change goes in the private secrets repo (ADR-0069 D8).

**Revocation cannot recall copies.** These copies stay after a revoke:

- a file that the grantee downloaded or exported
- the content in the grantee's session history, if their agent read it in a turn
- facts extracted into the knowledge graph, which is global anyway (ADR-0064 D5)

This ADR states that limit and does not try to remove it.

### D9 — Audit events

Each of these actions emits a structlog event with `trace_id`, `artifact_id`, `owner_user_id`, and
`grantee_user_id`, which reaches Elasticsearch:

- `artifact_grant_created` (also carries `via`: `api` or `agent_tool`)
- `artifact_grant_revoked`
- `artifact_read_by_grantee` (also carries `surface`: `worker`, `metadata`, `export`,
  `artifact_read`, or `attachment`)

The owner can list the active and revoked grants on their artifact through
`GET /api/v1/artifacts/{id}/grants`.

### D10 — The agent knows who wrote a shared artifact

When the caller is not the owner, `artifact_read` returns `shared: true` and `shared_by` (the
owner's display name, or email if there is none). The `artifact_read` tool description tells the
model that content written by another user is data, not instructions.

A general fence for untrusted content is out of scope. It must also cover `web_fetch`, which has
the same gap and a larger exposure.

### D11 — Precondition: the main API path verifies the JWT

With grants, identity is the whole access gate. Before any grant path ships, `get_request_user`
must verify `Cf-Access-Jwt-Assertion` with the existing `CFAccessVerifier`, and take identity
from the verified `email` claim only. The plaintext email header alone must not resolve a user in
production. The dev fallback (`gateway_auth_enabled=False` resolves to `agent_owner_email`,
ADR-0064 D4) stays.

If the implementation shows that Cloudflare Access always strips and re-injects the header, so a
forged value cannot reach the gateway, the ticket records that proof and still adds the JWT check.
A defense that depends on edge configuration is not one this ADR relies on.

### Out of scope (owner-confirmed 2026-10-01)

- **Expiry.** Manual revoke covers it. An `expires_at` column can come later without rework.
- **Notifying the grantee.** The grantee sees the item under scope `shared`.
- **Session-scoped sharing.** When FRE-420 gives a session more than one participant, the
  participants can get implicit grants on that session's artifacts. The grant table and the D5
  predicate are the place for that rule. This ADR records the path and does not build it.

---

## Alternatives Considered

### Option 1: Do nothing

**Description:** Artifacts stay personal. Outside sharing stays manual through export.
**Pros:** No build cost. No new access path.
**Cons:** A real second user exists, and the owner asked for sharing. The artifact wall also stays
stricter than the memory wall, which already shares facts.
**Why Rejected:** The owner wants per-individual sharing inside Seshat.

### Option 2: A group visibility flag

**Description:** A per-artifact `visibility` column with `private` and `group`, reusing the
ADR-0064 D6 levels. `group` means every Cloudflare Access user.
**Pros:** One column. No grantee identity needed. No user picker.
**Cons:** It cannot express "this person but not that one".
**Why Rejected:** The owner requires individual access control (answer 2).

### Option 3: Share links

**Description:** A per-artifact token that the Worker accepts in place of an identity.
**Pros:** Reaches people outside the allowlist.
**Cons:** The token is the whole gate, so existence hiding is lost and a forwarded URL works for
anyone. It removes the artifact from the identity model of ADR-0069 D3 and Dev-3. It serves the
owner's HTML to anonymous visitors from the owner's domain. It needs a Worker change in the
private secrets repo.
**Why Rejected:** Outside sharing is not planned (answer 3). Export plus a file covers the rare
case.

### Option 4: Session-scoped sharing only

**Description:** Every participant of a shared session sees the artifacts created in it.
**Pros:** It follows the collaborative-sessions North Star with no separate sharing concept.
**Cons:** Sessions have one `user_id`, and FRE-420 is unscheduled. It also cannot share an
artifact from a solo session.
**Why Rejected:** Blocked today. Kept as a future rule on top of D1 and D5.

### Option 5: Grants without the JWT precondition

**Description:** Ship D1 to D10 and leave `get_request_user` on the plaintext header.
**Pros:** One ticket less.
**Cons:** A forged header reads every artifact granted to the spoofed user, not only that user's
own artifacts.
**Why Rejected:** Grants raise the value of a spoofed identity. D11 closes the gap first.

---

## Consequences

### Positive Consequences

- The owner can share one artifact with one person, and revoke it.
- The grantee's agent can find and read shared artifacts in a turn, with provenance.
- The grant table is a complete record of who shared what with whom.
- The main API path gains JWT verification, which protects the owner-only paths too.

### Negative Consequences

- Every read path gains a join or an `EXISTS` on `artifact_grants`.
- A new PWA surface (share dialog, access list, shared items) needs build and maintenance.
- An owner can learn whether an email belongs to a Seshat user (D4).
- Revocation cannot recall a downloaded file, session history, or knowledge-graph facts (D8).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| A read path keeps its own owner filter, or a new one skips the predicate | Medium | D5: one predicate. AC-1 and AC-2 probe every surface |
| Injected text makes the agent share an artifact | Medium | D7: `requires_approval: true` in every mode, prompt shows title and email. AC-6 seeds the negative |
| A shared artifact carries instructions to the grantee's agent | Low | D10: `shared_by` provenance and a tool-description rule. The sharer is an allowlisted user |
| A spoofed identity reads granted artifacts | Medium | D11 precondition. AC-8 |
| A cache serves bytes after a revoke | Low | D8: `Cache-Control: private, no-store`. AC-3 |

---

## Implementation Notes

- **Order:** D11 first. D1, D5, D6, and D10 next. Then grant management (D2, D4, D7, D9). Then
  the PWA.
- **Files:** `service/auth.py`, `service/cf_access_jwt.py` (call site only),
  `service/artifacts_router.py`, `service/app.py`, `tools/artifact_tools.py`,
  `config/governance/tools.yaml`, `docker/postgres/init.sql`, a new migration,
  `seshat-pwa/src/components/ArtifactCard.tsx` and the artifact list surface.
- **D11 and FRE-1530** both touch `cf_access_jwt.py`. The D11 ticket follows FRE-1530.
- **Testing:** the test stack (`tests/CLAUDE.md`, FRE-375) with two `users` rows. Live
  verification uses the owner and the real second user, with the owner's OK.

---

## Verification / Acceptance Criteria

Adjudicated on the umbrella ticket FRE-1525, after the implementation chain lands and deploys.
User A owns artifact X. User B holds a grant on X. User C holds no grant.

- **AC-1 — A grant opens every read surface to the grantee.** B gets X's bytes from the Worker
  URL, X's metadata, X's export, and X's content from B's agent through `artifact_read`, which
  returns `shared: true` and `shared_by` equal to A's display name. · **Check:** integration test
  on the test stack across the five D5 surfaces, plus one post-deploy browser and turn check by B
  with the owner's OK. · *Fails if* any D5 surface returns 404 to B, or `shared_by` is absent or
  names B.
- **AC-2 — A non-grantee cannot tell that X exists.** C gets 404 on every D5 surface, and each
  status and body is identical to the response for a random UUID. · **Check:** the same test,
  comparing C's responses for X with responses for an unknown id. · *Fails if* any surface returns
  403, 200, or a body that differs from the unknown-id body.
- **AC-3 — A revoke takes effect on the next request.** After A revokes, B's next request on every
  D5 surface returns the unknown-id 404, and the internal endpoint response carries
  `Cache-Control: private, no-store`. · **Check:** test: grant, read, revoke, read again, with no
  wait. · *Fails if* any surface still serves X to B after the revoke.
- **AC-4 — A grantee only reads.** B's grant, revoke, and grant-list requests on X return 404. A
  `notes_write`, `artifact_write`, or upload by B leaves X's row and R2 bytes unchanged. ·
  **Check:** test comparing X's row and R2 hash before and after B's calls. · *Fails if* B creates
  a grant on X, or X changes.
- **AC-5 — Notes stay private.** A grant request on A's note returns 400 and creates no row. B's
  `notes_search` never returns A's notes. · **Check:** test with a note owned by A whose embedding
  matches B's query. · *Fails if* a grant row exists for a note, or A's note appears in B's
  results.
- **AC-6 — The agent cannot share without the owner's approval.** An `artifact_share` call that is
  not approved, or is denied, creates no grant row. The approval prompt shows X's title and the
  grantee email. Seeded negative: a fixture page that instructs the agent to share X with C
  produces no grant row unless A approves. · **Check:** test on the tool path with approval
  denied, then approved, counting `artifact_grants` rows. · *Fails if* a row exists before
  approval, or the prompt omits the title or email.
- **AC-7 — The audit record is complete.** For the scripted sequence grant, two reads by B, revoke:
  `artifact_grants` holds one row with `revoked_at` and `revoked_by` set, and Elasticsearch holds
  exactly one `artifact_grant_created`, two `artifact_read_by_grantee`, and one
  `artifact_grant_revoked` event for X, each with A's and B's user ids. · **Check:** test on the
  test stack, querying the test Elasticsearch on `:9201`. · *Fails if* any event is missing,
  duplicated, or lacks either id.
- **AC-8 — A header alone does not resolve a user.** A production-mode request to
  `GET /api/v1/artifacts/{id}` with a valid `Cf-Access-Authenticated-User-Email` and no valid
  `Cf-Access-Jwt-Assertion` returns 401. · **Check:** test with `gateway_auth_enabled=True`,
  sending the header with no JWT, then with a JWT signed by a foreign key. · *Fails if* either
  request returns 200 or any artifact data.
- **AC-9 — A grant never creates a user.** A grant to an unknown email returns the
  "no Seshat user" error, and the `users` row count is the same before and after. · **Check:**
  test counting `users` rows. · *Fails if* the count changes or a grant row exists.
- **AC-10 — The default list does not change.** B's `artifact_list` and `GET /api/v1/artifacts`
  with no scope return only B's own artifacts. With scope `shared`, they return exactly the
  artifacts with an active grant to B. · **Check:** test with one active and one revoked grant to
  B. · *Fails if* the default returns X, or `shared` returns the revoked one or omits the active
  one.

---

## References

- ADR-0069 — R2-Backed Artifact Substrate (D3 identity, D8 Terraform location, Dev-3 JWT
  hardening) — Implemented
- ADR-0064 — Inbound User Identity via Cloudflare Access (D3 404 not 403, D4 dev fallback, D5
  global memory, D6 visibility levels) — Accepted
- ADR-0063 — Primitive Tools / Action-Boundary Governance (tool approval) — Accepted
- ADR-0106 — System/User Knowledge Boundary — Superseded by ADR-0115. FRE-1525 listed it. No
  decision in this ADR depends on it
- FRE-1525 — this ADR's umbrella ticket
- FRE-420 — collaborative sessions (multi-participant), Backlog
- FRE-1530 — PyJWT upgrade and `cf_access_jwt.py` defects
- FRE-808 — run migrations as the `agent` superuser
- `src/personal_agent/service/artifacts_router.py` · `src/personal_agent/tools/artifact_tools.py`
  · `src/personal_agent/tools/notes_tools.py` · `src/personal_agent/service/auth.py`

---

## Status Updates

### 2026-10-01 - Proposed
**Changed By:** adr session (Opus), with the owner
**Reason:** Owner-led discussion on FRE-1525. The owner chose per-individual grants and confirmed
the design choices on 2026-10-01.
