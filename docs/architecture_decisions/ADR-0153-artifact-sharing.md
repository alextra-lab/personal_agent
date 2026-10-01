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

- **One owner per artifact.** The `artifacts` table (`docker/postgres/init.sql:404-421`) has a
  single `user_id` and no visibility, grant, or token column.
- **Every caller-facing read filters on the caller's `user_id`.** Some also filter on `type` or
  `upload_pending`. None admits a second user:

  | Caller-facing read | Location | On mismatch |
  |---|---|---|
  | `GET /internal/artifacts/{id}` (the Worker calls it) | `service/artifacts_router.py:141-240` | HTTP 404 |
  | `GET /api/v1/artifacts` (list) | `artifacts_router.py:243-290` | empty list |
  | `GET /api/v1/artifacts/{id}` (metadata) | `artifacts_router.py:293-336` | HTTP 404 |
  | `GET /api/v1/artifacts/{id}/export` (HTML only) | `artifacts_router.py:428-520` | HTTP 404 |
  | `artifact_list` tool (`type='artifact'` only) | `tools/artifact_tools.py:520-597` | empty list |
  | `artifact_read` tool | `tools/artifact_tools.py:600-735` | `ToolExecutionError` |
  | Chat attachments, first turn | `service/app.py:155-210` (`_validate_attachments`) | id dropped silently |
  | `notes_search` (pgvector) | `tools/notes_tools.py:387-484` | empty |

- **Three other reads exist. Two can serve bytes without the ownership check:**
  - **Attachment continuations.** A cloud-cost confirmation and a PDF page continuation rebuild
    `AttachmentRef` from a stored `r2_key` with no new check
    (`orchestrator/executor.py:4392-4405`, `4554-4565`). The bytes are then fetched from R2
    (`orchestrator/attachment_resolution.py:195-198`, `orchestrator/document_resolution.py:296-299`).
  - **`expand_tool_result`** passes a caller-supplied R2 key to `store.get` with only a content
    hash guard (`tools/tool_result_expand.py:75-102`). The store checks no namespace
    (`storage/artifact_store.py:230-266`). The tool is dormant: it registers only when
    `tool_result_compression_enabled` is on (`tools/__init__.py:137`, ADR-0085 Parked), and the
    live gateway leaves it off.
  - **System reads:** the joinability walk reads artifact ids by session
    (`observability/joinability/walk.py:595-620`), and `grafana_ro` can select the whole table
    (`docker/postgres/migrations/0028_grafana_ro_artifacts.sql`). Neither serves bytes to a user.
- **The only ID-addressed mutation is upload completion**, `POST /api/uploads/{artifact_id}/complete`
  (`service/uploads_router.py:223`), which filters on owner. The two `DELETE FROM artifacts`
  statements touch pending uploads only (`uploads_router.py:314`, `:402`).
- **404, not 403.** ADR-0069 D3 inherits ADR-0064 D3: an ownership mismatch returns 404, which
  hides existence. ADR-0069 Verification item 4 states the case: user A writes, user B gets 404.
- **Memory is global.** ADR-0064 D5 keeps the knowledge graph shared. Authenticated sessions write
  `group` visibility (`second_brain/consolidator.py:789-791`), and reads apply the visibility
  filter at `memory/service.py:187-212`. So user B's agent can recall a fact from the owner's
  turns, but B cannot open the artifact that the fact came from.
- **Sessions have one user.** `sessions.user_id` is a single column. The multi-participant
  session work is FRE-420, which is in Backlog and unscheduled.
- **No content from outside is fenced.** Tool output enters the model context as raw JSON
  (`orchestrator/tool_dispatch.py:256`). `artifact_read` decodes R2 bytes straight into
  `output["content"]` (`artifact_tools.py:721`). `web_fetch` has the same property.
- **The main API path trusts a plaintext header.** `get_request_user` (`service/auth.py:176`)
  takes identity from `Cf-Access-Authenticated-User-Email` at line 195, with no JWT check. The JWT
  verifier (`service/cf_access_jwt.py`) has one call site, the Worker path
  (`artifacts_router.py:190`, confirmed by FRE-1530). Reachability of this header without
  Cloudflare Access is not verified.
- **Tool approval fails open.** `check_permission` allows a tool that requires approval when there
  is no session id, when `approval_ui_enabled` is false, or when no transport is attached
  (`tools/executor.py:226-280`). No `ToolExecutionLayer` is built with a transport
  (`orchestrator/executor.py:3252`, `orchestrator/tool_dispatch.py:58`, `config/bootstrap.py:122`),
  and no code assigns one later. The live gateway sets `AGENT_APPROVAL_UI_ENABLED=true`. So a
  tool with `requires_approval: true` runs with a warning log and no prompt. This affects `bash`
  and seven MCP write tools today (`config/governance/tools.yaml`).

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
| `artifact_id` | `UUID NOT NULL` | FK `artifacts(id) ON DELETE RESTRICT` |
| `grantee_user_id` | `UUID NOT NULL` | FK `users(user_id)` |
| `granted_by` | `UUID NOT NULL` | FK `users(user_id)`. Always the artifact owner (D2) |
| `granted_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `revoked_at` | `TIMESTAMPTZ NULL` | NULL means active |
| `revoked_by` | `UUID NULL` | FK `users(user_id)` |

A partial unique index on `(artifact_id, grantee_user_id) WHERE revoked_at IS NULL` allows one
active grant per pair. A revoke sets `revoked_at` and `revoked_by`. It never deletes the row. A
later re-grant inserts a new row, so the history keeps every grant period.

`ON DELETE RESTRICT` keeps the history: an artifact with grant rows cannot be deleted. This costs
nothing today, because the only delete paths remove pending uploads, which cannot hold a grant
(D3). A future delete path for shared types must decide how to keep the history. It must not add a
cascade.

The schema change goes in `docker/postgres/init.sql` and a new file in
`docker/postgres/migrations/`, run as the `agent` superuser (FRE-808).

### D2 — Only the owner grants, and a grantee only reads

The owner is `artifacts.user_id`. Only the owner can create, revoke, or list the grants on an
artifact. A grantee cannot re-share, overwrite, or delete it.

**Check order.** Every grant-management request checks ownership first. A caller who does not own
the artifact gets the unknown-id response before any check of type, email, or self-grant. So a
non-owner cannot learn whether an id is a note, a capture, or a shareable artifact.

### D3 — Shareable types: `artifact` and `upload`

A grant can target `type IN ('artifact', 'upload')` with `upload_pending = FALSE`. `note` is
excluded. A note is a chain of revisions under one slug (`notes_tools.py:204-219`), and a grant on
one revision id does not fit that model. `capture` is excluded because it is not a user-facing
type. After the D2 ownership check, a grant request on an excluded or pending item returns 400 to
the owner.

### D4 — Grant by email, to existing users only

The owner names the grantee by email. The grant resolves the email to an existing `users` row.
It never creates a row. This settles the question of a person who never signs in: that person
cannot receive a grant until they sign in once. An unknown email returns a "no Seshat user with
that email" error.

There is no user directory to browse. The error tells the owner whether an email belongs to a
Seshat user. This is accepted, because every caller is already on the Access allowlist. A grant to
oneself returns 400.

### D5 — One read predicate, in the database, used by every read path

A Postgres function `artifact_readable_by(artifact_id UUID, user_id UUID) RETURNS boolean`
decides read access. It returns true when the artifact is not pending and one of these holds:

- the caller owns it, or
- the type is `artifact` or `upload` and an `artifact_grants` row exists for the artifact and
  the caller with `revoked_at IS NULL`.

The function is the one place the rule lives. It works from raw SQL and from SQLAlchemy
(`func.artifact_readable_by`), which covers both query styles in the read paths. These paths call
it and drop their own ownership filter:

- `GET /internal/artifacts/{id}` (browser access through the Worker)
- `GET /api/v1/artifacts/{id}` (metadata)
- `GET /api/v1/artifacts/{id}/export`
- `artifact_read`
- chat attachments on the first turn (`_validate_attachments`)
- **attachment continuations**: before a cloud-confirmation or PDF-continuation turn re-injects an
  attachment, it calls the function by `artifact_id` and drops any attachment that fails. A
  stored `r2_key` is never authority.

The list paths call the function for scopes `shared` and `all` (D6). Scope `own` keeps its owner
filter, because it lists only the caller's own rows.

Write, delete, upload-completion, and grant-management paths keep the owner-only filter.
`notes_search` keeps the owner-only filter, because notes are not shareable (D3). The system reads
(joinability walk, `grafana_ro`) do not change.

**`expand_tool_result` is confined to its namespace.** It rejects any key that does not start with
`tool-results/{session_id}/` for a session that the caller owns. It returns one error for a
rejected key, a missing object, and a hash mismatch, so it cannot confirm that a key exists. This
closes a byte path to `artifact/`, `upload/`, and `note/` keys that bypasses the function. The tool
is dormant today, so this is a condition for turning `tool_result_compression_enabled` on, built
with the read-path work because it is small.

### D6 — Discovery: a scope on the list paths

`artifact_list` and `GET /api/v1/artifacts` take a scope: `own` (the default), `shared`, or `all`.
Each row in a result appears once.

| Scope | `GET /api/v1/artifacts` (`type` filter still applies) | `artifact_list` |
|---|---|---|
| `own` | Caller's own rows of the requested type, as today | Caller's own `artifact` rows, as today |
| `shared` | Rows with an active grant to the caller. A `type` other than `artifact` or `upload` returns an empty list | Rows of type `artifact` or `upload` with an active grant to the caller |
| `all` | Union of `own` and `shared` | Union of `own` and `shared` |

Each shared row carries `shared: true` and `shared_by`. Pending uploads never appear. Revoked
grants never appear. The default stays `own`, so the existing behaviour of both paths does not
change.

The grantee's agent finds shared items with `artifact_list(scope="shared")` and reads them with
`artifact_read`. This meets the owner's answer 4.

### D7 — The agent can share, only with the owner's approval, and the tool fails closed

A new tool, `artifact_share(artifact_id, email)`, creates a grant on an artifact that the caller
owns, through the same D2 to D4 checks as the API.

- **The mechanism is an injected approval callable.** Today an executor receives only the frozen
  `TraceContext` (`tools/executor.py:453`), so a tool cannot prompt. `ToolExecutionLayer` passes
  an `approve` callable to executors that declare an `approve` parameter, the same way it passes
  `ctx` today. The callable sends one approval request through the layer's transport and returns
  the decision. When the layer has no transport or no session id, the callable is a stub that
  returns `deny`.
- **It fails closed.** The tool creates a grant only after an affirmative `approve` decision. With
  no transport, no session id, approval UI disabled, a timeout, or a denial, it creates nothing and
  returns a refusal. It does not rely on the generic `check_permission` approval branch, because
  that branch fails open (Context). Its entry in `config/governance/tools.yaml` therefore sets
  `requires_approval: false` and no `requires_approval_in_modes`, so the generic branch never sends
  a second request.
- **The prompt shows trusted values.** The tool runs the D2 to D4 checks, reads the artifact title
  from the `artifacts` row, and resolves the email to a user, all before its single approval
  request. The request shows that title and email, not only the model's arguments.
- **Dependency.** Today no `ToolExecutionLayer` is built with a transport, so the stub denies every
  call until the transport is wired into the primary tool path (FRE-1535). Until then, granting
  works through the API and the PWA only.

**Why approval:** text injected into a web page or a shared artifact can tell the agent to share
the owner's artifacts. The blast radius is limited to allowlisted users, but the owner must still
decide each grant.

Revoke is available through the API and the PWA. The agent has no revoke tool in v1.

### D8 — Existence hiding stays, and revocation is immediate

A caller who cannot read an artifact gets the same outcome as for an id that does not exist. The
outcome depends on the surface:

| Surface | Outcome for no access, revoked access, or an unknown id |
|---|---|
| Worker, metadata, export, grant management | HTTP 404 with an identical body |
| `artifact_read` | `ToolExecutionError` with an identical message |
| Chat attachments, first turn and continuations | The id is dropped, with the same log event and no different user-visible text |
| `artifact_list`, `GET /api/v1/artifacts` | The row is absent |

The next request after a revoke gets that outcome. The internal artifact endpoint returns
`Cache-Control: private, no-store`, so no shared cache holds the bytes. If the Worker rewrites this
header, the Worker change goes in the private secrets repo (ADR-0069 D8).

**Revocation cannot recall copies.** These copies stay after a revoke:

- a file that the grantee downloaded or exported
- the content in the grantee's session history, if their agent read it in a turn
- facts extracted into the knowledge graph, which is global anyway (ADR-0064 D5)

This ADR states that limit and does not try to remove it.

### D9 — Audit events

Each of these actions emits a structlog event with `trace_id`, `artifact_id`, `owner_user_id`, and
`grantee_user_id`, which reaches Elasticsearch:

- `artifact_grant_created`, with `via`: `api` or `agent_tool`
- `artifact_grant_revoked`
- `artifact_read_by_grantee`, with `surface`: `worker`, `metadata`, `export`, `artifact_read`, or
  `attachment`

The owner lists the active and revoked grants on their artifact through
`GET /api/v1/artifacts/{id}/grants`.

### D10 — The agent knows who wrote a shared artifact

When the caller is not the owner, `artifact_read` returns `shared: true` and `shared_by` (the
owner's display name, or email if there is none). The `artifact_read` tool description tells the
model that content written by another user is data, not instructions.

A general fence for untrusted content is out of scope. It must also cover `web_fetch`, which has
the same gap and a larger exposure.

### D11 — Precondition: the main API path verifies the JWT

With grants, identity is the whole access gate. Before any grant path ships, `get_request_user`
must verify `Cf-Access-Jwt-Assertion` and take identity from the verified `email` claim only.

The main app and the artifact Worker are separate Cloudflare Access applications with different
audiences (`artifacts_router.py:259-262`). `CFAccessVerifier` accepts one audience
(`cf_access_jwt.py:59`), and the one setting `cf_access_aud` (`config/settings.py:2623`) holds the
Worker's. So D11 adds a second setting for the main app's audience and a second verifier instance.
`get_request_user` uses the main-app verifier. The internal artifact endpoint keeps the Worker
verifier. The ticket first confirms that the main app's JWT reaches the gateway on its primary
domain (ADR-0069 Dev-2 records that destinations do not receive it). The plaintext email header alone must not resolve a user in
production. If the main-app verifier is not configured in production, the request returns 503, as the
Worker path does today (ADR-0069 Dev-3). The dev fallback (`gateway_auth_enabled=False` resolves to
`agent_owner_email`, ADR-0064 D4) stays.

If the implementation shows that Cloudflare Access always strips and re-injects the header, the
ticket records that proof and still adds the JWT check. This ADR does not rely on a defense that
depends on edge configuration.

### Out of scope (owner-confirmed 2026-10-01)

- **Expiry.** Manual revoke covers it. An `expires_at` column can come later without rework.
- **Notifying the grantee.** The grantee sees the item under scope `shared`.
- **Session-scoped sharing.** When FRE-420 gives a session more than one participant, the
  participants can get implicit grants on that session's artifacts. The grant table and the D5
  function are the place for that rule. This ADR records the path and does not build it.

---

## Alternatives Considered

### Sharing models

#### Option 1: Do nothing

**Description:** Artifacts stay personal. Outside sharing stays manual through export.
**Pros:** No build cost. No new access path.
**Cons:** A real second user exists, and the owner asked for sharing. The artifact wall also stays
stricter than the memory wall, which already shares facts.
**Why Rejected:** The owner wants per-individual sharing inside Seshat.

#### Option 2: A group visibility flag

**Description:** A per-artifact `visibility` column with `private` and `group`, reusing the
ADR-0064 D6 levels. `group` means every Cloudflare Access user.
**Pros:** One column. No grantee identity needed. No user picker.
**Cons:** It cannot express "this person but not that one".
**Why Rejected:** The owner requires individual access control (answer 2).

#### Option 3: Share links

**Description:** A per-artifact token that the Worker accepts in place of an identity.
**Pros:** Reaches people outside the allowlist.
**Cons:** The token is the whole gate, so existence hiding is lost and a forwarded URL works for
anyone. It removes the artifact from the identity model of ADR-0069 D3 and Dev-3. It serves the
owner's HTML to anonymous visitors from the owner's domain. It needs a Worker change in the
private secrets repo.
**Why Rejected:** Outside sharing is not planned (answer 3). Export plus a file covers the rare
case.

#### Option 4: Session-scoped sharing only

**Description:** Every participant of a shared session sees the artifacts created in it.
**Pros:** It follows the collaborative-sessions North Star with no separate sharing concept.
**Cons:** Sessions have one `user_id`, and FRE-420 is unscheduled. It also cannot share an
artifact from a solo session.
**Why Rejected:** Blocked today. Kept as a future rule on top of D1 and D5.

#### Option 5: Grants without the JWT precondition

**Description:** Ship D1 to D10 and leave `get_request_user` on the plaintext header.
**Pros:** One ticket less.
**Cons:** A forged header reads every artifact granted to the spoofed user, not only that user's
own artifacts.
**Why Rejected:** Grants raise the value of a spoofed identity. D11 closes the gap first.

### Enforcement mechanisms for D5

#### Option 6: The predicate repeated in application code

**Description:** Each read path adds its own "owned or granted" SQL.
**Pros:** No database object.
**Cons:** The read paths use raw SQL and SQLAlchemy in different files. Eight copies drift, and one
missed copy is a bypass. Two such bypasses already exist (attachment continuations, live, and
`expand_tool_result`, dormant).
**Why Rejected:** D5 needs one definition that both query styles can call.

#### Option 7: Postgres row-level security

**Description:** An RLS policy on `artifacts`, with the caller id set per request through a session
variable.
**Pros:** The database enforces the rule even for a query that forgets it.
**Cons:** The gateway shares pooled asyncpg connections, so a per-request session variable can leak
to the next request if one reset is missed. System reads (joinability walk, `grafana_ro`,
consolidation) need a bypass role or policy. Owner-only paths need a second policy. The failure is
silent: a wrong variable returns another user's rows with no error.
**Why Rejected:** The risk moves from a missed filter to a missed reset, which is harder to see. The
D5 function plus AC-11 gives one definition without per-connection state. RLS can come later if
the read paths grow.

---

## Consequences

### Positive Consequences

- The owner can share one artifact with one person, and revoke it.
- The grantee's agent can find and read shared artifacts in a turn, with provenance.
- The grant table is a complete record of who shared what with whom.
- The main API path gains JWT verification, which protects the owner-only paths too.
- Two bypasses close: attachment continuations (live) and `expand_tool_result` (dormant).

### Negative Consequences

- Every read path calls a function that may query `artifact_grants`.
- A new PWA surface (share dialog, access list, shared items) needs build and maintenance.
- An owner can learn whether an email belongs to a Seshat user (D4).
- Revocation cannot recall a downloaded file, session history, or knowledge-graph facts (D8).
- `artifact_share` refuses every call until the approval channel reaches the tool layer (D7).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| A read path keeps its own owner filter, or a new one skips the function | Medium | D5 function. AC-11 flags any `artifacts` query with neither the function nor an exemption marker |
| A persisted attachment serves bytes after a revoke | Medium | D5 continuation check. AC-3 covers continuations |
| Injected text makes the agent share an artifact | Medium | D7 fail-closed approval with trusted values. AC-6 seeds the negative |
| A shared artifact carries instructions to the grantee's agent | Low | D10 provenance and a tool-description rule. The sharer is an allowlisted user |
| A spoofed identity reads granted artifacts | Medium | D11 precondition. AC-8 |
| A cache serves bytes after a revoke | Low | D8 `Cache-Control: private, no-store`. AC-3 |

---

## Implementation Notes

- **Order:** D11 first. Then D1, D5, D6, D8, and D10 (schema and read paths). Then D2, D3, D4, and
  D9 (grant API and audit). Then the PWA. D7 comes last and depends on the approval channel.
- **Files:** `service/auth.py`, `service/artifacts_router.py`, `service/app.py`,
  `orchestrator/executor.py` (continuations), `tools/artifact_tools.py`,
  `tools/tool_result_expand.py`, `config/governance/tools.yaml`, `docker/postgres/init.sql`, a new
  migration, `seshat-pwa/src/components/ArtifactCard.tsx` and the artifact list surface.
- **D11 and FRE-1530** both touch the JWT code path. The D11 ticket follows FRE-1530.
- **The approval fail-open** in `check_permission` affects `bash` and seven MCP write tools today.
  D7 does not depend on fixing it, because `artifact_share` enforces its own approval. Fixing the
  generic branch is separate work: FRE-1535, filed outside this chain on the owner's choice.
- **Testing:** the test stack (`tests/CLAUDE.md`, FRE-375) with three `users` rows. Live
  verification uses the owner and the real second user, with the owner's OK.

---

## Verification / Acceptance Criteria

Adjudicated on the umbrella ticket FRE-1525, after the implementation chain lands and deploys.

**Fixtures.** User A owns HTML artifact X, text-tier PDF upload Y, vision-tier PDF upload V,
which is over the page budget so the resolver offers a continuation, and image upload W. A also
owns note N, capture K, and pending upload P. B owns pending upload Q. User B holds active grants
on X, Y, V, and W. User C holds no grant.
"Unknown id" means a random UUID.

- **AC-1 — A grant opens every read surface to the grantee.** For B: the Worker URL returns X's
  bytes. Metadata returns X and Y. Export returns X's HTML. `artifact_read` returns X's content
  with `shared: true` and `shared_by` equal to A's display name. A chat turn with Y attached
  produces a text content block with Y's extracted text. A chat turn with V attached produces V's
  first pages and a continuation offer, and the continuation turn re-injects V's next pages. · **Check:** integration test on the test stack, plus one post-deploy browser and turn
  check by B with the owner's OK. · *Fails if* any listed surface withholds X, Y, or V from B,
  V's continuation is missing or injects nothing, or `shared_by` is absent or names B.
- **AC-2 — A non-grantee cannot tell that X exists.** For C, each D8 surface gives exactly the
  outcome it gives for an unknown id: identical HTTP status and body, identical tool error message,
  identical attachment drop and log event, identical absence from lists. · **Check:** the same
  test, comparing C's outcome for X and Y with the outcome for an unknown id, surface by surface. ·
  *Fails if* any surface differs in status, body, message, log event, or list content.
- **AC-3 — A revoke takes effect on the next request, including continuations.** After A revokes
  B's grants, B's next request on every D8 surface gives the unknown-id outcome. A PDF continuation
  for V and a cloud-confirmation turn for W, both persisted before the revoke, inject nothing. W is
  attached under a cloud-priced primary model with the confirmation threshold set so that the cost
  gate fires. The
  internal endpoint response carries `Cache-Control: private, no-store`, and after deploy the
  response from the public Worker URL carries it too. · **Check:** test: grant,
  read, persist a continuation, revoke, then read and continue with no wait, plus one post-deploy
  header check on the Worker URL. · *Fails if* any surface or continuation serves X, Y, V, or W to B after the revoke, or either response lacks the header.
- **AC-4 — A grantee only reads.** B's requests to create, revoke, or list grants on X, and B's
  `POST /api/uploads/{Y}/complete`, give the unknown-id outcome. X's and Y's rows and R2 bytes are
  unchanged afterwards. · **Check:** test comparing rows and R2 hashes before and after B's calls.
  · *Fails if* B creates a grant, changes a row or bytes, or gets an outcome that differs from the
  unknown-id one.
- **AC-5 — Only shareable types can be shared, and the check order hides existence.** A's grant
  on Y succeeds. A's grants on N, K, and P return 400 and create no row. B's and C's grant requests
  on N, K, and P give the unknown-id 404. B's grant requests on Y, naming an unknown email and
  naming B's own email, also give the unknown-id 404. B's `notes_search` never returns N. · **Check:** test, with N's
  embedding matching B's query. · *Fails if* a grant row exists for N, K, or P, a non-owner gets 400 or an email-specific error,
  or N appears in B's results.
- **AC-6 — The agent cannot share without an affirmative approval.** `artifact_share` creates no
  grant row with no transport, with approval UI disabled, on timeout, or on denial. With approval,
  it creates one grant row and one `artifact_grant_created` event with `via: agent_tool`. The prompt shows X's title from the database and the
  resolved email, even when the model passes a false title in its text. Seeded negative: a fixture
  page that instructs the agent to share X with C produces no row unless A approves. · **Check:**
  test on the tool path for each case, with the injected `approve` callable returning each
  decision and with no transport, counting `artifact_grants` rows, counting approval requests, and
  querying the test Elasticsearch for `artifact_grant_created`. · *Fails if* a row exists without
  an `approve` decision, the prompt omits the database title or the email, a transport case sends
  more or fewer than one approval request, or the approved case lacks exactly one
  `artifact_grant_created` event with `via: agent_tool` and every D9 field.
- **AC-7 — The audit record is complete.** For the sequence grant X and Y to B, B reads X through
  the Worker, metadata, export, and `artifact_read`, B attaches Y in a chat turn, A revokes X, A
  re-grants X: `artifact_grants` holds two rows for (X, B), both with `granted_by` equal to A, the
  first with `revoked_at` set and `revoked_by` equal to A, and the second active. Elasticsearch
  holds three `artifact_grant_created` events with `via: api`, one `artifact_grant_revoked`, and
  five `artifact_read_by_grantee` events, one each with `surface` values `worker`, `metadata`,
  `export`, `artifact_read`, and `attachment`. Every event
  carries `trace_id` and both user ids. `GET /api/v1/artifacts/{X}/grants` by A lists both rows. ·
  **Check:** test on the test stack, querying the test Elasticsearch on `:9201`. · *Fails if* any
  event or field is missing or duplicated, or the list omits a row.
- **AC-8 — A header alone does not resolve a user.** With `gateway_auth_enabled=True`, a request to
  `GET /api/v1/artifacts/{id}` with a valid email header and no JWT returns 401. With a JWT signed
  by a foreign key, it returns 401. With the main-app verifier unconfigured, it returns 503. A valid
  Worker-audience JWT returns 401 on the main API, and a valid main-app JWT returns 404 on the
  internal artifact endpoint. A valid main-app JWT on the main API returns 200. · **Check:** test
  for each case. · *Fails if* any rejection case returns 200 or artifact data, or the valid
  main-app case fails.
- **AC-9 — A grant never creates a user, and grants to oneself fail.** A grant to an unknown email
  returns the "no Seshat user" error. A grant by A to A returns 400. The `users` row count is the
  same before and after, and no grant row exists. · **Check:** test counting rows. · *Fails if* a
  count changes or a grant row exists.
- **AC-10 — The list scopes return exactly the D6 matrix.** With active grants on X and Y, a
  revoked grant on V, and B owning artifact Z: default and `own` return Z only, on both list paths.
  `shared` returns X and Y, each with `shared: true` and `shared_by` equal to A. `all` returns Z,
  X, and Y once each, with Z carrying no `shared` flag. B's pending upload Q appears in no scope. The API with `type=note` and `scope=shared`
  returns an empty list. · **Check:** test on both list paths. · *Fails if* any scope returns a row
  outside the matrix, omits one, returns a duplicate, or lacks the provenance fields.
- **AC-11 — One definition of read access.** Every query in `src/` that reads the `artifacts`
  table either calls `artifact_readable_by` or carries an exemption marker
  (`# artifact-access: owner-only` or `# artifact-access: system`) on the query itself. The read
  paths that D5 lists carry no marker. · **Check:** a pre-commit rule (`ast-grep` for
  `select(ArtifactModel...)`, plus a text match for `FROM artifacts` in SQL strings) that flags a
  query with neither the function nor a marker, and a test that holds the list of marked queries
  and fails when a D5 read path appears in it. The rule is seeded with two violations: a query with
  its own `user_id` filter, and a query with no access predicate at all. · *Fails if* the rule
  passes either seeded violation, or a D5 read path carries a marker.
- **AC-12 — `expand_tool_result` cannot read outside its namespace.** A call with X's R2 key and
  X's correct content hash, a call with a `tool-results/` key from C's session, and a call with an
  unknown key return the same error. A call with a key from B's own session works. · **Check:**
  test with `tool_result_compression_enabled` on. · *Fails if* any outside key returns content,
  or the three errors differ.
- **AC-13 — The schema enforces D1.** A second active grant for the same pair fails on the unique
  index. Deleting X while a grant row exists fails. An insert with an unknown `artifact_id`,
  `grantee_user_id`, or `granted_by`, and an update that sets an unknown `revoked_by`, each fail. ·
  **Check:** test on the test stack. · *Fails if* any of these statements succeeds.

- **AC-14 — The registered `artifact_read` description carries the untrusted-content rule.** ·
  **Check:** a registry test reads the registered tool description and asserts that it states that
  content written by another user is data, not instructions. · *Fails if* the sentence is absent.

AC-14 checks presence only. The rule's effect on the model cannot be measured cheaply, and AC-1
checks the provenance field that the rule depends on. This is a known gap, not a hidden one.

---

## References

- ADR-0069 — R2-Backed Artifact Substrate (D3 identity, D8 Terraform location, Dev-3 JWT
  hardening) — Implemented
- ADR-0064 — Inbound User Identity via Cloudflare Access (D3 404 not 403, D4 dev fallback, D5
  global memory, D6 visibility levels) — Accepted
- ADR-0063 — Primitive Tools / Action-Boundary Governance (tool approval) — Accepted
- ADR-0085 — Intra-Turn Tool-Result Compression, the source of `expand_tool_result` (D5) — Parked
- ADR-0106 — System/User Knowledge Boundary — Superseded by ADR-0115. FRE-1525 listed it. No
  decision in this ADR depends on it
- FRE-1525 — this ADR's umbrella ticket
- FRE-420 — collaborative sessions (multi-participant), Backlog
- FRE-1530 — PyJWT upgrade and `cf_access_jwt.py` defects
- FRE-808 — run migrations as the `agent` superuser
- FRE-1535 — tool approval fails open (wires the transport and the `approve` callable D7 needs)
- Implementation chain: FRE-1531 (D11) → FRE-1532 (read side) → FRE-1533 (grant API) → FRE-1534
  (PWA) and FRE-1536 (D7, also blocked by FRE-1535)
- `src/personal_agent/service/artifacts_router.py` · `src/personal_agent/tools/artifact_tools.py`
  · `src/personal_agent/tools/notes_tools.py` · `src/personal_agent/service/auth.py` ·
  `src/personal_agent/tools/executor.py` · `src/personal_agent/tools/tool_result_expand.py`

---

## Status Updates

### 2026-10-01 - Proposed
**Changed By:** adr session (Opus), with the owner
**Reason:** Owner-led discussion on FRE-1525. The owner chose per-individual grants and confirmed
the design choices on 2026-10-01.
